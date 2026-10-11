> 三个公开仓库的安装、职责和配对方式见 [INTEGRATION.md](INTEGRATION.md)。本快照的未验证修复见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。云端优化任务见 [CLOUD_AGENT_PROMPT.md](CLOUD_AGENT_PROMPT.md)。

# 小萌 · 语音 Agent Harness

供个人开发与设备演示使用的独立端到端语音后端。Qwen Realtime 负责原生语音理解、回复和工具选择，Harness 负责会话、上下文、工具效果、打断、真实播放确认和异步任务结果。默认监听 `127.0.0.1:8766`。

## 已实现

- Qwen `qwen-audio-3.1-realtime-plus`，输入 PCM16/16kHz，输出 PCM16/24kHz，支持自然插话。
- 可选按键输入：`session.start(input_mode=push_to_talk)` 为该连接启用 manual；按下 `input.begin`、按住 `input.audio`、松开 `input.commit`，松开期间输入被丢弃。网页默认自动轮次模式保持不变。
- 独立 `audio` 与 `page` 通道：ESP32 或浏览器工作台收音/播放；业务网页提供工具和页面状态。
- 本地 JSON 会话持久化、具名会话创建、会话目录、设备会话同步切换。旧会话保留。
- 工具参数验证、调用去重、写操作串行、页面版本校验、未知执行结果与迟到回执处理。
- 语音触发的页面写操作等待同轮最终转写；缺失输入不执行。设备退出依然由模型调用 `end_conversation`。
- 长任务受理与完成分开：业务前端报告真实终态，Harness 等当前语音播完后再播报结果。
- 音量事件区分用户输入与设备实际输出；只把确认播完的回答计入已听到的对话记忆。
- 本地语音工作台及框架无关的前端 SDK，支持 React、Vue、普通 JavaScript。

已完成一次真人 ESP32 口述 → 机械臂真实仿真执行 → ESP32 完整播报的闭环验收。声学残余回声仍可能引发误打断，输入门控不是完整声学修复。知识库/BM25/embedding、数据库服务、公网多租户认证暂未实现。

## 在新电脑启动

需要 Python 3.11+。使用现有 ESP32 USB 桥接时，后端目录保留为 `xiaoyun-realtime`，与固件目录同级：

```powershell
git clone https://github.com/personoj/xiaomeng-agent-harness.git xiaoyun-realtime
cd xiaoyun-realtime
```

然后创建独立环境并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
$env:DASHSCOPE_API_KEY = "在本机填写你的百炼 API Key"
.\scripts\start.ps1
```

打开 <http://127.0.0.1:8766/>。可以输入文字测试模型语音，也可开启麦克风。已有 ESP32 占用同一会话的音频通道时，电脑工作台不要再占用它。

PowerShell 启动脚本优先使用 `XIAOYUN_PYTHON` 或项目 `.venv`，也可传 `-Python <Python路径>`；当前开发机兼容原有 Codex Python 路径。Linux/macOS 可在激活虚拟环境后运行：

```sh
export DASHSCOPE_API_KEY='在本机填写'
python -m uvicorn xiaoyun.app:app --host 127.0.0.1 --port 8766
```

从源码目录以 editable 方式运行，`web/` 与源码一起保留。Node.js 仅用于浏览器客户端测试，不是 Python 服务的运行依赖。

## 配置

复制 `settings.example.json` 到 `.local/settings.json` 可修改模型、音色、地域端点和轮次检测模式。密钥优先从 `DASHSCOPE_API_KEY` 环境变量读取；也支持 `api_key_file` 指向本机文本文件，其中使用 `DASHSCOPE_API_KEY=实际值`。不要把实际值写入示例、网页或源码。

`XIAOYUN_MODEL`、`XIAOYUN_VOICE`、`XIAOYUN_ENDPOINT`、`XIAOYUN_API_KEY_FILE` 可覆盖文件配置。模型空闲断开后重新唤醒/开启语音，会恢复已确认的历史。数据保存在 `.local/`，仓库不包含任何用户会话、录音、设备 token 或云端密钥；换电脑时另行配置凭据，旧对话可按需自行迁移本地目录。

## 会话选择与设备配对

- `GET /api/sessions`：返回会话名称、时间、状态和设备绑定标记，不返回对话正文或设备 token。
- `POST /api/sessions`，例如 `{"name":"机械臂演示"}`：创建并持久化新会话。
- `POST /api/device/session`，`{"session_id":"所选ID"}`：已配对设备切换到该会话。保留凭据与旧会话，当前设备回待机，再次唤醒进入新会话。

机械臂演示页的设置已使用这些接口：选择已有会话或新建并连接；服务地址和手工 ID 收在高级设置中。机器人网页及 MuJoCo 本体位于独立项目，不包含在此后端仓库。

ESP32 固件与 USB 桥接也是独立项目。当前终端已实现“你好小萌”唤醒、本地“我在”提示、模型工具退出和真实 DMA 播放回执。设备使用 `/device/ws` + 本地配对 token；业务网页使用 `/ws/{session_id}?role=page`。完整接口见 [前端与音频接入](docs/integration.md) 和 [设备接入](docs/esp32-handoff.md)。

## 开发与验证

```powershell
.\scripts\test.ps1
```

或者在项目虚拟环境中运行 `python -m pytest`，再运行 `node --test tests/browser.test.mjs`。当前回归覆盖会话目录的只读行为、设备同步切换、工具调用、异步完成通知、转写关联、打断及实际播放进度。

`scripts/probe_qwen.py`、`scripts/live_smoke.py`、`scripts/live_stream_check.py` 可进行真实 API 验证，会产生正常模型费用；结果只保存在忽略目录 `.local/`。这些脚本不能替代设备真人声学验收。

## 目录与边界

- `xiaoyun/harness.py`：语音生命周期与工具执行。
- `xiaoyun/speech_input.py`、`task_updates.py`：语音输入关联、异步任务通知。
- `xiaoyun/session_catalog.py`、`memory.py`：会话目录与持久化。
- `xiaoyun/device.py`、`app.py`：终端鉴权、模型退出工具、HTTP/WebSocket。
- `web/client.js`、`web/frontend.js`：音频客户端与业务前端 SDK。

默认仅本机访问。需要局域网设备时，启动脚本支持 `-LanAddress <本机IPv4> -Restart -EnablePrivateFirewall`；只将认证过的设备接口开放到 Private 本地子网，普通页面/API 仍限制在本机。此仓库尚不提供公网部署所需的账户与多用户权限体系。

此为公开源码快照，使用原创抽象音频占位动画，未附带本地受限角色素材。角色资源来源与使用说明保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，不要将第三方角色材料误标为本项目原创或另行授予开源许可。
