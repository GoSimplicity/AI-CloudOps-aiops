# SEC-44：autofix 自动执行加固

## 范围

只涉及 `/autofix/workflow`（run）的**免确认自动执行**，以及 run 与 `/autofix/workflow/confirm` 共用的写入口
`AutoFixRemediationEngine._execute_action`。`autofix.auto_execute_enabled` 默认仍为 `false`；默认配置下不会自动执行，
也不会多读一次集群。本单不涉及 G3、G5、G7、SEC-41、SEC-42/38。

## 行为变化

| 编号 | 变化 | 位置 |
| --- | --- | --- |
| G1 | 自动执行前从 Kubernetes **实时读取**目标 Deployment（取 selector）和 Pod 列表（`KubernetesService.get_pods(..., raise_errors=True)`，超时 15 秒，`EVIDENCE_TIMEOUT_SECONDS`）。证据要和动作对应的故障类型匹配才可能放行。证据缺失、读取失败或超时、没有 Pod、没有 selector、信号与故障类型不匹配时，一律转人工确认（pending）。请求里的文字或 `diagnosis` 不参与判定，单靠文本不会自动执行。确认（confirm）流程不受影响 | `remediation.py`：`_collect_live_evidence`、`_pod_signals`、`auto_execution_refusal`；`_assess_risk` 产出 `risk_assessment.live_evidence` |
| G2 | 写入口 `_execute_action` 在写集群前调用 `policy.ensure_mutation_allowed`，按**当前**配置检查命名空间，auto 触发时还检查 `auto_execute_enabled`，auto 与 confirm 都要经过。拒绝时不写集群，执行记录 `status="refused"`、`refusal="mutation_gate"`，并写一条 `refused` 审计。run 遇到这种情况回到 `needs_human_confirmation`；confirm 遇到这种情况返回 **403**（“写操作门禁未放行…”），本次审批作废 | `remediation.py::_mutation_refusal`、`policy.py::ensure_mutation_allowed`、`autofix_service.py` |
| G4 | 每个写动作先写 `intent` 审计；写入失败时**不执行**（run 回到 pending；confirm 返回 **503**，审批作废，需要重新生成计划）。执行后再写 `result` 审计。结果审计失败时写操作已经提交，无法回退，只在执行记录上标记 `audit_result_recorded=false` 并打 ERROR 日志 | `app/core/autofix/audit.py`（新增）、`remediation.py::_execute_action` |
| G6 | 只有免确认白名单内的动作才可能自动执行，白名单只有 `restart_deployment`（修正后）。`patch_image_pull_policy` 仍为 low 但一律确认；`scale_deployment` 和 `adjust_resource_requests`（medium）即使证据匹配也必须人工确认 | `remediation.py`：`ACTION_RISK_GRADES`、`AUTO_EXECUTE_ACTION_TYPES` |

纵深校验：同一条件 `auto_execution_refusal` 在三层各判一次，任何一层都能单独拦住自动执行，分别是
`_assess_risk`（计划）、`OpsMultiAgentWorkflow._may_auto_execute`（Reviewer/Executor，对任何执行引擎都适用）、
`execute_plan(trigger="auto")`（引擎入口）。`execute_plan` 新增仅限关键字的参数 `trigger`，可选 `"auto"`（默认，最严）或 `"confirm"`，
其他值抛 `ValueError`。确认接口固定传 `trigger="confirm"`。

## 动作分级与白名单（G6）

| 动作 | 风险等级 | 可自动执行 | 对应故障类型 |
| --- | --- | --- | --- |
| `restart_deployment` | low | 是（需证据） | `crash_loop_backoff` |
| `patch_image_pull_policy` | low | **否**，永远需要确认（auto 开启时照样读证据，只记录不放行） | `image_pull_failure` |
| `scale_deployment` | medium | **否**，永远需要确认 | `resource_insufficient` |
| `adjust_resource_requests` | medium | **否**，永远需要确认 | `resource_insufficient` |
| `manual_inspection`（不可执行） | none | 否 | — |

此外，以下情况仍按 SEC-26 规则升为 high 并强制确认，规则不变：影响多副本（replicas 大于 `max_auto_replicas`，默认 1）、资源不存在、RBAC 不允许或无法判定、
命名空间不在白名单、无回滚。

## 证据映射（G1）

| 故障类型 | 实时 Pod 信号（任一命中即匹配） |
| --- | --- |
| `crash_loop_backoff` | 容器 `state.waiting.reason=CrashLoopBackOff`；`restart_count >= 3`（修正后不再接受「Pod 未 Ready 且 `restart_count >= 1`」） |
| `image_pull_failure` | 容器 `state.waiting.reason` 为 `ImagePullBackOff` / `ErrImagePull` |
| `resource_insufficient` | `last_state.terminated.reason=OOMKilled`；`PodScheduled` 条件 `reason=Unschedulable` |
| 其它 / `unknown` | 无，永不匹配 |

证据格式为 `risk_assessment.live_evidence = {source: "live", fault_type, checked, matched, signals, pods_checked, reason}`。
未读取时 `source="none"`。只有 `auto_execute=true` 且计划里有可执行动作时才读取；读取失败的日志只记录异常类型和 status。

## 审计记录（G4）

默认 sink 为 `LoggerAuditSink`：每条记录是一行 JSON，写到 logger `aiops.audit.autofix`。可以用 `set_audit_sink(sink)` 注入其它实现，
只要提供 `write(record)` 方法即可；写入失败须抛异常。

| 字段 | 说明 |
| --- | --- |
| `event` / `schema_version` | 固定为 `autofix.mutation` / `1` |
| `audit_id` | 同一动作的 intent、result、refused 记录共用一个 ID |
| `phase` | `intent`（执行前）/ `result`（执行后）/ `refused`（门禁拒绝，尽力写入） |
| `time` | UTC ISO-8601 |
| `principal` | 来自 `X-CloudOps-User-ID` 的主体（`user:<id>`），内部调用为 `service:internal`，无上下文时为 `unknown` |
| `target` | `{namespace, kind: "Deployment", name}` |
| `action` | `{action_id, action_type}`，**不含** parameters 或 patch 内容 |
| `auto_executed` | `true` 表示免确认自动执行，`false` 表示人工确认后执行 |
| `result` | `pending` / `succeeded` / `failed` / `refused` |
| `error_type` | 只记异常类名，**不含** `str(e)`，也不含 API body |
| `reason` | 拒绝原因代码（`switch disabled` / `namespace not allowed` / `gate error` / `principal missing`） |

**对接 Go 侧审计**：sink 是可注入的协议。对接时可以实现一个 `GoAuditSink`，把记录同步 POST 到 Go 侧审计接口，
并在应用启动时调用 `set_audit_sink`。也可以保持日志 sink，由日志采集按 logger 名称转发。intent 必须同步写入成功，
远端失败时抛异常，这样就会拒绝执行。本单没有实现远端 sink，也没有连接任何服务。

## 升级影响

- **默认配置**（`auto_execute_enabled=false`）：run 仍然只出计划，confirm 照常执行。每个确认执行的动作会多写两条审计日志。
  confirm 的门禁**不要求** `repair_enabled`（默认为 false），否则默认配置下 confirm 全部会被拒绝。
- **已开启 `auto_execute_enabled=true` 的部署**：只有在实时 Pod 状态佐证的情况下，restart 才会自动执行。
  imagePullPolicy 修复、scale 和 requests 调整一律改走确认。每次 run 会额外读取一次 Pod 列表（list pods 需要 RBAC 权限；没有权限时读取失败，转人工确认）。
- 新的错误码映射：confirm 写入口门禁拒绝返回 403，意图审计写入失败返回 503。两种情况都不执行，审批作废，需要重新生成计划。
- `execute_plan` 的默认触发是 `auto`（最严）。直接调用引擎、并且在人工确认后执行的代码需要改为传 `trigger="confirm"`。
- 门禁按**当前**全局配置判定。直接构造引擎（不经过 `build_namespace_policy`）的代码，必须同时在 `autofix.allowed_namespaces` 中配置目标命名空间。
- `KubernetesService.get_pods` 新增仅限关键字的参数 `raise_errors=False`，默认行为不变。

## 修正提交（SEC-44 修正，TL 复核后追加）

| 编号 | 修正 | 位置 |
| --- | --- | --- |
| F1 | 免确认白名单只保留 `restart_deployment`；`patch_image_pull_policy` 风险等级仍为 low，但一律确认。auto 开启时照样读取并记录 `live_evidence`，只是不放行 | `remediation.py::AUTO_EXECUTE_ACTION_TYPES` |
| F2 | CrashLoop 证据只认 `waiting=CrashLoopBackOff` 或 `restart_count >= 3`，删除 `not_ready_after_restart` 信号（未 Ready 且重启 ≥1） | `remediation.py::FAULT_EVIDENCE_SIGNALS`、`_pod_signals` |
| F3 | result 审计写入失败：单独打一条 ERROR 日志，只含异常类名（不再带 audit_id）；`execute_plan` 返回 `audit_result_recorded`，confirm 响应与 run 状态顶层、`execution` 内均带该字段（失败为 `false`） | `remediation.py`、`ops_workflow.py::_finalize`、`autofix_service.py` |
| F4 | confirm 路径在写入口要求可信用户主体 `user:<id>`。缺少 `X-CloudOps-User-ID`（中间件记为 `service:internal`）或无主体（审计中为 `unknown`）时：记一条 `refused` 审计（`reason="principal missing"`），0 写入，confirm 返回 **401**。auto 路径调用方仍记为 `service:internal` | `remediation.py::_execute_action`、`autofix_service.py` |

F4 选 401 而不是 403 的原因：现有约定里「缺少/无效可信身份」一律是 401（`require_principal` 抛 `SessionAccessError(401, "缺少可信调用身份")`；
鉴权中间件对非法 `X-CloudOps-User-ID` 也返回 401），403 留给「身份已知但不允许」（门禁、命名空间、归属）。

其它说明：

- 直接构造引擎（不经过 `build_namespace_policy`）的调用方，必须同时在 `autofix.allowed_namespaces` 中配置目标命名空间，否则写入口门禁拒绝（auto 与 confirm 都适用）。
- confirm 路径的门禁**不要求** `repair_enabled`。**待用户早上审批**。
- 直接调用 `execute_plan(trigger="confirm")` 的代码，需要在 `principal_scope("user:<id>")` 内调用。

升级影响（修正）：已开启自动执行的部署中，imagePullPolicy 修复改走确认；仅靠「未 Ready 且重启 1–2 次」的 CrashLoop 不再自动重启；
没有 `X-CloudOps-User-ID` 的 confirm 调用从 200 变为 401（不执行）。

## 回滚

回退本提交即可，无配置迁移，也无数据迁移。如果只想停止自动执行，把 `autofix.auto_execute_enabled` 设为 `false`
（默认值）后重启即可。确认流程不受影响。
