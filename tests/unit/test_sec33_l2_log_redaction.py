"""SEC-33 第二层 P3-1 / P3-5：diagnose 链路 ERROR 日志只记操作名、异常类型与状态；原始异常文本只进 DEBUG。
Protocol / NullK8sClient 的 get_deployment 带 raise_unavailable。全部离线，只替换最底层 SDK 调用。"""

from contextlib import ExitStack
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kubernetes.client.rest import ApiException
import pytest

from app.common.exceptions import AutoFixInternalError, ServiceUnavailableError
from app.core.interfaces.k8s_client import K8sClient, NullK8sClient
from app.services import autofix_service as autofix_service_module
from app.services.base import BaseService
from tests.unit.test_autofix_sec26_error_sanitization import service_with_fixer
from tests.unit.test_autofix_sec26_p3e_diagnose_unavailable import raising, real_k8s

MARKER = "sec33-l2-LOG-MARKER-58213"
ROOT = Path(__file__).resolve().parents[2]
_FORMATTER = logging.Formatter("%(name)s %(levelname)s %(message)s")


def _rendered(record) -> str:
    """含 exc_info 堆栈与异常链的完整渲染文本"""
    return _FORMATTER.format(record)


def _error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def assert_marker_only_in_debug(caplog):
    errors = _error_records(caplog)
    assert errors, "应有 ERROR 级日志"
    for record in errors:
        assert MARKER not in _rendered(record), _rendered(record)
        assert record.exc_info is None or MARKER not in _rendered(record)
    assert any(MARKER in _rendered(r) for r in caplog.records if r.levelno == logging.DEBUG), "原始异常信息应只进入 DEBUG"


@pytest.fixture
def capture(caplog):
    # 其它用例可能给具体 logger 设了级别，这里逐个放开到 DEBUG（at_level 退出时恢复）
    with ExitStack() as stack:
        for name in ("", "aiops", "aiops.services.autofix", "aiops.services.sec33-l2", "aiops.kubernetes"):
            stack.enter_context(caplog.at_level(logging.DEBUG, logger=name or None))
        yield caplog


async def test_diagnose_api_500_error_logs_have_operation_type_status_but_no_raw_text(capture):
    service = service_with_fixer(real_k8s(raising(ApiException(status=500, reason=MARKER))))
    with pytest.raises(ServiceUnavailableError):
        await service.diagnose_kubernetes_issues("api", "default", include_pods=False, include_events=False)
    assert_marker_only_in_debug(capture)
    base = [r.getMessage() for r in _error_records(capture) if "get_deployment_for_diagnosis" in r.getMessage()]
    assert base and "ApiException" in base[0] and "500" in base[0]


class LeakyError(RuntimeError):
    pass


class _Service(BaseService):
    async def _do_initialize(self) -> None:
        return None

    async def _do_health_check(self) -> bool:
        return True


async def test_execute_with_timeout_error_log_is_redacted(capture):
    service = _Service("sec33-l2")

    def boom():
        raise LeakyError(MARKER)

    with pytest.raises(ServiceUnavailableError):
        await service.execute_with_timeout(boom, timeout=5, operation_name="op_marker_check")
    assert_marker_only_in_debug(capture)
    messages = [r.getMessage() for r in _error_records(capture)]
    assert any("op_marker_check" in m and "LeakyError" in m for m in messages)


async def test_diagnose_pod_log_failure_error_log_is_redacted(capture):
    k8s = AsyncMock()
    k8s.get_deployment.return_value = {"metadata": {"name": "api"}}
    k8s.get_pods.return_value = [{"name": "api-0"}]
    k8s.get_pod_logs.side_effect = LeakyError(MARKER)
    service = service_with_fixer(k8s)
    result = await service.diagnose_kubernetes_issues("api", "default", include_pods=True, include_logs=True, include_events=False)
    assert result["diagnosis"]["logs"] == {"api-0": "获取日志失败"}
    assert_marker_only_in_debug(capture)


async def test_diagnose_unknown_error_log_is_redacted(capture, monkeypatch):
    class Boom:
        @staticmethod
        def now():
            raise LeakyError(MARKER)

    monkeypatch.setattr(autofix_service_module, "datetime", Boom)
    service = service_with_fixer(real_k8s(lambda **kwargs: SimpleNamespace(to_dict=lambda: {})))
    with pytest.raises(AutoFixInternalError):
        await service.diagnose_kubernetes_issues("api", "default")
    assert_marker_only_in_debug(capture)


async def test_diagnose_upstream_error_outside_wrapper_log_is_redacted(capture, monkeypatch):
    service = service_with_fixer(real_k8s(lambda **kwargs: SimpleNamespace(to_dict=lambda: {})))

    async def upstream(*args, **kwargs):
        raise TimeoutError(MARKER)

    monkeypatch.setattr(service, "execute_with_timeout", upstream)
    with pytest.raises(ServiceUnavailableError):
        await service.diagnose_kubernetes_issues("api", "default")
    assert_marker_only_in_debug(capture)


def test_protocol_and_null_client_accept_raise_unavailable():
    for target in (K8sClient.get_deployment, NullK8sClient.get_deployment):
        parameter = inspect.signature(target).parameters.get("raise_unavailable")
        assert parameter is not None and parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is False


async def test_null_client_get_deployment_with_raise_unavailable_returns_none():
    assert await NullK8sClient().get_deployment("api", "default", raise_unavailable=True) is None


def test_sec33_release_doc_states_k8s_wrapper_unknown_errors_are_503():
    doc = (ROOT / "docs" / "release" / "SEC-33-mcp-write-gate.md").read_text(encoding="utf-8")
    line = next(line for line in doc.splitlines() if "diagnose" in line and "AUTOFIX_INTERNAL_ERROR" in line)
    assert "execute_with_timeout" in line and "503" in line
    guide = (ROOT / "docs" / "autofix_guide.md").read_text(encoding="utf-8")
    assert "任何异常都会被视为上游不可用（503）" in guide
