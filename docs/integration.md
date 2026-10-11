# 通用前端与音频接入

语音工作台只承担音频与对话界面；独立前端提供自己的状态和操作。前端可以使用 React、Vue、普通 HTML 或其他能运行浏览器 JavaScript 的框架。接入不依赖固定业务字段，也不会自动获得对任意第三方网站的控制权。

## 最小接入

1. 在工作台开始一次对话，从“工具与上下文”复制会话 ID。
2. 自己的前端通过 SDK 连接同一个 sessionId，用 pageId 标识当前页面。
3. 注册页面允许执行的函数，并主动上报页面状态。

```javascript
import { connectFrontend } from 'http://127.0.0.1:8766/static/frontend.js';

const bridge = await connectFrontend({
  serverURL: 'http://127.0.0.1:8766',
  sessionId: pairedSessionId,
  pageId: 'my-frontend',
  context: readPageContext(),
  actions: [{
    name: 'open_panel',
    description: '打开当前应用中指定的面板',
    side_effect: true,
    parameters: {
      type: 'object',
      properties: { panel: { type: 'string', enum: ['overview', 'details'] } },
      required: ['panel'],
      additionalProperties: false,
    },
    execute: async ({ panel }, execution) => {
      // 如有异步准备，在准备完成、真正修改之前检查页面是否仍有效。
      if (!execution.isCurrent()) {
        return { result: { error: 'page_context_changed' } };
      }
      openPanel(panel); // 与页面按钮复用同一个业务函数。
      return { result: { ok: true, panel }, context: readPageContext() };
    },
  }],
  onEvent(event) {
    if (event.type === 'error') console.error(event.message || event.code);
  },
});

// 用户手动操作、切换路由等，也应更新模型可见状态。
bridge.updateContext(readPageContext());

// 权限或页面能力变化时，全量替换当前工具。
// bridge.setActions(nextActions);

// 长任务启动工具先返回 result: {task_id: '任务ID', status: 'accepted'}。
// 页面收到该调用的 tool.completed/succeeded 后，真实任务结束时回传：
// bridge.reportTask({taskId: '任务ID', sourceCallId: '原始call_id',
//   status: 'succeeded', result: {success: true}});

// 在组件卸载或页面退出时断开。
window.addEventListener('pagehide', () => bridge.disconnect());
```

示例中的 `readPageContext`、`openPanel` 是业务前端自己实现的函数，SDK 没有内置面板业务。React 可在组件 effect 中建立连接并在清理函数中断开；Vue 可在 onMounted / onUnmounted 中做同样的事。异步连接结束前组件可能已经卸载，接入代码应保留 disposed 标记，避免留下后台连接。

`connectFrontend` 只建立 page 通道，不启动云端语音会话、不申请麦克风、不注入小云形象。连接结果返回 `sessionId`、`updateContext`、`setActions`、`reportTask`、`disconnect`。模型密钥留在本地服务，前端无需接触。

`context` 是全量快照，可由前端自由组织；服务器生成递增 revision。工具结果可携带最新 context，由 harness 原子更新，此时不要再额外发送同一份 context.update。传入的工具回调可读取 `execution.contextAtStart`、`execution.callId`，并在实际修改前调用 `execution.isCurrent()`。

## 会话配对与开发端口

`POST /api/sessions`，请求 `{}` 创建新会话；`{"session_id":"已有 ID"}` 恢复。服务返回 session_id。工作台的“开始新会话”会改变会话身份，业务前端也需改为绑定新的 ID。

底层连接为 `ws://127.0.0.1:8766/ws/{session_id}?role=page` 或 `role=audio`。每个角色同时只能有一个连接所有者；冲突会明确拒绝，SDK 不会假装连接成功。底层 VoiceClient 也支持 `duplex`，但当前工作台使用 `audio`，让独立业务前端占用 `page`。未来 ESP32 适配器替换 audio，业务前端不用重写工具。

默认允许 localhost、127.0.0.1、[::1] 的不同端口来源，因此 Vite 等本地开发服务器可直接加载 SDK、调用 HTTP 接口及连接 WebSocket。也可用开发代理保持同源。其他来源可通过配置 `allowed_origins` 或环境变量 `XIAOYUN_ALLOWED_ORIGINS`（逗号分隔）显式声明；这不改变服务仅监听 127.0.0.1 的默认行为，也不等于已经完成公网认证和部署。

## 核心事件

| 方向 | 事件 | 说明 |
|---|---|---|
| 音频→服务 | session.start / session.stop | 建立或结束云端连接，逻辑记忆保留 |
| 音频→服务 | input.audio | Base64 PCM16，16kHz 单声道，推荐 20ms 一片 |
| 音频→服务 | input.text | 开发验证或其他文字入口 |
| 音频→服务 | input.commit | 仅 manual 配置，提交一轮音频 |
| 音频→服务 | interrupt | 停止当前播报，开始新的输入轮次 |
| 页面→服务 | tools.register | 声明工具名称、说明、JSON Schema、side_effect |
| 页面→服务 | context.update | 当前页面全量快照 |
| 服务→各连接 | integration.updated | 当前页面连接、版本、上下文和可用工具摘要 |
| 服务→页面 | tool.request | call_id、args、expected_revision |
| 页面→服务 | tool.result | call_id、result，可附最新 context |
| 页面→服务 | task.update | task_id、source_call_id、status、result，报告已受理长任务的真实终态 |
| 服务→页面 | task.ack | 任务结果已持久化，同一页面/任务重复报告返回 duplicate=true |
| 服务→各连接 | task.completed / task.announcing | 最终结果事实 / 开始语音通知；并非重新执行任务 |
| 服务→音频 | audio.delta | response_id、seq、24kHz PCM16 |
| 服务→音频 | audio.end | 该响应生成结束及总样本数，不代表播放完成 |
| 服务→音频 | audio.clear | 立即停止指定 response_ids，拒收它们的迟到音频 |
| 音频→服务 | playback.progress | response_id、played_samples、drained |

播放进度指输出设备实际经过的样本数，不是收到的字节数。drained=true 必须等生成结束且全部缓冲播放完毕。浏览器以 AudioContext 时钟和音源结束事件确认；ESP32 适配器需用自身音频输出进度。

工具是经过明确注册的函数，harness 不解释任意 Python/JavaScript 代码。内置 get_page_context；知识库后续作为本地 Tool.handler 注册，实时模型决定是否调用。后续若接 Pi，只作为具体长任务执行器，不替代音频与会话状态管理。

## 长任务和完成通知

工具调用的超时只覆盖任务受理，不等待机器人等长任务执行完毕。启动工具返回 `result.task_id` 与 `status: accepted`，并及时返回当前快照。前端保留任务编号与 `execution.callId`，收到工具调用成功事件后再发送最终结果，避免报告早于受理落盘。完成状态只允许 succeeded/failed/cancelled。

服务核对原调用确已成功、任务编号一致、当前 pageId 是原受理页面，再写入 task_result 记忆并去重。前端可重复发送直到 task.ack，页面刷新后可恢复任务关联。任务事实与“用户听到回答”仍分开记录。

语音通知等待用户未在说话、当前生成结束、原音频实际排空、工具不再执行的时机；不打断当前回答。终端待机/断开时只保存结果，不启动云端连接，也不在下次唤醒重播旧通知。任务结果仍在会话记忆中可供查询。

真实千问测试发现仅追加 system 对话项后 response.create 会重复旧回答；当前使用明确标注“应用自动发送，非用户口述”的文本项触发结果播报。此内部输入不伪装成用户消息出现在工作台，也不按 user 写入本地记忆。结果字段是数据，非新指令。协议依据：https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events 。

频繁变化的动画坐标不应触发上下文版本更新。机器人示例仅在任务身份/运行状态变化时更新快照，阶段细节通过 get_robot_status 读取最新值，使同一任务的自然运动不致作废正在生成的“暂停”指令。

## 当前边界

- 当前重连由用户再次开启语音或发送文字触发；外部前端自行管理重连生命周期。恢复已确认的近期文本和工具事实，不重放旧音频或写操作。
- 远端写操作出现“已执行但回执丢失”时无法证明 exactly-once；记录 unknown，等待核对或迟到回执。
- 当前 Qwen-Audio 没有文档化的按播放时长截断 API。部分回答移出模型历史，只记录打断和时长，不会精确记住已听到的半句话。
- 一个会话绑定一个当前业务前端；框架通用不代表已实现多标签页自动选路。

## 输入与实际播放的音量事件

2026-10-10 为业务前端补充通用 `audio.activity` 事件（只发 page，不写 trace/记忆/模型上下文）。字段为 `channel: input|output`、`level: 0..1`、`active: boolean` 和 `source: capture|playback`。输入 RMS 从现有 input.audio 计算，最多约 12.5 Hz；输出 RMS 按已有 playback.progress 对应的真实已播放采样区间计算。生成音频本身不触发输出波形，播放排空、打断和断线会归零；迟到回执不会重新激活已打断回答。

现有设备协议不变。机器人前端还提供可选的直接输入适配接口，见 ../xiaoyun-robot-demo/docs/DEMO_AUDIO_INTERFACE.md。未触碰设备、串口或设备入口文件。本轮自动回归为 35 项 Python + 6 项 Node，全部通过。

## 语音操作的输入证据

页面的有副作用工具由语音触发时，Harness 按 input item_id 等待本轮最终转写（最多 1.5 秒）。模型意图仍由原端到端模型判断；不使用关键词、额外 ASR 请求或分类模型。转写为空、失败、缺失，或会话停止/新轮取代旧轮时，不派发页面操作，并记录 tool.blocked / unconfirmed_speech。文字输入和设备本地 end_conversation handler 保持原执行方式。等待期间不占会话锁，音频和事件仍可处理。

speech_stopped(reason=turn_invalid) 不入 expected_turns 队列，避免无效输入产生不存在的响应并阻塞完成通知。协议依据：[阿里云 Qwen-Audio 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)。此约束能挡住没有转写支撑的写操作，不等于消除了设备回声，也不能仅凭非空转写证明声源是真人。

## 会话目录与设备同步选择

后端提供以下本机接口，适用于自定义前端：

```js
const catalog = await fetch(serverURL + '/api/sessions').then(r => r.json());
// catalog.sessions: session_id、name、created_at、updated_at、state、
// page_connected、audio_connected、is_device、history_entries。
// 列表不返回对话内容、工具参数或设备token。
const created = await fetch(serverURL + '/api/sessions', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({name: '演示会话'})
}).then(r => r.json());
```

已配对设备同步切换：`POST /api/device/session`，JSON `{session_id: selectedId}`。服务器保留原token，原设备连接先回待机并关闭；下一次唤醒用新绑定。旧会话不删除。目标音频已占用或设备当前工具仍在执行时返回409，前端应显示失败而不是假报已连接。设备尚未配对时此接口返回device_not_paired。

机械臂页面的“选择会话 / 新建并连接”已接入这些接口。运行/暂停中的机械臂任务需先结束或停止再切换，避免丢掉尚未回传的任务结果；当前ID和服务地址在高级连接设置中保留。成功后同步浏览器URL，因此刷新仍使用新选择。

## 可选按住说话（PTT）

音频连接发送 `{"type":"session.start","input_mode":"push_to_talk"}`。服务器仅为该连接将 Qwen 的 turn_detection 设为 null；ready 回 `input_mode=push_to_talk`、`turn_detection=manual`，全局 Settings 与其他连接不变。

等 ready 后，每次按下发送 `input.begin`（打断旧响应并清空音频缓存），按住期间发送原格式 `input.audio`（PCM16/16k/单声道），松开发送 `input.commit`。未 begin、松开后和断线后的输入均不继续沿用；重连必须重新 begin。重复 begin/commit 不重开或重复提交同一轮；少于1600样本/100ms的输入会回 input.rejected(audio_too_short)，不请求回复。按住/待提交期间异步任务结果等待，避免抢播；已有响应正在取消时，新回复等待取消完成。

本轮 commit/item_id 仍与语音转写关联，页面写操作仍等待同轮最终转写。模型 end_conversation 保持原语义控制；设备收到 standby 后本地提示、关闭上传并返回本地唤醒。PTT 阻止松开时的残余回声进入云端，不等价于声学AEC修复。
