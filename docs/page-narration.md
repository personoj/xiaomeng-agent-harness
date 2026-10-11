# 页面预录配音

页面可在一个正在执行的工具调用中准备预录讲解，由 Harness 通过已经连接的音频端播放。它沿用 `audio.delta`、`audio.end`、`audio.clear` 和 `playback.progress` 协议，不打开第二条设备连接、不改变设备固件，也不暂停任何页面业务任务。

## SDK

```js
const narrationId = crypto.randomUUID();
await bridge.prepareNarration({
  sourceCallId: toolMetadata.callId,
  narrationId,
  text: completeNarrationText,
  pcm: rawPcmBytes,
});
return {accepted: true, narration_id: narrationId};
```

`rawPcmBytes` 必须是 `Uint8Array` 或 `ArrayBuffer`，内容是 **24 kHz、单声道、16位小端 PCM**，不含 WAV 头。最大长度为120秒。SDK自动转成base64；这份大载荷只走 `narration.prepare`，不能放入工具结果、上下文或普通任务结果。正文最长8000字符。

准备调用必须来自当前页面仍在执行的 `tool.request`。服务端同时核对页面连接、页面身份、工具调用ID、当前轮次和工具响应，只有 `dispatched` 状态可准备。一次只允许一份准备中或播放中的讲解。相同调用、ID、正文及PCM的重复准备幂等；不同内容不能覆盖已经准备的录音。已结束的调用不能重新发起声音。

准备成功不等于开始播放，更不代表用户听完。工具需返回上例的两个字段。准备后返回失败、缺少匹配ID或没有 `accepted:true`，会撤销这份讲解。服务仍把小型工具回执送给模型，但不再请求模型复述讲稿。

`bridge.cancelNarration(narrationId)` 取消当前页面自己的对应讲解；不推进对话轮次，不调用场景或机器人暂停。页面离开时调用 `bridge.disconnect()` 也会使服务撤销未结束的配音。

## 页面事件

| 事件 | 字段 | 含义 |
| --- | --- | --- |
| `narration.prepared` | `narration_id, source_call_id, duration, duplicate` | PCM已收妥；SDK的prepare Promise据此完成 |
| `narration.started` | `narration_id, response_id, duration` | 等待前一段声音真正播空后，开始向现有音频端发送 |
| `narration.progress` | `narration_id, response_id, played_seconds, total_seconds, completed` | 由音频端真实的已播样本回执换算；页面动画应以此为设备讲解时钟 |
| `narration.stopped` | `narration_id, response_id, reason` | 用户打断、页面取消、会话结束或通道离开；不自动续播 |
| `narration.failed` | `narration_id, code` | 准备被拒绝；SDK的prepare Promise拒绝 |

事件还带有通用的 `session_id` 和 `turn`。`response_id` 在尚未受理的准备取消时可为null。主页面只处理属于当前 `narration_id` 的事件，忽略上次讲解迟到的进度。

发送仅领先设备已播位置最多0.75秒，每包不超过100毫秒。没有播放回执就不会继续灌入整分钟音频。发送完毕时只发 `audio.end`，不会把生成结束标为完成。只有收到总样本数完全匹配且 `drained:true` 的真实播放确认，才发 `completed:true`、将完整正文保存为 `assistant_heard`。中断只保存现有的 `assistant_interrupted` 占位记录和已播时长。

若原工具调用前已有模型口头回应，预录配音等待那段声音的真实drain，而非仅等待模型生成完毕。等待期间保留一个未完成的本地响应，使普通任务完成通知继续排队。模型不为本地配音建立虚假的云端响应；打断配音也不向云端发送不存在的响应取消。

## 生命周期

- 新语音轮次、按下BOOT导致的 `input.begin`、显式interrupt、session.stop、音频断线都会停止当前配音、清除设备旧buffer并丢弃准备中的PCM。
- 页面断开时停止该页面的讲解；重新连接不会恢复它。
- 对已经取消的源调用，即使工具迟到返回 `accepted:true`，也不会再让模型续读旧讲稿。
- PCM不写入会话存储、trace或模型上下文。高频讲解进度同样不写入trace。完整正文只在真实听完后进入历史。
- 页面没有独立唤醒设备的权限；必须已经存在当前模型会话及音频通道，且由模型正在分派的工具发起准备。

## 检查范围

新增六个协议测试覆盖：角色/调用/页面归属与载荷限制、前序真实drain与发送窗口、重复回执与重复准备、BOOT新轮中断、准备后页面取消及迟到回执、页面断线。既有PTT/音量回执10项、普通工具及异步通知26项回归通过，共42项。

这些测试使用FakeProvider和可控播放回执，验证生命周期和数据流；没有向实际ESP32发声，也不能替代真实设备听感与同步验收。前端SDK通过JavaScript语法检查；主页面的动作注册和原理动画接入由调用方实现。
