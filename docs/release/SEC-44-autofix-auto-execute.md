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
| G4 | 每个写动作先写 `intent` 审计；写入失败时**不执行**（run 回到 pending；confirm 返回 **503** `confirm_audit_unavailable`，二次修正后审批**释放回 pending**，可用同一 `plan_id` 稍后重试，见「二次修正」Q1）。执行后再写 `result` 审计。结果审计失败时写操作已经提交，无法回退，只在执行记录上标记 `audit_result_recorded=false` 并打 ERROR 日志 | `app/core/autofix/audit.py`（新增）、`remediation.py::_execute_action` |
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

默认 sink 为 `LoggerAuditSink`：每条记录是 **stdout 上的一行纯 JSON**（logger `aiops.audit.autofix`，固定 INFO、`propagate=False`，
由专用 handler 直接写出，不受 `LOG_LEVEL`、`logging.disable` 和根 logger handler 影响；写入/flush 失败原样上抛，二次修正 P2-3）。可以用 `set_audit_sink(sink)` 注入其它实现，
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
- 新的错误码映射：confirm 写入口门禁拒绝返回 403（不执行，审批作废，需要重新生成计划）；意图审计写入失败返回 503（不执行；二次修正后审批释放回 pending，可重试，见「二次修正」Q1）。全部来源见「确认接口状态码与审批状态对照」。
- `execute_plan` 的默认触发是 `auto`（最严）。直接调用引擎、并且在人工确认后执行的代码需要改为传 `trigger="confirm"`。
- 门禁按**当前**全局配置判定。直接构造引擎（不经过 `build_namespace_policy`）的代码，必须同时在 `autofix.allowed_namespaces` 中配置目标命名空间。
- `KubernetesService.get_pods` 新增仅限关键字的参数 `raise_errors=False`，默认行为不变。

## 修正提交（SEC-44 修正，TL 复核后追加）

| 编号 | 修正 | 位置 |
| --- | --- | --- |
| F1 | 免确认白名单只保留 `restart_deployment`；`patch_image_pull_policy` 风险等级仍为 low，但一律确认。auto 开启时照样读取并记录 `live_evidence`，只是不放行 | `remediation.py::AUTO_EXECUTE_ACTION_TYPES` |
| F2 | CrashLoop 证据只认 `waiting=CrashLoopBackOff` 或 `restart_count >= 3`，删除 `not_ready_after_restart` 信号（未 Ready 且重启 ≥1） | `remediation.py::FAULT_EVIDENCE_SIGNALS`、`_pod_signals` |
| F3 | result 审计写入失败：单独打一条 ERROR 日志，只含异常类名（不再带 audit_id）；`execute_plan` 返回 `audit_result_recorded`，confirm 响应与 run 状态顶层、`execution` 内均带该字段（失败为 `false`） | `remediation.py`、`ops_workflow.py::_finalize`、`autofix_service.py` |
| F4 | confirm 路径在写入口要求可信用户主体 `user:<id>`。缺少 `X-CloudOps-User-ID`（中间件记为 `service:internal`）或无主体（审计中为 `unknown`）时：记一条 `refused` 审计（`reason="principal missing"`），0 写入。auto 路径调用方仍记为 `service:internal`。状态码原为 **401**，二次修正 Q7 改为 **403** `confirm_principal_required` | `remediation.py::_execute_action`、`autofix_service.py` |

~~F4 选 401 而不是 403~~（已被二次修正 Q7 取代）：前端把 401 当作 token 失效，会刷新 token 后重放、刷新失败则登出；
而这里与 token 无关，重放必然再次失败。改为 403 + 明确的 `error_code`。

其它说明：

- 直接构造引擎（不经过 `build_namespace_policy`）的调用方，必须同时在 `autofix.allowed_namespaces` 中配置目标命名空间，否则写入口门禁拒绝（auto 与 confirm 都适用）。
- confirm 路径的门禁**不要求** `repair_enabled`。**待用户早上审批**。
- 直接调用 `execute_plan(trigger="confirm")` 的代码，需要在 `principal_scope("user:<id>")` 内调用。

升级影响（修正）：已开启自动执行的部署中，imagePullPolicy 修复改走确认；仅靠「未 Ready 且重启 1–2 次」的 CrashLoop 不再自动重启；
没有 `X-CloudOps-User-ID` 的 confirm 调用从 200 变为拒绝（不执行；二次修正后为 403 `confirm_principal_required`）。

## 二次修正（03:33 TL 复核 P2/P3 + 04:00 追加 Q1/Q6/Q7/Q10）

| 编号 | 修正 | 位置 |
| --- | --- | --- |
| P2-1 | `restart_count >= 3` 只在容器当前 **未 Ready**（`ready is not True`）时算 `high_restart_count`；已恢复 Ready 的 Pod 不再触发自动重启 | `remediation.py::_pod_signals` |
| P2-2 | 实时证据只认目标 Deployment **当前 ReplicaSet** 的 Pod：Pod 的唯一 controller owner 为 `ReplicaSet <deployment>-<pod-template-hash>`；再实时读取该 ReplicaSet（`KubernetesService.get_replica_set` → `read_namespaced_replica_set`），要求其 controller owner 为 `Deployment <deployment>` 且 `deployment.kubernetes.io/revision` 与 Deployment 一致。Deployment 缺 revision 注解、ReplicaSet 读取失败/超时一律转确认。selector 含 `matchExpressions`（含 `match_expressions`）时直接 fail-closed，不读 Pod | `remediation.py::_collect_live_evidence`、`_pods_of_current_replica_set`、`_has_match_expressions`、`kubernetes.py::get_replica_set` |
| P2-3 | 默认审计 sink 改为专用 handler 直写 stdout 纯 JSON 行，不受 `LOG_LEVEL` / 根 handler 影响，写失败上抛（阻止执行） | `audit.py::StrictJsonLineHandler`、`LoggerAuditSink` |
| P2-4 | 测试锁定：门禁抛非预期异常 → 拒绝（0 写入 + `gate error` 审计）；`get_pods` 默认调用遇 ApiException 仍返回 `[]` | 测试 |
| P3-1 | auto 触发在写入口前**再实时读一次** Deployment/Pod/ReplicaSet 复核证据（`refusal="live_evidence"`），缩小证据采集与写入之间的 TOCTOU 窗口；confirm 不受影响 | `remediation.py::_recheck_live_evidence`、`execute_plan` |
| P3-2 / P3-3 | 测试锁定：`execute_plan(auto)` 校验引擎 `auto_execute`；`_execute_action` 默认 `auto_executed=True`；`plan_and_execute` 走 `trigger="auto"`；确认主体只取 `principal_context`（计划/动作里的 `principal` 字段不采信）。`X-CloudOps-User-ID` 只能由网关注入（见 `docs/production.md` 与下文 Q7） | 测试、文档 |
| P3-4 | `execute_plan` 文档说明：外部传入计划的 `live_evidence` 不被采信，由服务端重新推导 | `remediation.py` |
| Q1 | 确认时意图审计写入失败、且**能证明没有执行任何动作**（无已执行动作、无事后检查、选中动作全部未尝试）→ 审批**释放回 pending**（503 `confirm_audit_unavailable`），可用同一 `plan_id` 重试 | `workflow_approvals.py::release`、`autofix_service.py` |
| Q6 | 确认接口所有错误响应带 `data.approval_state` | `exceptions.py`、`decorators.py`、`error_handler.py`、`routes/autofix.py` |
| Q7 | 确认缺少可信用户主体：401 → **403** `confirm_principal_required` | `autofix_service.py` |

### Q1：审批释放的安全性

- **原子领取/释放（CAS）**：`claim` 在同一段无 `await` 的代码里检查 `state == "pending"` 并置为 `executing`，同时生成一次性 `claim_token`；
  `release(record, token)` 只有在 `state == "executing"`、`token` 与本次领取一致、`effects_started is False` 时才置回 `pending`，
  否则什么都不改并返回 `False`。两者都是单进程事件循环内的同步临界区（与原有 claim 的并发保证相同，审批记录本身也只在单进程内存中）。
- **不会并发重复执行**：领取期间（`executing`）的第二个确认请求在 `claim` 处得到 409（`approval_state=consumed`），不会进入执行；
  迟到的旧领取者持有的 token 已失效，不能把新领取者的执行状态改回 pending。
- **不绕过幂等**：只在执行器报告 `refusal="audit"` 且 `effects_started` 已回退为 `False` 时释放；部分执行、结果不确定（`uncertain`）、
  已完成（`completed`）、已作废（`invalidated`）的审批一律不释放。释放后 `record.result`（原计划）保持不变、过期时间不延长，
  重试时照常重新复核目标、动作参数、权限与命名空间，执行成功后进入 `completed`，再次确认得到 409。
- 其它领取后失败（复核不一致、命名空间、门禁、主体）仍作废（`invalidated`）；进入执行后的失败仍为 `uncertain`。

### Q6：`approval_state`

确认接口（`POST /api/v1/autofix/workflow/confirm`）的错误响应体 `data` 中新增 `approval_state`，只取三个值，不带其它审批信息：

| 值 | 含义 | 内部状态 |
| --- | --- | --- |
| `pending` | 审批仍有效，可用同一 `plan_id` 再次确认 | `pending`；以及本请求尚未识别身份 / 请求体校验失败 / 服务未就绪（本请求未触碰审批） |
| `consumed` | 审批已被领取或已执行（含结果不确定），不能再确认 | `executing` / `completed` / `uncertain` |
| `voided` | 审批已作废、不存在、已过期或属于他人（不区分，避免泄露） | `invalidated` / `building` / 无记录 / 他人记录 |

值在请求结束时按当前请求者重新读取；5xx 也带该字段（message 仍按原规则脱敏）。网关层错误（网关 401/403/502）不经过 aiops，没有该字段。

### 确认接口状态码与审批状态对照（Q10）

「审批作废」不等于「403」：下表列出确认接口所有 401/403/404/409/429/4xx/5xx 来源及审批状态（位置为本提交行号附近的函数）。

| 状态码 | `data.error_code` | 来源 | 发生时机 | `approval_state` | 前端是否重试 |
| --- | --- | --- | --- | --- | --- |
| 401 | —（网关自身响应） | 网关 JWT 无效/缺失或 Uid ≤ 0（`internal/app/aiops.go`） | 未到 aiops | 无字段（未触碰） | 按现有逻辑刷新 token 后重放 |
| 403 | —（网关自身响应） | 网关 RBAC 不允许该接口 | 未到 aiops | 无字段（未触碰） | 否 |
| 502 | —（网关自身响应） | 网关反向代理失败 | 可能已到达 aiops | 无字段（未知） | 否；重新诊断前先核对目标状态 |
| 401 | — | `ServiceAuthMiddleware`：服务 API Key 缺失/错误，或 `X-CloudOps-User-ID` 非法 | 网关配置错误，正常链路不会出现 | 无字段（未触碰） | 否（需运维修配置） |
| 401 | `SESSION_ACCESS_ERROR` | 路由 `require_principal()` | 中间件总会设置主体，正常链路不会出现 | `pending` | 否 |
| 400 / 422 | —（请求体校验） | Pydantic 请求体校验 | 领取前 | `pending` | 否（修正请求） |
| 400 | `VALIDATION_ERROR` | `plan_id` 为空 | 领取前 | `voided` | 否 |
| 404 | `WORKFLOW_APPROVAL_ERROR` | `WorkflowApprovals.get`：不存在/已过期/服务重启丢失 | 领取前 | `voided` | 否，重新诊断 |
| 403 | `WORKFLOW_APPROVAL_ERROR` | `WorkflowApprovals.get`：不是计划所有者 | 领取前，审批不变 | `voided`（不泄露他人状态） | 否 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | `claim`：状态不是 pending（正在执行、已完成、已作废、结果不确定） | 领取前 | `consumed` 或 `voided` | 否 |
| 400 | `VALIDATION_ERROR` | `claim`：动作 ID 为空/重复/不属于此计划 | 领取前，审批不变 | `pending` | 否（修正选择后可再确认） |
| 403 | `WORKFLOW_APPROVAL_ERROR` | `claim`：保存的硬限制（资源/权限/命名空间）未通过 | 领取前，审批不变 | `pending` | 否（重试仍是 403），重新诊断 |
| 503 | `WORKFLOW_APPROVAL_ERROR` | `_operation`：服务正在关闭 | 领取前 | `pending`（重启后会变成 404） | 是，稍后重试 |
| 429 | `WORKFLOW_APPROVAL_ERROR` | `_operation`：并发任务过多 | 领取前 | `pending` | 是，稍后重试 |
| 503 | `WORKFLOW_APPROVAL_ERROR` | `initialize`：领取后服务关闭 | 领取后、执行前 | `voided` | 否 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | 复核：目标不一致 / 动作或参数变化 / 目标资源不存在 / 动作 ID 失效 / 引擎未初始化等其它异常 | 领取后、执行前 | `voided` | 否，重新诊断 |
| 403 | `WORKFLOW_APPROVAL_ERROR` | 复核：当前权限或命名空间限制未通过 | 领取后、执行前 | `voided` | 否 |
| 403 | `WORKFLOW_APPROVAL_ERROR` | 执行前命名空间预检未通过；执行器命名空间拒绝 | 领取后、执行前 | `voided` | 否 |
| 403 | `WORKFLOW_APPROVAL_ERROR` | 写入口门禁未放行（G2） | 执行器内、写入前 | `voided` | 否 |
| 403 | `confirm_principal_required` | 写入口缺少可信用户主体（Q7，原 401） | 执行器内、写入前 | `voided` | 否 |
| 503 | `confirm_audit_unavailable` | 意图审计写入失败（G4），能证明未执行任何动作 | 执行器内、写入前 | **`pending`**（释放） | **是**，稍后用同一 `plan_id` 重试 |
| 503 | `confirm_audit_unavailable` | 意图审计写入失败，但无法证明无副作用 | 执行器内 | `consumed` | 否，人工核对 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | 部分执行/执行失败/结果未确认（含批量中第 2 个及之后动作被门禁或审计拒绝） | 已进入执行 | `consumed` | 否，人工核对 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | 确认请求超时（`AUTOFIX_WORKFLOW_TIMEOUT`） | 任意 | `consumed` 或 `voided` | 否，人工核对 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | 结果发布失败（无可保留待审批动作 / 超过 1 MiB） | 执行后 | `consumed`（已有写入）或 `voided`（未写入） | 否 |
| 500 | `confirm_internal_error` | 路由内未处理异常（message 脱敏） | 任意 | 按当前记录（通常 `pending`） | 仅 `pending` 时可稍后重试 |

前端规则可以简化为：**`approval_state == "pending"` 且状态码为 503/429（或 500）时保留确认入口、稍后重试；其余一律清掉计划**
（`consumed` 提示人工核对，`voided` 提示重新诊断）。

### Q7：网关链路能否碰到确认 401

只读核对主仓 `internal/app/aiops.go`（`188e907`）：`/api/v1/*path` 挂在 JWT/RBAC 中间件之后；
没有有效 claims 或 `Uid <= 0` 时网关自己返回 401，不会转发；转发时删除客户端的 `Authorization`、`Cookie` 和 `X-CloudOps-User-ID`，
再按 `claims.Uid` 设置 `X-CloudOps-User-ID` 与服务 `X-API-Key`。因此经网关的确认请求主体总是 `user:<Uid>`，
而计划所有者等于确认者，原先的确认 401 在正常网关链路上碰不到；只有绕过网关、持服务 Key 不带用户头生成并确认计划
（`service:internal`）或网关被错误配置时才会出现。为避免前端把它当成 token 过期去刷新/登出，仍改为 403 `confirm_principal_required`（防御性修改，可按 TL 决定回退）。

### 升级影响（二次修正）

- **RBAC 新增 `apps/replicasets get`**：自动执行前要读取 Pod 所属 ReplicaSet；没有该权限时读取失败，自动执行一律退回人工确认（不报错、不写集群）。
- 已开启自动执行的部署：已恢复 Ready 的高重启 Pod、缺 ownerReferences 或 revision 注解的工作负载、selector 含 `matchExpressions` 的 Deployment，不再自动执行。
- 每次自动执行额外读取：证据阶段 Deployment + Pod + 每个 ReplicaSet；执行前复核再读一遍。
- 审计输出改为 stdout 纯 JSON 行，不再经过根 logger 的格式和级别；日志采集按行解析 JSON 即可。审计写失败会阻止执行。
- 确认接口：缺主体 401 → 403 `confirm_principal_required`；意图审计失败的 503 后审批保留为 pending（原为作废）；所有错误体新增 `data.approval_state`。

### 残余风险

- Pod/ReplicaSet 名称形如 `<deploy>-<hash>` 的裸 ReplicaSet 已被 owner + revision 检查排除；但能在目标命名空间创建 ReplicaSet 并伪造
  `ownerReferences`（指向目标 Deployment、`controller: true`）与 revision 注解的主体，仍可能让证据匹配。该主体本身已具备直接改写工作负载的能力，不在本防护目标内。
- Pod 列表、ReplicaSet 与复核是多次独立读取，不是快照；复核缩小但不消除证据与写入之间的窗口。
- 审批记录只在单进程内存中，多副本部署时不同副本之间没有共享状态（与原有行为一致）。

## 回滚

回退本提交即可，无配置迁移，也无数据迁移。如果只想停止自动执行，把 `autofix.auto_execute_enabled` 设为 `false`
（默认值）后重启即可。确认流程不受影响。
