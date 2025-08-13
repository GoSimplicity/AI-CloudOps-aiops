# AI-CloudOps-aiops

面向云原生的 AIOps 平台，内置预测、RCA、自动修复、多智能体与助手等能力。项目已优化至精简、健壮、可测试，默认 UTC、统一日志和错误处理。

## 快速开始

### 运行（本地）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080
```

或使用 Docker：

```bash
docker compose up -d --build
```

环境变量参考 `env.example`，配置文件见 `config/`（环境变量 > config.[env].yaml > config.yaml）。

## 健康检查

- GET `/api/v1/health` 总览
- GET `/api/v1/health/detailed` 全量明细（不健康时返回 503）
- GET `/api/v1/health/k8s`、`/api/v1/health/prometheus`（异常返回 503 JSON）
- GET `/api/v1/readiness/health`、`/api/v1/liveness/health`

## 预测

- POST `/api/v1/predict/create`
  - 简化：`{namespace, deployment, duration_minutes}` → 用 Prometheus 同步 `query_range` 估算平均 QPS，`ceil(avg_qps/30)` 给出 `predicted_replicas`
  - 扩展：`PredictionRequest`（`current_qps/use_prom/metric/selector/window/...`）→ 异步预测
  - 无法识别的请求返回 422
- POST `/api/v1/predict/trend/create` 趋势预测
- GET `/api/v1/predict/model/info/detail`、`/api/v1/predict/model/validate/detail`、`/api/v1/predict/health/detail`

## 根因分析（RCA）

- POST `/api/v1/rca/analyze`、`/api/v1/rca/correlate`、`/api/v1/rca/correlations`
- 内部相关性器别名：`Correlator` 指向 `CorrelationAnalyzer`

## 自动修复（Autofix）

- POST `/api/v1/autofix/diagnose`、`/api/v1/autofix/fix`、`/api/v1/autofix/workflow`
- POST `/api/v1/autofix/notify` 使用 `NotificationService.send_webhook(url, payload)`
- GET/DELETE `/api/v1/autofix/records/{id}`、GET `/api/v1/autofix/health`、GET `/api/v1/history`

## 助手与多智能体

- 助手：GET `/api/v1/assistant/health/detail`、POST `/api/v1/assistant/chat/create`、POST `/api/v1/assistant/search/list`
- 多智能体：GET `/api/v1/multi-agent/status/detail`、POST `/api/v1/multi-agent/execute`、GET `/api/v1/multi-agent/coordination/detail`

说明：`storage` 模块已移除，相关端点不再提供。

## 测试与质量

```bash
make lint      # Ruff 规则
make fmt       # 格式化
make test      # pytest 全量测试
```

- 日志：`stderr` 输出，UTC ISO8601；压低第三方库冗余日志
- 错误处理：统一包装，422 用于校验失败，健康异常返回 503

## 目录速览

- `app/api/` Web 路由
- `app/core/` 预测/RCA/多智能体
- `app/services/` Prometheus/K8s/LLM/通知/Tracing 适配
- `app/db/` SQLAlchemy 基建
- `docs/` 架构说明（详见 `docs/ARCHITECTURE.md`）

## 部署

- 生产建议使用 `config/config.production.yaml`（`debug=false`，`log_level=WARNING`）
- 运行后可用 `GET /api/v1/health` 验证状态
