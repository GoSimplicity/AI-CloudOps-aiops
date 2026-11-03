# 巡检开关与内部周期任务边界

`inspection.enabled` / `INSPECTION_ENABLED` 控制新的巡检工作。关闭时服务初始化、手动同步/异步巡检及内部调度启动均拒绝，API返回403、`INSPECTION_FEATURE_DISABLED`和对应`switch`，不会把关闭的功能包装成成功的空报告。已经存在的报告、导出、任务状态、规则、配置及历史查询仍然可读，读取本身不触发Kubernetes或Prometheus初始化。

`inspection.scheduler.enabled` / `INSPECTION_SCHEDULER_ENABLED` 只控制内部周期任务：关闭后不能启动；手动巡检仍受总开关控制。打开开关不自动创建任务，仍需调用既有`/inspection/scheduler/start`。启动串行初始化并登记一个worker，重复或并发调用不会创建多个周期循环。周期表达式仍按既有简单间隔解析，不新增完整cron语义。

每轮执行前及完成后检查两个开关；发现关闭则结束worker并清理运行标记。`/inspection/scheduler/status`和再次启动时若发现关闭，会取消并等待既有worker结束；显式stop及服务cleanup也取消、等待并清理引用，可以在重新打开开关后显式启动新worker。旧worker结束不会清空新worker的状态。

配置在进程启动时加载，本阶段没有增加环境变量监听或管理端热更新接口。若进程内配置对象发生变化，睡眠中的worker最晚在下一轮检查到关闭；状态查询或stop可立即收停。已发出的采集请求不能追溯撤销；采集完成后若总开关已经关闭，不发布本次成功报告。尚未执行的异步任务也会在采集前检查总开关并记录失败。

验证使用真实服务与ASGI路由、内存任务、Mock采集器和本地事件循环，没有连接真实Kubernetes、Prometheus或通知服务。内存历史及任务仍不具备跨进程持久化能力。

2026-11-03已执行25项新服务/API隔离回归，含相关factory/lifecycle共33项通过；和嵌入模型配置共同冻结后的全量2498项通过、33项既有条件跳过、3项既有SWIG弃用警告。全仓Ruff通过。没有真实集群、监控、通知访问或上线。
