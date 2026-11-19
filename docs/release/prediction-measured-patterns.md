# 预测模式与量化指标按实测序列计算

基线是 `2bed5f6ed7eb6ff182afd13b76613b97f974d8f7`。这一批只改预测结果里的模式判断、量化统计，以及名为 `isolation` 的异常检测。请求体、HTTP 状态、`/info`、`/ready`、`/health`、`/models` 都没有改。成本估算和 `PredictionService.get_service_health_info` 的健康字典也没有改。

## 模式分析

`pattern_analysis` 的键不变：`has_periodicity`、`volatility`、`seasonality.daily_pattern`、`seasonality.weekly_pattern`、`seasonality.monthly_pattern`。

以前只要有 24 个点就报告有周期，并且日季节性恒为 true。现在先去掉线性趋势，再按采样间隔看日、周、月是否重复。相关不足、样本盖不住两个周期、采样比周期粗、时间戳重复或间隔忽长忽短时，对应项为 false。波动率仍是预测值的标准差。真实 0 保留，不把缺测补成 0。

## 量化指标

`quantitative_metrics` 仍使用原来的键。

- `prediction_count`、均值、中位数和值域只统计有限的 `predicted_value`。缺键、null、NaN、无穷大不记成 0。一个有限值都没有时，均值和中位数是 null，不给趋势。
- 前半段均值为 0 时，`trend_analysis.direction` 仍按后半段判断升降，`change_percentage` 是 null。负基线按原公式给出百分比，不再写成 0。
- 置信度 0 是实测值，会进入 `confidence_statistics`，解读提示词里显示 `0.00`。缺失或非有限置信度仍然不统计。

调用方如果把 `change_percentage`、均值或中位数当成必定是数字，需要把 null 显示成未知。这不是新字段，也不是新的状态码。

## 孤立森林

配置和直接调用里的 `method=isolation` 使用 scikit-learn 的 `IsolationForest`（`random_state=0`）。常数序列或少于 8 个点不报异常，也不再转去 MAD。默认配置仍是 `zscore`，普通预测请求不传检测方法，因此默认异常列表的算法没有变。未知方法名仍按原来的 Z-score 路径。

## 缓存

模式分析和量化指标会写入预测缓存。`RedisCacheManager.get` 命中时用 `setex` 按默认 TTL 续期，所以旧结果不会自己到期后消失。缓存参数 `prediction_contract` 从 `provenance-v2` 改为 `provenance-v3-measured-patterns`。旧键不再命中，也不会被这次读取续期。

不用 `history-schema-v4`。那个版本名属于未合并的 #26，它也改同一处 `prediction_contract`。将来两个改动都要保留时，需要再升一个同时包含历史结构和本次实测模式的新版本，不能只留其中一个名字。

## 验证

离线回归在 `tests/unit/test_prediction_measured_patterns.py`。修复前该文件 11 项失败、2 项通过：失败点是固定日周期、零基线变化率被写成 0、缺测被填 0 或直接算失败、置信度 0 被丢掉，以及 isolation 调用了 MAD。当时通过的两项是按小时重复的日周期（旧实现本来就把日季节性写成 true）和常数序列的 isolation（MAD 对常数也不报异常）。修复后该文件全部通过。

没有连接真实 Prometheus、Redis、Kubernetes、LLM 或生产模型。线性模型和孤立森林都是本地库。这不能代替真实指标回放。
