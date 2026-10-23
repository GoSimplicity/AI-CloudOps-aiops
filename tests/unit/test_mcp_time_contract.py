"""时间工具使用固定离线时钟验证已发布格式，不请求模型或网络"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.mcp.server.tools import time_tool


def fixed_clock(monkeypatch, instant):
    def now(zone):
        assert zone is timezone.utc
        return instant

    monkeypatch.setattr(time_tool, "datetime", SimpleNamespace(now=now))


@pytest.mark.parametrize("parameters", [{}, {"format": "iso", "timezone": "UTC"}])
async def test_default_and_explicit_iso_are_parseable_utc(monkeypatch, parameters):
    instant = datetime(2026, 10, 23, 0, 0, 0, 123456, tzinfo=timezone.utc)
    fixed_clock(monkeypatch, instant)
    result = await time_tool.TimeTool().execute(parameters)
    parsed = datetime.fromisoformat(result["time"].replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == instant
    assert result == {"time": "2026-10-23T00:00:00.123456Z", "format": "ISO-8601", "timezone": "UTC"}


@pytest.mark.parametrize("instant,zone", [
    (datetime(1970, 1, 1, tzinfo=timezone.utc), "UTC"),
    (datetime(2026, 10, 23, 0, 0, 0, 123456, tzinfo=timezone.utc), "UTC"),
    (datetime(2026, 10, 23, 0, 0, 0, 123456, tzinfo=timezone.utc), "Asia/Shanghai"),
])
async def test_unix_seconds_and_published_timezone_shape_are_preserved(monkeypatch, instant, zone):
    fixed_clock(monkeypatch, instant)
    result = await time_tool.TimeTool().execute({"format": "timestamp", "timezone": zone})
    assert type(result["timestamp"]) is int
    assert result == {"timestamp": int(instant.timestamp()), "timezone": zone}
    assert datetime.fromtimestamp(result["timestamp"], timezone.utc) == instant.replace(microsecond=0)
