# 测试入口与外部访问边界

默认命令与 CI 相同：

```sh
python -m pytest -q
```

默认运行 `tests/unit/` 与 `tests/integration/`，不读取本机 `.env`，不访问 Redis、Prometheus、Kubernetes 或模型供应商。离线单元测试原有场景完整保留，预测服务契约测试移到 `tests/unit/test_predict_service_contract.py`。真实依赖在测试中显式注入替身；不能通过捕获网络异常把连接失败变成“通过”。

| 层级 | 目录与标记 | 依赖与执行方式 |
| --- | --- | --- |
| 离线单元 | `tests/unit/`、`unit` | 真实业务代码与显式客户端替身，默认执行 |
| 隔离组件集成 | `tests/integration/`、`integration` | 完整 FastAPI 路由、鉴权、错误封装和 ASGI，业务依赖替换，默认执行 |
| 真实服务契约 | `tests/external/`、`external` | 明确配置的隔离部署，仅存活、鉴权、OpenAPI 和非法输入，不操作真实集群 |
| 真实模型验收 | `tests/external/test_model_tools.py`、`model` | 额外开关，可能计费；仅提供本地计算器，最多四轮模型请求 |
| 历史排障脚本 | `scripts/manual_checks/` | 历史场景保留，不是发布门禁；可能含过时契约、变更和通知动作，必须逐项审阅 |

离线守卫拦截 DNS、TCP 和 UDP。即使业务代码吞掉连接异常，测试 teardown 仍失败。回归测试通过隔离 pytest 子进程验证守卫、外部模块导入边界和缺配置失败，不依赖真实互联网。AF_UNIX 仅供本机事件循环与线程通信。

按层执行：

```sh
python -m pytest tests/unit -q
python -m pytest -m integration -q
```

## 真实服务验收

仅针对已授权的隔离部署，在当前进程中注入实际服务密钥及地址；禁止把密钥写进命令历史或测试源码。`AIOPS_EXTERNAL_BASE_URL` 是 AIOps Python API 地址，不是使用 JWT 的 Go 网关。开关缺失时外部模块在导入前排除；打开但未配置地址或密钥时立即报配置错误，不跳过测试或回退到默认生产地址。

```sh
# 先通过安全的进程环境注入 AIOPS_API_KEY
export AIOPS_EXTERNAL_BASE_URL=http://127.0.0.1:18081
AIOPS_RUN_EXTERNAL_TESTS=1 python -m pytest tests/external/test_service_contract.py -q
```

模型验收另需由安全环境注入 `LLM_API_KEY`，并显式配置 `LLM_BASE_URL`、`LLM_MODEL`。不使用占位值；`AIOPS_RUN_EXTERNAL_TESTS=1` 不会自动启用计费测试。

```sh
AIOPS_RUN_EXTERNAL_TESTS=1 AIOPS_RUN_MODEL_TESTS=1 python -m pytest tests/external/test_model_tools.py -q
```

真实服务不可达、鉴权失败、模型不选工具或返回错误答案均是真实失败。测试不会因服务下线而自动 skip，不会自动重启或修改生产环境。SSH、Kubernetes 变更、通知投递与知识库写入仍由单独授权的隔离发布场景验收，不属于这里的只读服务契约。

## 历史手工检查

原 `tests/` 根目录中返回布尔值的打印脚本、真实服务测试与旧全量调度脚本已移入 `scripts/manual_checks/`。它们保留有用诊断场景，但含历史的字段、根路由和通知等断言，不能据其打印的成功字样宣称 1.0 验收通过。旧 Flask fixture、未使用的占位 service fixture 和收集函数名特判已移除。

这些资料没有计入默认通过数，也没有通过 skip 隐藏单元测试失败。只有明确审阅隔离目标与具体动作后，才能设置 `AIOPS_RUN_LEGACY_CHECKS=1` 并选择某个脚本；默认执行会在业务模块导入前拒绝。不建议运行历史全量脚本，也不能把它连接到生产集群或真实通知群。

会话隔离用两个可信 principal 的真实 ASGI HTTP/WebSocket 交叉请求验证，包括已知其他用户 SID、旧无 owner SID、请求体伪报身份、非法/重复网关头、服务主体隔离、相同问题跨用户/历史缓存、并发 contextvars、取消锁释放、容量/TTL 与底层状态清理。服务层直接单测在 `tests/unit/conftest.py` 中显式绑定内部服务主体，鉴权和越权回归仍使用真实服务认证中间件覆盖该上下文。
