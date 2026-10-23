"""直接服务层单测显式模拟已认证的内部服务，HTTP鉴权测试另走真实中间件"""

import pytest

from app.common.identity import INTERNAL_PRINCIPAL, principal_scope


@pytest.fixture(autouse=True)
def internal_service_identity():
    with principal_scope(INTERNAL_PRINCIPAL):
        yield


@pytest.fixture(autouse=True)
def reset_mcp_file_root():
    """MCP_FILE_ROOT 的 realpath 在进程内固定（SEC-33 P3），每个测试前后清除，避免测试间互相影响"""
    from app.utils.file_boundary import reset_file_root_cache

    reset_file_root_cache()
    yield
    reset_file_root_cache()
