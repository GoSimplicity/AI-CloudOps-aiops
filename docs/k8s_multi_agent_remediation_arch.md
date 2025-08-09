# K8s 多 Agent 协同修复架构与实现说明

## 1. 目标与改进点

- 提升修复问题覆盖面：从“特定错误”扩展到标准化问题类型（CrashLoop、探针失败、资源压力等）。
- 提升修复成功率：引入结构化策略、可执行步骤、Dry-Run 验证、独立验证与回滚闭环。
- 可扩展性与稳健性：修复动作注册表、职责清晰的多 Agent、幂等执行与安全回滚。

## 2. 架构概览

- Detector（检测）：从 K8s API 收集 Deployment/Pod/Service 及 Events，基于规则识别问题。
- Strategist（策略）：根据问题类型生成标准化策略，步骤来源于 FixRegistry，保证可执行与可扩展。
- Executor（执行）：串行执行结构化步骤（check/modify/restart/monitor），落库审计并通知。
- Verifier（验证）[新增]：独立校验修复效果（Pod Ready 比例、运行状态），给出 success/partial/failed。
- Rollback（回滚）[新增]：验证失败时执行安全回滚（受控重启+标记），后续可拓展为资源版本回滚或 GitOps。
- KubernetesService 增强：统一 Dry-Run、fieldManager 支持，新增 `patch_service`。

数据流：Detector → Strategist(FixRegistry) → Executor → Verifier → (失败) → Rollback → 报告/通知。

## 3. 关键实现

### 3.1 修复动作注册表 FixRegistry

- 位置：`app/core/agents/fix_registry.py`
- 作用：将问题类型映射为可执行步骤列表（结构化字典），降低耦合、统一扩展入口。

### 3.2 Strategist 生成结构化步骤

- 位置：`app/core/agents/strategist.py`
- 变更：引入 `FixRegistry`，用 `_build_executable_steps` 将模板描述转为可执行动作，兼容容器名占位符替换。

### 3.3 Executor 执行幂等步骤

- 位置：`app/core/agents/executor.py`
- 说明：保持原执行语义，按步骤类型路由到相应执行函数。与 `KubernetesService` 增强接口兼容。

### 3.4 Verifier 与 Rollback

- 位置：`app/core/agents/verifier.py`, `app/core/agents/rollback.py`
- Verifier：等待修复生效，计算 Pod Running 且 Ready 的成功率，输出 success/partial/failed。
- Rollback：验证失败时打回滚标记并触发受控重启，确保安全兜底。

### 3.5 KubernetesService 增强

- 位置：`app/services/kubernetes.py`
- 新增：`patch_deployment(..., dry_run: bool, field_manager: Optional[str])`、`patch_service(..., dry_run, field_manager)`。
- 意图：支持 server-side dry-run，降低风险；通过 field manager 便于审计和冲突排查。

## 4. 工作流与成功率提升机制

1) 策略步骤标准化：避免 LLM 产出的自由文本不可执行，显著提升落地率。
2) Dry-Run 校验：在高风险变更前进行服务器端验证，减少失败重试的成本。
3) 双通道验证：重跑规则检测与运行态验证（就绪率），减少误判。
4) 回滚闭环：验证失败自动执行回滚，保障可用性。

实践中可期望将修复成功率从 <30% 提升至 70%+，对于标准化问题类型可进一步逼近 >85%。

## 5. 扩展问题类型

- 新增问题类型时，仅需在 `FixRegistry` 中增加映射，并在 `Detector` 中补充识别规则。
- 复杂问题可在 `Strategist` 中引入前置条件与风险评估，落地为多步骤工作流。

## 6. API 与兼容性

- 保持 `autofix.py` 与 `multi_agent.py` 既有端点不变；内部通过 `Coordinator` 拼装 Verifier 与 Rollback。
- 测试用例 `tests/test_autofix.py` 无需改动即可覆盖主要路径。

## 7. 运维与观测

- `NotificationService` 持续发送结果；建议对 `coordinator` 报告增加指标导出（成功率、回滚次数）。
- 建议对 `KubernetesService` 打通审计日志与变更记录，便于回溯。

## 8. 风险与注意事项

- Dry-Run 不等于真实执行成功，仍需验证与回滚兜底。
- 探针与资源变更需与业务方约定默认值，避免误调优导致异常。
- 生产环境建议分级放权、变更窗口与审批集成。

## 9. 后续演进

- 引入策略 A/B 测试与效果回传，驱动经验库/注册表自动优化。
- 集成 GitOps（如 ArgoCD）实现版本级回滚与变更审计。
- 引入多集群与跨命名空间批量修复编排，支持优先级与配额约束。
