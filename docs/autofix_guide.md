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
| `autofix.auto_execute_enabled` / `AUTOFIX_AUTO_EXECUTE_ENABLED` | `false` | `true` 时恢复旧行为：风险评估放行的动作无需确认直接执行（仍受白名单限制，不推荐） |
| `autofix.allowed_namespaces` / `AUTOFIX_ALLOWED_NAMESPACES` | `[]`（空） | 允许执行修复动作的命名空间白名单；环境变量为逗号分隔。为空时任何命名空间都不能执行（确认也会被 403 拒绝） |
| `autofix.repair_enabled` / `AUTOFIX_REPAIR_ENABLED` | `false` | 是否开放旧入口 `POST /api/v1/autofix/repair` |
| `autofix.bootstrap_enabled` / `AUTOFIX_BOOTSTRAP_ENABLED` | `false` | 是否开放 `POST /api/v1/autofix/bootstrap`（创建 test 命名空间与故障演示资源）；开启后 `test` 仍须在 `allowed_namespaces` 内且不是受限命名空间，否则 403 |

`kube-system`、`kube-public`、`kube-node-lease` 等受限命名空间即使写入白名单也不会执行。`/autofix/run`、`/workflow/confirm`、`/autofix/repair` 与 `/autofix/bootstrap` 使用同一套命名空间判定（`AutoFixRemediationEngine._namespace_allowed`）。

**配置取值规则（启动时解析，修改后需重启）**：

- 三个布尔开关**只有 `true` 才开启**（大小写不敏感，如 `true`/`TRUE`）；`false` 为关闭；其余任何值——包括空字符串、`1`、`0`、`yes`、`no`、`on`，以及 YAML 中的数字 `1`——一律按 `false`（关闭）处理，并在启动时打印 WARNING 日志（logger `aiops.config.autofix`），提示该值不是 true/false。
- `allowed_namespaces` **大小写敏感、按字面精确匹配**，不做大小写归一；Kubernetes 命名空间均为小写，写成 `Staging` 不会匹配 `staging`。
- **`*` 不是通配符**：`*`、`prod-*` 等只会按字面匹配，实际不会放行任何命名空间；要放行多个命名空间请逐个列出。
- 启动时白名单中出现含 `*` 的条目或含大写字母的条目会打印 WARNING（不会自动改写，仍按字面生效）。
- 确认执行时会按当前配置重新读取白名单；生产中白名单只随重启变化。

**审批状态仅保存在进程内存**：计划、待确认动作与防重放状态都在单进程内存中，重启即丢失（确认会返回 404）。部署时必须**单 worker 单副本**，或在多副本前配置**会话粘性（sticky session，按主体/plan 固定到同一实例）**；跨 worker 随机负载均衡会导致确认找不到计划或绕过防重放保证。审批状态持久化/共享存储为后续独立工单，不在 SEC-26 范围内。

**写操作统一闸门**：`app/core/agents/k8s_fixer.py` 中所有可能修改集群的入口（`analyze_and_fix_deployment`、`_identify_and_fix_common_issues`、`_execute_fix`、`_execute_additional_action`、`process_agent_state`）以及每个实际写调用（`patch_deployment`、`restart_deployment`、`scale_deployment`）之前都经过 `app/core/autofix/policy.py::ensure_mutation_allowed`：`autofix.repair_enabled` 未开启时抛 `AUTOFIX_FEATURE_DISABLED`（403），命名空间不在白名单或属于受限命名空间时抛 `AUTOFIX_NAMESPACE_NOT_ALLOWED`（403），均不会调用 Kubernetes 客户端。

#### 错误响应契约

业务异常统一返回 `{"code": <HTTP 状态>, "message": "...", "data": {"status_code": ..., "path": ..., "detail": "<message>", "error_code": "<错误码>", "details": {...}}}`。`data.error_code` 恒定可用于前端分支判断；`data.details` 仅在 4xx（或 debug 模式）返回，5xx 在非 debug 模式下 message 统一为“内部服务器错误”、`data.detail` 为 `Internal server error`，且不返回 details（`data.error_code` 仍返回）。常见错误码：

| HTTP | `data.error_code` | 场景 |
| --- | --- | --- |
| 403 | `AUTOFIX_FEATURE_DISABLED` | repair/bootstrap 或 k8s_fixer 写操作开关关闭 |
| 403 | `AUTOFIX_NAMESPACE_NOT_ALLOWED` | 目标命名空间不在白名单或受限 |
| 400 | `VALIDATION_ERROR` | 参数错误，如 `force=true` |
| 403/404/409 | `WORKFLOW_APPROVAL_ERROR` | 确认相关：见下文 403 与 409 区别；计划不存在或已过期为 404 |
| 502 | `AUTOFIX_ERROR` | 自动修复内部错误（固定文案，异常仅记服务端日志） |
| 503 | `SERVICE_UNAVAILABLE` | 依赖不可用 |

所有响应均不包含原始异常文本（`str(e)`）；完整堆栈只通过 `logger.exception` 记录在服务端。

#### 前端契约

1. **生成计划**：`POST /api/v1/autofix/workflow`。默认响应 `data.status = "needs_human_confirmation"`、`data.next_action = "human_confirm"`，并带：
   - `data.plan.plan_id`：确认时使用；
   - `data.approval.state = "pending"`、`data.approval.confirmation_required = true`；
   - `data.approval.confirm_endpoint = "/api/v1/autofix/workflow/confirm"`；
   - `data.approval.pending_action_ids`：可确认的动作 ID；`data.blocked_actions` / `data.candidate_actions` 为动作详情；
   - `data.approval.expires_at`、`data.approval.retry_safe = false`；`data.review.confirmation_required = true`；
   - `data.executed_actions` 为空。
   前端应以 `approval.confirmation_required === true`（或 `status === "needs_human_confirmation"`）判定“待确认”，不要再假设 low 风险动作已执行。
2. **确认执行**：`POST /api/v1/autofix/workflow/confirm`，请求体 `{"plan_id": "...", "approved_action_ids": ["..."]}`。只执行显式选择的原计划动作。
   - **403（`WORKFLOW_APPROVAL_ERROR`，未执行任何动作）**：确认前重新校验时目标命名空间不在 `allowed_namespaces`（包括计划生成后白名单被收紧）、属于受限命名空间、资源或权限检查不通过；以及执行阶段命名空间硬校验拒绝且没有任何动作被执行。403 表示可安全地修正配置后重新诊断。
   - **409（可能已产生副作用）**：重复确认/并发确认/重放、计划目标或动作在重新校验时发生变化，以及执行中途失败或部分执行。409 禁止盲目重试，应先人工核对集群实际状态。
3. **功能关闭时**：`/autofix/repair`、`/autofix/bootstrap` 在开关关闭时返回 **HTTP 403**，响应体 `{"code": 403, "message": "自动修复入口 /autofix/repair 已关闭（autofix.repair_enabled / AUTOFIX_REPAIR_ENABLED=false）…", "data": {...}}`（bootstrap 同理，message 指明 `autofix.bootstrap_enabled`）。`data.error_code = "AUTOFIX_FEATURE_DISABLED"`，`data.details = {"switch": "autofix.repair_enabled" | "autofix.bootstrap_enabled"}`。缺少可信主体时仍先返回 401。关闭时不会初始化 Kubernetes 客户端。
   **`/autofix/bootstrap` 开启但命名空间不允许时**：目标命名空间 `test` 不在 `AUTOFIX_ALLOWED_NAMESPACES` 内（含白名单为空）或属于受限命名空间，返回 **HTTP 403**，`data.error_code = "AUTOFIX_NAMESPACE_NOT_ALLOWED"`，`data.details = {"namespace": "test", "switch": "autofix.allowed_namespaces"}`，message 示例：`测试资源引导入口 /autofix/bootstrap 已开启，但目标命名空间 test 不在白名单内：autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 为空（fail-closed），请将 test 加入 AUTOFIX_ALLOWED_NAMESPACES；已拒绝创建测试资源`。此检查同样先于服务初始化，拒绝时不初始化 Kubernetes 客户端、不创建命名空间或任何资源。要使用 bootstrap 需同时设置 `AUTOFIX_BOOTSTRAP_ENABLED=true` 与 `AUTOFIX_ALLOWED_NAMESPACES` 包含 `test`。判定顺序：401（无可信主体）→ 403 `AUTOFIX_FEATURE_DISABLED` → 403 `AUTOFIX_NAMESPACE_NOT_ALLOWED`。
4. **`/autofix/repair` 开启后**：不再直接修改集群，内部改走与 `/autofix/workflow` 相同的计划→确认链路，响应保留 `status/deployment/namespace/success/actions_taken/timestamp/execution_time` 字段，并新增 `next_action`、`plan_id`、`approval`、`candidate_actions`、`blocked_actions`、`review`；默认 `success=false`、`actions_taken=[]`，需要按 `approval.confirm_endpoint` 确认。`force=true` 返回 400（force 修复已停用）。未预期的内部异常返回 HTTP 502、`data.error_code = "AUTOFIX_ERROR"`；非 debug 模式 message 为“内部服务器错误”，debug 模式为固定文案“自动修复错误: 修复操作失败”（均不拼接异常文本），完整异常堆栈仅记录在服务端日志（`logger.exception`）。旧实现中的硬编码回退镜像替换与 force 激进规则已删除，不能通过开关恢复。
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
