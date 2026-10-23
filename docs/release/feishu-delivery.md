# 飞书专用通道投递合同

真实链路保持为同源 JWT/RBAC 网关 `/api/v1/autofix/notify` → 内部服务认证 → `NotificationService.send_notification` → 自定义机器人 webhook。该直发路径只初始化通知服务，不调用模型、Kubernetes、其他通知目标或自动修复工作流，也不走修复通知的重试 helper。Go 应用机器人的 AppID/chat_id 通道不使用自定义机器人 webhook。

按[飞书官方自定义机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)，成功响应的当前字段是 `code: 0`；`StatusCode`、`StatusMessage` 是冗余历史字段。项目原本只接受 `code`，本次不新增 `StatusCode` 单字段回退。当 `code` 存在时以它为准，非零不能被旧字段的零覆盖。

仅 HTTP 200 且 `type(code) is int` 且为零才返回成功布尔。缺失、字符串、浮点数、布尔、非 JSON、HTTP 失败或重定向均失败。关闭自动跟随重定向，单次通用发送只有一次 POST；结果未知时不因此自动重发。API 仍返回原 `success` 布尔，没有新增公开上游诊断接口。

日志只记录严格整数 HTTP 状态、业务码或静态无效标记，异常只记录类型；上游响应体、异常原文和 webhook 不写日志。`NotificationConfig.feishu_webhook` 排除 dataclass repr，但实际值保留供发送。只读审查中，通知服务信息只返回 `webhook_configured` 布尔，AutoFix/RCA/cache 的配置字典未包含 webhook。

配置读取仍为 `FEISHU_WEBHOOK` 和 `NOTIFICATION_ENABLED`。本地 release 由部署层将私有环境中的专用通道值映射给该变量；凭据不写源码、文档、参数、测试日志或公开输出。

新增离线 canary 先复现 7 项失败，修复后通知相关 38 项通过：包括上游响应回显、解析异常含敏感片段、成功响应额外数据不入日志、布尔零假成功、HTTP 错误、冲突历史码、repr、只读信息与一次发送。所有网络均为显式替身，遵守套件禁止真实 DNS/TCP/UDP 的防护。

真实投递验收与离线回归分开：由专用通道授权后，在服务重建健康的序列窗口内，通过 JWT 网关只发一条固定标题“AI-CloudOps 1.0 专用通道验收”的非业务消息，内容为验收说明、唯一 nonce 和 UTC 时间。一次请求的安全日志通过 request_id 关联 HTTP 200/业务码 0，证据仅保留非敏感元数据；没有收到明确结果时先核对证据，不盲目重试。离线测试结果不替代以下真实通道投递证据。

本阶段完整离线门禁为 483 项通过（13 项既有弃用等警告），Ruff lint、专项格式检查、py_compile 通过。未在离线门禁中发送真实消息。


## 专用通道真实验收

用户明确授权专用机器人目标后，在主代理确认 Go `3ac15b5`、AIOps `4dc31ac` 镜像健康且暂停其他实发的序列窗口，于 `2026-10-23T09:28:45.798854+00:00` 通过项目同源 JWT 网关只执行一次发送。标题为“AI-CloudOps 1.0 专用通道验收”，正文仅含非业务说明、nonce 与 UTC 时间。

安全元数据：request_id/nonce 为 `cloudops-feishu-882ab79190264608`；网关 HTTP `200`、业务码 `0`、`success=true`；同 request_id 匹配恰好一条上游投递日志，HTTP `200`、业务码 `0`。飞书已接受该条消息，本验收不宣称接收者已读。

验收脚本在发出请求前持久化一次性标记，成功后未重发，也没有调用模型、Kubernetes、其他通知目标。仅保存安全状态字段，没有输出或存储 webhook、JWT、原始日志或响应体；部署私有配置不进入此记录。发送窗口已结束。
