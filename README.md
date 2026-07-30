# KamlPro

AI 智能网关平台。前端 ↔ KamlPro ↔ 后端服务（ComfyUI / TTS / 云端API / 本地LLM）。

核心原则：**双轨异步分流调度 + 统一 WebSocket 接口**。云端API支持并发线程池与流式直连，本地服务（LLM/TTS/ComfyUI）统一采用 poll 轮询模式，Kalm 后台轮询伪装成 WebSocket 对调用者透明。

## 快速开始

### 方式一：使用启动脚本（推荐）

```powershell
# 双击运行，或使用 launcher.bat（推荐 Windows 用户）
launcher.bat

# 支持命令行参数
launcher.bat --port 8080 --host 0.0.0.0
launcher.bat --install-deps   # 自动安装依赖
```

`launcher.bat` 会自动检测 Python 环境（conda / 虚拟环境 / 系统 Python），检查并安装依赖，然后启动服务。

### 方式二：手动启动

```powershell
pip install -r requirements.txt
python main.py
```

服务默认运行在 `http://localhost:7000`。

## 双轨异步分流调度

KamlPro 采用**双轨调度架构**，根据模型配置自动路由：

| 轨道 | 后端类型 | 调度方式 | 流式支持 |
|------|---------|---------|---------|
| **本地 GPU 轨道** | Ollama 等本地推理 | FIFO 串行队列 | ❌ 仅 poll 模式 |
| **云端 API 轨道** | DeepSeek / OpenAI 等 | ThreadPoolExecutor 并发 | ✅ 支持流式直连 |

### 统一 WebSocket 接口

所有后端服务（ComfyUI / TTS / 本地LLM）对调用者暴露**统一的 WebSocket 接口**：

- 后端实际采用 HTTP poll 轮询执行
- Kalm 后台轮询后端，将进度/结果通过 WebSocket **伪装成实时推送**发送给调用者
- 调用者无需关心后端是 ws 还是 poll，统一使用 `WS /interface/tasks/{id}/ws` 获取进度

### 云端 API 2×2 忙闲自适应矩阵

| 响应模式 \ 队列状态 | 闲时直连 (Idle) | 忙时排队 (Busy) |
|---------------------|-----------------|-----------------|
| **流式 (Stream)** | 直接返回 HTTP 流 (`GET /tasks/{id}/stream`) | WS 监听 → 收到 `task_running` → 建立 HTTP 流 |
| **非流式 (Poll)** | 轮询 `GET /tasks/{id}/status` 获取结果 | WS 监听 → 轮询获取结果 |

提交任务时响应 `can_stream_immediately` 标志：
- `true`：槽位空闲，可直接拉取流式响应
- `false`：槽位已满，任务排队，需通过 WebSocket 监听出队信号

## API 端点

### 任务提交

```http
POST /interface/tasks/submit
Content-Type: application/json

{
  "task_type": "comfyui",       // comfyui | llm | tts | subtitle
  "payload": { ... }            // 透传给后端，不同任务类型不同
}
```

返回：
```json
{
  "task_id": "xxx",
  "status": "queued",
  "track_mode": "ws",
  "hint": "Connect to ws://host:port/interface/tasks/xxx/ws for real-time progress"
}
```
`track_mode` 标记告诉前端用什么方式收结果：`ws` → WebSocket、`poll` → 轮询、`stream` → NDJSON 流。

### 任务状态查询

```http
GET /interface/tasks/{task_id}/status
```

### 任务取消与中断

```http
POST /interface/tasks/{task_id}/cancel
```

返回：
```json
{
  "code": 200,
  "message": "Task be7fb062-8f1b-41ee-a8ba-4672e8ab5a3c cancel signal sent."
}
```
此接口会立即向相关后端发送物理中断请求，并中止调度/执行线程的继续运行，以节省 GPU 算力和云端 API 费用。

### 队列广播 WebSocket（推荐）

**一次连接，全程通知，无需轮询。**

```
ws://localhost:7000/interface/queue/ws
```

收到三种消息：`task_enqueued`（入队）、`task_started`（开始执行）、`task_completed`（完成，含下一任务信息）。客户端按 `task_id` 过滤自己的任务。

```javascript
const queueWs = new WebSocket("ws://localhost:7000/interface/queue/ws");
queueWs.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.task_id !== myTaskId) return; // 不是我的，忽略
  // msg.type: "task_enqueued" | "task_started" | "task_completed"
};
// 每 30 秒发 ping 保活
setInterval(() => { queueWs.send("ping"); }, 30000);
```

### 任务 WebSocket 进度

**所有任务类型统一使用 WebSocket 获取实时进度**。Kalm 内部将不同后端协议统一为 WebSocket 推送：

```javascript
const taskWs = new WebSocket(`ws://localhost:7000/interface/tasks/${taskId}/ws`);
taskWs.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // ComfyUI: 透传原生 ws 协议并下发平滑进度 {type: "overall_progress", data: {percent: 65, message: "..."}}
  // TTS / 本地LLM: Kalm 后台 poll 轮询转发，伪装成 ws 推送 {status: "running", ...}
  // 云端API排队: 等待 {type: "task_running"} 信号后切换到 HTTP 流
};
```

> 详细说明见 `docs/KamlPro_混合流式与并发调度机制设计文档.md`

### LLM 任务提交

通过统一任务提交端点提交 LLM 任务，`task_type` 区分流式与非流式：

```http
POST /interface/tasks/submit
Content-Type: application/json

{
  "task_type": "llm_streaming",   // 流式（仅云端API） / "llm"（非流式/poll）
  "payload": {
    "model": "deepseek-v4-flash",
    "prompt": "你好",
    "options": {"temperature": 0.7, "max_tokens": 100}
  }
}
```

#### 云端 API — 流式任务 (`llm_streaming`)

提交后根据并发槽位返回不同状态：

**闲时直连（槽位未满）：**
```json
{
  "task_id": "xxx",
  "status": "running",
  "can_stream_immediately": true
}
```
客户端直接发起 HTTP 流式请求：
```http
GET /interface/tasks/{task_id}/stream
```
返回 `application/x-ndjson` 流，逐 token 推送。支持 `reasoning_content` 思考流，自动以 `<think>...</think>` 标签包装。

**忙时排队（槽位已满）：**
```json
{
  "task_id": "xxx",
  "status": "queued",
  "can_stream_immediately": false
}
```
客户端通过 WebSocket 监听出队信号：
```javascript
const ws = new WebSocket(`ws://localhost:7000/interface/tasks/${taskId}/ws`);
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'task_running') {
    // 收到起跑信号，立刻建立 HTTP 流式连接
    fetch(`/interface/tasks/${taskId}/stream`)
      .then(r => r.body.pipeThrough(new TextDecoderStream())
        .pipeTo(new WritableStream({ write(chunk) { console.log(chunk); } })));
  }
};
```

#### 本地 LLM — 非流式任务 (`llm`)

本地 LLM（如 Ollama）**仅支持 poll 模式**，使用方式与 TTS 完全一致：

```http
POST /interface/tasks/submit
Content-Type: application/json

{
  "task_type": "llm",
  "payload": {
    "model": "qwen3:14b",
    "prompt": "你好"
  }
}
```

通过统一 WebSocket 接口获取进度（Kalm 后台轮询转发，对调用者透明）：
```javascript
const ws = new WebSocket(`ws://localhost:7000/interface/tasks/${taskId}/ws`);
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // 等待 task_complete 后从 msg 中获取生成结果
};
```

或轮询状态：
```http
GET /interface/tasks/{task_id}/status
```

### 模型列表

```http
GET /interface/llm/models
```

### TTS 语音 + 字幕（一条龙）

```http
POST /interface/tasks/submit
Content-Type: application/json

{ "task_type": "tts", "payload": { "path": "/v1.5/generate", "text": "...", "speaker": "...", "generate_subtitle": true } }
```

完成后通过静态端点下载产物：
```http
GET /file/tts/static/{backend_task_id}/{backend_task_id}.wav   # 音频
GET /file/tts/static/{backend_task_id}/{backend_task_id}.srt   # 字幕
```

### 字幕独立生成（已有音频）

```http
POST /interface/tasks/submit
Content-Type: multipart/form-data

task_type: subtitle
text: 文稿内容
audio_file: <音频文件>
```

产物下载：`GET /file/tts/static/{backend_task_id}/subtitle.srt`

### TTS 发音人列表

获取 TTS 后端支持的所有发音人（自动代理到后端服务，透传认证 token）：

```http
GET /interface/tts/speakers
```

返回：
```json
{
  "code": 200,
  "data": {
    "categories": [
      {
        "name": "中文男声",
        "speakers": ["speaker_001", "speaker_002"]
      }
    ]
  }
}
```

### 文件访问

```http
GET /file/{service_name}/{file_path}
```

## 后端服务

| 服务 | 端口 | 协议 | 调度轨道 |
|------|------|------|---------|
| ComfyUI | 7001 | HTTP + WebSocket | 本地 GPU（FIFO） |
| Ollama | 11434 | HTTP | 本地 GPU（FIFO, poll 模式） |
| TTS | 8001 | HTTP | 本地 GPU（FIFO, poll 模式） |
| 云端API | 依配置 | HTTP | 云端 API（并发线程池） |

## 测试

`test/20260527/` 目录提供完整的集成测试用例，覆盖本地/云端 LLM、TTS、ComfyUI 等所有任务类型。

### LLM 测试矩阵

#### WebSocket 自适应接入（最佳实践）
根据任务提交返回的 `track_mode` 智能路由到 WebSocket，支持配置变更时自动降级。

| 用例 | 模型 | 说明 |
|------|------|------|
| `test_llm_local_ws.py` | Ollama (gemma3:12b) | 本地大模型自适应 WS |
| `test_llm_cloud_ws.py` | DeepSeek (v4-flash) | 云端大模型自适应 WS |

#### 纯 HTTP REST 降级（兜底方案）
强行忽略 `track_mode: ws` 推荐，直接通过 HTTP `/status` 轮询获取结果。

| 用例 | 模型 | 说明 |
|------|------|------|
| `test_llm_local_poll.py` | Ollama (gemma3:12b) | 本地大模型纯 HTTP 轮询 |
| `test_llm_cloud_poll.py` | DeepSeek (v4-flash) | 云端大模型纯 HTTP 轮询 |

#### 云端流式输出（仅云端 API）
SSE 流式逐 Token 推送，支持闲时直连/忙时排队 + TTFT（首字延迟）测量。

| 用例 | 模型 | 说明 |
|------|------|------|
| `test_llm_cloud_stream.py` | DeepSeek (v4-flash) | SSE 流式 + TTFT 展示 |

### 其他测试

```powershell
# ComfyUI 图像/视频生成
python test/20260527/test_comfyui_image.py
python test/20260527/test_comfyui_video.py
# TTS + 字幕
python test/20260527/test_hybrid_audio_ws.py
python test/20260527/test_hybrid_audio_poll.py
```

产物输出到 `test/20260527/outputs/` 下对应子目录。

### ComfyUI 封面生成测试 (`test/20260605/`)

```powershell
# 封面三尺寸批量生成测试（验证智能进度拦截器 + 多输出节点）
python test/20260605/test_cover3sizes.py
```

验证要点：
- **智能进度拦截器**：全局进度条严格单调递增（无回跳），底层节点进度被平滑聚合
- **多输出节点**：工作流包含 3 个 SaveImage 节点 (`264, 266, 296`)，全部输出正确捕获
- 详细分析见 `test/20260605/ComfyUI_Overall_Progress_Report.md`

产物输出到 `test/20260605/outputs/` 下对应子目录。

### 任务取消与物理中断测试 (`test/20260620/`)

提供在后台生成中途执行取消操作的压力与正常流程测试用例。支持 ComfyUI、TTS，并能自动将成功的生成物拉取下载至 `test/20260620/outputs/`。

| 用例 | 类型 | 说明 |
|------|------|------|
| `test_comfyui_single.py` | ComfyUI | 单任务生成/取消及生成物自动下载保存 |
| `test_comfyui_multiple.py` | ComfyUI | 多任务批量排队生成与并行/排队取消 |
| `test_tts_single.py` | TTS | 单任务语音配音生成与中途取消 |
| `test_tts_multiple.py` | TTS | 多任务配音并发排队及取消测试 |

运行示例：
```powershell
python test/20260620/test_comfyui_single.py
```

产物输出到 `test/20260620/outputs/` 下对应子目录。

## 配置

- `config.yaml` — API 配置、任务映射、LLM 本地模型
- `services.yaml` — 后端子进程定义
- 配置值支持 `${ENV_VAR}` 语法引用环境变量

## 资源释放与批量提交

Kalm 每个任务完成后会检查队列，决定是否释放 GPU 资源：

| 队列状态 | 行为 |
|---|---|
| 有同类型任务排队 | 不释放，模型常驻复用 |
| 有不同类型任务 | 释放，为下一任务腾显存 |
| 队列为空 | 释放，回收显存 |

**重要**：释放决策只看队列里**已入队**的任务。如果前端串行提交（等上一个完成才提交下一个），每次完成时队列都是空的，必然触发释放 → 重新加载模型。

要避免频繁释放重载，**批量提交同类型任务**，让它们在队列中排队：
```
A入队 → B入队 → C入队  （A完成后 B 已在队列，跳过释放，直接复用）
```

而不是：
```
A入队 → 等A完成 → B入队 → 等B完成 → C入队  （每次完成队列都空，每次都释放+重载）
```

## 项目结构

```
src/
├── api/
│   ├── main.py              # FastAPI 应用工厂 + 生命周期
│   └── routes/
│       ├── tasks.py         # 任务提交 + 状态查询
│       ├── ws_proxy.py      # WebSocket 进度透传（含兜底轮询）
│       ├── queue_ws.py      # 队列广播 WebSocket
│       ├── stream_proxy.py  # HTTP 流式透传 (NDJSON)
│       ├── llm.py           # LLM 端点 (模型列表/普通生成)
│       ├── speaker.py       # TTS 发音人列表代理
│       ├── file_proxy.py    # 通用文件代理
│       └── system.py        # 健康检查
├── core/
│   ├── scheduler.py         # FIFO 队列调度器 + 智能资源释放
│   ├── task_manager.py      # 内存任务状态存储
│   ├── ws_manager.py        # WS 连接管理 (per-task + 队列广播)
│   ├── service_controller.py # 后端服务生命周期管理
│   ├── response.py          # API 响应辅助
│   ├── security.py          # Token 认证（支持浏览器 WebSocket subprotocol）
│   ├── providers/           # Provider 模块化架构（按类型分类）
│   │   ├── llm/             # LLM 提供商
│   │   │   ├── base.py      # LLM 基类
│   │   │   ├── manager.py   # LLM 提供商管理器
│   │   │   ├── ollama.py    # Ollama 本地推理
│   │   │   └── openai_compatible.py # OpenAI 兼容接口
│   │   ├── audio/           # 音频提供商
│   │   │   ├── base.py      # 音频基类
│   │   │   ├── manager.py   # 音频提供商管理器
│   │   │   └── index_tts.py # IndexTTS 语音合成
│   │   └── image/           # 图像提供商
│   │       ├── base.py      # 图像基类
│   │       ├── manager.py   # 图像提供商管理器
│   │       └── comfyui.py   # ComfyUI 图像生成（支持多输出节点）
│   └── executors/
│       ├── llm.py           # LLM 生成 (普通 + 流式)
│       ├── tts.py           # TTS 语音生成（支持 generate_subtitle）
│       ├── subtitle.py      # 字幕生成（文件上传）
│       └── image.py         # 图像生成
├── logic/
│   ├── logger.py            # 日志
│   ├── yaml_config_loader.py # YAML 配置加载
│   ├── task_cleanup.py      # 定期清理过期任务
│   ├── comfy_progress_interceptor.py # ComfyUI 智能进度拦截器（单调递增平滑进度条）
│   └── platform_log_filter.py # 平台日志过滤
└── utils/
    ├── json_helper.py       # JSON 工具函数
    └── path_helper.py       # 路径翻译（支持非路径字段排除）
```

## 最近更新

### v20260730
- **服务控制器 cmd 类型扩展**: `cmd` 类型服务新增 `path` 字段支持，可通过 `cmd /c` 直接启动 `.bat` 批处理文件，保留原有 `command` 字段的 `powershell -Command` 执行方式
- **ComfyUI 启动路径迁移**: Windows 平台改用 `KamlPro-service/launcher.bat` 替代旧版 `run.ps1`，平台类型由 `ps1` 改为 `cmd`；Linux 平台同步更新为 `KamlPro-service/launcher.sh`
- **日志过滤优化**: 新增 403 屏蔽规则，过滤第三方/无效客户端向非目标子服务发起 WebSocket 连接时产生的 `403 Forbidden` 和 `connection rejected/closed` 刷屏日志

### v20260620
- **全链路任务停止与物理中断机制**：新增 `POST /interface/tasks/{task_id}/cancel` 接口，支持在任务执行过程中中途取消。
- **ComfyUI 双重物理中断**：同时从 API 路由层与执行循环层（主动+被动双保险）向 ComfyUI 后端发送 `/queue` 任务删除与 `/interrupt` 中断请求，防止由于 Websocket 阻塞导致的中断延迟。
- **多后端物理中断集成**：LLM 大模型自动关闭连接以停止模型推理计费，TTS 语音合成自动退出状态轮询。
- **日志净化与 Traceback 屏蔽**：屏蔽用户手动中止时产生的长段异常 Traceback，只在控制台输出友好提示 `任务已被用户手动停止`，确保日志清爽。
- **20260620 测试套件**：添加了针对 ComfyUI/TTS 的单任务、多任务并发与排队物理取消测试，支持生成物自动下载并归档至 `test/20260620/outputs/`。

### v20260605
- **ComfyUI 智能全局进度条**: 拦截并平滑化 ComfyUI 底层反复横跳的 WebSocket 进度，提供严格单调递增的 `overall_progress` 推送，实现 100% 零侵害后向兼容的完美商业级体验
- **ComfyUI 多输出节点支持**: `final_output_node_id` 支持单节点字符串 `"9"` 或多节点数组 `["264","266","296"]`，自动过滤输出
- **WebSocket 浏览器认证**: 支持通过 `Sec-WebSocket-Protocol` 头传递 token（格式: `['kalm-auth', 'token']`），解决浏览器 WebSocket 认证 subprotocol 报错问题
- **TTS 发音人代理端点**: 新增 `GET /interface/tts/speakers`，前端无需知道后端 TTS 地址
- **llm_streaming 配置启用**: `configs/tasks.yaml` 中启用云端流式任务配置，`service: "cloud"` 占位防止本地误调用
- **启动脚本升级**: `launcher.bat` 替代 `run.bat`，自动检测 conda/虚拟环境/系统 Python，支持 `--install-deps` 参数
- **ComfyUI WS 消息处理优化**: 分离 `executing` 和 `progress` 消息处理，支持多输出工作流正确检测执行完成
