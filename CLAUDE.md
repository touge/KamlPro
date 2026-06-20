# CLAUDE.md

## 项目定位

Kalm 是纯 AI 任务中转控制站。前端 ↔ Kalm ↔ 后端服务（ComfyUI / TTS / Ollama）。

核心原则：**透明代理 + FIFO 排队**。后端用什么协议，Kalm 对前端就暴露什么协议。只做排队和透传，不做协议转换。

## 架构概要

```
POST /interface/tasks/submit      → scheduler (FIFO 队列) → executor → 后端服务
GET  /interface/tasks/{id}/status → TaskManager (内存状态)
POST /interface/tasks/{id}/cancel → TaskManager (标记取消) & 各后端物理中断
WS   /interface/tasks/{id}/ws     → ws_manager (实时推送，ComfyUI 原生 WS 协议)
WS   /interface/queue/ws          → ws_manager 队列广播 (任务生命周期通知)
POST /interface/llm/generate-stream → 排队 → StreamingResponse (NDJSON 透传)
GET  /file/{service}/{path}      → httpx → 后端 → 流式返回前端
```

- 调度器：`src/core/scheduler.py`，单例单线程，`queue.Queue` + 服务生命周期。支持普通任务和流式任务（带 `output_queue`/`started_event`）。支持智能资源释放（详见下方"资源释放策略"）。
- 执行器：`src/core/executors/*.py`，每个只做 提交→透传。普通执行器签名 `def execute(task_id, **payload)`，流式执行器签名 `def execute_stream(task_id, output_queue, **payload)`。
- 文件代理：`src/api/routes/file_proxy.py`，通用端点。`service_name` 可以是 `task_type`（如 `comfyui`），通过 `config.yaml` 的 `tasks.{task_type}.service` 映射到实际服务名（如 `ComfyUI_Windows`）。使用 requests 库同步拉取 + ThreadPoolExecutor。
- 任务状态与取消：`src/core/task_manager.py`，内存字典 + threading.Lock + TTL 自动清理。提供 `is_cancelled` 和 `cancel` 功能。
- 任务提交支持 JSON 和 multipart/form-data 两种 Content-Type（字幕任务需文件上传）。
- 服务管理：`src/core/service_controller.py`，引用计数 + 子进程启停。`_find_service_key` 大小写不敏感，精确匹配失败后模糊匹配兜底（`ComfyUI` → `ComfyUI_Windows`）。支持 `auto_start` 常驻模式（Kalm 启动时自动拉起，常驻不回收）和启动时端口强杀。
- WS 管理：`src/core/ws_manager.py`，桥接同步执行器线程 → 异步 WS 推送（`run_coroutine_threadsafe`）。支持 per-task 推送和全局队列广播（`broadcast_queue()`）。
- WS 端点：`src/api/routes/ws_proxy.py`，前端连 `/tasks/{id}/ws` 获取实时进度。内部有兜底轮询（`poll_fallback`，每 2 秒检查 TaskManager），不依赖特定后端协议的 WS 也能检测完成。
- 队列广播端点：`src/api/routes/queue_ws.py`，前端连 `/queue/ws` 接收任务生命周期通知（`task_enqueued` / `task_started` / `task_completed`）。所有连接客户端收到相同广播，按 `task_id` 自行过滤。
- 流式端点：`src/api/routes/stream_proxy.py`，前端 POST `/llm/generate-stream` 获取 NDJSON 流式响应。
- 平台伪装：`src/api/main.py` 中 `/api/jobs` 返回空任务列表，安抚部署平台的监控探活。`src/logic/platform_log_filter.py` 过滤 uvicorn 访问日志，屏蔽探活请求的日志刷屏。

## 安全认证

双层认证架构，`config.yaml` 中 `api_server.tokens` 配置驱动：

| 层 | 机制 | 适用范围 |
|---|---|---|
| HTTP 路由 | `include_router(dependencies=[Depends(require_token)])` | main.py 中对 5 个 HTTP router 统一挂载，Swagger UI 自动显示 Authorize 按钮 |
| WS 中间件 | `WebSocketAuthMiddleware` | 仅 Kalm 路径（`/interface/queue/ws`、`/interface/tasks/*`）需认证，其他 WS 路径（如平台探活 `/ws`）直接关闭码 1000，不触发认证 |

`OptionalHTTPBearer` 是 `HTTPBearer` 的子类，`request` 参数可选：WebSocket 路由无 `Request` 对象时走默认值 `None` 跳过 HTTP 认证层，交由中间件处理。

**WebSocket 认证失败反馈**：先 `accept()` 完成握手，再 `close(code=4001, reason="...")`。客户端通过关闭码 4001 区分认证失败与其他错误。

**服务名解析**：`_find_service_key` 二级查找 — 精确匹配（大小写不敏感）→ 模糊匹配兜底（`ComfyUI` 匹配 `ComfyUI_Windows`）。

详细使用指南：`API_AUTH.md`

## LLM Provider 插件体系

所有 LLM 调用路径统一经由 `src/core/providers/` 的插件化体系。**不要在 executor 中硬编码任何 LLM 地址或协议。**

```
src/core/providers/
├── base.py              <- BaseLLMProvider 抽象基类
├── manager.py           <- LLMProviderManager（pkgutil 自发现 + 单例）
├── ollama.py            <- OllamaProvider  (@register_provider("ollama"))
openai_compatible.py  <- OpenAICompatibleProvider (@register_provider("openai_compatible"))
```

**新增 Provider 三步走（零侵入）**：
1. 在 `src/core/providers/` 新建 `.py` 文件
2. 继承 `BaseLLMProvider`，实现 `generate()` + `generate_stream()`
3. 用 `@register_provider("类型名")` 装饰类

系统启动时自动发现，无需修改任何已有文件。`configs/llm_config.yaml` 中每个 provider 条目**必须**显式声明 `type` 字段，缺失则 `RuntimeError` 快速失败，严禁静默推导。

**DeepSeek 推理透传**：`OpenAICompatibleProvider` 自动检测流式帧中的 `reasoning_content` 字段，将推理过程包裹为 `<think>...</think>` 标签透明混入正常流中，无需客户端感知。

## 混合流式与并发调度

`GET /interface/llm/generate-stream` 实现**排队心跳 + 推理流式**全程 SSE 长连接：

- 排队阶段：每 500ms 发送 `{"status":"queued","position":N}` 心跳，连接不断
- 获得槽位后：无缝切换为逐 Token 推送 `{"response":"...","done":false}`
- 完成：推送 `{"response":"","done":true}`

并发上限由 `configs/llm_config.yaml` 的 `api_concurrency_limit` 控制，`asyncio.Semaphore` 实现 FIFO 公平排队。

详细架构与测试代码：`docs/KamlPro_混合流式与并发调度机制设计文档.md`

## 协议透传与统一接口原则

为了简化前端逻辑，网关采取 **“双轨制统一 WS 推送”** 理念：对需要排队的本地服务（ComfyUI 绘图、TTS 配音、Subtitle 字幕对齐、Ollama 本地大模型），网关统一通过 WS 代理伪装（`ws_proxy.py` 的后台轮询兜底）对前端暴露统一的 WebSocket 进度监听管道，使前端消费组件能够实现 100% 接口一致性。

| 后端 | 后端原生协议 | 网关推荐前端协议 (YAML配置) | 真实底层实现 |
|---|---|---|---|
| ComfyUI | HTTP POST + WS 进度 | **WebSocket (ws)** | `image.py` + 原生透传 + `overall_progress` 智能平滑拦截 |
| Ollama 本地非流式 | HTTP 一次性 POST | **WebSocket (ws)** | `llm.py` + 网关后台代理轮询兜底推送 |
| TTS 语音合成 | HTTP 轮询 | **WebSocket (ws)** | `tts.py` + 网关后台代理轮询兜底推送 |
| 字幕强制对齐 | multipart POST + HTTP 轮询 | **WebSocket (ws)** | `subtitle.py` + 网关后台代理轮询兜底推送 |
| 云端大模型流式 | HTTP SSE/NDJSON 流 | **流式 (stream)** | `/interface/tasks/{id}/stream` (并发限额直连) |
| 云端大模型非流式 | HTTP 一次性 POST | **轮询 (poll)** | `/interface/tasks/{id}/status` (轻量无状态轮询) |
| 文件访问代理 | HTTP GET | HTTP GET | `file_proxy.py` (资源读取通道) |

> **⚠️ 本地 Ollama 只能使用非流式接口**，并且在网关 `tasks.yaml` 中其 `track_mode` 统一声明为 `ws`，由网关在后台代劳轮询并推送。若前端环境仅具备 HTTP REST 能力，允许忽略推荐并直接强行调 `/status` 接口轮询（网关全向向下兼容）。

> **💡 ComfyUI 进度条智能平滑**：为彻底解决复杂工作流（多采样器、内部切片循环）造成的进度条反复横跳问题，KamlPro 引入了纯增量的进度拦截器。通过高水位锁下发单调递增的 `overall_progress` 事件，下游前端应优先绑定此事件。同时拦截器也原封不动透传了所有底层原生数据，实现了 100% 零侵害的后向兼容。

## 代码约定

- 所有模块通过 `importlib.import_module` 动态加载，由 `config.yaml` 驱动。**不要静态导入 executor。**
- 添加新任务类型：在 `src/core/executors/` 新建文件 → 在 `config.yaml` 注册（含 `track_mode`） → 重启，不改调度器代码。
- 普通 executor 函数签名：`def execute(task_id: str, **payload):`，结果通过 `TaskManager.update_task()` 写回。
- 流式 executor 函数签名：`def execute_stream(task_id: str, output_queue: queue.Queue, **payload):`，逐行写入 `output_queue`，结束写 `None`。
- 支持 WS 推送的执行器：额外调用 `ws_manager.send(task_id, message)` 推送进度，完成时发 `{"type": "task_complete"}`，失败发 `{"type": "task_failed"}`。
- executor 绝不做：下载文件、改写 URL、处理音频/图片、判断代理/下载模式。
- executor 获取服务名：**统一从 `payload["_service_name"]` 获取**（调度器注入，来源于 `config.yaml` 的 `tasks.{task_type}.service`），不要硬编码服务名。默认值仅作 fallback。
- 所有 API 响应走 `src/core/response.py` 的 `success()` / `error()` 辅助函数。
- 环境变量用 `${VAR}` 语法在 `config.yaml` 中引用。
- 日志用 `src/logic/logger.py` 的 `log` 实例，支持 `log.success()`。

## 配置

- `config.yaml` — 核心主配置文件，已实现**极致精简与模块化**。仅保留主服务参数，复杂的动态业务配置已全部抽离。
- `configs/` — 专属模块化配置目录：
  - `model_extensions.yaml` — 自定义 AIGC weights 及各类模型/图像/音视频文件后缀大一统过滤列表。
  - `services.yaml` — 后端子进程定义，采用多系统级联继承覆盖架构。支持 `auto_start`（Kalm启动时自动拉起常驻）及资源释放接口 `free_api`。
  - `tasks.yaml` — 任务类型（如 comfyui, tts, subtitle, llm）到后端服务与执行器的路由映射关系。
  - `llm_config.yaml` — 大模型提供商注册中心（`providers:` 块）与并发调度参数（`api_concurrency_limit`）。**此文件含真实密钥，已在 `.gitignore` 中排除。** 初次部署请复制 `configs/llm_config.yaml.example` 并填入凭证。。
- **装载与智能拆箱（Smart Unwrapper）**：
  - `yaml_config_loader.py` 支持在 `config.yaml` 中使用 `key: "configs/filename.yaml"` 极简键值直接映射。
  - 当子配置文件中含有同名 key 时，加载器会**全自动拆箱解套（Unwrap）**，完美保留了“外部文件有 key 易读”与“总配置无嵌套扁平”的优雅特性。

## 资源释放策略

调度器在**每个任务完成后**决策是否释放后端服务的 GPU 资源（调用 `services.yaml` 中配置的 `free_api` 接口）。决策逻辑：

| 下一任务类型 | 行为 | 说明 |
|---|---|---|
| 同类型（已在队列中） | 跳过释放 | 模型常驻，避免重复加载 |
| 不同类型 | 释放 | 切换服务前释放旧模型 |
| 队列为空（暂无下一任务） | 释放 | 确认无后续任务，回收显存 |

**关键限制**：释放决策基于队列中**已入队**的任务。如果下游串行提交（等上一个完成才提交下一个），上一个完成时队列为空，必然触发释放 → 重载。要利用"同类型跳过释放"特性，必须**批量提交，让任务在队列中排队**。

只有 `services.yaml` 中配置了 `free_api` 的服务才会被调用释放接口（目前仅 ComfyUI）。TTS、Ollama 等未配置，自动跳过。

`auto_start` 仅保证服务**进程**常驻不回收，模型仍可能被 `free_api` 释放。两者独立。

## 队列 WS 广播机制

调度器在三个关键节点通过 `ws_manager.broadcast_queue()` 广播任务生命周期事件：

| 触发点 | 消息类型 | 携带数据 |
|---|---|---|
| `submit_task()` 入队后 | `task_enqueued` | task_id, task_type, track_mode |
| `_scheduler_loop` 取到任务 | `task_started` | task_id, task_type, track_mode |
| `_scheduler_loop` 执行完毕 | `task_completed` | task_id, next_task_id/type/track_mode |

**客户端行为**：
- 连上 `/interface/queue/ws` 后收到所有广播，按 `task_id` 过滤自己的任务
- `task_started` 后，ws 和 poll 模式统一连接 `/tasks/{id}/ws` 获取执行进度
- `task_completed` 后 GET `/tasks/{id}/status` 拉取最终结果

**任务 WS 双保险**：
- 主线：executor 通过 `ws_manager.send()` 实时推送进度
- 兜底：`ws_proxy.poll_fallback` 每 2 秒轮询 TaskManager，完成时补发通知
- 主线推送失败不影响最终结果，兜底最多 2 秒延迟

**心跳保活**：客户端每 30 秒发 `ping`，Kalm 回 `pong`，防止中间网络层断开空闲连接。

## 测试脚本 (20260527 统一重构版)

为方便下游厂商与前端开发无缝接入，我们在 `test/20260527/` 目录下提供了一整套基于**”智能自适应 WS”**与**”纯 HTTP/REST 降级”**双轴对齐的高质量示范用例：

### 1. WebSocket 优先自适应接入示范 (最佳实践)
这类用例在提交任务后，能根据网关返回的 `track_mode` 智能路由到 WebSocket 通道，并在发生配置变更时自动平滑降级。
* `test/20260527/test_llm_local_ws.py` - **本地大模型 (Ollama) 自适应 WS 推送测试** (监听 `gemma3:12b`)
* `test/20260527/test_llm_cloud_ws.py` - **云端大模型 (DeepSeek) 自适应 WS 推送测试** (监听 `deepseek-v4-flash`)
* `test/20260527/test_hybrid_audio_ws.py` - **语音配音与字幕对齐自适应 WS 推送测试** (综合级联测试，下载 WAV 与 SRT)

### 2. 纯 HTTP REST 降级接入示范 (兜底/降级方案)
此类用例专门为【不支持 WebSocket 协议的轻量级开发环境】准备。代码中包含大量的商业级架构设计说明与注释，演示如何忽略 `track_mode: ws` 推荐并直接强行进行 HTTP `/status` 状态轮询。
* `test/20260527/test_llm_local_poll.py` - **本地大模型 (Ollama) 纯 HTTP 轮询测试** (强行轮询 /status)
* `test/20260527/test_llm_cloud_poll.py` - **云端大模型 (DeepSeek) 纯 HTTP 轮询测试** (强行轮询 /status)
* `test/20260527/test_hybrid_audio_poll.py` - **语音配音与字幕对齐纯 HTTP 轮询测试** (强行轮询并下载音频字幕)

### 3. 云端流式输出接入示范 (仅云端 API)
云端模型独有的 SSE 流式逐 Token 推送模式。
* `test/20260527/test_llm_cloud_stream.py` - **云端大模型 (DeepSeek) SSE 流式测试** (闲时直连/忙时排队 + 首字延迟 TTFT 展示)

### 4. 其他核心测试用例
* `test/20260527/test_comfyui_image.py` - ComfyUI 文生图采样进度 WS 推送（第一阶段：生成高清概念图）
* `test/20260527/test_comfyui_video.py` - ComfyUI 图生视频采样进度 WS 推送（第二阶段：OS级跨目录文件拷贝并生成视频）
* `test/20260527/test_providers_refactor.py` - 架构提供商层与执行器动态代理的 6 大模块单元测试

测试产物统一分类归档并输出到 `test/20260527/outputs/` 的对应子目录下。

### 5. 任务取消与物理中断测试 (20260620 版本)
包含在生成过程中进行手动取消、状态轮询以及自动下载生成物的核心逻辑：
- `test/20260620/test_comfyui_single.py` - **ComfyUI 单任务生成与状态轮询/取消测试**（自动输出至 `test/20260620/outputs/`）
- `test/20260620/test_comfyui_multiple.py` - **ComfyUI 多任务批量生成与排队取消测试**
- `test/20260620/test_tts_single.py` - **TTS 单任务配音生成与取消测试**
- `test/20260620/test_tts_multiple.py` - **TTS 多任务并发与排队取消测试**

## 任务停止与双重物理中断机制

为了防止 GPU 计算资源和云端大模型 API 计费的浪费，KamlPro 实现了**全链路任务停止与双重物理中断机制**：

- **任务取消端点**：`POST /interface/tasks/{id}/cancel`。接收到取消请求后，立即在 `TaskManager` 中标记取消状态并生成取消事件。
- **ComfyUI 双重物理中断**：
  1. **网关异步主动切断**：在 `cancel` 路由收到请求时，立即向 ComfyUI 后端发送 `/queue` 任务删除和 `/interrupt` 中断请求，防止由于 Websocket 阻塞导致取消延迟。
  2. **执行线程被动检测**：在 ComfyUI 轮询/WS 接收循环中，每次循环开始或收到数据时检测 `TaskManager.is_cancelled(task_id)`。一旦触发，立即发起 `/queue` 与 `/interrupt` 物理中断，并抛出 `RuntimeError("任务已被用户手动停止")` 中断当前执行流程。
- **大模型 (LLM) 物理中断**：流式或非流式大模型在检测到任务取消后，立即主动关闭 HTTP 响应流连接 (`resp.close()`)，停止后端模型计算与计费。
- **语音合成 (TTS) 物理中断**：轮询检测状态时实时检查取消状态，一旦取消，退出轮询，拒绝拉取或处理生成的音频/字幕产物。
- **日志净化**：在各执行器捕获 `RuntimeError("任务已被用户手动停止")` 异常时，屏蔽大段 Traceback，仅打印清晰的用户取消 Info 日志，确保控制台日志的干净整洁。

## 运行

```powershell
pip install -r requirements.txt
python main.py
```
