# KamlPro

AI 智能网关平台。前端 ↔ KamlPro ↔ 后端服务（ComfyUI / TTS / 云端API / 本地LLM）。

核心原则：**双轨异步分流调度 + 统一 WebSocket 接口**。云端API支持并发线程池与流式直连，本地服务（LLM/TTS/ComfyUI）统一采用 poll 轮询模式，Kalm 后台轮询伪装成 WebSocket 对调用者透明。

## 快速开始

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
  // ComfyUI: 透传原生 ws 协议 {type: "executing", ...}
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

```powershell
# 独立字幕
python test/test_subtitle.py
# TTS + 字幕一条龙
python test/test_tts_subtitle.py
# 交叉并发测试
python test/test_cross.py
```

产物输出到 `test/output/` 下对应子目录。

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
│       ├── file_proxy.py    # 通用文件代理
│       └── system.py        # 健康检查
├── core/
│   ├── scheduler.py         # FIFO 队列调度器 + 智能资源释放
│   ├── task_manager.py      # 内存任务状态存储
│   ├── ws_manager.py        # WS 连接管理 (per-task + 队列广播)
│   ├── service_controller.py # 后端服务生命周期管理
│   ├── response.py          # API 响应辅助
│   ├── security.py          # Token 认证
│   └── executors/
│       ├── image.py         # ComfyUI 图像生成
│       ├── llm.py           # LLM 生成 (普通 + 流式)
│       ├── tts.py           # TTS 语音生成（支持 generate_subtitle）
│       └── subtitle.py      # 字幕生成（文件上传）
└── logic/
    ├── logger.py            # 日志
    ├── yaml_config_loader.py # YAML 配置加载
    └── task_cleanup.py      # 定期清理过期任务
```
