# MCP 时间工具 UTC 格式修复

`get_current_time` 继续保留已发布的两种格式和返回字段，普通助手的只读工具策略不变。

- 默认及 `format=iso` 返回 `time`、`format=ISO-8601`、`timezone=UTC`。时间为UTC、带单一 `Z` 后缀并保留微秒，例如 `2026-10-23T00:00:00.123456Z`
- 原实现对 aware UTC 的 `isoformat()` 再追加 `Z`，产生 `+00:00Z`，标准ISO解析失败。修复仅将已有 `+00:00` 表示换为单一 `Z`，不重复标记时区
- `format=timestamp` 仍返回整数 Unix 秒 `timestamp` 与原 `timezone` 字段，不转为字符串或毫秒，保留真实零。该字段沿用已发布接口回显传入时区（默认UTC），epoch数值本身不受显示时区影响
- ISO输出仍为UTC，不新增本地时区转换。普通助手仍只允许其现有UTC参数，内部高权限工具的其他既有参数行为不在本次修改范围内

验证完全离线：固定时钟证明默认/显式ISO可解析且offset为0、时间与微秒相等；Unix epoch零、当前数值及既有时区字段形状均回归。先复现2项ISO失败、3项数值通过，再修复；不调用模型、网络、通知或集群。

```sh
.venv/bin/python -m pytest tests/unit/test_mcp_time_contract.py -q
.venv/bin/python -m pytest -q
```
