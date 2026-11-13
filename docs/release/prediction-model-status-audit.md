# 预测就绪与跳过项审计

基线是 `e9309a65007915ec750e753f1ddbf1f18bcbaa1f`。本文件只记录这次核对过的行为。离线替身不能代替真实 Prometheus、Redis、Kubernetes、LLM 或生产验收。

本批代码只改 `GET /api/v1/predict/models`。`/ready`、`/health`、`/info` 和预测响应的既有字段、HTTP 状态与成功文案没有改。

## 模型接口在修之前

探测用本地 ASGI，Redis 构造直接失败（不连 Redis），`PrometheusService.is_healthy` / `health_check` / `query_range` 是替身。模型是本地 joblib 对象或损坏字节。没有外部 LLM 请求，网络尝试次数为 0。增强组件在空密钥下初始化失败，服务退回基础预测。

| 场景 | `/ready` 与 `/health` | `/models`（修复前） | `/info` |
| --- | --- | --- | --- |
| 四个文件都缺失，Prometheus 替身健康 | HTTP 503，`code=503`，`error_code=SERVICE_UNAVAILABLE`，消息 `服务 prediction 当前不可用`。没有 `ready=true`，也没有模型路径 | HTTP 200，`code=0`，消息 `AI-CloudOps 模型信息成功`，`status=healthy`，`loaded_models=0`，`models_loaded=0`，`total_models=0` | 调用前 `status=unavailable`；调用 `/ready` 或 `/models` 之后变成 `available`。`prediction_models.rule_fallback=true`，四类 `status=missing` |
| 四个文件都损坏，Prometheus 替身健康 | 同上 503 | 同上，仍是 `status=healthy` | 四类 `status=load_failed`，`rule_fallback=true`。服务 `status` 仍会在单例创建后变成 `available` |
| 四个模型都加载，Prometheus 替身健康 | HTTP 200，`ready=true`，`status=ready`，`prediction_models` 四类 `loaded` | HTTP 200，`status=healthy`，但 `loaded_models=0`、`models_loaded=0`，同时 `total_models=4` | 冷启动仍是 `unavailable`，之后是 `available`。`rule_fallback=false` |
| 只有 QPS 文件，其余借用 QPS | HTTP 200，`ready=true`，`any_fallback=true`，cpu/memory/disk 为 `adapted` | HTTP 200，`status=healthy`，`loaded_models=0`，`total_models=4`，列表里看不出借用 | `rule_fallback=false`，`any_fallback=true` |
| 无模型，`enable_ai_insights=false`，历史指标是替身 | 预测 HTTP 200，`method=rule_based`，`fallback_mode=true`，`fallback_reason=预测模型不可用`，`model_accuracy=null`，置信度为 null | 仍报 `healthy` | 规则结果不是模型成功 |
| 无模型且请求 AI | 预测 HTTP 200，规则字段同上，另有 `ai_enhanced=false`，`ai_fallback_reason=AI增强组件不可用` | 仍报 `healthy` | 信封消息仍是“预测成功”。降级靠字段区分，不靠 HTTP 状态 |

`/models` 报错的原因是路由读取了不存在的 `loaded_models`，再用缺省 `status=healthy`。服务层实际给出的是布尔 `models_loaded`，没有 `status`。四个模型已经加载时，数量仍是 0。

## 这次修好的契约

`GET /api/v1/predict/models` 仍是 HTTP 200、`code=0`。它报告模型状态，不代替 `/ready`。

- `loaded_models` 与 `models_loaded` 都是已装入内存的类型数量，包含借用来的类型。
- `status=healthy`：四类都是专用模型，没有兜底。
- `status=degraded`：至少有一个类型在内存中，但存在缺失、加载失败或借用。
- `status=not_loaded`：一个都没装入，文件是缺失。
- `status=load_failed`：一个都没装入，且至少有一个文件加载失败。
- 列表里的每一项带 `status` 与 `fallback`，不带目录或文件路径。借用项是 `adapted` / `fallback=true`。

缺模型或加载失败时，`/ready` 与 `/health` 在 Prometheus 替身健康的情况下仍返回 503。这个行为原来就有，这次用回归锁住，没有改响应体。

## 有意不改、需要协调的契约

这些都会改变已有字段或状态语义，本批不动。

1. `/info` 的 `status` 只表示预测服务单例有没有被创建过。模型齐全的冷启动是 `unavailable`；模型全缺但有人调用过 `/ready` 之后变成 `available`。真实模型状态在 `prediction_models`。建议调用方只读 `prediction_models`。若要把 `status` 改成与模型一致，需要先和 Go 调用方约定，不能在这一批改。
2. `/ready` 与 `/health` 的 503 不区分缺模型、加载失败和 Prometheus 不可用，也不附带 `prediction_models`。文档要求此时看启动日志和 `/info`。建议以后增加不含路径的 `unavailable_reason`（`models_not_loaded`、`models_load_failed`、`metrics_unavailable`、`service_unhealthy`），先协调再加。
3. 规则预测和 AI 不可用仍返回 HTTP 200 与“成功”文案，用 `method`、`fallback_mode`、`fallback_reason`、`ai_enhanced`、`ai_fallback_reason` 区分。不要把规则外推当成模型成功，也不要在未约定前改成功文案或 HTTP 状态。
4. 只有 QPS 模型时 `/ready` 仍是 200，`any_fallback=true`。这与 `docs/release/prediction-model-source.md` 一致：503 只覆盖一个模型都没加载，或 Prometheus 不可用。`curl --fail` 只看 HTTP 状态，看不到借用。

## 跳过的 36 项

命令是 `python -m pytest -q -rs`。实际 36 项，不是文档里的旧数字。本次运行里 Markdown 处理器可以导入，`tests/unit/test_stage_admission.py` 的条件跳过没有触发。

| 数量 | 测试 | 跳过原因 | 需要什么 | 是否掩盖离线缺陷 |
| --- | --- | --- | --- | --- |
| 13 | `tests/integration/test_assistant_session_redis.py` 中使用 `owned_redis` 的用例 | `CLOUDOPS_TEST_ASSISTANT_REDIS!=true` | 显式开启的隔离 Redis 容器 | 否。同目录有离线守卫测试。默认跳过是为了不连真实 Redis |
| 22 | `tests/unit/test_generation_gc.py` 中使用 `isolated_redis` 的用例，含 1 个两项参数化 | `CLOUDOPS_TEST_GENERATION_GC!=true` | 显式开启的隔离 Redis 容器 | 否。同文件仍有不依赖容器的用例 |
| 1 | `tests/unit/test_generation_gc.py::test_admin_cli_inspects_and_resolves_with_os_identity` | 同一环境变量 | 隔离 Redis，并且映射本机端口 | 否。它检查管理命令，不是把失败改成跳过 |

验收：CI 的 Verify offline 在拉起 `redis:7.4.11-alpine` 后设置 `CLOUDOPS_TEST_GENERATION_GC=true` 与 `CLOUDOPS_TEST_ASSISTANT_REDIS=true` 再跑这些文件。没有该容器和开关时，不能把这 36 项算作已通过，也不能删掉跳过。

`tests/external/` 默认不收集。缺少开关时不会变成 skip 后通过；打开开关但没有地址或密钥会直接失败。

## 生产文档里核对过的其他缺口

已由现有代码和测试覆盖，这次没有再改：

- 进程 `GET /health` 不检查模型。没挂模型时仍是 200。见 `tests/unit/test_app_boot.py`。
- 缺模型或损坏模型时预测走规则，并带 `fallback_reason=预测模型不可用`。见 `tests/unit/test_prediction_model_source.py`。
- 无回测依据时准确率和置信度保持 null。见 `docs/release/prediction-provenance.md` 与对应测试。
- 生产环境打开 debug 会拒绝启动。见 `docs/production.md` 与 `app/config/envmode.py`。

仍缺，但不在本批修改：

- `app/core/prediction/feature_extractor.py` 在历史为空时用当前值的 0.95/0.9/0.85 填滞后特征。HTTP 预测会先要求至少两个连续小时的真实样本，所以这条路径不是当前 API 的成功路径。若其它调用者把空历史交给已加载模型，仍会把填出来的特征当成模型预测。适合下一批离线回归，不要和本批状态字段混在一起。
- `app/core/prediction/cost_analyzer.py` 在没有 `cost_per_hour` 时用内置单价，最低返回 0.1。只有请求带了资源约束才会进入。它不是实测云账单。
- `PredictionService.get_service_health_info` 把 `error_rate` 写成 0.0。这不是 `/ready` 的响应。
- 真实 Prometheus、Redis、Kubernetes、LLM、飞书和发布网关仍未在本环境验收。`docs/production.md` 列出的预生产验收继续有效。

本地基线曾出现 6 个失败，都在 `tests/unit/test_test_boundaries.py`。原因是 pytest 把子进程的 `HOME` 指到临时目录，而依赖装在用户目录，子进程找不到 pytest。把 `PYTHONUSERBASE` 固定到原用户目录后这 6 项通过。这不是产品缺陷，CI 把依赖装进解释器环境，不受影响。
