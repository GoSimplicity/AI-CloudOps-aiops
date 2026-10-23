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
| `mcp.kubeconfig_allowed_dir` | `MCP_KUBECONFIG_ALLOWED_DIR` | `""`（空） | SEC-46：K8s 只读工具 `config_path` 的白名单目录。空 = 禁用自定义 `config_path`，详见下文「config_path（SEC-46）」 |

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
| `MCP_INVALID_OPERATION` | 参数中带 `operation` 但不是字符串（数组、对象、数字、布尔、`null`），在分类前拒绝，`details.switch` 为 `mcp.operation`；任何开关都不能放行 |

检查顺序：只读工具放行 → operation 类型检查 → 只读 operation 放行 → Secret 读（开关 → config_path → 白名单）→ 集群级拒绝 → 写（config_path → 写开关 → repair_enabled → 白名单）。

审计/告警日志（logger `aiops.mcp.write_gate`）只记录 `tool`、`error_code`、`switch`，**不记录 operation、命名空间、资源名、
config_path、YAML 等任何参数值**；响应文案也不回显参数值。

## 分类表

未在下表"只读"中列出的工具或 operation（包括新工具、拼写变体、大小写或空白不同）**一律按写处理**；参数中带 `operation` 但不是字符串时在分类前按 `MCP_INVALID_OPERATION` 拒绝（SEC-33 L2 P3-3）。

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

> **SEC-46 起，下面这段「绑定首次配置来源」的行为已被「config_path（SEC-46）」一节取代**，保留原文仅供对照 L2 的修复背景。

K8s 工具客户端与配置来源绑定（SEC-33 L2 P1-1）：每个 K8s 工具实例在首次调用时用独立的 `kubernetes.client.Configuration`
加载 kubeconfig（显式 `config_path` → `~/.kube/config` → in-cluster），**不修改 kubernetes 全局默认配置**，并把客户端绑定到该来源
（显式路径按 realpath 比较，否则为 default）。之后任何请求的来源与绑定来源不一致（包括先只读带 `config_path`、再发默认来源的写，
或先默认、再带其它 `config_path`）都直接拒绝，`message` 为固定文案“K8s工具已绑定首次配置，不能在请求中切换集群”，不会复用缓存客户端。
指向同一文件的符号链接按 realpath 视为同一来源。显式 `config_path` 不是存在的文件时报错，**不再静默回退**到 `~/.kube/config` 或 in-cluster。
修复前，只读调用带 `config_path` 会让缓存客户端（以及全局默认配置）指向另一集群，随后不带 `config_path` 的写操作和 Secret 读
通过门禁后就落到那个集群上。

K8s 工具基类的通用异常（SEC-33 L2 P3-4）：`execute()` 捕获到的未知异常只返回 `{"error": "执行失败", "message": "K8s工具执行失败"}`，
加载 kubeconfig 失败只返回“无法加载Kubernetes配置”，不再回显 `str(e)`；ERROR 日志只记工具名和异常类型，原始异常只进 DEBUG。
各工具自己 try/except 里返回 `str(e)` 的分支不在本次范围内。

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

校验后替换（SEC-33 L2 P3-2）：文件工具在路径校验后不再按路径名打开，而是从固定 root 的目录 fd 出发逐级 `openat(O_NOFOLLOW)`
打开每个组件，再用 `fstat` 的 dev/ino 与校验时的 `lstat` 比对；`list_directory` 基于目录 fd 列举、`get_file_stats` 用 `os.fwalk`
基于 fd 遍历。校验之后文件或任一中间目录被换成符号链接（即使指向 root 外的真实文件）、或被换成另一个 inode，都返回
“路径在校验后发生变化（可能被替换为符号链接），已拒绝”，不读取、不返回目标内容或元数据。

## 升级影响

- 升级后默认所有 MCP 写操作和 Secret 读取被拒绝。依赖 MCP 写工具的调用方会收到 HTTP 200 + `status="error"` + `error_code`。
- 普通助手（mode=2 / WebSocket）本来就只走只读 allowlist，不受影响。
- `list_secrets` / `get_secret` 的返回结构变了（只剩键名），调用方不能再依赖 `data`。
- `describe_resource` / `get_resource_yaml`：`resource_type` 为 Secret 或无法识别（含拼写错误、未列出的资源类型）时默认被拒绝；
  开启 Secret 读后对 Secret 也只返回键名摘要。工具本身只支持 `pod`、`service`、`deployment`、`replicaset`、`configmap`、
  `ingress`、`namespace`、`node`、`pv`、`pvc` 这些原名；门禁额外认识复数和简称等别名，只是为了在无法判定时拒绝请求（fail-closed），
  用别名调用会通过门禁，但工具仍不支持。
- 生产环境 `MCP_FILE_ROOT` 配置不安全时 MCP 服务将无法启动。
- （SEC-46 修订，取代 L2 的「绑定首次配置来源」）K8s 工具不再绑定首次调用的来源：默认来源（`~/.kube/config` → in-cluster）的客户端
  单独保存，读写都用它；自定义 `config_path` 默认**整体禁用**（`mcp.kubeconfig_allowed_dir` 为空），任何非空 `config_path` 都返回
  “config_path 不可用…”而不会再像 L2 那样加载任意存在的文件。配置白名单目录后，只读工具可以按 realpath 使用白名单内的 kubeconfig，
  这些客户端按 realpath 缓存且**只读**；写工具（含 `apply_yaml`）和 Secret 读带任何非空 `config_path` 一律拒绝，不再出现
  “K8s工具已绑定首次配置，不能在请求中切换集群”这条文案。升级风险：
  - 之前依赖「只读工具传任意路径的 kubeconfig」（尤其是 `data/`、知识库目录下的文件，或带 `exec` / `auth-provider` 的 kubeconfig，
    如 EKS `aws eks get-token`、GKE `gke-gcloud-auth-plugin`、OIDC 插件）的调用方，升级后会全部失败，需要改用 token / 客户端证书型
    kubeconfig 并放进白名单目录。
  - 之前「先只读带 config_path、再默认写」会被拒绝，现在默认写会正常落到默认集群（这是预期行为，不再需要重启 MCP 服务）。
  - 自定义 kubeconfig 中证书、`tokenFile` 等相对路径按 MCP 进程的工作目录解析，而不是按 kubeconfig 所在目录（`load_kube_config_from_dict`
    没有 base path），建议在白名单内的 kubeconfig 中写绝对路径或内联 `*-data`。
  - 白名单内的 kubeconfig 修改后，已缓存的同一 realpath 客户端不会自动刷新，需要重启 MCP 服务（或换一个文件名）。
- 参数中 `operation` 为非字符串的调用改为返回 `MCP_INVALID_OPERATION`（之前按写拒绝，返回 `MCP_WRITE_DISABLED` 等；
  `k8s_resource_operations` 传数组/对象时之前会抛 `TypeError`）。
- K8s 工具未知异常的 `message` 统一为“K8s工具执行失败”，调用方不能再依赖异常原文。
- 文件工具对校验后被替换为符号链接的路径拒绝访问；root 内指向 root 内的符号链接照常可用。
- 日志：diagnose 链路和 `execute_with_timeout` 的 ERROR 日志不再带 traceback 和异常原文，排障需要临时打开 DEBUG。
  **生产环境不得长期开启 DEBUG**：DEBUG 日志包含原始异常文本与 traceback，可能带出集群地址、kubeconfig/凭据片段、Secret/ConfigMap
  内容（ApiException body）以及 `config_path` 被拒绝的内部原因；排障完成后立即恢复 INFO，并按敏感日志处理期间产生的日志。
  （另：生产环境开启 `debug` 开关本身会拒绝启动，见 `ensure_debug_safe`。）
- 同一发布中 `/autofix/diagnose`（SEC-26 P3-e）：diagnose 未知异常返回 500 `AUTOFIX_INTERNAL_ERROR`；上游超时与 Kubernetes 不可用返回 503 `SERVICE_UNAVAILABLE`（不再是 502）；Kubernetes 调用经 `execute_with_timeout` 包装，包装内出现的任何异常（含未知异常）都视为上游不可用，返回 503 而不是 500，与 autofix_guide 一致。diagnose 链路的 ERROR 日志只记操作名、异常类型和 status，原始异常文本只进 DEBUG 日志。详见 [autofix_guide.md「diagnose 错误映射」](../autofix_guide.md#diagnose-错误映射)。

## config_path（SEC-46）

修复前（P1）：通过知识库上传接口 `upload_knowledge_file` 上传一个扩展名为 `.txt`、内容为带 `exec` 插件的 kubeconfig（落在
`data/knowledge_base`，platform 与 mcp 经 `./data` 共享），再让任一只读 K8s 工具带 `config_path` 指向它，kubernetes 客户端在加载时
就会执行插件命令——即在 MCP 主机上执行任意命令。只读工具不受写门禁约束，默认配置下即可触发。

现在的规则（`app/mcp/server/tools/kubeconfig_policy.py`，所有接受 `config_path` 的工具共用）：

- 配置项 `mcp.kubeconfig_allowed_dir`（环境变量 `MCP_KUBECONFIG_ALLOWED_DIR` 优先），**默认空字符串 = 禁用自定义 `config_path`**，
  `config/config.yaml`、`config/config.production.yaml`、`config/config.test.yaml`、`env.example` 都显式写成空。
- 用法：设为一个**专用**目录的路径，例如 `MCP_KUBECONFIG_ALLOWED_DIR=/etc/aiops/kubeconfigs`，把只读用的 kubeconfig 放进去，
  请求里 `config_path` 写该目录内文件的绝对路径，或相对该目录的路径（如 `prod-readonly.yaml`）。
- 白名单目录按 realpath 解析；不存在、不是目录、是 `/`，或与 `data/`、知识库目录（`rag.knowledge_base_path`）重叠（位于其下**或包含它们**）
  时视为无效，等同禁用，并记一条告警。
- `config_path` 按 realpath 判断必须位于白名单目录内（不能是目录本身）且不在 `data/`、知识库目录下；指向白名单外的符号链接、经 `..` 解析后落到白名单外的路径都被拒绝。
- 文件用 `O_NOFOLLOW | O_NONBLOCK` 打开，必须是普通文件、硬链接数为 1、不超过 1 MiB，UTF-8 YAML。
- **加载前先解析**：`users[].user` 中含 `exec` 或 `auth-provider`（键名去空白、不区分大小写）一律拒绝；结构异常（顶层不是映射、
  `users` 不是列表等）同样拒绝。通过检查的同一份 dict 交给 `load_kube_config_from_dict(..., persist_config=False)`，不再按路径二次读取，
  也不回写文件。
- 来源分离：默认来源客户端单独保存；自定义来源按 realpath 缓存，**只读**。写工具（含 `apply_yaml`）和 Secret 读带任何非空
  `config_path` 在门禁处返回 `MCP_CONFIG_PATH_DENIED`；即使绕过门禁直接调用工具，工具基类也会在初始化客户端前拒绝。
  取代了 L2 的「绑定首次来源」。
- 所有拒绝原因（未启用、不在白名单、文件不合规、含插件、加载失败）对调用方统一返回
  “config_path 不可用（未启用自定义 kubeconfig、不在白名单目录内或不是允许的只读 kubeconfig），未执行任何操作”（P3-G），
  不区分原因，避免成为文件存在性/内容探测的 oracle；具体原因只记 WARNING 日志且不含路径与内容。

## MCP_INVALID_OPERATION 契约变化

SEC-33 L2 P3-3 起，参数中带 `operation` 但不是字符串（数组、对象、数字、布尔、`null`）时，在分类前就被拒绝：

- HTTP 接口仍返回 **HTTP 200**，`status="error"`、`result=null`、`error_code="MCP_INVALID_OPERATION"`、`details.switch="mcp.operation"`；
  进程内调用抛 `FeatureDisabledError`（`status_code=403`，`error_code` 相同）。
- 之前同样的请求会被当成写操作处理，返回 `MCP_WRITE_DISABLED` / `MCP_NAMESPACE_NOT_ALLOWED` 等，开关全开时甚至可能进入工具
  （`k8s_resource_operations` 会抛 `TypeError`）。按 `error_code` 做分支的调用方需要新增这一取值；它不是开关问题，**任何开关都不能放行**，
  应修正请求参数而不是重试或开开关。

## 文件工具加固（SEC-33 L3）

- P3-a：平台没有 `os.O_NOFOLLOW` 时，模块导入与 MCP 启动检查照常进行，只是 `read_file` / `list_directory` / `get_file_stats` 一律返回
  “当前平台不支持 O_NOFOLLOW，文件工具已禁用”。
- P3-b：`get_file_stats` 对非目录不再打开目标文件本身，而是经 `openat(O_NOFOLLOW)` 打开父目录后
  `os.stat(name, dir_fd=..., follow_symlinks=False)`，再比对校验时的 dev/ino；对 FIFO、socket、无读权限的文件也能返回元数据而不会触发 open。
- P3-B：普通文件要求 `st_nlink == 1`。`read_file` 与 `get_file_stats` 对多硬链接的普通文件返回“文件存在多个硬链接，已拒绝”；
  `list_directory` 与目录统计对这类文件的大小记 `0`（不计入总大小）。启动检查（或首次使用）时除了固定 root 的 realpath，还固定 root
  目录的 fd，之后逐级打开都从该 fd 出发；root 路径之后被换成另一个目录时，工具不会读到新目录里的内容（校验身份不一致 → 拒绝）。
  升级风险：知识库目录里若有硬链接文件（例如用 `cp -l` / `rsync --link-dest` 部署），升级后会无法读取，需要改为普通复制；
  运行中替换 `MCP_FILE_ROOT` 目录（而不是替换其中的文件）后需要重启 MCP 服务。

## 回滚

1. 运行时回滚（不改代码）：设置 `MCP_WRITE_TOOLS_ENABLED=true`（另需 `autofix.repair_enabled=true` 和白名单）恢复写；
   `MCP_SECRET_READ_ENABLED=true` 恢复 Secret 键名读取。集群级写、config_path、Secret 值返回无法通过开关恢复。
   SEC-46：只读工具的自定义 kubeconfig 可通过 `MCP_KUBECONFIG_ALLOWED_DIR` 指向专用目录恢复；含 `exec` / `auth-provider`
   的 kubeconfig、写工具与 Secret 读的 `config_path` 无法通过配置恢复（这是安全修复，不提供开关）。
2. 代码回滚：`git revert` SEC-33 提交（门禁、Secret 输出收敛、MCP_FILE_ROOT 启动检查一起回退）。
