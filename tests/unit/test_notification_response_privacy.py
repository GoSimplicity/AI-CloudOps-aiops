"""飞书投递只接受明确成功码，日志和信息响应不包含凭据或响应体"""

import json
import logging
from types import SimpleNamespace

import pytest

from app.config.settings import NotificationConfig
from app.services.notification import NotificationService


@pytest.mark.parametrize(
    'status,payload,expected',
    [
        (200, {'code': 0}, True),
        (200, {'code': 0, 'StatusCode': 0, 'StatusMessage': 'success'}, True),
        (200, {'code': 0, 'StatusCode': 999}, True),
        (200, {'code': 999, 'StatusCode': 0}, False),
        (200, {'StatusCode': 0}, False),
        (200, {'code': False}, False),
        (200, {'code': True}, False),
        (200, {'code': 0.0}, False),
        (200, {'code': '0'}, False),
        (200, {'code': None}, False),
        (200, {}, False),
        (200, [], False),
        (200, None, False),
        (500, {'code': 0}, False),
        (302, {'code': 0}, False),
        (200.0, {'code': 0}, False),
        (True, {'code': 0}, False),
    ],
)
def test_only_http_200_and_integer_current_code_zero_succeed(status, payload, expected):
    response = SimpleNamespace(status_code=status, json=lambda: payload)
    assert NotificationService()._handle_response(response) is expected


def test_failure_never_logs_echoed_webhook_or_provider_payload(caplog):
    marker = 'offline-private-echo-canary'
    response = SimpleNamespace(status_code=200, json=lambda: {'code': 999, 'msg': marker, 'url': 'https://offline.invalid/' + marker})
    with caplog.at_level(logging.INFO, logger='aiops.notification'):
        assert NotificationService()._handle_response(response) is False
    assert marker not in caplog.text and 'offline.invalid' not in caplog.text
    assert 'http_status=200 business_code=999' in caplog.text


def test_invalid_json_exception_never_logs_response_fragment(caplog):
    marker = 'offline-invalid-json-body-canary'

    def invalid():
        raise ValueError(marker)

    with caplog.at_level(logging.INFO, logger='aiops.notification'):
        assert NotificationService()._handle_response(SimpleNamespace(status_code=200, json=invalid)) is False
    assert marker not in caplog.text
    assert 'ValueError' in caplog.text


def test_success_log_contains_only_numeric_delivery_metadata(caplog):
    marker = 'offline-success-body-canary'
    with caplog.at_level(logging.INFO, logger='aiops.notification'):
        assert NotificationService()._handle_response(SimpleNamespace(status_code=200, json=lambda: {'code': 0, 'data': marker})) is True
    assert 'http_status=200 business_code=0' in caplog.text
    assert marker not in caplog.text


def test_notification_config_repr_hides_secret_without_mutating_value():
    marker = 'https://offline.invalid/private-config-canary'
    configuration = NotificationConfig(feishu_webhook=marker)
    assert configuration.feishu_webhook == marker
    assert marker not in repr(configuration)


async def test_notification_service_info_only_reports_presence():
    marker = 'https://offline.invalid/private-info-canary'
    service = NotificationService()
    service.feishu_webhook = marker
    service.enabled = True
    info = await service.get_service_info()
    assert info['webhook_configured'] is True
    assert marker not in json.dumps(info)


async def test_delivery_does_not_follow_redirect_or_retry(monkeypatch):
    from unittest.mock import Mock

    import requests

    service = NotificationService()
    service.enabled = True
    service.feishu_webhook = 'https://offline.invalid/dedicated-test'
    post = Mock(return_value=SimpleNamespace(status_code=302, json=lambda: {'code': 0}))
    monkeypatch.setattr(requests, 'post', post)
    assert await service.send_notification('本地测试', '仅离线替身') is False
    post.assert_called_once()
    assert post.call_args.kwargs['allow_redirects'] is False
