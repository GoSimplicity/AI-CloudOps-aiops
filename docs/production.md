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

离线门禁为 `python -m pytest tests/unit -q`、`python -m pip check` 和 Compose 配置校验。历史 `tests/` 顶层脚本包含真实服务调用，不作为离线门禁。上线验收还需在隔离预生产集群验证 Redis、Prometheus、LLM、嵌入模型和 MCP 的真实连通性与业务流程；没有这些环境的验证结果不能宣称生产验收完成。

依据：[FastAPI 鉴权与定时安全比较](https://fastapi.tiangolo.com/advanced/security/http-basic-auth/)、[Docker 构建最佳实践](https://docs.docker.com/build/building/best-practices/)。

服务默认按需初始化业务依赖，启动与存活检查不会调用模型。仅在依赖配置完成且需要启动预热时设置 `AIOPS_PREWARM=true`。Prometheus 地址使用 `PROMETHEUS_HOST=prometheus:9090`，不带协议。RAG 使用普通 Redis 存储文档、缓存与元数据，使用本地 FAISS 做向量检索；无需 Redis Stack。WebSocket 与 HTTP 使用同样的服务密钥，禁止将内部密钥下发浏览器。

MCP 文件工具仅能访问 `MCP_FILE_ROOT`（默认 `data/knowledge_base`），相对路径以此为基准，拒绝越界路径及符号链接越界，单文件读取上限 1 MiB。不要将此目录配置为应用根目录或凭据目录。计算器使用受限算术 AST，拒绝 Python 属性访问、代码执行和超大指数。

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

知识库单文件限制为 10 MiB，服务层最多读取限制加一个字节，超限直接拒绝，不加载完整上传文件；缺失或无效内容不会先初始化模型。Multipart 解析由框架在路由前执行，此限制不替代反向代理的请求体、临时文件配额及并发上传限制。

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

RAG 缓存带 `cosine-ip-v1` 契约版本。答案缓存键同时包含知识集合与版本；向量查询缓存前缀为 `<collection>:query_cache:cosine-ip-v1:`。升级后不会命中旧 L2 检索或旧答案缓存，也不会为了迁移扫描删除旧全局 `query_cache:*` 或其他集合键；旧缓存按原 TTL 自然到期。新缓存的写入失效和清理仅针对当前集合的命名空间。
