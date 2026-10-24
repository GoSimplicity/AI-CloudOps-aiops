# Kubernetes 运行时与修复验收

同步 Kubernetes SDK 的网络调用通过工作线程执行，连接/读取期限为3/10秒；异步服务中的初始化和健康探测同样移出事件循环。旧修复代理的重试等待使用异步sleep。补丁日志只记录目标、顶层字段和错误类型，不记录补丁内容或服务端异常正文

初始化未成功时不再放行操作。显式K8S_CONFIG_PATH或非默认配置项路径缺失会失败，不回退到其他文件；修复代理不再搜索工作目录/用户目录并修改全局KUBECONFIG。SDK标准默认配置仅由服务器启动配置选择，浏览器和模型不能据此切换目标

滚动更新完成要求控制器观察到当前generation、新副本达到期望、旧副本退出且副本可用/就绪。旧Pod仍健康但surge新Pod坏探针时不算恢复。动作API接受但rollout失败返回failed，已发出的写入可能仍有副作用。取消协程不保证取消底层SDK线程；1秒轮询阈值在默认2秒轮询间隔下实测约2.015秒，不承诺严格1秒截止

旧修复代理调用模型前，复制Deployment并隐藏env.value及last-applied完整配置注解，主分析和简化备用分析使用同一安全副本；SDK内部原对象仍保留真实值供修复。含datetime的模型JSON上下文继续使用显式可序列化转换，不因SDK时间对象丢失主分析上下文。此规则针对上述已知结构字段，不等于任意用户日志文本的通用脱敏

演示引导函数只创建缺失资源，已有资源不更新。只有结构化SDK错误全部为409才可返回幂等已存在；混合409/403等失败返回失败及可能部分变更，不以错误字符串中出现AlreadyExists就宣称全部成功

## 验证

9项先红复现SDK阻塞/无期限、初始化假成功、错误配置回退、跨集群搜索及引导混合错误假成功；模型发送副本与SDK时间对象回归通过，surge状态3组旧逻辑先红。修改过程中全量414项离线测试通过，新增显式执行/结果防覆盖/非回环拒绝3项后，当前20项边界专项通过；Ruff和diff检查通过，全部默认禁止真实网络

独立审查用真实本机kind先红后绿：单副本restart注解与generation递增确认；随后注入自有Pod必失败探针，实际total2/updated1/ready1/available1，旧实现错误completed，修复后动作API接受但top failed/post ready false。仅创建唯一namespace，按owner标签和UID检查后清理；没有模型、通知或其他命名空间操作

同一验收已保存为显式手动工具：

```sh
.venv/bin/python scripts/integration/autofix_kind_smoke.py --execute --kubeconfig /tmp/cloudops-kind-kubeconfig --result /tmp/new-autofix-result.json
```

该脚本直接构造修复引擎并显式传入 `auto_execute=True` 与自建 namespace 白名单（SEC-26 后服务默认只出计划），仅用于隔离kind验收。只接受kind-cloudops-release的HTTPS回环配置，拒绝exec/auth-provider及不安全TLS，并核验本机Docker kind节点标签、6443回环端口对应；BusyBox使用imagePullPolicy Never，需预加载busybox:1.37.0。缺少--execute、结果文件已存在或配置指向外部地址均在写入前拒绝。持久化工具本次也已实际运行通过并确认namespace清理，不默认接入CI的真实集群写入
