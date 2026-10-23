"""测试入口自身的安全边界必须可复现验证"""

from pathlib import Path

import pytest

CONFTEST = (Path(__file__).parents[1] / "conftest.py").read_text()


@pytest.mark.parametrize("operation", [
    'socket.create_connection(("127.0.0.1", 9), timeout=1)',
    'socket.socket().connect(("127.0.0.1", 9))',
    'socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"guard-probe", ("127.0.0.1", 9))',
])
def test_network_attempt_cannot_be_hidden_by_catching_error(pytester, operation):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(f'''
import socket

def test_accidental_connection():
    try:
        {operation}
    except AssertionError:
        pass
''')
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*离线测试尝试访问外部网络*"])


def test_external_module_is_not_imported_without_opt_in(pytester):
    pytester.makeconftest(CONFTEST)
    external = pytester.path / "tests" / "external"
    external.mkdir(parents=True)
    (external / "test_forbidden.py").write_text("raise RuntimeError('External import side effect')")
    unit = pytester.path / "tests" / "unit"
    unit.mkdir()
    (unit / "test_safe.py").write_text("def test_offline(): assert True")
    result = pytester.runpytest_subprocess("tests", "-q")
    result.assert_outcomes(passed=1)


def test_external_opt_in_requires_explicit_target_and_key(pytester, monkeypatch):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile("def test_offline(): assert True")
    monkeypatch.setenv("AIOPS_RUN_EXTERNAL_TESTS", "1")
    monkeypatch.delenv("AIOPS_EXTERNAL_BASE_URL", raising=False)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == 4
    result.stderr.fnmatch_lines(["*AIOPS_EXTERNAL_BASE_URL*AIOPS_API_KEY*"])


def test_offline_module_cannot_access_network_during_collection(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile('''
import socket
socket.create_connection(("127.0.0.1", 9), timeout=1)
def test_unreachable(): pass
''')
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*测试收集阶段禁止真实网络访问*"])
