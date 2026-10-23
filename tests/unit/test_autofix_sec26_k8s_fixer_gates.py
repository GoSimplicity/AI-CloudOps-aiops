"""SEC-26 P2-2：K8sFixerAgent 所有写入口 / 写执行点受 repair_enabled + 命名空间门禁（全部离线）"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.common.exceptions import FeatureDisabledError, NamespaceNotAllowedError
from app.config.settings import config
from app.core.agents import k8s_fixer as k8s_fixer_module
from app.core.agents.k8s_fixer import K8sFixerAgent

DEPLOYMENT = {
    "metadata": {"name": "nginx-demo", "namespace": "default"},
    "spec": {"template": {"spec": {"containers": [{"name": "nginx", "image": "nginx:1.27", "resources": {"limits": {"memory": "64Mi"}}}]}}},
}
OOM_CONTEXT = {"pods": [{"status": {"phase": "Running", "container_statuses": [{"last_state": {"terminated": {"reason": "OOMKilled"}}, "ready": False}]}}]}


def make_agent():
    k8s = AsyncMock()
    llm = AsyncMock()
    agent = K8sFixerAgent(k8s_client=k8s, llm_client=llm)
    agent.retry_delay = 0
    return agent, k8s, llm


def deployment_in(namespace):
    return {**DEPLOYMENT, "metadata": {"name": "nginx-demo", "namespace": namespace}}


# 5 个写入口：(名称, 以给定命名空间调用入口的协程工厂)
ENTRIES = {
    "analyze_and_fix_deployment": lambda agent, ns: agent.analyze_and_fix_deployment("nginx-demo", ns, "CrashLoopBackOff"),
    "_identify_and_fix_common_issues": lambda agent, ns: agent._identify_and_fix_common_issues(deployment_in(ns), OOM_CONTEXT, force_fix=True),
    "_execute_fix": lambda agent, ns: agent._execute_fix("nginx-demo", ns, {"action": "重启部署"}),
    "_execute_additional_action": lambda agent, ns: agent._execute_additional_action("nginx-demo", ns, "扩展副本 replicas 3"),
    "process_agent_state": lambda agent, ns: agent.process_agent_state(SimpleNamespace(context={"deployment": "nginx-demo", "namespace": ns, "problem": "x"})),
}


def assert_k8s_untouched(k8s, llm):
    # 构造时 `k8s_client or NullK8sClient()` 会触发 __bool__，其余任何调用（patch/scale/restart/get_*/health_check）都不允许
    assert [c for c in k8s.mock_calls if not c[0].startswith("__")] == []
    assert k8s.patch_deployment.await_count == 0
    assert k8s.scale_deployment.await_count == 0
    assert k8s.restart_deployment.await_count == 0
    assert [c for c in llm.mock_calls if not c[0].startswith("__")] == []


@pytest.fixture
def default_autofix(monkeypatch):
    # 与 config/config.yaml 及 AutoFixConfig 的默认值一致：repair 关闭、白名单为空
    monkeypatch.setattr(config.autofix, "repair_enabled", False)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", [])


@pytest.mark.parametrize("entry", list(ENTRIES))
async def test_entry_rejected_under_default_config(default_autofix, entry) -> None:
    agent, k8s, llm = make_agent()
    with pytest.raises(FeatureDisabledError) as raised:
        await ENTRIES[entry](agent, "default")
    assert raised.value.error_code == "AUTOFIX_FEATURE_DISABLED"
    assert_k8s_untouched(k8s, llm)


@pytest.mark.parametrize("entry", list(ENTRIES))
@pytest.mark.parametrize(
    "allowed,namespace",
    [([], "default"), (["default"], "prod"), (["default", "kube-system"], "kube-system"), (["Default"], "default"), (["*"], "default")],
)
async def test_entry_rejected_when_namespace_not_allowed(monkeypatch, entry, allowed, namespace) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    agent, k8s, llm = make_agent()
    with pytest.raises(NamespaceNotAllowedError) as raised:
        await ENTRIES[entry](agent, namespace)
    assert raised.value.error_code == "AUTOFIX_NAMESPACE_NOT_ALLOWED"
    assert raised.value.details["namespace"] == namespace
    assert_k8s_untouched(k8s, llm)


WRITE_POINTS = [
    ("patch_deployment", lambda agent: agent._execute_fix("nginx-demo", "default", {"action": "修改资源限制", "limits": {"memory": "256Mi"}})),
    ("patch_deployment", lambda agent: agent._execute_fix("nginx-demo", "default", {"action": "修改readinessProbe", "failureThreshold": 3})),
    ("restart_deployment", lambda agent: agent._execute_fix("nginx-demo", "default", {"action": "重启部署"})),
    ("scale_deployment", lambda agent: agent._execute_additional_action("nginx-demo", "default", "扩展副本 replicas 3")),
    ("patch_deployment", lambda agent: agent._identify_and_fix_common_issues(DEPLOYMENT, OOM_CONTEXT, force_fix=True)),
]


@pytest.mark.parametrize("operation,call", WRITE_POINTS)
async def test_write_point_guard_blocks_k8s_write_even_if_entry_passed(monkeypatch, operation, call) -> None:
    """入口放行后，紧贴 patch/scale/restart 的执行点门禁仍会二次校验并阻断写入"""
    seen = []

    def fake_guard(namespace, op):
        seen.append(op)
        if op in {"patch_deployment", "scale_deployment", "restart_deployment"}:
            raise NamespaceNotAllowedError("blocked", namespace=namespace)

    monkeypatch.setattr(k8s_fixer_module, "ensure_mutation_allowed", fake_guard)
    agent, k8s, _ = make_agent()
    with pytest.raises(NamespaceNotAllowedError):
        await call(agent)
    assert seen[-1] == operation
    assert k8s.patch_deployment.await_count == 0
    assert k8s.scale_deployment.await_count == 0
    assert k8s.restart_deployment.await_count == 0


async def test_write_points_execute_when_switch_and_namespace_allowed(monkeypatch) -> None:
    """放行配置下原逻辑保持不变（未删除任何修复代码，删除留给 SEC-30）"""
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    agent, k8s, _ = make_agent()
    agent._verify_fix = AsyncMock(return_value="ok")
    await agent._execute_fix("nginx-demo", "default", {"action": "重启部署"})
    k8s.restart_deployment.assert_awaited_once_with("nginx-demo", "default")
    assert await agent._execute_additional_action("nginx-demo", "default", "扩展副本 replicas 3") == "扩展部署到3个副本"
    k8s.scale_deployment.assert_awaited_once_with("nginx-demo", 3, "default")
