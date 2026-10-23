# SEC-33：MCP 写工具门禁

## 范围

MCP 服务端所有工具调用（HTTP `POST /tools/execute`、`MCPAssistant` 客户端、进程内 `MCPServer.execute_tool`）都在
`tool.execute` 之前经过 `app/mcp/server/write_gate.py::enforce_write_gate`。门禁拒绝时**不初始化 Kubernetes
客户端、不发起任何 Kubernetes 调用**。SSE 只列出工具，不执行。

## 新增开关

| 开关 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mcp.write_tools_enabled` | `MCP_WRITE_TOOLS_ENABLED` | `false` | MCP 写类工具总开关。fail-closed：只认 `true`（大小写同 autofix 开关），其它值都按关闭处理 |
| `mcp.secret_read_enabled` | `MCP_SECRET_READ_ENABLED` | `false` | Secret 读开关：`list_secrets` / `get_secret`，以及 `resource_type` 为 Secret 或无法识别的 `describe_resource` / `get_resource_yaml`。fail-closed，同上 |

写操作要同时满足：`mcp.write_tools_enabled=true`、`autofix.repair_enabled=true`、目标命名空间在
`autofix.allowed_namespaces` 白名单内且不是受限命名空间（kube-system 等，复用 `ensure_namespace_allowed`）。
环境变量优先于 YAML。`config/config.yaml`、`config/config.production.yaml`、`config/config.test.yaml` 都显式写成 `false`。

## 拒绝契约与错误码

被门禁拒绝的调用仍返回 **HTTP 200**，响应体 `status="error"`、`result=null`、`error` 为固定中文文案、`error_code` 为下表之一。
（进程内调用抛 `FeatureDisabledError`，`status_code=403`，`error_code` 相同。）成功响应的 `error_code` 为 `null`。

| error_code | 触发条件 |
| --- | --- |
| `MCP_WRITE_DISABLED` | 写操作且 `mcp.write_tools_enabled` 关闭；或 `autofix.repair_enabled` 关闭（`details.switch` 区分） |
| `MCP_NAMESPACE_NOT_ALLOWED` | 目标命名空间不在白名单 / 属于受限命名空间 / 无法确定（含 `apply_yaml` 解析失败；写操作或 Secret 读（含按 Secret 处理的 `describe_resource` / `get_resource_yaml`）带 `all_namespaces`，即使白名单含 `default` 也拒绝） |
| `MCP_CLUSTER_SCOPED_DENIED` | 集群级写：`create_namespace`、`delete_namespace` 及 `k8s_namespace_management` 其它非读 operation；`add_labels` 等作用于 namespace/node 等非命名空间资源（即 patch_namespace / patch_node）；`apply_yaml` 含集群级 kind（kind 比较不区分大小写）。**任何开关都不能放行** |
| `MCP_CONFIG_PATH_DENIED` | 写操作或 Secret 读取带非默认 `config_path`（缺省、`null`、空串为默认，其它值一律拒绝） |
| `MCP_SECRET_READ_DISABLED` | `list_secrets` / `get_secret`，或 `resource_type` 为 Secret / 无法识别的 `describe_resource` / `get_resource_yaml`，且 `mcp.secret_read_enabled` 关闭 |

检查顺序：只读放行 → Secret 读（开关 → config_path → 白名单）→ 集群级拒绝 → 写（config_path → 写开关 → repair_enabled → 白名单）。

审计/告警日志（logger `aiops.mcp.write_gate`）只记录 `tool`、`error_code`、`switch`，**不记录 operation、命名空间、资源名、
config_path、YAML 等任何参数值**；响应文案也不回显参数值。

## 分类表

未在下表"只读"中列出的工具或 operation（包括新工具、拼写变体、大小写或空白不同、非字符串）**一律按写处理**。

整工具只读：`get_current_time`、`get_system_info`、`read_file`、`list_directory`、`get_file_stats`、`calculate`、
`calculate_statistics`、`convert_units`、`k8s_cluster_check`。

| 工具 | 只读 operation | Secret 读（受 `mcp.secret_read_enabled`） | 写 operation |
| --- | --- | --- | --- |
| `k8s_pod_management` | `list_pods`、`get_pod_details`、`get_pod_events` | | `delete_pod`、`restart_pod` |
| `k8s_service_management` | `list_services`、`get_service_details`、`get_endpoints` | | `create_service`、`delete_service` |
| `k8s_deployment_management` | `list_deployments`、`get_deployment_status`、`get_rollout_history` | | `update_image`、`rollback`、`scale`、`restart_deployment` |
| `k8s_config_management` | `list_configmaps`、`get_configmap` | `list_secrets`、`get_secret` | `create_configmap`、`create_secret`、`update_configmap`、`update_secret`、`delete_configmap`、`delete_secret` |
| `k8s_logs_viewer` | `get_pod_logs`、`get_container_logs`、`get_previous_logs`、`tail_logs`、`search_logs` | | |
| `k8s_resource_monitor` | `get_node_metrics`、`get_pod_metrics`、`get_resource_quotas`、`get_limit_ranges`、`get_top_pods`、`get_top_nodes` | | |
| `k8s_namespace_management` | `list_namespaces`、`get_namespace`、`get_namespace_resources` | | `create_namespace`、`delete_namespace`（集群级，始终拒绝） |
| `k8s_scaling_management` | `list_hpa`、`get_hpa_status` | | `scale_deployment`、`scale_replicaset`、`create_hpa`、`delete_hpa` |
| `k8s_ingress_management` | `list_ingresses`、`get_ingress`、`get_ingress_classes` | | `create_ingress`、`delete_ingress` |
| `k8s_resource_operations` | `describe_resource`、`get_resource_yaml`（仅已知非 Secret 的 `resource_type`） | `describe_resource`、`get_resource_yaml`（`resource_type` 为 Secret 或无法识别） | `add_labels`、`remove_labels`、`add_annotations`、`remove_annotations`、`apply_yaml` |

- `describe_resource` / `get_resource_yaml`（SEC-33 P1-1）：`resource_type` 先 strip + 小写，再按白名单匹配已知非 Secret 类型
  （pod/pods/po、service/services/svc、deployment/deployments/deploy、replicaset/replicasets/rs、configmap/configmaps/cm、
  ingress/ingresses/ing、namespace/namespaces/ns、node/nodes/no、pv/persistentvolume(s)、pvc/persistentvolumeclaim(s)），
  命中才按只读放行。白名单里没有任何一项包含 `secret`（NFKC + casefold 后比较，有测试保证）；`secrets.v1`、`Secret.core`、`v1/secrets`、
  带零宽字符的 `secret\u200b`、全角 `ｓｅｃｒｅｔ` 都不在白名单内。`secret`/`secrets`（任意大小写、前后空白）以及无法识别的值（缺省、空串、`null`、非字符串、未知字符串）
  **一律按 Secret 读处理**：要求 `mcp.secret_read_enabled=true`、默认 `config_path`、命名空间在白名单内且非受限、不允许
  `all_namespaces`。修复前这两个 operation 被整体列为只读，默认配置下即可经 MCP 直连读取任意命名空间 Secret 的完整内容。
- `add_labels` / `remove_labels` / `add_annotations` / `remove_annotations`：`resource_type` 只有
  pod、service、deployment、replicaset、configmap、secret、ingress 按命名空间写处理，其它（namespace、node 等）按集群级拒绝。
- `apply_yaml`：用工具自己的解析器取出每个文档的 kind/namespace，所有文档都必须在白名单内；文档未写 namespace 时按参数
  `namespace`（缺省 `default`）；解析失败按 `MCP_NAMESPACE_NOT_ALLOWED` fail-closed；含集群级 kind（Namespace、Node、
  ClusterRole、ClusterRoleBinding、CRD 等）按 `MCP_CLUSTER_SCOPED_DENIED`；kind 比较不区分大小写（`namespace`、`NAMESPACE`、
  `clusterrole` 同样拒绝，SEC-33 P3）。工具执行时仍会用 discovery 二次拒绝非命名空间资源（列表外的集群级 kind 靠这一层）。
  `config_path` 为 `null` 或空串与缺省等价，按默认 kubeconfig 处理，不会因解析失败被误判为 `MCP_NAMESPACE_NOT_ALLOWED`。
- 其它写 operation 取参数 `namespace`，缺省 `default`（也必须在白名单内）。
- 写操作带 `all_namespaces`（缺省或显式 `false` 以外的任何取值，包括 `"false"`、`0`、`null`）一律按命名空间无法确定拒绝
  （`MCP_NAMESPACE_NOT_ALLOWED`），即使白名单含 `default`；Secret 读同理（SEC-33 P3，补上 TL 变异 M9 的测试缺口）。

## Secret

- `get_secret` 在开关关闭时选择**拒绝**（`MCP_SECRET_READ_DISABLED`），不做打码返回。
- 开关打开时，两者仍要求命名空间在白名单内、不能是受限命名空间、不允许 `all_namespaces`、不允许非默认 `config_path`。
- `list_secrets` 每项只返回 `name`、`type`、`keys`；`get_secret` 只返回 `name`、`namespace`、`type`、`keys`。
  **任何情况下都不返回 `data` / `stringData` 的值**，也不返回 labels / annotations（`last-applied-configuration` 注解可能含明文）。
  不返回 labels / annotations 比原要求（只要求不返回 data）更严，经 Tech Lead 确认保留，原因是 `kubectl apply` 写入的 `last-applied-configuration` 注解里可能包含 Secret 的明文。
- 工具自身也拒绝 `all_namespaces=true`，不再调用 `list_secret_for_all_namespaces`。
- `k8s_resource_operations` 工具侧第二层（不依赖门禁，直接调用工具也生效）：`describe_resource`、`get_resource_yaml`、
  `_resource_to_dict` 遇到 Secret（`V1Secret` 对象、`kind` 为 Secret，或请求的 `resource_type` 归一化后是 secret/secrets）
  只返回 `name`、`namespace`、`type`、`keys`，丢弃 `data`、`stringData`、`annotations`（含 `last-applied-configuration`）、
  `labels`；对 Secret 执行 `add_labels` / `remove_labels` / `add_annotations` / `remove_annotations` 时，响应里的
  新旧标签、被删除的注解、`remove_labels` 的 `remaining_labels` 等也只返回键名。`added_labels` / `added_annotations` 会原样回显
  调用方自己在本次请求里传入的值（不含集群里已有的值）。
- `_resource_to_dict` 的 `resource_type` 为必填参数（SEC-33 P3），调用方必须传入请求的类型；没有 `kind`、但同时有 `data` 和
  `type` 字段的 dict（Secret 的形状）也按 Secret 处理。
- 失败响应不回显异常文本（ApiException 的 reason/body 可能带出 Secret/ConfigMap 内容）：`k8s_config_management` 所有
  ConfigMap/Secret 操作和 `k8s_resource_operations` 的 describe/labels/annotations/get_resource_yaml 失败时，`message` 与
  `error_type` 只是异常类型名，另带 `api_status`（ApiException 的 HTTP status，其它异常为 `null`）和按 status 给出的固定中文
  `reason`，与 `apply_yaml` 的 `errors` 项写法一致。

## config_path

写类工具和 Secret 读取带非默认 `config_path` 一律 `MCP_CONFIG_PATH_DENIED`。其中**对 Secret 读取也做 config_path 检查是比原要求更严的设计，经 Tech Lead 确认保留**：切换 kubeconfig 会绕过当前集群的白名单语义，而 Secret 键名本身也属敏感元数据。只读工具暂时保持原行为。
是否允许按白名单切换 kubeconfig/集群、以及只读工具的 config_path 收敛，**后续在 SEC-39 处理**。

## MCP_FILE_ROOT 启动检查

MCP 服务启动（lifespan）时检查 `MCP_FILE_ROOT`（默认 `data/knowledge_base`，按 realpath 解析符号链接）。检查时解析出的
realpath 会被**固定**下来，之后文件工具一直使用这个值，root 本身或路径上的符号链接在检查后被替换也不会生效（TOCTOU，SEC-33 P3）；
未经启动检查直接使用工具时，首次使用时解析并固定。相对路径的 `MCP_FILE_ROOT` 按固定时的工作目录解析。测试用
`file_boundary.reset_file_root_cache()` 清除。

- 是 `/`，或与以下位置相同、位于其下、或包含它们：`/etc`、`/root`、`/proc`、`/sys`、`/dev`、`/boot`、`/run/secrets`、
  `/var/run/secrets`、`/var/lib/kubelet`、`~/.kube`、`~/.ssh`、`~/.aws`、`~/.azure`、`~/.config`、`~/.docker`、`~/.gnupg`、
  项目 `config/` 目录、项目 `.env`，以及 `$KUBECONFIG` 里的每个文件（按 `:` 分隔，可以有多项；报错只写 `$KUBECONFIG[序号]`，
  不回显路径）。
- 其他用户的 home 目录（如 `/home/<其他用户>/.kube`）不在自动检查范围内，部署时不要把 `MCP_FILE_ROOT` 指向或包含其他用户的 home。
- 生产环境（`app/config/envmode.py::is_production`：production/prod/prd、空值和未知值）→ 抛 `UnsafeFileRootError` 拒绝启动；
  非生产（test/testing、development/dev）→ 只记一条告警。

文件工具（`read_file`、`list_directory`、`get_file_stats`）的报错和结果里的路径只显示相对 `MCP_FILE_ROOT` 的路径；其中**成功结果也只返回相对路径**是比原要求（只要求报错）更严的设计，经 Tech Lead 确认保留，避免通过成功结果反推服务器目录结构；
越界或无法解析时回显用户传入的原值；底层异常只保留类型名，**不暴露 resolve 后的绝对路径**。

`list_directory` 对符号链接的处理（SEC-33 P3）：目标仍在 `MCP_FILE_ROOT` 内的链接照常跟随，按目标的类型、大小、修改时间返回；
指向 root 外或悬空的链接只做 `lstat`，`type` 记为 `symlink`，`size_bytes` 记 `0`（lstat 的 size 是目标路径长度，也不返回），
`modified_time` 是链接自身的时间，不泄露目标的 size / mtime。`get_file_stats` 统计目录总大小时同样不计入这类链接（文件数照旧计入）。
悬空链接不再导致整个目录列举失败。

## 升级影响

- 升级后默认所有 MCP 写操作和 Secret 读取被拒绝。依赖 MCP 写工具的调用方会收到 HTTP 200 + `status="error"` + `error_code`。
- 普通助手（mode=2 / WebSocket）本来就只走只读 allowlist，不受影响。
- `list_secrets` / `get_secret` 的返回结构变了（只剩键名），调用方不能再依赖 `data`。
- `describe_resource` / `get_resource_yaml`：`resource_type` 为 Secret 或无法识别（含拼写错误、未列出的资源类型）时默认被拒绝；
  开启 Secret 读后对 Secret 也只返回键名摘要。工具本身只支持 `pod`、`service`、`deployment`、`replicaset`、`configmap`、
  `ingress`、`namespace`、`node`、`pv`、`pvc` 这些原名；门禁额外认识复数和简称等别名，只是为了在无法判定时拒绝请求（fail-closed），
  用别名调用会通过门禁，但工具仍不支持。
- 生产环境 `MCP_FILE_ROOT` 配置不安全时 MCP 服务将无法启动。
- 同一发布中 `/autofix/diagnose`（SEC-26 P3-e）：diagnose 未知异常返回 500 `AUTOFIX_INTERNAL_ERROR`；上游超时与 Kubernetes 不可用返回 503 `SERVICE_UNAVAILABLE`（不再是 502）。详见 [autofix_guide.md「diagnose 错误映射」](../autofix_guide.md#diagnose-错误映射)。

## 回滚

1. 运行时回滚（不改代码）：设置 `MCP_WRITE_TOOLS_ENABLED=true`（另需 `autofix.repair_enabled=true` 和白名单）恢复写；
   `MCP_SECRET_READ_ENABLED=true` 恢复 Secret 键名读取。集群级写、config_path、Secret 值返回无法通过开关恢复。
2. 代码回滚：`git revert` SEC-33 提交（门禁、Secret 输出收敛、MCP_FILE_ROOT 启动检查一起回退）。
