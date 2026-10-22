# 1.0 安全与依赖验证记录

验证日期：2026-10-22。执行环境为全新 Python 3.11 虚拟环境，不依赖开发机原有 site-packages。

## 依赖处理

原环境审计发现 36 个包的 385 条漏洞记录；其中包括本地 black、pip 等开发工具，因此不等同于发布风险数。原 requirements.txt 审计为 33 个包的 356 条记录；漏洞库可能以多个别名记录同一问题，数量不代表独立可利用攻击路径。

实际运行风险包括 aiohttp / Starlette 的 HTTP 请求处理、python-multipart 上传解析，以及 LangChain / LangGraph 的序列化与工具处理。依赖已升级到修复版本。原文件还冻结了没有代码调用的 unstructured、langdetect、nltk、pypdf、实验版 LangChain、Redis 集群客户端等；这些依赖已移除，减少镜像体积和不必要的解析器攻击面。

`requirements.in` 维护直接依赖，`requirements.txt` 为 Python 3.11 跨平台完整版本锁。迁移到 LangGraph 1.x、LangChain Core 1.x、OpenAI 2.x；Ollama embedding 使用官方 `langchain-ollama` 包。Kubernetes 客户端升级到 37.x，以解除旧版本对有漏洞 urllib3 的上限约束。数值模型依赖 numpy、pandas、scipy、scikit-learn、joblib、faiss 保持原版本，避免持久化模型 ABI 漂移。

最终锁审计：**没有发现已知漏洞**，原始结果见 [release-dependency-audit.json](release-dependency-audit.json)。这说明该日期漏洞数据库未报告这些版本的已知漏洞，不代表不存在未知漏洞。pytest / pytest-asyncio 仍随锁安装，用于相同环境的离线验证。

```sh
uv pip compile requirements.in --python-version 3.11 --universal --upgrade -o requirements.txt
uv venv .venv-release --python 3.11
uv pip sync --python .venv-release/bin/python requirements.txt
uv pip check --python .venv-release/bin/python
.venv-release/bin/python -m pytest tests/unit tests/test_predict_service_unit.py -q
uvx pip-audit -r requirements.txt --no-deps --disable-pip --format json
```

87 个离线测试通过。新旧 FastAPI 的路由内部表示不同，路由覆盖测试改用公开 OpenAPI 路径；生产请求契约不变。镜像强制从 wheel 安装，不再依赖 langdetect 的构建隔离环境。

## 请求与工具边界

HTTP 与 WebSocket 使用同一内部服务密钥。Go 网关注入服务密钥、移除终端用户 Authorization/Cookie；Python 不把服务密钥作为终端用户身份。浏览器不得得到内部密钥。前端 AIops API 模块的字面路径与注册路由静态对照没有发现缺失；这不等于真实模型业务验收。

自动修复或 RCA 依赖缺失时 `/ready` 返回 503；预测就绪检查同时验证模型组件与 Prometheus。`/health` 仅表示进程可服务。没有 Kubernetes 凭据时不得视作自动修复就绪。RAG 检查实际向量索引和模型依赖，MCP 助手检查工具服务及模型元数据接口；OpenAI 探针使用 models.list，不再发送计费的生成请求。仅支持生成而不提供模型列表的兼容供应商会被保守标记为不可用，需要部署前确认接口支持。

MCP 文件工具限制在专用根目录，拒绝符号链接越界；上传和新增知识文档只接受无目录成分的文件名，拒绝绝对路径、`..`、反斜杠和符号链接写入。计算器不再执行 Python eval，限制 AST 类型、长度及结果规模。静态检索 MCP 和自动修复代码未发现 subprocess shell、os.system 或动态 exec 调用；Kubernetes 写操作仍需要部署环境的最小权限 RBAC。

历史版本泄露过的集群客户端私钥与 Redis 密码必须撤销轮换，删除 Git 当前文件不能撤销历史泄露。

资料：[FastAPI release notes](https://fastapi.tiangolo.com/release-notes/)、[LangGraph v1 migration](https://docs.langchain.com/oss/python/migrate/langgraph-v1)、[Ollama embeddings](https://docs.langchain.com/oss/python/integrations/embeddings/ollama)、[pip-audit](https://pypi.org/project/pip-audit/)。

助手 `/info` 和 `/config` 不初始化模型或向量库；info 的 initialized 表示真实服务实例初始化状态。首次业务初始化使用异步且15秒有界的 embedding 探测，Redis构造不阻塞事件循环。模型缺失返回503，不使用虚构维度或散列嵌入冒充模型结果。

RCA 配置和缓存统计不初始化完整诊断服务。缓存未初始化时 available/healthy 为 false、统计为 null；已有缓存连接按实际 Redis 探测结果返回。RCA 与自动修复 info 的 components=null 表示未执行健康探测，不能解释为已连接；实际就绪状态应查询 /ready。
