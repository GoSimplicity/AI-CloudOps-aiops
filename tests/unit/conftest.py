"""直接服务层单测显式模拟已认证的内部服务，HTTP鉴权测试另走真实中间件"""

import pytest

from app.common.identity import INTERNAL_PRINCIPAL, principal_scope


@pytest.fixture(autouse=True)
def internal_service_identity():
    with principal_scope(INTERNAL_PRINCIPAL):
        yield
