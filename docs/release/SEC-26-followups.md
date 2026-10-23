# 承接 SEC-26 第一层审查的后续项（随 SEC-33 发布）

本说明覆盖 SEC-26 第一层审查遗留的后续项，以及 Tech Lead 补充并入 SEC-33 的三项决定。不含任何密钥，不改部署文件。

## 行为变化

| 编号 | 变化 | 升级影响 |
| --- | --- | --- |
| s1 | autofix 路由（repair / diagnose / bootstrap）：Kubernetes `ApiException` 与上游超时（`urllib3` 超时、`TimeoutError`）映射为 **502 `AUTOFIX_ERROR`**；其他未预期异常为 **500 `AUTOFIX_INTERNAL_ERROR`**。路由级整体超时仍为 504 `REQUEST_TIMEOUT`。 | 原来全部 502；前端如按 502 判断“内部错误”，需同时处理 500 |
| s4 | autofix 布尔开关解析规则：**先 strip，再 lower，等于 true 才开启**（`app/config/settings.py::_parse_autofix_bool`，SEC-33 的 MCP 写工具开关复用同一函数）。YAML 写成 `1`（数字）按关闭处理并打 WARNING。 | 原来 `" true"`（带空白）按关闭；现在开启 |
| s5 | 确认执行时重读白名单。配置只在启动时加载：**改白名单要重启，重启会清掉待审批项**。 | 文档化，无行为变化 |
| s6 | `POST /autofix` 请求体校验失败返回 400 `VALIDATION_ERROR`，固定文案“请求参数无效”，不回显校验器信息；`/autofix/diagnose` 的领域异常按类型映射（ValidationError 400、ServiceUnavailable 503、AutoFixError 502…），不再统一改写为 400 + 异常文本。 | diagnose 依赖不可用时由 400 变为 503 |
| s7 | remediation 日志只记异常类型、HTTP status 和 reason（截断 64 字符），不记 body/headers。 | 无 |
| P3-3 | autofix 路由与服务的 warning/error 日志只记异常类型，完整内容放 debug 日志。 | 排障需临时开 debug 日志级别 |
| P3-5 | assistant / predict / rca 的 `/ready` 503 不再在 `details` 里带异常文本。 | 无 |
| P3-6 | 确认时重新校验发现 `resource_exists=false` 返回 **409**（原 403），文案写明尚未执行、需重新诊断。见 `docs/autofix_guide.md`「错误响应契约」。 | 前端按 409 提示“资源已不存在” |
| P3-7 | 确认重读白名单时在局部构建策略（`AutoFixRemediationEngine.with_policy`：`allowed_namespaces`、`auto_execute`、受限命名空间取并集），不修改共享单例。 | 无 |
| P3-d | autofix / MCP 五个开关的 YAML 显式标签 `!!bool` 只接受不加引号的 `true`/`True`/`TRUE`/`false`/`False`/`FALSE`；`!!bool yes`、`!!bool 'true'`、`!!bool tRuE`、`!!bool 1` 等按关闭处理并告警（告警不含取值）。只改严格 loader，全局 `yaml.safe_load` 不变。 | 原来 `!!bool yes`/`on` 等为开启、`!!bool 'true'` 为开启，现在关闭 |
| P3-e | `/autofix/diagnose`：上游超时与 Kubernetes 不可用（`ApiException` 无状态码 / 0、429、5xx）由 502 `AUTOFIX_ERROR` 改为 **503 `SERVICE_UNAVAILABLE`**（`details` 为空）；未知异常为 500 `AUTOFIX_INTERNAL_ERROR`。覆盖 s1 中 diagnose 的 502 部分，repair / bootstrap 不变。见 `docs/autofix_guide.md`「diagnose 错误映射」。 | 前端按 502 判断 diagnose 上游失败的需改为 503 |

## Tech Lead 补充决定（同一提交）

1. **重放**：白名单拦截后重放仍返回 409，不改。
2. **生产环境 debug**：生产环境开启 debug 拒绝启动；“什么算生产”与 Go `pkg/envmode` 一致（详见 `docs/production.md`）。错误处理器在生产环境下即使 debug 也不回显 5xx details。
3. **`namespace_refusal` 公开化**：`AutoFixRemediationEngine.namespace_refusal` 为公开接口，`RemediationEngine` Protocol（`app/core/autofix/remediation.py`）声明该方法；保留 `_namespace_refusal` 兼容别名。`autofix_service` 确认执行前对**所有**引擎做预检（去掉了只对 `AutoFixRemediationEngine` 预检的分支）；引擎未实现或预检异常按拒绝处理（403，记录 invalidated，不调用 execute_plan）。

## 回滚

回退本提交即可恢复原行为；无配置迁移、无数据迁移。若只需临时放开 debug 启动限制，请把 `ENV` 设为 `development`/`test`（仅限非生产环境）。
