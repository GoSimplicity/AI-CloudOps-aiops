# 暂存知识版本发布准入

旧行为只在创建stage前检查已有版本容量，没有检查构建完成的新stage。离线复现使用真实MD分块和存储方法：2个MD文件合计646286字节，分别650块、每次都低于1000块上限，却产生1300个doc、1300个vec和101401个term，共104001个快照键。刷新此前能够成功发布，下一次clone/上传/刷新才因100000键预算失败。这里只有1300个向量，不能把该案例夸称为语义查询本身已不可读；它直接证明成功发布破坏了后续修改准入。

现在完整检查发生在 `mark_unknown` 之前，不通过就保留旧active，按已知未提交stage执行abort与安全回收：

- 全前缀唯一物理键（含revision）最多100000，避免业务键刚好100000、再加revision就让下一次检查失败。
- doc/vec/term/idx快照键最多100000，doc与vec各不超过现有100000读取上限，并严格配对；ID须为当前32位小写十六进制格式。
- 文档为包含content和metadata的hash；content为不超过10MiB的UTF8。metadata是现行pickle格式，不当作UTF8解码，也不执行类加载或外部引用。先检查原生值操作码白名单，拒绝GLOBAL/REDUCE/EXT及外部buffer等，再接受顶层字符串键dict及受限原生值/容器，在构造Unpickler之前限制memo索引为0..99999、memo写入不超过100000次，并拒绝未定义或越界引用，避免小pickle触发巨型memo分配；同时拒绝非有限浮点、循环、超过64层或100000节点的结构、超过1MiB的pickle及尾随内容。当前公开API和实际MD处理器产生的元数据已有回归验证；程序化自定义类等旧元数据不自动兼容或迁移，检查失败保留旧active，不删原始数据。
- 向量必须完整存在，字节长度匹配当前store的float32维度，且全部数值有限。term必须是集合，所有成员引用本代文档；分批SSCAN检查，不加载全部集合引用到一个无界列表。
- 当前writer不产生持久idx。既有clone继续跳过旧idx，直接混入新stage的idx或其他未知键被拒绝，不猜测旧FAISS位置映射。

扫描按唯一键计数并设现有30秒检查预算；文档/向量/词条均分批读取。检查后确认target revision稳定，再由Redis原子操作比较该revision、把building改为unknown并写sealed证据。stage的增、删和COPY事务WATCH其所属state且必须仍为building；每批COPY也推进target revision。验证期间已提交的写入会使freeze失败，已经准备但尚未EXEC的晚写会被freeze的WATCH变化阻止。最终发布CAS再次检查sealed target revision，继续检查原source revision、lease、删除标记和unknown状态。

这些约束覆盖遵循协议的代码路径，不能阻止拥有原始Redis管理权限的非合作客户端随意改数据。状态存在同一集合hash中，其他版本状态切换可能触发保守WATCH冲突；此时不覆盖active，也不把失败当成功。结果未知的CAS沿用原审计处置流程，不自动放弃或删除。

GC保护判定没有放宽：active、有活跃reader租约、unknown及旧固定前缀仍不能自动清理。超限但已明确未提交的registered stage可在abort、释放租约后流式分批回收；GC自身不使用100000的构建/读取准入上限。真实自有Redis验证超过100000键的stage全部回收后，下一次小更新可以成功，同时旧reader数据和固定前缀保留。

本阶段没有改公开 `/assistant/clear-cache` 的语义；它只清缓存，不调用向量库clear。向量库Python `clear` 的超限恢复与已空时是否省略发布，也没有假装成公开HTTP API故障。本阶段未增加持久模型manifest、分布式索引或新保留策略。

```sh
.venv/bin/pytest -q tests/unit/test_stage_admission.py tests/unit/test_rag_ingestion.py
PYTHONPATH=. .venv/bin/python scripts/manual_checks/rag_stage_admission_redis.py --execute
```

新脚本默认dry-run，不加载真实凭据；仅在显式execute后先拒绝非空DOCKER_HOST/DOCKER_CONTEXT，清理endpoint/TLS环境变量再只读发现本地Docker Unix endpoint；后续操作与清理固定使用该endpoint的--host，不再依赖可变环境。随后自建带随机标签、绑定随机loopback端口的Redis7.4.11。结束时核对标签后删除自有容器与卷，不接受现有Redis地址。验证超限拒绝/回收/后续小更新、公开MD元数据、freeze前revision变化、已WATCH晚写与发布后写入拒绝、sealed revision的CAS、旧idx跳过/新idx拒绝，以及reader/unknown/legacy保护。没有使用真实模型或通知通道。
