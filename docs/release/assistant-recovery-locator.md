# 助手失败响应的安全恢复定位

首次查询允许省略 `session_id`。此前服务在内部建立会话、成功领取请求后，如果保存回答失败或租约失效，5xx 错误没有返回内部生成的 ID；调用者无法确定应该查询哪个会话。

现在仅在 `begin` 已验证当前主体归属并返回有效领取后，失败响应增加：

```json
{"data":{"recovery":{"session_id":"00000000-0000-4000-8000-000000000001","turn_id":"11111111-1111-4111-8111-111111111111","retry_safe":false}}}
```

HTTP 装饰器转换的错误、直接领域错误和 WebSocket 错误使用同一结构。此字段只含两个 canonical UUID 和固定的 `retry_safe:false`，不含 owner、claim token、问题、回答、内部错误或持久化状态。通用5xx message/details的生产脱敏保持，不能借 details 绕过它。

定位字段不声称 `unknown` 已经落盘：结果可能已提交但回复丢失，unknown 保存也可能失败。客户端应由当前认证主体显式查询 `/assistant/session/{session_id}`，按持久状态人工核对；不能依据 locator 自动重复模型、工具或动作。

无权访问、无效 ID、参数错误、begin 之前的存储失败不附 locator。调用方取消仍传播取消；连接断开后无法保证发送错误响应。需要覆盖此类丢失响应的客户端应先建立并保存会话 ID，继续遵守既有稳定 turn_id 去重合同。

离线回归覆盖两条HTTP处理路径、生产debug配置下脱敏、WS一致性、未知状态不重跑、完成但丢ACK的保存结果重放、归属/ID/begin前失败、unknown保存失败及取消。真实Redis新增 `implicit_recovery` 场景由独立本机测试执行；不使用真实模型、通知或集群。

测试fixture先拒绝非空DOCKER_HOST/DOCKER_CONTEXT，清除TLS路由变量，只读核验当前context为实际本机Unix socket，再对所有daemon命令固定显式 `--host`。容器操作使用捕获的64位完整ID并验证随机owner标签/名称；volume保留重启语义，清理前核验随机名称、标签、local driver和创建身份。没有连接或修改生产。

首次真实隔离回归还识别并修正了 `begin` 的错误分类顺序：session hash类型仍先检查，随后先判owner，再检查global/owner索引类型。陌生主体没有owner索引时仍返回403而不是被503遮蔽；同一主体的缺失/损坏索引继续返回503，且不领取请求、不调用模型或修改会话字段。两者都属于begin未成功，不附locator。

fixture创建volume的调用与首次inspect均处于cleanup轨迹内。回执未知时只凭精确随机名称、owner标签、local driver恢复身份，再二次核对创建身份；无法证明归属则保留而非猜删。子进程失败公开信息仅为固定分类和整数exit，不能拼接driver stdout/stderr、payload或原异常文本；该隐藏不将失败视作通过。

Root最终验收：真实owned Redis十三场景全部通过（原十项、恢复定位两项及owner/index顺序一项），没有跳过。完整pytest为2538 passed、36按既有显式条件跳过，三项既有SWIG弃用警告；Ruff0.15.0全app/tests与app编译通过。最初两项新增真实回归在foreign403断言失败，定位到上述索引判定顺序，保持原断言后重跑十三项全绿；不是把拒绝码预期放宽或提前创建foreign索引消红。模型/RAG检索边界仍是离线替身，不作为真实模型服务可用性或生产恢复SLA证明。
