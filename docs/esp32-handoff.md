# ESP32 接入

设备固件和串口桥接在独立项目中；此仓库包含电脑语音后端。USB 模式由桥接读取板端 PCM 并连接后端，热点模式由板端直接访问允许的设备地址。电脑不需要通过浏览器麦克风参与设备对话。

## 配对

本地 `.local/device-pairing.json` 保存 `session_id` 和随机 `token`，不提交 Git。首次 USB 配对由设备项目的启动脚本生成；迁移电脑时使用同一后端目录布局，或在设备项目配置新的后端位置。云端 API Key 仅在后端，不写进固件。

设备连接 `/device/ws`，使用 `Authorization: Bearer <本地设备token>`。当前配对会话由服务器解析，不由固件硬编码。只有该音频通道可执行 session.start/input.audio/playback.progress 等音频消息。业务网页与设备共用 sessionId，独立连接 page 通道注册工具。

`POST /api/device/session` 是本机管理入口：从会话列表选定新的 ID 后，保持 token 不变、切换持久绑定；现有设备连接先回待机并关闭。设备下次唤醒会自动绑定所选会话，固件与桥接无需重新烧录。旧会话和旧记忆保留。目标音频通道占用或当前工具正在执行时，会拒绝切换。

## 音频及生命周期

- 输入：`input.audio`，Base64 PCM16LE，16kHz 单声道，推荐20ms一片。
- 输出：`audio.delta`，24kHz 单声道，response_id+seq；收到不等于播完。
- `audio.end` 表示生成结束；终端用实际 DMA 已播样本报告 `playback.progress`，全部播完且缓冲为空才报告 drained=true。
- `audio.clear` 立即停止指定响应并丢弃迟到片段；已执行的网页操作不会因此撤销。
- 本地识别“你好小萌”后播放“我在”并发 session.start；设备进入连续对话。
- 模型理解自然结束意图后调用 `end_conversation`，后端发 device.standby(reason=spoken_exit)，终端停止上传、清旧回复、播放本地退出确认并返回本地唤醒待机。
- 设备待机或断开时，异步任务结果只持久化，不主动唤醒或下次重新播旧通知。

页面可订阅 `audio.activity`（input/output）显示两个独立波形；输出能量按真实播放确认计算。页面收不到设备音频内容或云端凭据。

完整会话、工具与任务完成协议见 [integration.md](integration.md)。已通过的真人机械臂演示闭环不代表所有环境下的回声/误打断均已消除。
