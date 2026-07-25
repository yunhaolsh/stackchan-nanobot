# StackChan + Nanobot + GLM 实现计划

状态：第一阶段离线实现、固件构建和真机刷写完成，等待 GLM Key与真机端到端验收  
更新日期：2026-07-24  
工作目录：`/home/yunhao/github/stackchan`

## 1. 目标

在当前 M5Stack StackChan 上实现以 Nanobot 为 Agent 后端的完整语音交互闭环：

- 用户通过 StackChan 麦克风说话。
- 云端 ASR 将音频转成文字。
- Nanobot维护会话、记忆、Skill 和 Tool Call。
- GLM理解意图并选择设备 MCP Tool。
- StackChan执行灯光、转头、音量、屏幕、计时器等本地能力。
- 云端 TTS生成语音并由 StackChan播放。
- 第一阶段在本机完成闭环；第二阶段迁移到公网 VPS，使设备不依赖开发电脑常开。

默认模型栈使用国内智谱开放平台：

| 能力 | 默认模型 |
|---|---|
| 对话与工具调用 | `glm-4.7-flash` |
| 语音识别 | `glm-asr-2512` |
| 语音合成 | `glm-tts` |
| 摄像头视觉理解 | `glm-4.6v-flash` |

Chat Provider允许通过配置切换为 DeepSeek；切换后 ASR/TTS仍默认使用 GLM。

## 2. 总体架构

```text
StackChan ESP32-S3
  |  Xiaozhi WebSocket v3: Opus + JSON + MCP JSON-RPC
  v
StackChan Bridge
  |-- Device Gateway: 设备连接、认证、会话、音频上下行
  |-- Media Gateway: ASR/TTS格式转换和调用
  |-- Capability Gateway: MCP发现、调用、超时、权限和结果关联
  v
Nanobot Agent Runtime
  |-- 多轮会话、记忆、Skill
  |-- Tool Router与Tool Call循环
  v
GLM / DeepSeek
```

职责边界：

- StackChan只运行设备固件、本地能力和可靠的 RTC/NVS任务。
- Bridge必须存在，但可运行在本机、NAS或云服务器，不必运行在 ESP32-S3。
- Nanobot是实际 Agent后端；Bridge不得绕过 Nanobot自行完成模型 Tool Call循环。
- GLM只做 ASR、语义理解、Tool选择和 TTS，不直接连接设备。
- API Key只保存在 Bridge/Nanobot主机环境变量中，不写入固件或仓库。

## 3. 当前基础与缺口

当前已经具备：

- StackChan固件 v1.4.3 可正常构建和刷机。
- 固件通过 `stackchan-nanobot.local` 访问本机 Bridge。
- Xiaozhi WebSocket v3音频上下行和 OTA配置入口。
- Bridge已能接收 Opus帧、调用 ASR/Nanobot/TTS并回传音频。
- 固件已注册状态、音量、亮度、主题、摄像头、灯光、头部角度和 Reminder MCP Tools。
- Nanobot v0.2.2支持 OpenAI兼容 Provider和 MCP Client。

实施前缺口（现已完成）：

- 当前 Bridge收到 `type=mcp` 后仅记录日志，没有完成 MCP请求/响应关联。
- Nanobot尚未注册 StackChan MCP Server，真实 Tool Call没有打通。
- 默认语音栈仍需从 Gemini切换为国内智谱全栈。
- Reminder只是内存服务，不是支持持久化和完整界面的多计时器 App。
- 舞蹈等部分原生能力尚未注册为 AI可调用 MCP Tool。
- 本地启动方式存在环境和地址耦合，尚无 Docker/VPS部署。

## 4. 第一阶段：本机完整闭环

### 4.1 GLM Provider与Nanobot

- 实现国内智谱 Chat、ASR、TTS和 Vision适配器。
- Chat默认 `glm-4.7-flash`，关闭深度思考以降低语音控制延迟。
- ASR接收最长30秒的设备音频，将 Opus转换为智谱支持的 WAV后上传。
- TTS获取 WAV/PCM后编码为 StackChan需要的 Opus帧。
- 提供 `STACKCHAN_CHAT_PROVIDER=glm|deepseek` 运行时切换。
- Nanobot保持一个长期实例和稳定的设备会话键，避免每轮重建 MCP连接。
- 所有密钥从本地 `.env` 读取；示例配置只包含变量名和非敏感默认值。

### 4.2 设备 MCP发现与调用

设备连接后由 Bridge执行：

1. 发送 MCP `initialize`。
2. 分页调用 `tools/list`，分别获取普通和 user-only工具。
3. 缓存工具名、描述、参数 Schema、风险级别和在线状态。
4. 为每个 JSON-RPC请求生成 ID并关联响应。
5. 处理调用超时、设备断线、重复响应、错误响应和重新发现。

Bridge向 Nanobot提供标准 Streamable HTTP MCP入口。Nanobot通过该入口获取允许暴露的设备 Tools；GLM产生 `tool_calls` 后，调用沿以下路径执行：

```text
GLM tool_call
 -> Nanobot MCP Client
 -> Bridge Capability Gateway
 -> StackChan tools/call
 -> 设备本地 C++ 回调
 -> MCP result
 -> Nanobot
 -> GLM最终回答
```

### 4.3 Tool Router与能力权限

Tool筛选运行在 Bridge/Nanobot主机，不运行在 ESP32-S3：

- 保留不超过10个常驻核心工具。
- 根据命名空间、中文关键词、同义词和会话上下文选择候选领域。
- 单轮最多向模型提交20个完整 Tool Schema。
- 当前工具较少时可提交全部低风险工具；扩展到100+ Tools时使用 Top-K候选。
- 候选 ToolRegistry通过 Nanobot每轮 `tools` 参数传入，不修改 `.venv` 中的 Nanobot源码。

权限策略：

| 等级 | 工具示例 | 策略 |
|---|---|---|
| 自动执行 | 状态、灯光、转头、音量、亮度、主题、计时器 | Schema和设备状态校验后执行 |
| 需要确认 | 摄像头拍照、视觉分析 | 必须收到用户明确语音确认 |
| 默认禁止 | 网络重配、固件升级、重启、任意URL预览、资源地址修改、屏幕上传 | 不注册给模型 |

模型决定“想做什么”，Bridge决定“是否允许”，设备固件决定“当前是否能安全执行”。

### 4.4 原生多计时器 App

新增 `TIMER` Mooncake App和共享计时服务：

- 最多8个命名计时器。
- 支持创建、列表、暂停、继续和取消。
- 运行中使用 RTC绝对截止时间；暂停时保存剩余秒数。
- 使用 NVS持久化，设备重启后恢复未到期计时器。
- 共享服务放在 HAL公共层，在桌面 App关闭和 AI Agent模式中都继续运行。
- 桌面提供 `TIMER` 图标和完整列表。
- AI头像页面显示最近到期计时器角标，点击进入详情。
- 到期时显示全屏提醒并播放本地提示音。

新增 MCP Tools：

```text
self.timer.start
self.timer.list
self.timer.pause
self.timer.resume
self.timer.cancel
```

现有以下接口保留为兼容别名：

```text
self.robot.create_reminder
self.robot.get_reminders
self.robot.stop_reminder
```

### 4.5 其他原生能力

- 保留并验证设备状态、音量、亮度、主题、摄像头、灯光和头部角度 Tools。
- 增加 `self.robot.dance` 与停止动作 Tool，复用现有 DanceModifier。
- 摄像头 Tool在用户确认后拍照，将图片发送给 Bridge Vision接口，由 GLM视觉模型解释。
- 舵机角度、速度和灯光值在设备端执行最终范围限制。

### 4.6 第一阶段测试与验收

自动测试：

- WebSocket hello、Opus帧、listen状态和 TTS回传。
- MCP initialize、分页 list、call、result、error、timeout和断线。
- Tool Router候选选择、数量上限和多意图合并。
- 普通、确认、禁止三类权限。
- GLM Chat/ASR/TTS/Vision请求与响应格式；外部测试必须显式开启。
- 多计时器创建、暂停、恢复、取消、NVS序列化、RTC变化和重启恢复。
- ESP-IDF完整构建和现有固件 Host Tests。

真机验收：

1. StackChan成功连接 Bridge。
2. Bridge收到真实 Opus音频。
3. GLM-ASR输出真实转写。
4. Nanobot收到转写并产生文本回答或 Tool Call。
5. 灯光、转头和计时器至少各执行成功一次。
6. MCP返回真实设备执行结果。
7. GLM-TTS生成音频并由 StackChan播放。
8. 设备重启后计时器恢复。
9. 摄像头操作先确认，拒绝时不得拍照。

完成证据必须来自 Bridge日志、固件串口日志和设备实际行为，不能只用单元测试代替。

## 5. 第二阶段：公网 VPS常驻部署

第一阶段通过后再实施：

- 同时提供 Docker Compose标准部署和原生 Python/systemd部署。
- VPS运行 Bridge、Nanobot、MCP网关、Nginx和证书续期服务。
- Nanobot工作区、会话和运行配置使用持久卷。
- 公网只开放 `80/443`；管理入口限制来源地址。
- 当前没有域名，使用 Let’s Encrypt短期公网 IP证书。
- Certbot要求5.4或更高版本，使用 `shortlived` Profile，并通过定时续期和 deploy hook重载 Nginx。
- 设备入口：

```text
https://<PUBLIC_IP>/xiaozhi/ota/<bootstrap-token>/
wss://<PUBLIC_IP>/ws
```

- OTA入口同时校验随机 bootstrap token、Device-Id和 Client-Id。
- WebSocket使用独立 Bearer Token，不复用 GLM API Key。
- 本机验收完成后，为公网入口重新构建并刷入一次固件。
- 模型、Prompt、Skill和云端代码更新不需要再次刷机。
- 公网 IP改变仍需更新固件；正式 AIBuddy产品必须使用固定域名或运行时可配置入口。

第二阶段验收：设备关闭开发电脑连接后，在其他 Wi-Fi网络中完成语音问答、设备 Tool Call、计时器、TTS播放和断线重连。

## 6. 与 AIBuddy产品架构的映射

| StackChan Demo | AIBuddy产品 |
|---|---|
| StackChan固件 | AIBuddy RTOS + Device SDK |
| 固件 MCP Tools | Capability Runtime |
| Bridge WebSocket | Device/Media Gateway |
| Bridge MCP代理 | Capability Gateway |
| Nanobot | Agent Runtime原型 |
| GLM | 内部或公有云模型 |

AIBuddy产品中，固件发布时生成版本化 Capability Manifest；设备上线只上报型号、固件版本、能力哈希和动态可用状态。完整 Schema由云端 Capability Registry保存，每轮 Tool筛选在 Agent Runtime进行。

## 7. 安全与实施边界

- 当前第一版只服务一台 StackChan，不实现多租户和设备批量管理。
- 不提交任何真实 API Key、VPS Token或设备密钥。
- 用户此前公开过的 Gemini Key需要作废；默认方案不再依赖该 Key。
- 正常刷机不主动擦除 NVS；修改分区表前必须先备份和评估。
- 所有硬件动作必须有设备端安全限制，不能只依赖模型提示词。
- 物理刷机或设备操作需要用户配合时，软件工作继续推进到只剩明确物理步骤为止。
- 第一阶段未取得完整真机证据前，不得宣称目标完成。

## 8. 实施顺序

- [x] 建立依赖清单、配置模板和可重复启动基线。
- [x] 完成国内智谱 Chat/ASR/TTS/Vision适配。
- [x] 完成设备 MCP发现、调用和标准 Nanobot MCP入口。
- [x] 完成 Tool Router、权限和语音确认流程。
- [x] 实现多计时器共享服务、App、AI角标和 MCP Tools。
- [x] 补充舞蹈及其他原生能力并完成固件构建。
- [x] 将第一阶段固件刷入真机并校验所有写入分区。
- [ ] 完成第一阶段真机端到端验收。
- [ ] 生成 Docker与原生 VPS部署包。
- [ ] 切换公网入口并完成第二阶段跨网络验收。
