# StackChan + Nanobot 操作手册

本文记录当前 demo 的常用操作：刷固件、启动本机 Bridge、启动本地模型、Windows 迁移、Lightsail 部署和验证。

## 当前架构

```text
StackChan
  -> OTA: /xiaozhi/ota/
  -> WS : /ws
  -> Bridge
  -> Nanobot
  -> LLM / ASR / TTS / Vision
  -> MCP tools
  -> StackChan 本地能力
```

当前固件已经支持进入 Agent 后预连接 Bridge。屏幕出现：

```text
Nanobot 已就绪，请说唤醒词
```

看到这句后再说唤醒词。

## 注意事项

- API Key 只能放在本地 env 或服务器环境变量里，禁止提交到仓库。
- `.run/`、模型文件、ESP-IDF、构建产物不要提交。
- `stackchan-nanobot.local` 只适合局域网 mDNS；公网服务器要使用公网 IP 或域名。
- StackChan 连接 Bridge 时不要求 USB 连接电脑，USB 只在刷机、串口诊断、读 coredump 时需要。

## 刷固件

1. 让 StackChan 进入下载模式。
2. 执行：

```bash
cd /home/yunhao/github/stackchan
STACKCHAN_PORT=/dev/ttyACM0 ./scripts/flash_stackchan.sh
```

3. 看到 `Hard resetting via RTS pin...` 后，按一次 Reset 正常启动。
4. 进入 Agent 页面，等待屏幕显示：

```text
Nanobot 已就绪，请说唤醒词
```

## Linux 本机启动 Bridge

### 云端 GLM 模式

先确保环境变量里有 GLM/Zhipu Key，例如：

```bash
export ZHIPU_API_KEY="your-local-key"
```

启动：

```bash
cd /home/yunhao/github/stackchan
STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot-glm.env" \
  ./scripts/start_stackchan_nanobot_hotspot.sh
```

### 纯本地模型模式

先启动本地 ASR/TTS/LLM 服务：

```bash
cd /home/yunhao/github/stackchan
./scripts/start_stackchan_local_inference.sh
```

再启动 Bridge：

```bash
STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot-local.env" \
  ./scripts/start_stackchan_nanobot_hotspot.sh
```

### 停止服务

```bash
cd /home/yunhao/github/stackchan
./scripts/stop_stackchan_nanobot.sh
./scripts/stop_stackchan_local_inference.sh
```

## Windows 运行

PowerShell 建议在仓库根目录执行。

### 准备本地模型

此脚本会拉取/准备：

- llama.cpp
- Qwen3-4B GGUF
- SenseVoice ASR
- vits-melo TTS

```powershell
.\scripts\setup_stackchan_local_inference_windows.ps1
```

可选 GPU 后端：

```powershell
.\scripts\setup_stackchan_local_inference_windows.ps1 -GpuBackend vulkan
```

或 CUDA：

```powershell
.\scripts\setup_stackchan_local_inference_windows.ps1 -GpuBackend cuda
```

### 启动本地模型

```powershell
.\scripts\start_stackchan_local_inference_windows.ps1
```

### 启动 Bridge

纯本地模式：

```powershell
$env:STACKCHAN_ENV_FILE="$PWD\.run\stackchan-nanobot-local.env"
.\scripts\start_stackchan_nanobot_windows.ps1 -RestartBridge
```

云端模式：

```powershell
$env:ZHIPU_API_KEY="your-local-key"
$env:STACKCHAN_ENV_FILE="$PWD\.run\stackchan-nanobot-glm.env"
.\scripts\start_stackchan_nanobot_windows.ps1 -RestartBridge
```

如果自动识别的 Windows 局域网 IP 不对，手动指定：

```powershell
.\scripts\start_stackchan_nanobot_windows.ps1 -PublicHost 192.168.18.6 -RestartBridge
```

### 停止 Windows 服务

```powershell
.\scripts\stop_stackchan_nanobot_windows.ps1
.\scripts\stop_stackchan_local_inference_windows.ps1
```

## Lightsail / 公网服务器部署

推荐最小可行方案：

```text
StackChan -> Lightsail Bridge -> GLM 云端 ASR/TTS/LLM
```

这样 Lightsail 不需要跑本地大模型，稳定性更好。

### 服务器要求

- 开放公网 TCP `12800`
- 不要公网开放 `12801`、`18080`、`18081`
- 低配 Lightsail 不建议跑本地 LLM；本地 LLM 至少建议 8GB RAM 起步

### 启动 Bridge

使用公网 IP 或域名：

```bash
cd /path/to/stackchan
export ZHIPU_API_KEY="your-local-key"
STACKCHAN_PUBLIC_HOST="<your-public-ip-or-domain>" \
STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot-glm.env" \
  ./scripts/start_stackchan_nanobot_hotspot.sh
```

StackChan 的 OTA 地址设置为：

```text
http://<your-public-ip-or-domain>:12800/xiaozhi/ota/
```

Bridge 会返回：

```text
ws://<your-public-ip-or-domain>:12800/ws
```

### 大陆访问检查

在与 StackChan 同网络的电脑或手机上验证：

```bash
curl --connect-timeout 5 --max-time 10 -v \
  http://<your-public-ip-or-domain>:12800/health
```

能返回 `{"ok":true,...}`，StackChan 理论上也能连接。

更稳定的公网方式是用 Nginx/Caddy 在 `443` 上反代：

```text
https://<domain>/xiaozhi/ota/
wss://<domain>/ws
```

## 常用验证

查看 Bridge 状态：

```bash
curl --noproxy '*' http://127.0.0.1:12800/health
```

查看日志：

```bash
tail -f .run/stackchan-bridge.log .run/mdns-alias.log
```

串口启动诊断：

```bash
cd /home/yunhao/github/stackchan
sudo .venv-nanobot/bin/python scripts/diagnose_stackchan_serial.py \
  --port /dev/ttyACM0 --seconds 20
```

读 coredump：

```bash
cd /home/yunhao/github/stackchan
./scripts/read_stackchan_coredump.sh
```

## 交互测试指令

等待屏幕显示：

```text
Nanobot 已就绪，请说唤醒词
```

然后说唤醒词，再测试：

```text
设置一个20秒倒计时
开始秒表
停止秒表
开始25分钟专注
添加待办事项，晚上八点吃饭
列出我的待办事项
删除所有待办事项
把灯光改成红色
向左转头10度
跳个舞
```

## 当前可用本地能力

通过 MCP / fast action 暴露给 Nanobot 的能力包括：

- `self.timer.start/list/pause/resume/cancel`
- `self.stopwatch.start/stop/reset/status`
- `self.focus.start/stop/status`
- `self.todo.add/list/delete/clear`
- `self.robot.get_head_angles`
- `self.robot.set_head_angles`
- `self.robot.set_led_color`
- `self.robot.dance`
- `self.robot.stop_dance`
- `self.audio_speaker.set_volume`
- `self.screen.set_brightness`
- `self.screen.set_theme`
- `self.get_device_status`
- `self.get_system_info`

高风险能力默认不暴露给模型或需要确认，例如摄像头、重启、升级、网络配置。

## 换唤醒词

唤醒词是固件侧 WakeNet 模型能力，不是简单改 prompt。流程：

1. 确认固件内已有该 WakeNet 模型。
2. 修改固件 WakeNet 配置。
3. `idf.py build`
4. 刷固件。

当前测试更稳定的唤醒词是：

```text
你好小智
```

如果切换到 Hi Kira，需要确认当前固件资源里已有对应 WakeNet 模型，否则不能直接生效。

## 提交与安全检查

外层仓库使用 `.git-local`：

```bash
git --git-dir=.git-local --work-tree=. status --short
GIT_DIR=.git-local GIT_WORK_TREE=. python3 scripts/check_no_secrets.py --staged
```

固件子仓库：

```bash
git -C StackChan status --short
```

不要提交：

- API Key
- `.run/`
- `models/`
- `.local-runtime/`
- ESP-IDF
- firmware build 产物

