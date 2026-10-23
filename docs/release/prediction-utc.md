# 预测输出 UTC 时间契约

本阶段只统一实际预测点 `predicted_data[].timestamp` 和同源扩缩容建议 `trigger_time`，不把旧无时区字符串猜成 UTC，也不批量调整其他模块时间字段。

统一预测引擎的模型路径、无模型规则路径及模型失败后降级路径均以 aware UTC 时钟生成未来点。分钟、小时、天分别按 60/3600/86400 秒累加，JSON 输出明确包含 `Z` 或 `+00:00`；纽约夏令时切换不会生成重复或跳过的实际小时。真实零值仍然是零值，不补虚构样本或将它视为缺失。

输出时钟与算法特征分开：模型特征仍接收对应时刻的本地 naive 副本，规则工作时间因子仍按部署时区日历。没有修改训练数据、训练索引、算法实现或给不支持时区的算法索引强加 tz。数值模型仍保留既有部署日历语义；本修复承诺输出时刻明确，不宣称模型数值已消除部署时区差异。

`ScalingAdvisor` 本就复制预测点时间为建议触发时间，成本分析也对同源时间做差，因此无需另写触发时钟或修改 DTO。真实 FastAPI/Pydantic 序列化已核对预测点与建议均输出显式 UTC，前端不必猜部署时区。

基础服务的趋势计算另修复真实零起点和单点除零：单点为稳定，零起点按后段均值判断升/降/稳定，不伪造相对百分比，不改变预测值或样本。

## 升级与缓存

原预测结果可在 Redis 缓存中保留 1 小时，增强结果 2 小时；其中旧 naive 输出没有足够信息可安全补上 `Z`。预测缓存参数新增 `time_contract=utc-v1`，基础和增强请求均使用该版本，异常降级键也包含 `utc-v1`。相同请求不会命中旧时间契约结果，新 UTC 结果仍正常命中与沿用原 TTL。没有清 Redis 或改写旧记录；旧记录按既有 TTL 到期，RAG、助手会话和其他缓存不受影响。多副本滚动升级时旧进程仍可使用自己的旧键，应完成所有实例升级后统一验收新合同。

## 验证

离线固定时钟验证 UTC、Asia/Shanghai、America/New_York 三种部署时区，模型、规则及模型失败路径，分钟/小时/天采样，春秋夏令时变化与真实零值；使用真实特征提取器、建议器、成本时间匹配及 ASGI 路由序列化。旧持久化键来自修复前真实参数，测试先证明旧 naive 结果被命中，再证明版本隔离与新结果命中；基础和增强两条真实服务调用链均覆盖。

所有回归沿用测试套件的禁止真实 DNS/TCP/UDP 防护，没有调用 Prometheus、Redis、云供应商、集群或模型提供商。Python 项目没有独立静态类型门禁，本阶段执行 Ruff、py_compile 与适用预测测试，不将语法检查称作完整类型检查。

本次实际结果：时间契约原 33 项先红后绿；新增旧基础/增强缓存隔离 2 项先红，完整零值链进一步复现趋势除零；补齐单点/零起点后，专项共 43 项通过，预测相关共 78 项通过，整套离线回归 460 项通过。Ruff lint、专项格式检查、py_compile 与 `uv pip check --python .venv/bin/python`（91 个包）通过。此虚拟环境未安装 pip，使用 uv 检查同一解释器的安装元数据，没有为校验额外安装包。

```sh
ENV=test PYTHON_DOTENV_DISABLED=1 K8S_CONFIG_PATH=/nonexistent KUBECONFIG=/nonexistent LLM_API_KEY='' .venv/bin/python -m pytest -q
ruff check app/core/prediction/unified_predictor.py app/services/prediction_service.py tests/unit/test_prediction_utc_contract.py
```
