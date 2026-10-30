# 向量查询缓存合同

向量查询缓存使用 `search-config-v2` key 合同，绑定 owner、generation/revision、查询、k 及最终生效的完整 `SearchConfig`。默认省略配置与显式默认值等价；数值权重按浮点值规范化。请求开始时复制配置，避免等待嵌入/Redis期间调用方修改参数，导致缓存合同与实际搜索不一致。阈值、MMR开关和MMR lambda均独立区分，缓存不会绕过实际候选过滤及排序。

批量预嵌入路径另外复制实际查询向量，并绑定 dtype、形状及完整字节摘要，与普通查询分开缓存。仅完整、已知的配置合同启用缓存；自定义子类、额外字段、非有限参数或未知字段类型不读写缓存，继续执行原搜索。未初始化实例缓存作用域的对象也不复用缓存。

当前 generation 尚没有持久的模型指纹，所以每个 store 在构造时生成随机实例UUID作为缓存作用域；同实例相同请求可以命中，不同worker/store不能共享向量结果。该取舍减少跨worker命中，不代表已解决模型与generation持久向量不匹配。只有未来模型manifest及完整索引协议经过验证后，才可恢复跨实例共享。

新key前缀不读取遗漏排序参数的旧MD5 key。旧查询缓存按原TTL自然到期，不扫描删除其他集合、原知识文档或历史固定前缀；更上层答案缓存有独立合同，本阶段没有重写。

[Redis SCAN允许重复返回同一元素](https://redis.io/docs/latest/commands/scan/)，保留容量检查按唯一物理键计数，集合达到上限+1立即拒绝。重复页不消耗额外数量额度，原30秒deadline、真实唯一键上限和generation数量上限继续生效；无限重复页仍会超时。此修复不调整clear超限准入、刷新总目录预算、旧索引清理或数据保留政策。

离线回归：

```sh
.venv/bin/pytest -q tests/unit/test_rag_search_cache_parameters.py tests/unit/test_rag_retained_capacity.py tests/unit/test_rag_cache_contract.py tests/unit/test_rag_ingestion.py tests/unit/test_rag_refresh_atomic.py
```

测试执行真实查询/融合/MMR入口，嵌入及Redis边界使用内存替身；不访问真实模型、通知或既有Redis。
