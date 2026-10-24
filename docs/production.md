# AIops 1.0 部署边界

API 与 MCP 是 Go 后端调用的内部服务，不直接向浏览器或公网开放。生产环境设置 `ENV=production`，通过密钥管理系统注入至少 32 字符的 `AIOPS_API_KEY`；Go 后端、API 与 MCP 必须一致。请求使用 `X-API-Key` 或 `Authorization: Bearer`。密钥缺失或过短会阻止启动。`GET /health` 只返回进程存活状态，不代表 Redis、Kubernetes 或模型可用；各业务 `/ready` 需要鉴权。

复制 `env.example` 为 `.env`，填写独立随机 `REDIS_PASSWORD`、服务密钥和模型/监控配置。`openssl rand -hex 32` 可以生成服务密钥。Compose 默认端口只绑定本机；远程访问通过带 TLS 和访问控制的 Go 网关。`CORS_ORIGINS` 是显式逗号分隔域名，不允许通配符。外部模型和 Kubernetes 的真实权限由部署者配置；不要使用集群管理员身份，按实际工具授予最小 RBAC。

Kubeconfig 放在本地 `deploy/kubernetes/config`，该目录通过只读挂载传入服务；镜像不包含集群凭据。运行容器的非 root 用户需要读取配置、写入 `data` 和 `logs` 的权限。不要把 kubeconfig、`.env` 或模型供应商密钥提交到 Git。

```sh
# 校验不会启动依赖或调用模型
docker compose config --quiet
# 构建并启动（需要正确配置外部依赖）
docker compose up -d --build
# 存活检查
curl --fail http://127.0.0.1:8080/health
# 业务就绪检查（不要在共享终端打开 shell trace）
curl --fail -H "X-API-Key: $AIOPS_API_KEY" http://127.0.0.1:8080/api/v1/predict/ready
```

历史版本曾跟踪 Redis 密码与包含客户端私钥的 kubeconfig。删除当前版本中的凭据不能撤销历史泄露；投入生产前必须撤销并重新签发对应集群客户端凭据、轮换 Redis 密码。不要复用历史值。

## 发布验证

离线门禁统一为 `python -m pytest -q`、`python -m pip check` 和 Compose 配置校验，默认包含离线单元与ASGI集成。历史手工脚本已移到 `scripts/manual_checks/`，真实服务验收必须显式开启，详见[测试说明](testing.md)。上线验收还需在隔离预生产集群验证 Redis、Prometheus、LLM、嵌入模型和 MCP 的真实连通性与业务流程；没有这些环境的验证结果不能宣称生产验收完成。

依据：[FastAPI 鉴权与定时安全比较](https://fastapi.tiangolo.com/advanced/security/http-basic-auth/)、[Docker 构建最佳实践](https://docs.docker.com/build/building/best-practices/)。

服务默认按需初始化业务依赖，启动与存活检查不会调用模型。仅在依赖配置完成且需要启动预热时设置 `AIOPS_PREWARM=true`。Prometheus 地址使用 `PROMETHEUS_HOST=prometheus:9090`，不带协议。RAG 使用普通 Redis 存储文档、缓存与元数据，使用本地 FAISS 做向量检索；无需 Redis Stack。WebSocket 与 HTTP 使用同样的服务密钥，禁止将内部密钥下发浏览器。

MCP 文件工具仅能访问 `MCP_FILE_ROOT`（默认 `data/knowledge_base`），相对路径以此为基准，拒绝越界路径及符号链接越界，单文件读取上限 1 MiB。不要将此目录配置为应用根目录或凭据目录。SEC-33 起 MCP 启动时检查该目录：生产环境为 `/` 或包含/位于 /etc、~/.kube、凭据或项目配置目录时拒绝启动，非生产只告警；文件工具报错只显示相对路径。MCP 写工具默认关闭（`MCP_WRITE_TOOLS_ENABLED`）、Secret 读取默认关闭（`MCP_SECRET_READ_ENABLED`），详见 `docs/release/SEC-33-mcp-write-gate.md`。计算器使用受限算术 AST，拒绝 Python 属性访问、代码执行和超大指数。

预测要求真实历史指标。Prometheus 请求失败或返回空集时返回明确错误，不生成模拟历史数据；缺模型时的规则外推仍是启发式估计，其置信度不是经过校准的统计保证，不应直接据此自动扩缩容。生产容量策略需要独立的负载回放与模型评估。

依赖升级、安全边界及复现命令见 [安全验证记录](security-review.md)。

## 慢速指标源与 API 可用性

Prometheus 的异步查询、连接测试与健康检查将阻塞 HTTP I/O 放入工作线程，保留 `PROMETHEUS_TIMEOUT` 超时；预测历史数据读取使用异步健康检查。指标源缓慢或不可达时，事件循环仍能处理存活检查和其他请求。该隔离不代表系统具有无限并发能力，部署时仍需限制并发请求并配置合理超时；取消协程不会中止已经开始的同步 HTTP 请求。

回归命令：`python -m pytest tests/unit/test_prometheus_concurrency.py -q`。测试以离线模拟的慢指标源验证 HTTP `/health` 在指标请求尚未完成时仍返回，不访问实际监控系统。

## 通知投递与真实依赖验收

`POST /api/v1/autofix/notify` 仅在飞书 HTTP 状态为 200 且响应 `code=0` 时返回成功；禁用、未配置、网络错误、供应商拒绝或解析失败均返回 HTTP 503。通知请求在线程执行，10 秒网络超时仍有效，不阻塞事件循环。供应商确认接收不等于最终用户已阅读；网络超时后是否已接收无法保证，人工重试可能导致重复消息。

真实业务验收至少需要以下配置，离线测试不替代这些验收：

- 内部访问：一致且不少于 32 字符的 `AIOPS_API_KEY`，隔离的验收网络
- 模型推理：可访问的 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_TASK_MODEL` 与供应商密钥 `LLM_API_KEY`；使用本地 Ollama 时需配置 `LLM_PROVIDER=ollama`、`OLLAMA_BASE_URL` 和已经下载的 `OLLAMA_MODEL`
- 知识库：所选模型服务可用的嵌入模型、`RAG_OPENAI_EMBEDDING_MODEL` 或 `RAG_OLLAMA_EMBEDDING_MODEL`，可写的 `RAG_VECTOR_DB_PATH` 与 `RAG_KNOWLEDGE_BASE_PATH`，以及对应 `REDIS_HOST`、`REDIS_PORT`、`REDIS_DB`、`REDIS_PASSWORD`
- 指标和集群：可达的 `PROMETHEUS_HOST` 与真实指标数据、只读测试或经授权的验收集群 kubeconfig；执行变更另需最小 RBAC 权限与明确的验收目标
- 通知：`NOTIFICATION_ENABLED=true`、独立测试群机器人的 `FEISHU_WEBHOOK`，以及明确允许向该群发送验收消息的授权；不向真实生产群进行自动试投递

本 AIOps 仓库没有 SSH 会话或 SSH 远程命令执行实现，其集群操作通过 Kubernetes API。SSH 联调所需的主机、端口、账号、密钥及主机指纹应在实际提供该能力的 Go 后端单独配置与验收。

## 上传与关闭边界

知识库仅接受非空 UTF-8 `.md` / `.txt`，Word/PDF 不支持。单文件及添加文档后的完整文本限制为 10 MiB，服务层最多读取限制加一个字节，超限直接拒绝，不加载完整上传文件；可信身份、文件名（含路径/控制字符/符号链接）、扩展名、编码、大小和内容均在路由触发模型初始化前校验，参数错误维持 HTTP 400 错误信封。持服务密钥无用户头的内部主体仍按隔离内部调用契约工作。Multipart 解析由框架在路由前执行，此限制不替代反向代理的请求体、临时文件配额及并发上传限制。

服务工厂持有后台初始化任务，关闭时停止接收新的工厂服务获取，取消并等待初始化任务结束后再清理实例，避免资源在清理后重新创建。后台初始化异常会被取回并记录。应用生命周期通过 `finally` 执行关闭逻辑，覆盖运行上下文异常和预热失败。任务取消依赖协程配合；已经进入同步工作线程的网络 I/O 仍由其自身超时结束。

## 指标观测与预测数据契约

范围查询原样保留实际采样时间与序列标签。`step` 传递给 Prometheus，不再固定重采样到一分钟，也不前向填充缺口。内部 DataFrame 将非法数值、NaN 与正负无穷标为缺失，真实 `0` 仍是有效观测。预测与 RCA 输出边界剔除无效样本，JSON 不输出 NaN/Infinity，不将缺失伪装为零。RCA 同时支持 DatetimeIndex 和历史的显式 timestamp 列。

当前预测特征按小时位置读取历史，因此预测输入至少需要两个连续小时的有限、单一序列观测；两个样本仅是结构门禁，不代表统计可靠性。缺少样本、小时缺口或多个序列返回 HTTP **422**，标准 `{code, message, data}` 响应保留可展示原因，不执行预测。自定义 PromQL 应按业务选择 `sum`、`avg` 等显式聚合，或者限定为单个目标；系统不替用户平均不同主机或挂载点。多节点环境中的内存、磁盘默认查询可能返回多序列，需明确提供符合业务含义的 `metric_query`。

PromQL 被上游以 400/422 拒绝时也归类为查询错误 422；真正的连接、超时、上游 HTTP 或格式错误仍按既有依赖失败契约返回 502。预测缓存键包含观测数据契约版本，避免升级后复用旧插值结果。可用性与数据完整性通过离线替身验收，预测准确率仍需实际指标回放和独立评估。

## 独立 MCP 生命周期与超时

独立 MCP 应用在正常退出、运行上下文错误和工具注册失败时均执行 shutdown，清空服务器引用。每次生命周期构造独立工具实例，避免复用已关闭的客户端及线程池。SSE 生成器关闭时仅回收连接，不在 finally 继续发送事件；服务器关闭信号会唤醒等待心跳的连接。

工具执行受 `MCP_TIMEOUT`（默认 30 秒）控制，超时等待协程取消完成后返回。为兼容现有 MCP 客户端，工具失败仍使用 HTTP 200 的 `ToolResponse`，其中 `status=error` 且 `error` 非空；超时不会伪装成 `result=null` 的成功响应。关闭时先取消并等待活跃工具协程，再释放工具资源。Kubernetes 工具明确停止排队任务并关闭客户端；系统信息工具的 CPU 采样在线程执行，避免阻塞事件循环。

协程取消不等于撤销远程操作，已经进入同步 SDK 工作线程的请求可能继续执行。超时后应先确认集群实际状态，再决定是否重试变更；此处未承诺强制终止线程、自动回滚或幂等保证。

## MCP 模型工具调用验收

MCP 助手使用 Chat Completions 的原生 `tools` / `tool_calls` 协议：将注册工具的 JSON Schema 交给模型，校验工具名与参数对象后调用 MCP，将带 `tool_call_id` 的真实结果交回模型生成最终回答。工具选择来自模型，不使用时间或计算关键词执行兜底。每个问题最多四轮模型请求，关闭 SDK 隐式重试；工具结果 `0`、`false` 不会被误认为调用失败。协议依据见 [官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。

模型、工具或协议失败返回真实错误，取消“兜底提示也算成功”的行为。模式 2 不初始化 RAG、嵌入模型和知识库；助手工厂按需创建，模式 1 仍正常初始化 RAG。服务关闭时回收模型 HTTP 连接池与会话历史。

2026-10-23 在独立回环 MCP 服务中使用 SiliconFlow 的 `Qwen/Qwen3-30B-A3B-Instruct-2507` 完成两次真实验收：模型选择 `calculate` 计算 `2+3*4` 与 `1-1`，实际结果分别为 14 与 0，再由模型回答完全相同结果。共 4 次模型请求；执行守卫只允许计算器，无集群和通知操作。此证据验证最新本地调用链，完整容器/JWT代理仍由发布验收单独执行。

## 知识写入、检索分数与缓存可见性

FAISS HNSW 使用归一化向量的内积度量，使返回分数和余弦相似度一致（越大越相关）。修复前默认 L2 距离被当成相似度，可能把最相关的新文档记为低分并过滤。索引写入完成后同步失效检索、重排、答案与向量查询缓存，避免新增文档后仍返回旧答案；聚类累计统计不再被最后一批覆盖，清库会实际移除聚类中的旧文档。

`add-document` 保留顶层 `title` / `file_name`，同时兼容既有 `{content, metadata}` 输入。上传/添加仅在索引返回非空文档 ID、查询缓存失效完成后报告成功。空写入、部分写入、索引失败或缓存失败返回错误；源文件可能已保存，便于恢复，不应将磁盘落地等同于可检索。排障后可刷新知识库；刷新部分失败同样返回错误，不再报告操作成功。RAG 模型生成失败也返回真实 HTTP 502，不提供成功兜底文本。

2026-10-23 使用独立临时 Redis、真实 FAISS HNSW、`BAAI/bge-m3` 与 `Qwen/Qwen3-30B-A3B-Instruct-2507`，在同一问题的旧检索结果已缓存时新增唯一标识文档，经实际 ASGI HTTP 查询取得文档中的测试恢复码 `ORBIT-7426`，首条来源包含新文档原文。共 5 次嵌入和 1 次生成请求，临时容器与源文件已清理。此为隔离的完整 Python HTTP 调用链证据，发布环境仍需重建最新镜像后经 JWT 网关复验。

## MCP 调用日志

MCP 服务端和客户端日志只保留工具名称、参数字段数、耗时与异常类型，不记录参数值、工具输出、带凭据的服务地址或异常正文。工具协议错误也不直接回显第三方异常正文；成功结果仍正常交付给已鉴权的调用方。此规则覆盖工具HTTP入口、执行核心、SSE与MCP服务生命周期，不改变工具调用成功结果。

RAG 缓存带 `cosine-ip-owner-v2` 契约版本。答案缓存键同时包含知识集合与版本；向量查询缓存前缀为 `<collection>:query_cache:cosine-ip-owner-v2:`。升级后不会命中旧 L2 检索或旧答案缓存，也不会为了迁移扫描删除旧全局 `query_cache:*` 或其他集合键；旧缓存按原 TTL 自然到期。新缓存的写入失效和清理仅针对当前集合的命名空间。

## 可信用户身份与会话归属

网关必须先验证 JWT 与当前用户状态，从已验证的 `UserClaims.Uid` 覆盖注入 `X-CloudOps-User-ID`，值为规范十进制正整数；先删除客户端同名头，不得从请求体、查询或其他用户声明复制身份。Go 配套实现与 Python 必须一起发布。Python 只有服务密钥验证成功后才信任此头；非法或重复身份头返回 401，空服务密钥的开发态也不能靠自报身份进入会话 API。

持服务密钥但无用户头的既有内部调用使用独立 `service:internal` 主体。它能创建自己的服务会话，不能访问任何 `user:<id>` 会话；浏览器通过 JWT 网关始终带已验证的用户身份。`user_id` 请求参数不是身份来源，旧 `create_or_get_session(user_id)` 内部入口也必须与可信主体相符。

会话记录明确绑定 owner，不用 UUID 或推导格式替代授权。查询、会话信息与 WebSocket 消息均在读取历史或调用模型前验证归属：其他 owner 返回 403，未知、旧无 owner 或已过期会话返回 404，缺可信身份返回 401。`POST /assistant/session` 不调用模型；未带 SID 的 query 自动创建当前主体的会话。stream 只协商公开的 `cloudops-v1`，不回显认证协议；归属错误发送 `success=false`、`data.code` 后以 1008 关闭，无子协议的内部连接保持兼容。

答案、检索、重排及向量查询缓存带可信 principal、SID 与当前会话轮次作用域，答案键另包含历史摘要输入。MCP 历史与 LangGraph checkpoint 也按 principal/SID 分离。同一 SID 的生成串行处理，最多容纳 4 个正在执行或等待的请求，取消会释放锁与等待计数。

当前实现是**单进程、单副本的易失会话**：Docker 入口 `python -m app.main` 与默认 Compose 使用此模式。全局最多 1000 个活跃会话，每主体最多 50 个，闲置 3600 秒到期；容量不足返回 429，不驱逐其他用户活跃会话。创建会话时及访问时清理闲置记录，执行中/排队中的会话不淘汰，到期同步删除 RAG 上下文、MemorySaver 线程和 MCP 历史。WebSocket 接收队列最多 32 条。

RAG 冷启动也先校验会话归属，拒绝请求不会因为已有知识文档而触发初始化、嵌入或生成。合法 owner 的首次查询在服务层完成一次按需初始化；MCP 查询继续独立于 RAG 初始化。

重启、部署或服务清理后，旧 SID 失效并返回 404，客户端应创建新对话；不自动认领旧无 owner 数据。不要将当前内存方案配置为多 worker 或跨副本随机负载均衡，也不要将其描述为持久化或高可用会话存储。多副本部署需要共享 owner/历史存储和相应一致性设计后独立验收。知识库目前是应用级共享资料，本模块不引入私人知识库或文档租户 ACL。

## 自动修复审批归属与重试边界

**SEC-26 执行边界**：工作流默认只出计划、不自动执行任何动作；执行必须调用 `/autofix/workflow/confirm`，且目标命名空间必须在 `autofix.allowed_namespaces`（默认空，fail-closed）中。`/autofix/repair` 与 `/autofix/bootstrap` 默认关闭并返回 403；bootstrap 开启后 `test` 仍须在 `autofix.allowed_namespaces` 内，否则返回 403 `AUTOFIX_NAMESPACE_NOT_ALLOWED`。生产环境如需开放，需显式配置 `AUTOFIX_ALLOWED_NAMESPACES`、`AUTOFIX_REPAIR_ENABLED`、`AUTOFIX_BOOTSTRAP_ENABLED`，`AUTOFIX_AUTO_EXECUTE_ENABLED` 应保持 `false`。布尔开关只有 `true`（大小写不敏感）才开启，`1`/`yes`/空字符串等其它值一律视为关闭并在启动时打 WARNING；`allowed_namespaces` 大小写敏感、按字面匹配，`*` 不是通配符（`*`/含大写的条目启动时打 WARNING）。审批状态只存在进程内存中，必须单 worker 单副本或配置会话粘性（sticky session），审批持久化为独立工单。完整配置项、默认值与前端契约见[自动修复指南](autofix_guide.md#执行边界与前端契约sec-26)。

`/autofix/workflow` 与 `/autofix/workflow/confirm` 使用相同的服务密钥和可信 `X-CloudOps-User-ID` 链路；没有用户头的持密钥内部调用使用隔离的 `service:internal`。工作流与旧修复、诊断、测试资源入口在依赖调用前要求可信主体，空密钥开发态不能自报用户身份绕过。请求体中的 `owner` / `user_id` 不决定审批归属。

1.0 审批契约是**仅计划所属主体可审批**，不提供工作区全局代批。服务先预留容量再运行工作流，用随机 ID 定位显式 owner 记录，取消目标名称加秒级时间的碰撞覆盖。未知、过期、重启后或旧无 owner 的计划返回 404；已知其他主体计划返回 403，拒绝时不初始化模型或读取集群。没有公开的按 ID 查询计划结果接口，确认入口不会向其他主体回显计划。

确认请求先验证所选动作属于原计划且未重复/未消费，在任何依赖和执行之前原子领取；同进程的并发确认与已完成、失败或中断后的重放返回 409；计划目标或动作在重建时发生变化也返回 409。执行前重建风险检查，但只执行原计划中明确选择的动作，绑定原 namespace、Deployment、action type、target、parameters、可执行性及回滚属性；不会执行重建时新出现的动作。资源存在性、命名空间和权限检查必须明确通过，缺少权限探针也不能自动放行，人工审批不能覆盖硬限制。资源、权限或命名空间重新校验未通过（含计划生成后白名单被收紧），以及执行阶段命名空间硬校验拒绝且未执行任何动作时，返回 403 `WORKFLOW_APPROVAL_ERROR`，可修正配置后重新诊断。

流程进入人工审批后，原计划中尚未执行的可执行动作都需要逐项明确选择。部分选择全部成功时仅保留原计划剩余动作，允许所属主体继续明确审批；已执行动作不能再次选择。执行记录必须完整对应所选动作并逐项成功，接口不会把缺失记录或失败结果报告为成功。响应包含 `approval.expires_at`、`approval.state` 和 `approval.retry_safe: false`，后者表示不得自动重发执行请求。

异常、超时、取消及不完整（部分）执行会终结该次审批并返回 409。HTTP 409 会明确提示可能已经产生副作用、禁止盲目重试；应先人工核对集群实际状态，再决定后续诊断。HTTP 超时或断连不代表没有执行，重启后的 404 也不能作为未执行的证据。即使重新生成计划也不替代这一步核对。

记录最多 1000 条、每主体 50 条，包含预留、待审批和有界终结记录；单条有效 JSON 上下文最多 1 MiB，拒绝 NaN/Infinity 等非有限值，不把它们转换为零或留给 HTTP 序列化时才失败。待审批与终结记录 15 分钟到期，执行中的记录不驱逐；成功处理部分动作后，剩余审批重新获得 15 分钟窗口。容量满返回 429，不在执行完外部动作后才发现无法保存审批。修复服务最多 64 个在途操作、每主体 4 个；此限额作用于服务执行入口。关闭时先拒绝新操作，取消并等待在途任务，再清理记录和依赖，已关闭实例不能重建客户端。

该状态与防重保证属于**单进程、单事件循环、单副本的易失内存契约**，不提供持久审批、多 worker、跨副本或外部系统的 exactly-once 执行。当前 Docker 默认入口符合单进程约定。升级到多副本必须引入共享事务式审批存储及资源一致性设计，不能直接扩大 worker 数。此阶段使用离线 ASGI、并发与替身执行器验证，没有调用真实模型、通知、MCP 或集群审批动作。

## 普通助手与高权限 MCP 边界

普通助手 HTTP mode=2 与 WebSocket stream 共用本地只读 allowlist。仅计算、统计、换算、UTC 时间、六类资源指标/配额与 HPA list/status 可调度；混合工具只公布只读 operation，并在每次执行前严格拒绝未知字段、类型不符、越界数值与 config_path。schema 不是执行授权，模型忽略 schema 时仍拒绝实际调用。目录新增工具默认不可用，必须审查真实输出后改本地策略。部署状态可能含 env.value、通用 YAML 可能含 Secret，不能按 get/list 名称泛开放。文件工具已有 MCP_FILE_ROOT 边界，普通助手仍不开放此入口，避免根目录误配置泄露凭据。

独立 MCP /tools/execute 保留内部高权限能力，只由可信服务经密钥访问。不得把服务密钥发给浏览器、公开 MCP 端口或将 Go 普通助手权限等同于独立 MCP 写权限。内部 namespace 也不能通过普通助手升级为写操作。变更使用单独授权的 AutoFix；普通诊断不会绕过审批 owner、目标指纹或资源/权限/命名空间硬限制。指标/HPA 查询只受部署 Kubernetes 身份的 RBAC 约束，不声称提供逐用户 Kubernetes 租户授权；不同集群租户应部署隔离服务身份。

## 内部高权限 YAML 应用

`k8s_resource_operations` 的 `apply_yaml` 使用 Kubernetes 37.0.1 SDK 原生 `server_side_apply`，发送 `application/apply-patch+yaml`。固定 `fieldManager=ai-cloudops-mcp`、`force=false`，不允许请求强夺字段所有权或自定义管理器；仅支持已存在命名空间中的可 PATCH 资源，包括能动态发现的 namespaced CR。Namespace、CRD等集群资源及 List不支持，未知/不能唯一发现的 apiVersion/kind明确失败。Kubernetes自身的认证、RBAC、准入及字段冲突检查不被绕过。[SSA官方说明](https://kubernetes.io/docs/reference/using-api/server-side-apply/)

输入为UTF-8 YAML，最多1MiB、20个文档、64层；拒绝重复对象键、锚点/别名、NaN/Infinity、重复资源和非JSON数据。每文档必须有明确apiVersion、kind、metadata.name；不支持generateName、status及managedFields/resourceVersion/uid等服务端metadata。显式 `namespace` 参数必须与所有文档一致；不传时使用文档namespace，文档也未指定则为default。命名空间不会自动创建。客户端应提供目标namespace，避免默认命名空间歧义。显式config_path必须为存在的文件，错误路径不会回退其他集群；资源工具实例绑定首次配置来源，后续请求传不同来源（包括显式/默认互换）会拒绝，应使用隔离部署或重启后明确配置。

先完整解析与发现全部资源，再对全部文档发送 `dryRun=All`、`fieldValidation=Strict`，任一失败则不持久化任何资源。全部预检通过才顺序实际apply，每次必须收到匹配名称/命名空间及UID/resourceVersion才计入applied_resources。多资源不是事务：写阶段失败立即停止，返回applied_resources、errors、not_attempted、phase、outcome_unknown和retry_safe=false。HTTP403/404/409/422等失败返回状态和安全原因，不回传Secret内容或SDK异常body。字段冲突保持原字段管理器和原值。

网络请求连接/读取期限5/15秒，PATCH禁隐式重试；SDK操作在线程中执行。超时/取消不能保证正在运行的线程或API请求未生效，应核对资源实际状态，禁止盲目重试或将404解释为从未执行。预检不能消除与其他写入者之间的竞态，也不提供跨资源原子回滚。独立 MCP HTTP 保持既有200协议外壳，明确失败的工具返回status=error及非空error，结构化部分结果仍保留在result。

本阶段只在显式本地kind配置、随机自有namespace中验证ConfigMap创建、重复apply更新与另一fieldManager冲突409，读取验证原值/所有权未被抢占并finally删除命名空间；没有生产或模型调用。真实业务资源上线仍由用户按其集群准入/RBAC独立验收。

## 生产环境开启 debug 拒绝启动

debug 模式下 5xx 响应会带出 `message` 与 `data.details`（可能包含内部信息），因此**生产环境开启 debug 拒绝启动**：API（`app/main.py`）与 MCP（`app/mcp/main.py`）在 lifespan 开始时调用 `app/config/envmode.py::ensure_debug_safe`，抛出 `DebugInProductionError`，进程不会开始服务。

- **环境取值**：`config.env`，即 `ENV` 环境变量 > YAML `app.env` > 默认 `development`（`ENV` 同时决定加载 `config/config.{ENV}.yaml`，不存在时回退 `config/config.yaml`）。
- **debug 取值**：`DEBUG` 环境变量 > YAML `app.debug`。
- **什么算生产**（与 Go 后端 `pkg/envmode` 一致）：先 strip 再 lower；`production`/`prod`/`prd` 为生产；`development`/`dev` 为开发；`test`/`testing` 为测试（复用开发配置，允许 debug）；**空值或无法识别的取值（如 `staging`）按生产处理**（fail-closed）。
- 与 Go 的唯一差异：`ENV` 与 `app.env` 都未设置时，aiops 沿用既有默认 `development`（这是本地默认启动加载 `config/config.yaml` 的前提）；Go 侧未设置按生产。Docker 镜像与 Compose 均显式设置 `ENV=production`，不受此差异影响。
- 注意：`ENV=prod`/`prd` 会被判为生产，但没有 `config/config.prod.yaml` 时会回退到 `config/config.yaml`（其中 `debug: true`），此时会拒绝启动；生产请使用 `ENV=production`（加载 `config.production.yaml`，`debug: false`），或同时设置 `DEBUG=false`。
- 双保险：即使 debug 被意外打开，错误处理器在生产环境下也不回显 5xx 的 `message`/`details`（`_debug_details()` = debug 且非生产）。

恢复启动：设置 `DEBUG=false`（或 `app.debug: false`）；若确实不是生产环境，把 `ENV` 设为 `development`/`dev` 或 `test`/`testing`。
