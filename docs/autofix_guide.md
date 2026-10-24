# Kubernetes 故障自动修复模块使用指南

## 概述

Kubernetes 故障自动修复模块是 AI-CloudOps 系统中的核心组件，旨在自动检测、诊断和修复 Kubernetes 集群中的常见问题，减少人工干预，提高系统可靠性。该模块利用人工智能技术分析集群问题，并应用适当的修复策略。

## 主要特性

- **自动诊断**: 识别常见的 Kubernetes 问题，如健康检查配置不当、资源限制不合理等
- **智能修复**: 基于问题分析自动生成和应用修复策略
- **修复验证**: 执行修复操作后验证修复效果
- **工作流协作**: 多个智能 Agent 协作完成复杂修复任务
- **通知机制**: 在重要事件发生时通知运维人员

## 架构设计

自动修复系统由多个组件协同工作：

```
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│                  │      │                  │      │                  │
│  SupervisorAgent │◄────►│   K8sFixerAgent  │◄────►│ KubernetesService│
│                  │      │                  │      │                  │
└────────┬─────────┘      └──────────────────┘      └──────────────────┘
         │
         │
         ▼
┌──────────────────┐
│                  │
│   NotifierAgent  │
│                  │
└──────────────────┘
```

- **SupervisorAgent**: 控制修复工作流程，协调各 Agent 的行动
- **K8sFixerAgent**: 执行具体的 Kubernetes 问题分析和修复操作
- **KubernetesService**: 提供与 Kubernetes API 交互的接口
- **NotifierAgent**: 处理修复过程中的通知发送

## 可检测和修复的问题

### Pod 健康检查问题

- ReadinessProbe 配置错误
- 探针路径不存在
- 探针频率过高/过低
- 失败阈值不合理

### 资源配置问题

- 资源请求过高，导致 Pod 无法被调度
- 内存/CPU 限制不合理，导致容器被终止
- 资源配置与应用实际需求不符

### 其他问题

- 镜像拉取失败
- 配置文件语法错误
- 存储卷配置问题
- 权限问题

## 使用方法

### 配置要求

在使用自动修复模块前，确保：

1. Kubernetes 配置文件正确设置
2. 环境变量已正确配置：

   ```bash
   export KUBECONFIG=/path/to/kubeconfig
   export K8S_CONFIG_PATH=/path/to/kubeconfig
   ```

3. 应用运行环境变量：
   ```bash
   export PYTHONPATH=$(pwd)
   ```

### 执行边界与前端契约（SEC-26）

自 SEC-26 起，自动修复**默认只生成计划，不自动执行任何动作（包括 low 风险动作）**；执行必须由计划所属主体调用确认接口显式选择动作。所有开关默认关闭（fail-closed），配置项不含任何密钥：

| 配置（YAML / 环境变量） | 默认值 | 说明 |
| --- | --- | --- |
| `autofix.auto_execute_enabled` / `AUTOFIX_AUTO_EXECUTE_ENABLED` | `false` | `true` 时，`/autofix/workflow`（run）中**低风险白名单动作且有实时证据佐证**的才无需确认直接执行（SEC-44，见下文「自动执行规则」）；**对 `/autofix/repair` 无效**，repair 恒为只出计划 |
| `autofix.allowed_namespaces` / `AUTOFIX_ALLOWED_NAMESPACES` | `[]`（空） | 允许执行修复动作的命名空间白名单；环境变量为逗号分隔。为空时任何命名空间都不能执行（确认也会被 403 拒绝） |
| `autofix.repair_enabled` / `AUTOFIX_REPAIR_ENABLED` | `false` | 是否开放旧入口 `POST /api/v1/autofix/repair` |
| `autofix.bootstrap_enabled` / `AUTOFIX_BOOTSTRAP_ENABLED` | `false` | 是否开放 `POST /api/v1/autofix/bootstrap`（创建 test 命名空间与故障演示资源）；开启后 `test` 仍须在 `allowed_namespaces` 内且不是受限命名空间，否则 403 |

`kube-system`、`kube-public`、`kube-node-lease` 等受限命名空间即使写入白名单也不会执行。`/autofix/run`、`/workflow/confirm`、`/autofix/repair` 与 `/autofix/bootstrap` 使用同一套命名空间判定（`AutoFixRemediationEngine._namespace_allowed`）。

**配置取值规则（启动时解析，修改后需重启）**：

- 三个布尔开关解析规则：**先 strip，再 lower，等于 true 才开启**；`false` 为关闭；其余任何值一律按 `false`（关闭）处理，并在启动时打印 WARNING 日志（logger `aiops.config.autofix`），提示该值不是 true/false：
  - **环境变量**：先 strip 再 lower（`true`/`True`/`TRUE`/` true `/`true\n` 均为开启）；`1`、`0`、`yes`、`no`、`on`、空字符串等都按关闭处理并告警。
  - **YAML**：开关字段使用专门的严格解析（不认 YAML 1.1 的布尔别名），只有不加引号的 `true`/`True`/`TRUE` 为开启、`false`/`False`/`FALSE` 为关闭。不加引号的 `yes`/`Yes`/`on`/`On`/`no`/`off` 等**不再**被当作布尔，按关闭处理并告警；`y`、数字 `1`/`0`、加引号的 `'yes'` 同样按关闭处理并告警。加引号的字符串与环境变量规则一致（先 strip 再 lower），`'true'`/`' TRUE '` 视为开启。注意：不加引号但大小写混写的 `tRUE`、`tRue`、`fALSE` 等不是 YAML 原生布尔，会先被读成字符串，再按环境变量规则（strip + 不区分大小写）判定，因此 `tRUE` 视为**开启**、`fALSE` 视为关闭，且不告警；建议统一写成 `true`/`false`。显式标签 `!!bool` 只接受不加引号的 `true`/`True`/`TRUE`/`false`/`False`/`FALSE`（如 `!!bool true`）；其它写法（`!!bool yes`、`!!bool 'true'`、`!!bool tRuE`、`!!bool 1` 等）一律按关闭处理并告警，告警只含开关名与配置文件路径，不含取值（SEC-26 P3-d）。
  - 告警日志只输出字段名和值的类型（如 `类型 str`、`类型 int`），不输出值本身；白名单告警只输出条目序号（第 N 项）。
  - 严格解析只作用于 `autofix.auto_execute_enabled`、`autofix.repair_enabled`、`autofix.bootstrap_enabled` 这三个字段，以及沿用同一解析函数的 SEC-33 开关 `mcp.write_tools_enabled`、`mcp.secret_read_enabled`；其它配置项（如 `app.debug`、`dev.auto_reload`）仍按原有 YAML 1.1 规则解析，行为不变。
- `allowed_namespaces` **大小写敏感、按字面精确匹配**，不做大小写归一；Kubernetes 命名空间均为小写，写成 `Staging` 不会匹配 `staging`。
- **`*` 不是通配符**：`*`、`prod-*` 等只会按字面匹配，实际不会放行任何命名空间；要放行多个命名空间请逐个列出。
- 启动时白名单中出现含 `*` 的条目或含大写字母的条目会打印 WARNING（不会自动改写，仍按字面生效）。
- 确认执行时会按当前配置重新读取白名单（在局部重新构建策略：`allowed_namespaces`、`auto_execute` 与受限命名空间，不修改共享的执行引擎单例）。但配置本身只在启动时加载：**改白名单要重启，重启会清掉待审批项**（审批状态只在进程内存中，重启后旧 plan_id 确认返回 404，需要重新生成计划）。

**审批状态仅保存在进程内存**：计划、待确认动作与防重放状态都在单进程内存中，重启即丢失（确认会返回 404）。部署时必须**单 worker 单副本**，或在多副本前配置**会话粘性（sticky session，按主体/plan 固定到同一实例）**；跨 worker 随机负载均衡会导致确认找不到计划或绕过防重放保证。审批状态持久化/共享存储为后续独立工单，不在 SEC-26 范围内。

#### 自动执行规则（SEC-44）

> 历史说明：SEC-44 合入之前，生产环境不要打开自动执行（`auto_execute_enabled`）。当时只要风险评估放行，仅凭请求文字/diagnosis 就可能免确认写集群。

SEC-44 起，`auto_execute_enabled=true` 时一个动作要免确认执行，必须**同时**满足以下条件，任一条不满足就转人工确认（pending），不写集群：

1. **免确认白名单**：只有 `restart_deployment` 在白名单内。`patch_image_pull_policy` 风险等级仍为 low，但一律需要确认（auto 开启时照样读取并记录证据，只是不放行）。`scale_deployment` 和 `adjust_resource_requests`（medium）即使证据匹配也必须确认。
2. **实时证据**：执行前实时读取 Deployment 和 Pod，信号必须与动作对应的故障类型匹配（CrashLoop 只认 `state.waiting.reason=CrashLoopBackOff`，或「重启次数 ≥3 且容器当前**未** Ready」；「未 Ready 且重启 ≥1」不算；镜像拉取对应 `ImagePullBackOff`/`ErrImagePull`）。读取失败、超时（15 秒）、没有 Pod 或信号不匹配时一律转确认；请求文字或 `diagnosis` 不参与判定。
   - **只认目标 Deployment 当前 ReplicaSet 的 Pod**（二次修正）：Pod 的 controller owner 必须是名为 `<deployment>-<pod-template-hash>` 的 ReplicaSet；该 ReplicaSet 还要实时读取（`read_namespaced_replica_set`，**需要 RBAC `apps/replicasets get`**），其 controller owner 必须是目标 Deployment，且 `deployment.kubernetes.io/revision` 与 Deployment 一致。缺 owner、缺 revision 注解、ReplicaSet 读取失败或超时一律转确认。
   - **selector 含 `matchExpressions` 时不支持，直接转确认**（fail-closed，不读 Pod），避免只按 `matchLabels` 扩大匹配范围。
   - **执行前复核**：auto 触发在写入口之前再实时读一次 Deployment 与 Pod 重新判定证据（缩小 TOCTOU 窗口）；复核不通过时整份计划转确认。外部传入的计划里自带的 `live_evidence` 不被采信，一律由服务端重新推导。
3. **写入口门禁**：写集群前按当前配置调用 `ensure_mutation_allowed`。auto 触发要求 `auto_execute_enabled` 仍为开启，且命名空间在白名单内；confirm 触发只要求命名空间在白名单内，**不要求** `repair_enabled`（这一点待用户早上审批）。被拒绝时不写集群：run 退回人工确认，confirm 返回 403。直接构造引擎（不经过 `build_namespace_policy`）的调用方，必须同时在 `autofix.allowed_namespaces` 中配置目标命名空间，否则写入口门禁会拒绝。
4. **审计先行**：执行前写 intent 审计，失败则不执行（confirm 返回 503 `confirm_audit_unavailable`，审批释放回 pending，可用同一 `plan_id` 稍后重试，见下文），执行后写 result 审计；result 审计失败时写操作已提交、无法回退，会单独打一条只含异常类名的 ERROR 日志，并在 confirm 响应和 run 状态里带 `audit_result_recorded=false`（正常为 `true`）。
   默认审计 sink 把每条记录写成 **stdout 上的一行纯 JSON**（不带时间/级别前缀）：logger `aiops.audit.autofix` 固定 INFO、`propagate=False`，**不受 `LOG_LEVEL` 和根 logger handler 影响**；写入或 flush 失败原样上抛，按上面的规则阻止执行。字段包括 principal、target、action、auto_executed、result、time、error_type，不含 patch 内容和异常文本。
5. **确认需要可信用户主体**：confirm 路径在写入口检查主体，必须是 `user:<id>`。`X-CloudOps-User-ID` **只能由网关注入**（网关会删除客户端自带的同名头，再按 JWT 的 Uid 设置；aiops 只信任持有服务 API Key 的调用方传来的该头）。缺少该头（中间件记为 `service:internal`）或无主体（`unknown`）时不执行：记一条 `refused` 审计（`reason="principal missing"`），0 写入，返回 **403**，`data.error_code="confirm_principal_required"`（二次修正：原为 401，改为 403 以免前端当成 token 过期去刷新/登出）。auto 路径的调用方仍记为 `service:internal`。
6. **确认接口错误契约**（二次修正）：`/autofix/workflow/confirm` 的所有错误响应都带 `data.approval_state`，取值 `pending`（仍可用同一 `plan_id` 确认）/ `voided`（已作废、不存在、已过期或无权查看）/ `consumed`（已被领取或已执行，含结果不确定）。各状态码来源与审批状态的完整对照见发布说明。

残余风险：Pod/ReplicaSet 的 owner 与名字都来自集群 API，能在目标命名空间里创建 ReplicaSet 并伪造 ownerReferences/revision 注解的主体，仍可能让证据匹配；这类主体本身已能直接改写工作负载，不在本防护目标内。

完整的映射表、字段说明和升级影响见 `docs/release/SEC-44-autofix-auto-execute.md`。

**写操作统一闸门**：`app/core/agents/k8s_fixer.py` 中所有可能修改集群的入口（`analyze_and_fix_deployment`、`_identify_and_fix_common_issues`、`_execute_fix`、`_execute_additional_action`、`process_agent_state`）以及每个实际写调用（`patch_deployment`、`restart_deployment`、`scale_deployment`）之前都经过 `app/core/autofix/policy.py::ensure_mutation_allowed`：`autofix.repair_enabled` 未开启时抛 `AUTOFIX_FEATURE_DISABLED`（403），命名空间不在白名单或属于受限命名空间时抛 `AUTOFIX_NAMESPACE_NOT_ALLOWED`（403），均不会调用 Kubernetes 客户端。

#### 错误响应契约

业务异常统一返回 `{"code": <HTTP 状态>, "message": "...", "data": {"status_code": ..., "path": ..., "detail": "<message>", "error_code": "<错误码>", "details": {...}}}`。`data.error_code` 恒定可用于前端分支判断；`data.details` 仅在 4xx（或非生产环境的 debug 模式）返回，5xx 在非 debug 模式或生产环境下 message 统一为“内部服务器错误”、`data.detail` 为 `Internal server error`，且不返回 details（`data.error_code` 仍返回）。生产环境（`ENV` 为 production/prod/prd，或空值/无法识别的取值）开启 debug 时服务拒绝启动，见 [production.md](production.md)。常见错误码：

| HTTP | `data.error_code` | 场景 |
| --- | --- | --- |
| 403 | `AUTOFIX_FEATURE_DISABLED` | repair/bootstrap 或 k8s_fixer 写操作开关关闭 |
| 403 | `AUTOFIX_NAMESPACE_NOT_ALLOWED` | 目标命名空间不在白名单或受限 |
| 400 | `VALIDATION_ERROR` | 参数错误，如 `force=true`；`POST /autofix` 请求体校验失败固定文案“请求参数无效”，不回显校验器信息 |
| 403/404/409 | `WORKFLOW_APPROVAL_ERROR` | 确认相关：见下文 403 与 409 区别；计划不存在或已过期为 404 |
| 409 | `WORKFLOW_APPROVAL_ERROR` | 确认时重新校验发现目标资源已不存在（`resource_exists=false`）：尚未执行任何动作，需重新诊断 |
| 500 | `AUTOFIX_INTERNAL_ERROR` | 自动修复服务内部未预期异常（非上游错误；固定文案，异常仅记服务端日志） |
| 502 | `AUTOFIX_ERROR` | 仅 repair / bootstrap：上游错误，即 Kubernetes API 返回错误（`ApiException`）或上游超时（固定文案，异常仅记服务端日志）；diagnose 不再返回 502，见下文「diagnose 错误映射」 |
| 503 | `SERVICE_UNAVAILABLE` | 依赖不可用（就绪检查失败时 `details` 不含异常文本）；diagnose 的上游超时 / Kubernetes 不可用（`details` 为空） |
| 504 | `REQUEST_TIMEOUT` | 路由级整体超时（如 repair/diagnose 总耗时超限） |

所有响应均不包含原始异常文本（`str(e)`）；完整堆栈只通过 `logger.exception` 记录在服务端。

#### diagnose 错误映射

`/autofix/diagnose`（SEC-26 P3-e）：

- 只有 `VALIDATION_ERROR` 返回 400（如 `namespace` 为空）。
- **上游超时与上游不可用返回 503 `SERVICE_UNAVAILABLE`**（不再是 502）：包括 Kubernetes 调用超过 `AUTOFIX_K8S_TIMEOUT`、`urllib3` 连接/读取超时、`TimeoutError`，以及表示上游不可用的 Kubernetes `ApiException`（无状态码 / 0、429、5xx）。服务层会去掉 `details`（原 `details.error` 可能含异常文本）后上抛，响应不含异常文本。
- 读取 Deployment 时其它 `ApiException`（如 404/403）与原行为一致：视为查不到，`diagnosis.deployment` 为 `null`，整体仍返回 200。
- **未知异常（非领域异常，如 `RuntimeError`）返回 500 `AUTOFIX_INTERNAL_ERROR`**（Tech Lead 裁决），文案固定。
- 已脱敏的领域异常（`AUTOFIX_FEATURE_DISABLED`、`AUTOFIX_NAMESPACE_NOT_ALLOWED`、`WORKFLOW_APPROVAL_ERROR`、`REQUEST_TIMEOUT` 等）原样上抛，按上表映射；路由级整体超时仍为 504 `REQUEST_TIMEOUT`。
- 仍可能出现的 502：diagnose 自身不再产生 `AUTOFIX_ERROR`；只有当下游组件显式抛出已脱敏的 `AutoFixError` / `RCAError` 领域异常时，才按统一映射返回 502（表示“上游返回了错误结果”，与“上游不可用”区分）。repair / bootstrap 的 Kubernetes `ApiException` / 上游超时仍为 502（本次未改，见上表）。
- 注意：Kubernetes 调用经 `execute_with_timeout` 包装，调用过程中出现的任何异常都会被视为上游不可用（503），而不是 500。

均不拼接异常文本。

#### 前端契约

1. **生成计划**：`POST /api/v1/autofix/workflow`。默认响应 `data.status = "needs_human_confirmation"`、`data.next_action = "human_confirm"`，并带：
   - `data.plan.plan_id`：确认时使用；
   - `data.approval.state = "pending"`、`data.approval.confirmation_required = true`；
   - `data.approval.confirm_endpoint = "/api/v1/autofix/workflow/confirm"`；
   - `data.approval.pending_action_ids`：可确认的动作 ID；`data.blocked_actions` / `data.candidate_actions` 为动作详情；
   - `data.approval.expires_at`、`data.approval.retry_safe = false`；`data.review.confirmation_required = true`；
   - `data.executed_actions` 为空。
   前端应以 `approval.confirmation_required === true`（或 `status === "needs_human_confirmation"`）判定“待确认”，不要再假设 low 风险动作已执行。
2. **确认执行**：`POST /api/v1/autofix/workflow/confirm`，请求体 `{"plan_id": "...", "approved_action_ids": ["..."]}`。只执行显式选择的原计划动作。部分确认后若仍有待确认动作，`data.status = "needs_human_confirmation"`，`data.review.confirmation_required` 与 `data.approval.confirmation_required` 仍为 `true`；全部确认执行后两者均为 `false`（`approval.state = "completed"`）。
   - **403（`WORKFLOW_APPROVAL_ERROR`，未执行任何动作）**：确认前重新校验时目标命名空间不在 `allowed_namespaces`（包括计划生成后白名单被收紧）、属于受限命名空间、权限检查不通过；以及执行阶段命名空间硬校验拒绝且没有任何动作被执行（所有执行引擎都必须实现 `namespace_refusal` 并经过该预检，未实现按拒绝处理）。403 表示可安全地修正配置后重新诊断。
   - **409（`resource_exists=false`，未执行任何动作）**：确认前重新校验时目标资源已不存在（例如 Deployment 已被删除），审批不能覆盖；尚未调用任何写接口，需重新诊断。
   - **409（可能已产生副作用）**：重复确认/并发确认/重放、计划目标或动作在重新校验时发生变化，以及执行中途失败或部分执行。409 禁止盲目重试，应先人工核对集群实际状态。
3. **功能关闭时**：`/autofix/repair`、`/autofix/bootstrap` 在开关关闭时返回 **HTTP 403**，响应体 `{"code": 403, "message": "自动修复入口 /autofix/repair 已关闭（autofix.repair_enabled / AUTOFIX_REPAIR_ENABLED=false）…", "data": {...}}`（bootstrap 同理，message 指明 `autofix.bootstrap_enabled`）。`data.error_code = "AUTOFIX_FEATURE_DISABLED"`，`data.details = {"switch": "autofix.repair_enabled" | "autofix.bootstrap_enabled"}`。缺少可信主体时仍先返回 401。关闭时不会初始化 Kubernetes 客户端。
   **`/autofix/bootstrap` 开启但命名空间不允许时**：目标命名空间 `test` 不在 `AUTOFIX_ALLOWED_NAMESPACES` 内（含白名单为空）或属于受限命名空间，返回 **HTTP 403**，`data.error_code = "AUTOFIX_NAMESPACE_NOT_ALLOWED"`，`data.details = {"namespace": "test", "switch": "autofix.allowed_namespaces"}`，message 示例：`测试资源引导入口 /autofix/bootstrap 已开启，但目标命名空间 test 不在白名单内：autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 为空（fail-closed），请将 test 加入 AUTOFIX_ALLOWED_NAMESPACES；已拒绝创建测试资源`。此检查同样先于服务初始化，拒绝时不初始化 Kubernetes 客户端、不创建命名空间或任何资源。要使用 bootstrap 需同时设置 `AUTOFIX_BOOTSTRAP_ENABLED=true` 与 `AUTOFIX_ALLOWED_NAMESPACES` 包含 `test`。判定顺序：401（无可信主体）→ 403 `AUTOFIX_FEATURE_DISABLED` → 403 `AUTOFIX_NAMESPACE_NOT_ALLOWED`。
4. **`/autofix/repair` 开启后**：不再直接修改集群，内部改走与 `/autofix/workflow` 相同的计划→确认链路，且**固定只出计划**：即使同时设置 `AUTOFIX_AUTO_EXECUTE_ENABLED=true`，repair 也不会免确认执行任何动作（`status = "needs_human_confirmation"`、`executed_actions = []`），必须经确认接口执行；该限制只作用于 repair 本次调用，不影响并发的 `/autofix/workflow` 请求。响应保留 `status/deployment/namespace/success/actions_taken/timestamp/execution_time` 字段，并新增 `next_action`、`plan_id`、`approval`、`candidate_actions`、`blocked_actions`、`review`；默认 `success=false`、`actions_taken=[]`，需要按 `approval.confirm_endpoint` 确认。`force=true` 返回 400（force 修复已停用）。Kubernetes API 错误（`ApiException`）或上游超时返回 HTTP 502、`data.error_code = "AUTOFIX_ERROR"`（debug 文案“自动修复错误: 修复操作失败”）；其他未预期内部异常返回 HTTP 500、`data.error_code = "AUTOFIX_INTERNAL_ERROR"`（debug 文案“自动修复内部错误: 修复操作失败”）；非 debug 模式或生产环境 message 为“内部服务器错误”（均不拼接异常文本），完整异常堆栈仅记录在服务端日志（`logger.exception`）。旧实现中的硬编码回退镜像替换与 force 激进规则已删除，不能通过开关恢复。
5. `GET /api/v1/autofix/config` 新增 `data.execution_policy`，返回上述四个开关的当前值与 `confirm_endpoint`。

### API 接口说明

#### 1. 自动修复部署

**端点**: `POST /api/v1/autofix`

**请求体**:

```json
{
  "deployment": "nginx-deployment",
  "namespace": "default",
  "event": "Pod启动失败的问题描述",
  "force": false
}
```

**参数说明**:

- `deployment`: 要修复的部署名称
- `namespace`: 部署所在的命名空间
- `event`: 问题描述，用于 AI 分析
- `force`: 已停用（SEC-26）；传 `true` 返回 400。入口默认关闭，开启后也只生成待确认计划

**响应**:

```json
{
  "status": "success",
  "result": "修复结果描述",
  "deployment": "nginx-deployment",
  "namespace": "default",
  "actions_taken": ["修改了readinessProbe配置", "调整了资源限制"],
  "timestamp": "2025-07-03T03:48:23.234063",
  "success": true
}
```

#### 2. 集群健康诊断

**端点**: `POST /api/v1/autofix/diagnose`

**请求体**:

```json
{
  "namespace": "default"
}
```

**参数说明**:

- `namespace`: 要诊断的命名空间，可选

**响应**:

```json
{
  "status": "success",
  "namespace": "default",
  "diagnosis": "诊断结果...",
  "timestamp": "2025-07-03T03:48:23.252909"
}
```

#### 3. 执行完整修复工作流

**端点**: `POST /api/v1/autofix/workflow`

**请求体**:

```json
{
  "problem_description": "详细的问题描述，用于工作流分析"
}
```

**参数说明**:

- `problem_description`: 问题的详细描述，越具体越好

**响应**:
包含工作流执行的详细步骤和结果。

### 实际使用示例

#### 示例 1: 修复健康检查问题

以下是一个自动修复 Nginx 部署健康检查问题的示例：

```bash
# 创建有问题的部署
kubectl apply -f data/sample/problematic-deployment.yaml

# 验证Pod无法启动
kubectl get pods -l app=nginx-problematic

# 调用自动修复API
curl -X POST http://localhost:8080/api/v1/autofix \
  -H "Content-Type: application/json" \
  -d '{
    "deployment": "nginx-problematic",
    "namespace": "default",
    "event": "Pod启动失败，原因是readinessProbe探针路径(/health)不存在，探针频率过高，且资源请求过高",
    "force": true
  }'

# 验证修复效果
kubectl get pods -l app=nginx-problematic
```

#### 示例 2: 集群健康诊断

```bash
# 执行集群诊断
curl -X POST http://localhost:8080/api/v1/autofix/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "default"
  }'
```

## 测试

项目包含一个自动化测试脚本`tests/test-autofix.py`，用于验证自动修复功能。该脚本会测试各 API 端点，并验证修复效果。

### 运行测试

```bash
# 确保环境变量已设置
export KUBECONFIG=deploy/kubernetes/config
export PYTHONPATH=$(pwd)

# 启动应用
python app/main.py

# 在另一个终端运行测试
python tests/test-autofix.py
```

### 测试用例

测试脚本包含以下测试用例：

1. 健康检查 API 测试
2. 自动修复 API 健康状态测试
3. 集群诊断测试
4. 正常部署的自动修复测试
5. 问题部署的自动修复测试
6. 通知发送测试
7. 完整工作流测试

## 常见问题排查

### 1. 无法连接到 Kubernetes

**症状**:

- `KubernetesService未初始化`错误
- API 返回"无法连接到 Kubernetes 集群"

**解决方案**:

- 检查 kubeconfig 文件是否存在且有效
- 确认环境变量正确设置
- 验证集群是否可访问：`kubectl get nodes`

### 2. 修复不生效

**症状**:

- API 返回成功但 Pod 仍未就绪

**可能原因**:

- 集群资源不足
- 应用实际问题与分析不符
- 网络或权限问题

**解决方案**:

1. 检查 Pod 事件：`kubectl describe pod <pod-name>`
2. 查看应用日志：`kubectl logs <pod-name>`
3. 尝试手动减小资源请求：
   ```bash
   kubectl patch deployment <deployment-name> -p '{"spec":{"template":{"spec":{"containers":[{"name":"nginx","resources":{"requests":{"memory":"32Mi","cpu":"50m"},"limits":{"memory":"64Mi","cpu":"100m"}},"readinessProbe":{"httpGet":{"path":"/"},"periodSeconds":10,"failureThreshold":3}}]}}}}'
   ```

### 3. LLM 模型调用超时

**症状**:

- API 请求超时
- 修复操作未完成

**解决方案**:

- 增加 API 请求超时时间
- 检查 LLM 服务状态
- 检查网络连接

## 最佳实践

1. **提供详细的问题描述**：在 API 请求中提供尽可能详细的问题描述，帮助 AI 更准确分析
2. **逐步修复**：对于复杂问题，先尝试修复最关键的问题，再处理次要问题
3. **结合监控**：将自动修复系统与监控系统集成，实现问题的自动检测和修复
4. **人工验证**：对于关键系统，建议在自动修复后进行人工验证

## 限制和注意事项

- 自动修复系统不能解决所有 Kubernetes 问题，特别是应用逻辑错误
- 对于生产环境，建议启用通知功能，及时了解修复操作
- 复杂或高风险操作可能需要人工介入
- 确保有适当的备份和回滚机制

## 未来计划

1. **更多资源类型支持**：扩展到更多 Kubernetes 资源类型的诊断和修复
2. **历史数据分析**：基于历史修复数据改进修复策略
3. **预测性修复**：在问题严重影响系统前预测并修复潜在问题
4. **跨集群支持**：支持多集群环境的统一诊断和修复
