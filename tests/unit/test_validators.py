"""validators.py：正常值、边界、非法值和注入字符串。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils.validators import (
    sanitize_input,
    validate_confidence,
    validate_deployment_name,
    validate_metric_list,
    validate_metric_name,
    validate_namespace,
    validate_qps,
    validate_time_range,
)

INJECTION_SAMPLES = [
    "<script>alert(1)</script>",
    "'; DROP TABLE metrics; --",
    "nginx; cat /etc/passwd",
    "up{namespace=\"kube-system\"}",
    "../deploy/kubernetes/config",
    "name`whoami`",
    "a&b",
    "admin' OR '1'='1",
    "pod\x00name",
    "ns\nname",
]


def test_time_range_accepts_past_window_at_exact_limit():
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    start = end - timedelta(minutes=1440)
    assert validate_time_range(start, end) is True
    assert validate_time_range(start, start + timedelta(minutes=10), max_range_minutes=10) is True


def test_time_range_accepts_non_utc_aware_datetimes():
    end = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))) - timedelta(minutes=5)
    start = end - timedelta(minutes=30)
    assert validate_time_range(start, end, max_range_minutes=30) is True


def test_time_range_rejects_empty_inverted_future_and_overlong():
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    assert validate_time_range(past, past) is False
    assert validate_time_range(past, past - timedelta(minutes=1)) is False
    assert validate_time_range(past, datetime.now(timezone.utc) + timedelta(minutes=1)) is False
    assert validate_time_range(past - timedelta(minutes=1440, seconds=1), past) is False
    assert validate_time_range(past - timedelta(minutes=10), past, max_range_minutes=9) is False
    assert validate_time_range(past - timedelta(minutes=1), past, max_range_minutes=0) is False


@pytest.mark.xfail(strict=True, reason="已知问题：naive datetime 与带时区的当前时间比较会抛 TypeError，而不是返回 False，见 PR 描述")
def test_naive_datetimes_return_false():
    assert validate_time_range(datetime(2020, 1, 1, 0, 0, 0), datetime(2020, 1, 1, 1, 0, 0)) is False


@pytest.mark.parametrize(
    "name",
    ["up", "http_requests_total", "_private", ":node_cpu:", "A", "metric:name_1"],
)
def test_metric_name_accepts_prometheus_names(name):
    assert validate_metric_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["", " ", "1up", "http-requests", "up{job=\"a\"}", "rate(up[5m])", None, 1, ["up"], *INJECTION_SAMPLES],
)
def test_metric_name_rejects_blank_illegal_and_injection(name):
    assert validate_metric_name(name) is False


def test_metric_list_requires_every_name_to_be_valid():
    assert validate_metric_list(["up", "http_requests_total"]) is True
    assert validate_metric_list(["up"]) is True
    assert validate_metric_list([]) is False
    assert validate_metric_list(None) is False
    assert validate_metric_list("up") is False
    assert validate_metric_list(("up",)) is False
    assert validate_metric_list(["up", "bad-name"]) is False
    assert validate_metric_list(["up", None]) is False
    assert validate_metric_list(["'; DROP TABLE metrics; --"]) is False


@pytest.mark.parametrize("name", ["a", "nginx", "my-app-1", "a" * 253, "a" + ("-b" * 126)])
def test_deployment_name_accepts_kubernetes_names_through_253(name):
    assert len(name) <= 253
    assert validate_deployment_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["", "A", "-nginx", "nginx-", "my_app", "my.app", "a" * 254, None, 1, *INJECTION_SAMPLES],
)
def test_deployment_name_rejects_illegal_length_and_injection(name):
    assert validate_deployment_name(name) is False


@pytest.mark.parametrize("name", ["a", "default", "kube-system", "a" * 63, "ns-1"])
def test_namespace_accepts_names_through_63(name):
    assert len(name) <= 63
    assert validate_namespace(name) is True


@pytest.mark.parametrize(
    "name",
    ["", "Default", "kube_system", "-default", "default-", "a" * 64, None, ["default"], *INJECTION_SAMPLES],
)
def test_namespace_rejects_illegal_length_and_injection(name):
    assert validate_namespace(name) is False


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.5, 100000, 1e-9])
def test_qps_accepts_non_negative_numbers(value):
    assert validate_qps(value) is True


@pytest.mark.parametrize("value", [-0.1, -1, "10", None, float("nan"), float("-inf")])
def test_qps_rejects_negative_and_non_numeric(value):
    assert validate_qps(value) is False


@pytest.mark.xfail(strict=True, reason="已知问题：bool 被当成数字，正无穷也能通过 QPS 检查，见 PR 描述")
def test_qps_and_confidence_reject_booleans_and_positive_infinity():
    assert validate_qps(True) is False
    assert validate_qps(False) is False
    assert validate_qps(float("inf")) is False
    assert validate_confidence(True) is False
    assert validate_confidence(False) is False
    assert validate_confidence(float("inf")) is False


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_confidence_accepts_unit_interval(value):
    assert validate_confidence(value) is True


@pytest.mark.parametrize("value", [-0.01, 1.01, "0.5", None, float("nan"), float("inf")])
def test_confidence_rejects_out_of_range_and_non_numeric(value):
    assert validate_confidence(value) is False


def test_sanitize_input_strips_markup_and_quotes_then_truncates():
    assert sanitize_input("plain text") == "plain text"
    assert sanitize_input("<script>alert('xss')</script>") == "scriptalert(xss)/script"
    assert sanitize_input("a&b\"c'e`f") == "abcef"
    assert sanitize_input("'; DROP TABLE users; --") == "; DROP TABLE users; --"
    assert sanitize_input("<>" + "a" * 10, max_length=3) == "aaa"
    assert sanitize_input("abcd", max_length=4) == "abcd"
    assert sanitize_input("abcd", max_length=0) == ""
    assert sanitize_input("你好\nworld") == "你好\nworld"
    assert sanitize_input("") == ""


@pytest.mark.parametrize("value", [None, 1, b"<script>", ["<script>"], {"text": "<b>"}])
def test_sanitize_input_returns_empty_for_non_strings(value):
    assert sanitize_input(value) == ""
