# 语音 Agent harness 设计

本文说明当前实现的职责和取舍。技术栈为 Python asyncio/FastAPI + 原生 Qwen WebSocket，浏览器端使用 Web Audio。Pi 没有被禁止；本轮的 Qwen 自己负责实时对话和工具选择，不再添加一个文本模型循环。若未来确有长任务需求，可把 Pi 或其他执行器接入本地 Tool.handler。

## 职责与状态

- Session：逻辑会话，独立于浏览器 WebSocket 和 Qwen 连接。磁盘保存用户输入、确认播放完的回答、打断标记、工具执行记录。
- Turn：用户一次新输入；speech_started 或明确输入开启新轮次。旧轮次的输出不能继续播报。
- Response：一次模型生成；一个 Turn 可经历工具调用和多次 Response。response.done 只代表生成结束。
- Playback：实际播放样本数、已生成样本数、播放结束确认。以终端回执确定用户听到的范围。
- Context：当前页面快照和递增版本。工具执行前和页面回执时检查，防止旧结果覆盖用户刚切换的页面。
- Tool call：按 call_id 记录生命周期。已分发的写操作在插话时不擅自当成撤销；超时可能是 unknown，不能自动重试。

audio 与 page 是两个独立通道。当前语音工作台只绑定 audio；独立业务前端通过 web/frontend.js 的 connectFrontend 绑定 page，上报快照并注册自己实现的工具。两者共享 session_id，前端不需要导入语音 UI、申请麦克风或持有模型密钥。ESP32 通过认证的设备接口绑定 audio，业务网页继续绑定 page。连接角色验证、单通道所有者和会话 ID 避免误控制另一页面。底层保留 duplex 供需要同时承担两种职责的客户端使用。

integration.updated 向连接方提供已接入前端、上下文版本和可用工具摘要，工作台据此展示实际能力。默认只有 get_page_context 等通用能力，不预置地区筛选、企业页面或其他业务字段。默认支持本机不同端口的前端开发服务器，当前仍仅监听回环地址。

## 实时性

模型事件接收和状态变更串行处理；工具执行在异步任务中进行，不能阻塞音频收发。多个工具结果全部返回且原响应 response.done 后，仅触发一次后续推理。新输入取消旧回答，过时工具结果仍保存事实，但不触发旧轮次继续发声。

页面状态立即在本地更新；发给模型的 session.update 会在语音采集中、等待生成或正在生成时延迟到安全边界。真实 smart_turn 回归中曾观察到活跃语音采集时更新会话会干扰续答，因此不能每次 speech_started 都重新配置模型。云端也可能先返回 cancelled、随后才返回 speech_started，这两种事件顺序均有回归覆盖。系统指令明确要求新意图覆盖旧回答，插话后不自动补完旧话题。

## 打断与听到的内容

Qwen-Audio 客户端协议当前有 conversation.item.delete，但没有文档化的按 audio_end_ms 截断事件。本项目不虚构该能力。打断时立即清空终端缓冲、取消回复、移除被打断的模型回答；本地仅记录已播放时长，不按照音频比例猜测已经听到的文字。完整播放后的文本才进入 assistant_heard。已完成的工具效果独立保存。

这会保守丢失一部分已听到的半句记忆，但不会把没播出来的后半段当作用户已听过。未来若模型提供可靠的词级对齐或截断 API，再改进此投影。

## 持久化与恢复

每个会话一个原子替换的 JSON 文件，位于 .local/sessions。恢复时向新模型会话注入有长度上限的已确认记录。当前页面必须由页面连接重新上报。未确认完成的写操作在进程重启后标为 unknown，不自动重放。不是完整长期记忆系统：目前保留最近完整记录，较早记录保存在文件但不全部注入模型。

## 第一版验证

离线回归验证事件乱序、重复工具调用、工具完成屏障、错误参数、页面版本变化、打断迟到音频、已生成但未播放完、超时结果未知、角色隔离、重连恢复。真实 API 验证模型连接、输入输出音频、工具调用、上下文和取消。

真实麦克风的声学回声消除效果、ESP32 唤醒词和播放中打断需要设备实测，不由模拟音频或接口测试替代。

参考：
- https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides
- https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events
- https://github.com/badlogic/pi-mono/tree/main/packages/agent
- https://docs.livekit.io/agents/logic/turns/
