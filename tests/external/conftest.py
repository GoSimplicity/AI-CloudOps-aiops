"""真实服务必须由调用者显式提供隔离目标与凭据"""

import os
from urllib.parse import urlsplit

import httpx
import pytest


@pytest.fixture
def external_client():
    base_url = os.environ["AIOPS_EXTERNAL_BASE_URL"].rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        pytest.fail("AIOPS_EXTERNAL_BASE_URL 必须是无内嵌凭据的 http(s) 地址")
    with httpx.Client(base_url=base_url, headers={"X-API-Key": os.environ["AIOPS_API_KEY"]}, timeout=10, trust_env=False) as client:
        yield client
