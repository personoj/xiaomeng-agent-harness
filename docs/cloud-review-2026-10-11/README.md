# Harness云端复审证据

补充证据，不改变功能。协议和生命周期见 [latency.md](../latency.md)，完整 [BOOT→动作→实际播放路径、串行等待/锁/队列和时钟](protocol-path.md)。

20次离线注入延迟试验，仅测“最终播放报告收到→请求生成结果语音”：p50 65.3585→0.1495ms；p95 95.362→0.556ms。500ms quiet guard、用户说话优先和明确drained要求保留。原始 [before](task-notification-before.json) / [after](task-notification-after.json)；不是线上模型、设备、首可见帧或声学端到端延迟。与其他测试重叠的批次未纳入。

67 Python＋13 Node测试通过。实际SDK+工作坊controller的9个注入等待边界通过，包括BOOT/同轮session.stop，以及额外显式audio.clear覆盖。正常page-only连接通过turn.started/state收到中断，audio.clear三项是duplex/显式转发行为，不能冒充page-only路由验证。

配套升级SDK与工作坊await后检查；保持同轮转写、幂等、已执行结果留存和真实播放记忆。语音中断不暂停已受理机械任务。模型/浏览器/设备验收未做；USB串行ACK队头等待只记录，未改。

JSON仅替换绝对工作区前缀，数值与样本不变；无私人会话、录音、密钥或私有Library链接。原始基线和已测本地HEAD见 [test-summary.json](test-summary.json)。发布只建draft PR，不合并。
