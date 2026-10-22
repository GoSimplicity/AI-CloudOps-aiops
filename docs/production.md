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
