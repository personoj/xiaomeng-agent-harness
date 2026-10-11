# 三仓联调与云端审查入口

这是 2026-10-11 本地演示的公开源码快照，包含发布前最新未提交修复；本轮发布没有重新运行设备、云端 API 或焊接测试。

| 部分 | GitHub | 本地目录名 | 职责 |
| --- | --- | --- | --- |
| ESP32 | [xiaomeng-esp32](https://github.com/personoj/xiaomeng-esp32) | `xiaoyun-esp32` | 按键收音、USB/Wi-Fi 传输、播放回执、屏幕 |
| Harness | [xiaomeng-agent-harness](https://github.com/personoj/xiaomeng-agent-harness) | `xiaoyun-realtime` | 8766，实时语音、工具生命周期、已播放记忆、页面 SDK |
| 工作坊 | [xiaomeng-robot-workshop](https://github.com/personoj/xiaomeng-robot-workshop) | `xiaoyun-robot-demo` | 8090，Three.js/MuJoCo、道具、点焊、四主题讲解 |

```sh
git clone https://github.com/personoj/xiaomeng-esp32.git xiaoyun-esp32
git clone https://github.com/personoj/xiaomeng-agent-harness.git xiaoyun-realtime
git clone https://github.com/personoj/xiaomeng-robot-workshop.git xiaoyun-robot-demo
```

三个目录保持同级。内部包名 `xiaoyun` 为兼容接口保留；产品名是“小萌”。它们是一次发布的干净快照，不带本地开发历史；上游源及其许可证见各仓库说明。

## 没有设备、没有 API Key 的云端

可审查所有核心代码、使用固定录音预览原理动画、运行纯本地单元测试及构建。不能据此声称完成 ESP32 真人声学验收，也不能将网络模型的延迟写成模拟测试结论。

Python 3.11+（建议 3.12）、Node.js 22.12+；创建一个独立环境，安装项目锁定/声明依赖，不升级已有项目依赖来掩盖问题：

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e './xiaoyun-realtime[test]' -e './xiaoyun-robot-demo[test]'
python -m pip install -r xiaoyun-esp32/requirements.txt
cd xiaoyun-robot-demo
npm ci
npm run build
python -m uvicorn embodied_jev.xiaoyun_demo:create_app --factory --app-dir src --host 127.0.0.1 --port 8090
```

主界面 `http://127.0.0.1:8090/`；动画预览 `/principle-studio.html?topic=coordinates`，另有 `inverse_kinematics`、`pointnet`、`planning`。预录配音已随仓库发布。首次开启服务会创建新的本地场景，私人旧场景没有上传。

另一个终端在同一环境运行 `python -m uvicorn xiaoyun.app:app --app-dir xiaoyun-realtime --host 127.0.0.1 --port 8766`（工作目录为三个仓库的父目录）。实际语音/API 使用前按 Harness README 配置自己的 Key；环境变量不应进入网页、日志或提交。

Windows 一键入口是 `xiaoyun-robot-demo/scripts/start-all.ps1`；设置 `XIAOYUN_PYTHON` 为安装完上述依赖的 Python 路径，或把父目录环境命名为 `.venv`。可加 `-NoBrowser`、`-Port COM9`。缺设备时 USB 桥等待接入；进程启动不等于设备已连接。

## 场景决策配置

在工作坊根目录把 `decision.example.json` 复制到 `.local/decision.json`，`key_file` 指向自己的一行纯文本决策模型 Key。示例端点是本项目最后使用的配置，实际账号开通的模型/地域可能不同，发布没有重新探测。语音 Key 与决策专用 Key 是两项独立配置。

## 必须维持的协议

ESP32/浏览器音频端占 `audio`，机器人业务网页占 `page`，共享同一 session。设备绑定保存在 Harness 自动生成的 `.local/device-pairing.json`。同一 session 不要启动两个音频端或两个控制页争抢所有权。

按住 BOOT 开始输入、松开提交；播报中按下取消旧播放。用户请求、工具受理、机械动作完成、声音实际播完是四个不同事件。长任务反馈和原理讲解都通过同一个 Harness；语音打断不应隐式停止机械动作。

讲解时钟：浏览器用 `audio.currentTime`，ESP32 用真实 `playback.progress` 已播样本。禁止用发送结束、模型生成结束或定时器假装已听完。

## 公开版差异

- 不包含 Key、设备配对 token、Wi-Fi 密码、私人会话、现场录音、Flash/NVS 备份、构建缓存或内部讨论。
- Harness 原角色素材明确限制再分发，公开版使用独立编写的抽象音频图标；ESP32 提供同格式原创占位资源生成器。原受限引擎及其派生动画二进制没有上传。本机演示未被替换。
- 项目固定提示 PCM 与四主题讲解 WAV 属于产品资源，与现场录音不同；一并发布以便同步和动画审查。
- 工作坊保留 Panda、FANUC 等许可证及来源；品牌名称/标志不因为公开源码而转让商标权。历史上游大体积实验报告/录屏不重复打包，见 `README.upstream.md` 与上游仓库。
- `RELEASE_NOTES.md` 是本快照状态；旧文档中的旧阶段记录不应视为当前所有功能的验收证据。

云端任务请使用根目录 [CLOUD_AGENT_PROMPT.md](CLOUD_AGENT_PROMPT.md)。
