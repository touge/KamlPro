# Changelog

## [v1.6.0] - 2026-11-15

### 🦙 LlamaSwap 算力调度中枢接入、大模型零配置动态自发现与端口冲突强杀增强

彻底消除大模型物理微服务与逻辑路由间的配置冗余（SSOT 单一数据源原则），接入轻量级 `llama-swap` 算力调度中枢并打通全自动模型发现、视觉模态识别、物理显存一键卸载闭环，同时解决物理子进程端口占用与虚假退出卡死问题：

- **LlamaSwap 调度中枢接入与物理微服务唯一真实源 (`configs/services.yaml`)**:
  - 彻底淘汰旧版单一 `LlamaCpp` 进程模式，接入轻量高效的 `llama-swap` 算力调度中枢（Go 原生高性能反向代理 + 毫秒级多模型按需热切换）；
  - `services.yaml` 统一定义物理服务 `LlamaSwap`（`port: 11435`、`auto_start: true`、`free_api: /api/models/unload`），成为物理微服务的唯一真实源（Single Source of Truth）。
- **零配置冗余：`base_url` 智能推导与端口解耦 (`src/core/providers/llm/openai_compatible.py`, `configs/llm_config.yaml`)**:
  - `OpenAICompatibleProvider.get_base_url()`：当配置中未显式指定 `base_url` 时，自动关联 `service_name` 并通过 `service_controller.get_service_url(service_name) + "/v1"` 动态拼装；
  - `llm_config.yaml` 彻底免配 `base_url` 与 `11435` 端口号，`llamaswap` 节点仅需 5 行配置，杜绝两头重复修改；
  - 本地微服务免 API Key 场景自动赋予 `"local"` 默认凭据，规避误入 Simulated 模拟模式。
- **零配置冗余：大模型双模自发现与视觉多模态智能映射 (`src/core/providers/llm/manager.py`, `src/core/providers/llm/openai_compatible.py`, `src/api/routes/llm.py`)**:
  - **在线探测**：未显式配置 `models` 列表时，自动请求 `GET /v1/models` 提取可用大模型（`qwen3.8-code-fast`、`qwen3.8-code-128k`、`qwen3.8-art-vision`）；
  - **离线回退**：若微服务离线或未启动，自动回退解析 `llama-swap/config.yaml` 提取模型定义；
  - **模态自适应**：智能识别 `art-vision`、`vision` 或包含 `--mmproj` 参数的模型，自动打上 `['text', 'vision']` 多模态标签；
  - **懒加载发现**：任务调度中未注册模型自动触发按需刷新；重构 `get_available_models()` 并驱动 `GET /llm/models` 实时返回动态发现的所有可用模型。
- **原生物理显存一键回收 (`POST /api/models/unload`)**:
  - 接入 `llama-swap` 原生显存卸载端点，当调度器切换任务或空闲释放时自动触发，实测单卡显存从 22GB 瞬间物理归还至 1.2GB（释放 20.8GB），无缝让渡给 TTS 与 ComfyUI。
- **端口冲突强杀与 TCP 套接字确认增强 (`src/core/service_controller.py`)**:
  - `_get_pids_by_port()`：全面支持非 LISTEN 状态连接扫描与 Windows `netstat -ano` 降级兜底；
  - `_ensure_port_released()`：在杀除后通过底层 `socket.bind()` 循环探测，确保操作系统内核完全回收 TCP 端口后再返回；
  - `_kill_process_on_port()`：整合 `psutil` 递归子进程树与 Windows 原生 `taskkill /F /T` 强杀机制；
  - `wait_until_ready()` 增强崩溃日志捕获，杜绝子进程被外部占用闪退时错误日志丢失。
- **自动化测试**:
  - 新增 `test/20261115/test_llamaswap_integration.py`（5/5 测试通过）；
  - 历史 `test_proxy_and_tts_integration.py`（5/5）、`test_speaker_on_demand.py`（6/6）全部回归通过。

---

## [v1.5.0] - 2026-11-11

### 🎙️ TTS 常驻零显存改造、GPU 推理子进程隔离与全方法透明代理 (`/proxy/{service_name}/...`)

彻底消除“网关绑定 TTS 业务路由”与“TTS 常驻占用显存/关闭导致管理与试听 502”之间的架构矛盾，打通前端零耦合透明代理与单卡显存 100% 物理回收闭环：

- **KamlPro 全方法透明代理与试听鉴权 (`src/api/routes/file_proxy.py`, `src/core/security.py`, `src/api/main.py`)**:
  - **新增通用代理端点**：`/proxy/{service_name}` 与 `/proxy/{service_name}/{path:path}`，完整支持 `GET / HEAD / POST / PUT / DELETE / PATCH` 方法及 Query 参数、Request Body（JSON / `multipart/form-data` / 二进制）、`Range` / `206 Partial Content` 响应头透传，自动跟随后端尾部斜杠 `307` 重定向（`follow_redirects=True`），并拆分独立端点函数（Swagger `/docs` 仅展示常用 `GET/POST/PUT/DELETE` 避免 `operationId` 冲突与空路径参数混淆），自动清洗浏览器跨域头与代填后端 `Authorization` Token；
  - **双模式试听鉴权 (`require_proxy_token`)**：全部代理请求支持 `Authorization: Bearer <token>` 请求头；对 `GET / HEAD` 额外兼容 `?token=<token>` 查询参数（转发给下游前自动剥离网关 `token` 参数），兼顾原生 `<audio src="...?token=...">` / `<video>` 直连试听与写操作（`POST/PUT/DELETE/PATCH`）禁传 Query Token 的安全边界；
  - **跨事件循环连接池修复**：使用 `WeakKeyDictionary[AbstractEventLoop, httpx.AsyncClient]` 按事件循环隔离异步客户端并配置 `trust_env=False`，解决多 Loop / `TestClient` 切换场景下的 `Event loop is closed` 异常与本地代理劫持问题；
  - **向后兼容保留**：原有 `/file/{service_name}/{file_path:path}` 与 `/interface/tts/speakers`（`src/api/routes/speaker.py`）完整保留。
- **TTS 调度常驻化、`free_api` 鉴权与单轨进度增强 (`configs/services.yaml`, `src/core/scheduler.py`, `src/core/executors/tts.py`, `src/core/executors/subtitle.py`)**:
  - `configs/services.yaml` 中 `TTS` 服务调整为 `auto_start: true`，并新增 `free_api`（`POST /free`, `unload_models: true, free_memory: true`）；
  - 修复 `TaskScheduler._free_service_resources()` 在调用 `free_api` 时未携带下游服务 `token` 的缺陷，自动附加 `Authorization: Bearer <token>` 请求头；
  - `tts.py` 与 `subtitle.py` 执行器增强单轨进度提取逻辑，兼容顶层 `progress`、`data.progress` 与分段进度 `current_chunk / total_chunks` 自动换算并节流推送 `task_progress`。
- **IndexTTS-API 轻量化常驻与 GPU Worker 子进程隔离 (`D:\AIGC\Background_Tasks\IndexTTS-API`)**:
  - **新增 `app/core/inference_worker.py` (`WorkerProcessManager`, `worker_main`)**：通过 `multiprocessing.get_context("spawn")` 将所有 PyTorch / IndexTTS V1.5 / IndexTTS2 V2.0 / Faster-Whisper 加载与推理彻底隔离到独立子进程中；FastAPI Web 主进程永不导入 `torch`、永不初始化 CUDA（**0 MB 显存常驻**），随时响应 `/speaker/*`、`/emo/*`、`/static/*`、`/files/*`、`/download/*`；
  - **按配置可控加载 (`config.yaml` `tts.model`)**：新增 `enabled_versions`（控制允许加载的模型版本）、`preload_version`（默认 `null`，启动不预加载）、`subtitle_enabled`、`idle_unload_seconds`（默认 15s 空闲回收）；未配置启用的模型请求在入队前直接返回 `400` 拦截，不拉起子进程；
  - **显存物理归零、物理取消与数值进度 (`app/core/queue_manager.py`, `app/api/task_routes.py`, `app/api/schemas.py`, `v1_5/routes.py`, `v2_0/routes.py`, `subtitle_routes.py`, `app/api/main.py`)**：
    - 新增 `POST /free`：空闲时立即退出/销毁 GPU Worker 子进程，由操作系统 100% 物理回收显存与 CUDA Context（运行中则标记队列清空后立即销毁）；
    - 新增 `POST /cancel/{task_id}`：排队任务直接标记中止，运行中任务直接终止 GPU Worker 子进程释放 GPU；
    - 新增 `GET /model/status` 与 `POST /model/config`：支持查询 Worker 存活状态、已加载版本，并在运行时动态调整策略或显式按需 `load` / `unload`；
    - 新增规范复数列表路由 `GET /speakers` 与 `GET /emotions`（同时兼容简写 `GET /emos`，免末尾斜杠），并将旧单数列表路由 `GET /speaker/`、`GET /emo/` 标记为 `deprecated=True` 保留兼容（其余子路由及 KamlPro `src/api/routes/speaker.py` 已切往 `/speakers`）；
    - `GET /status/{task_id}`（`TaskStatusResponse` & `TaskData`）补充数值化 `progress`（0-100）与分段进度描述 `message`。
- **详细运行时日志完善**:
  - `src/api/routes/file_proxy.py` 与 `src/core/security.py` 补全全方法代理出入参摘要、Range / `query_token` 剥离标记、异常状态响应及鉴权拦截日志；
  - `D:\AIGC\Background_Tasks\IndexTTS-API\app\api\task_routes.py` 与 `inference_worker.py` 补全 `/free`、`/cancel`、`/model/config`、Worker 启停 PID 与模型加载/卸载日志。
- **测试与文档**:
  - 新增 `test/20261111/test_proxy_and_tts_integration.py`（5/5 通过）、`D:\AIGC\Background_Tasks\IndexTTS-API\tester\test_worker_isolation_and_lifecycle.py`（7/7 通过）、`test/20261111/test_speaker_on_demand.py` 回归（6/6 通过）及跨进程实机联动冒烟测试；
  - 更新 `README.md` 与 `docs/下游接入契约.md`，新增完整接入文档 `docs/TTS与透明代理服务前端接入指南.md`。

---

## [v1.4.1] - 2026-10-11

### ⚡ 队列顺位轻量广播精简、严格零兜底强校验与单轨全生命周期标准收敛

针对真实短剧推演等复杂高并发场景中下游对接暴露的顺位卡死、重复冗余广播、多通道事件歧义以及匿名妥协兜底痛点进行的系统级规范收敛：

- **排队顺位流转广播收敛（仅在任务实质离开队列时触发）**:
  - `src/core/scheduler.py`: 仅保留调度器出队起跑/吹哨（1 次）以及用户主动取消排队（1 次）两处实质出队触发点，彻底移除在 `task_completed`、`task_failed`、`task_enqueued` 处的无效重复广播；
  - 连上握手增强（`queue_ws.py`）：首帧下发 `queue_snapshot` 后紧跟推送 `queue_positions_updated` 实时映射字典 `{ [task_id]: position }`，驱动客户端即时校准；
  - 新增 `remove_from_queue(task_id)` 与 `abort_asset_wait(task_id)`：支持排队中或等待素材中的任务被取消时即时移出 FIFO 队列并触发广播 `task_cancelled`。
- **出队生命周期与进度事件规范统一**:
  - 移除出队时的多余历史事件 `task_running`，统一只下发标准的 `type: "task_started"`；
  - 全局单轨长连接（`/queue/ws`）统一度量衡进度为 `type: "task_progress"`（300ms 智能节流防洪），指导下游彻底摒弃杂乱的旧单任务连接及底层私有报文（`progress` / `overall_progress`）；
  - **ComfyUI 单轨全局进度单调递增保障**：在 `src/core/executors/image.py` 中彻底隔离 ComfyUI 单节点局部 step 步数（`progress`），单轨 `task_progress` 严格收敛由 `src/logic/comfy_progress_interceptor.py` 高水位加权合成的 `overall_progress` 驱动，并将当前执行节点 `node` 注入进度报文，彻底杜绝复杂多采样器工作流中“前置节点 100% 后跳崖回 0%”的严重进度回退 Bug；
  - **进度数值变动去重拦截（Deduplication）与日志可读性升级**：在 `src/core/ws_manager.py` 的 `emit_progress_throttled` 中增加状态检测，进度未变动时（如连续处于 96%）直接静默拦截，杜绝高频相同数值疯狂广播；同时在 `[WS Send] pong`、`[WS Broadcast]` 与 `[QueueWS]` 断开日志中透传输出所属 `client_id`，极大方便实时监控与排错。
- **严格零兜底原则（Fail-Fast 强校验拦截）**:
  - 贯彻“绝不做妥协性兜底”指令，`TaskManager.create_task` / `scheduler.submit_task` / `ws_manager.subscribe_queue` / `ws_manager.send_to_client` / `ws_manager.emit_to_client` 全链路强校验；
  - 凡缺失或空白 `client_id`，**直接抛出 `ValueError` 异常中断后续一切程序执行**；
  - 彻底铲除 `emit_to_target_or_broadcast` 内部的全网广播兜底与匿名抄送死逻辑，改为纯粹的 `emit_to_client` 精准定向投递，目标离线直接丢弃并打 Warning，严防串台；
  - HTTP 端点（400）与 WS 握手（1008 Policy Violation）在最前沿一票否决任何无身份请求。
- **自动化测试与接入文档**:
  - `test/20261010/test_single_track_queue.py`: 补齐强校验断言、进度单调性保障及全流程测试，15 个测试全绿；
  - `docs/下游接入契约.md`: 全面更新 7.1 节多租户原则与 7.2 节消息类型表、7.3 节状态 8 / 状态 9 规范。

---

## [v1.4.0] - 2026-10-10

### 🎯 JIT 按需延迟素材上传与超时/失联连坐清理机制 (Purge)

彻底解决大文件任务（视频/音频等）批量提交挤爆服务端磁盘与网络带宽，以及客户端意外离线/超时空耗 GPU 算力的痛点：

- **`src/core/task_manager.py`**:
  - 任务状态扩展：新增 `STATUS_AWAITING_ASSETS`（等待客户端上传资产）与 `STATUS_ASSET_UPLOADING`（上传中传输保护）。
  - 任务资产元数据：支持 `asset_status`（"ready" / "not_ready"）与 `required_assets` 声明。
  - Payload 动态合并：新增 `get_task_payload()` 与 `update_task_payload()`，支持按需注入上传文件路径。
  - 连坐清理核心：新增 `purge_client_tasks(client_id)`，批量取消指定客户端所有未完成任务。
- **`src/core/ws_manager.py`**:
  - 客户端离线检测：新增 `_client_disconnect_times`，实现 `is_client_connected()` 与 `get_client_offline_duration()` 离线秒数统计。
- **`src/core/scheduler.py`**:
  - 资产就绪事件池：新增 `_asset_events` 与 `notify_asset_ready()`。
  - 调度吹哨等待：当任务轮到且 `asset_status != "ready"` 时，向单轨 WS 发送 `task_asset_required` 吹哨广播并阻塞等待 30 秒。
  - 防误杀 Watchdog：若客户端正在上传中（`asset_uploading`），自动延长等待至 300 秒，保证大文件完整传输不被误杀。
  - 连坐清理联动：吹哨超时未响应或客户端断开 WS 超过 30 秒时，调用 `purge_client_tasks()` 连坐清理排队任务，拒绝浪费 GPU。
- **`src/logic/asset_resolver.py` & `src/api/routes/tasks.py`**:
  - **两级秒传架构落地**：针对短剧推演场景多镜头复用主角图/音色痛点，升级 `POST /tasks/{task_id}/assets`。
  - 支持纯 JSON 免传开闸（场景 B）：发送 `reuse_assets: [...]`，0 字节大文件传输，服务端在 ComfyUI `input/`、历史任务及全局资产库自动检索并建立软链接映射，毫秒级开闸交付 GPU！
  - 支持混合表单上传（场景 A）：支持上传新分镜文件同时携带 `reuse_assets` 复用旧物料。
  - 级别 1 客户端预检直通：前端提交 `asset_status: "ready"` 时跳过吹哨直接起跑。
- **自动化测试与接入文档**:
  - `test/20261010/test_single_track_queue.py`: 新增纯 JSON 免传开闸及混合表单复用 2 个集成测试（12/12 100% 通过）。
  - `docs/下游接入契约.md`: 丰富第 12.4 节为《专属素材注入与两级秒传架构》，提供详细参数与响应示例。

---

## [v1.2.0] - 2026-10-10

### 🚀 全局单轨 WebSocket 架构与队列大盘感知机制

彻底收敛任务状态追踪为“单轨机制”，解决双轨 WS 连接泄露与状态打架痛点，提供 100% 向下兼容的商业级单轨体验。

- **`src/core/ws_manager.py`**:
  - 连接池升级：支持 `client_id` 分组索引，多端隔离，定向投递 (`send_to_client`)。
  - 智能节流器：新增 `emit_progress_throttled`，以 300ms 为窗口平滑聚合高频采样步数。
  - 路由兼容：新增 `emit_to_target_or_broadcast`，有 client_id 定向推送，无 client_id 全网广播。
- **`src/core/task_manager.py`**:
  - `create_task`: 支持绑定可选的 `client_id`。
  - `list_tasks`: 支持按 `client_id` 过滤专属任务列表。
- **`src/core/scheduler.py`**:
  - 出入队事件携带 `queue_position` 与 `total_queued`，向目标客户端精准推送。
  - 新增 `get_queue_status(client_id)` 调度队列大盘快照方法。
- **`src/core/executors/` (`image.py`, `tts.py`, `subtitle.py`)**:
  - 执行器进度回调统一接入 `ws_manager.emit_progress_throttled`，平滑透传单轨进度。
- **`src/api/routes/` (`tasks.py`, `queue_ws.py`)**:
  - `POST /tasks/submit`: 自动解析 `client_id`（支持 JSON/表单/Header），响应体回显 client_id。
  - `GET /interface/queue/status`: 新增全局调度队列水位大盘 HTTP 查询接口。
  - `GET /interface/tasks`: 支持 `?client_id=xxx` 参数过滤。
  - `WS /interface/queue/ws`: 支持 `?client_id=xxx` 订阅，并在握手建立时主动下发第 1 帧 `queue_snapshot`。

---

## [Unreleased] - 2026-08-23

### 📡 WebSocket 消息日志输出与调试控制

支持将后端向客户端发送的 WebSocket 消息日志打印到控制台，并统一受 `config.yaml` 中的 `app.debug` 开关控制。

- **`src/core/ws_manager.py`**:
  - `_send_async`: 推送任务执行进度与完成/失败状态时，当 `app.debug: true` 时在控制台输出 `[WS Send] [task_id] <message>`。
  - `_send_queue_async`: 广播任务生命周期通知到队列订阅者时，当 `app.debug: true` 时在控制台输出 `[WS Broadcast] [subscriber_id] <message>`。
- **`src/api/routes/ws_proxy.py`**:
  - 统一封装 `_send_ws_json`，在任务初始状态直推（如任务已完成/已失败/不存在）及轮询兜底完成时，受 `app.debug` 控制输出发送日志。
- **`src/api/routes/queue_ws.py`**:
  - 队列 WebSocket 心跳回复 `pong` 时，受 `app.debug` 控制输出发送日志。
- **Rich 格式兼容**:
  - 输出日志时配置 `extra={"markup": False}`，避免 JSON 报文或 task_id 中的特殊符号被 Rich markup 标签误解析。

---

## [Unreleased] - 2026-08-15

### 🔧 任务列表端点缺陷修复

修复 `GET /interface/tasks` 返回 500 的问题。

- **`task_manager.py`**: 补充缺失的 `list_tasks()` 类方法，返回全部任务状态的深拷贝快照（任务 id → 任务字典映射），与 `/tasks` 列表端点契约对齐。
- 此前该端点调用不存在的 `TaskManager.list_tasks()`，会抛出 `AttributeError` 导致 FastAPI 兜底 500。

### 📝 下游接入契约文档

- **新增 `docs/下游接入契约.md`**: 系统梳理面向前端 / 下游厂商的协议、通知与状态码全景，涵盖认证、HTTP 信封与状态码、任务状态机、`track_mode` 路由、WS 单任务 / 队列双频道消息类型、流式 NDJSON 协议、取消语义与边界注意事项。

---

## [Unreleased] - 2026-07-31

### 🎯 单一信任源 (Single Source of Truth) 服务控制架构

解决本地微服务（Ollama / ComfyUI / TTS）在多文件间配置冗余与重复控制问题。

- **`configs/services.yaml` 集中收口**: 物理微服务开关 (`enabled: true/false`) 与自启动预热 (`auto_start: true/false`) 统一收口在 `services.yaml`。
- **全链路自动推导与无感静音**:
  - `service_controller.py`: 新增 `is_enabled()` 核心状态判断，并在 `safe_start()`、`start()` 与 `_launch_process()` 中建立一票否决硬门禁（当 `enabled: false` 时禁止任何形式拉起子进程）；自动收集 `enabled: true` 且 `auto_start: true` 的服务进行预热拉起，向下兼容旧版 `config.yaml`。
  - `scheduler.py`: 注册 Task 时校验关联服务状态，服务禁用时自动静音并跳过任务注册。
  - `providers/llm/manager.py` & `providers/audio/manager.py`: 自发现机制与重载方法校验服务状态，完全跳过禁用模块的 import 与模型实例化。
- **配置与文档解耦**:
  - `config.yaml`: 废弃 `auto_start_services` 数组，由 `services.yaml` 唯一判定。
  - `llm_config.yaml`: 移除 Ollama 等本地服务节点重复的 `enabled` 开关，专用于云端 API (DeepSeek 等) 的控制与模型元数据定义。
  - 配置文件补全完整注释说明书。

---

## [Unreleased] - 2026-07-30

### 🔧 服务控制器 cmd 类型扩展

扩展 `cmd` 类型服务的启动方式，支持批处理文件直接启动。

- **双模式支持**: `cmd` 类型新增 `path` 字段支持，通过 `cmd /c` 直接启动 `.bat` 批处理文件，保留原有 `command` 字段通过 `powershell -Command` 执行的方式
- **ComfyUI 启动路径迁移**: Windows 平台改用 `KamlPro-service/launcher.bat` 替代旧版 `run.ps1`，Linux 平台同步更新为 `KamlPro-service/launcher.sh`
- **平台类型调整**: ComfyUI Windows 平台配置类型由 `ps1` 改为 `cmd`

### 🧹 日志过滤优化

减少控制台无效日志刷屏，提升开发体验。

- **403 日志过滤**: 新增过滤规则，屏蔽第三方/无效客户端向非目标子服务发起 WebSocket 连接时产生的 `403 Forbidden` 和 `connection rejected/closed` 日志
- 仅影响控制台输出，不影响错误定位与调试

---

## [Unreleased] - 2026-06-05

### 🎯 ComfyUI 智能进度拦截器

解决 ComfyUI 多节点或内部切片循环导致的进度条"回跳"问题。

- **新增 `src/logic/comfy_progress_interceptor.py`**: 使用高水位锁和"芝诺逼近法"合成严格单调递增的 `overall_progress`
- **ComfyUI 多输出节点支持**: `final_output_node_id` 支持单节点字符串 `"9"` 或多节点数组 `["264","266","296"]`
- **100% 后向兼容**: 原始 WebSocket 事件透传，新增 `overall_progress` 消息为零侵害叠加
- **测试覆盖**: `test/20260605/test_cover3sizes.py` 验证三尺寸封面批量生成场景

### 🔐 WebSocket 浏览器认证

- 支持通过 `Sec-WebSocket-Protocol` 头传递 token（格式: `['kalm-auth', 'token']`）
- 解决浏览器 WebSocket 无法自定义请求头导致的认证 subprotocol 报错问题

### 🎙️ TTS 发音人代理端点

- 新增 `GET /interface/tts/speakers` 接口
- 前端无需直接访问 TTS 后端地址，由网关统一代理并透传认证 token

### 🚀 启动脚本升级

- `launcher.bat` 替代 `run.bat`
- 自动检测 conda / 虚拟环境 / 系统 Python
- 支持 `--install-deps` 参数自动安装依赖
- 支持 `--port` 和 `--host` 命令行参数

### ⚙️ 配置与路由优化

- `llm_streaming` 任务类型在 `configs/tasks.yaml` 中启用
- TTS 发音人路由 `src/api/routes/speaker.py` 新增
- ComfyUI WS 消息处理优化：分离 `executing` 和 `progress` 消息

---

## [Unreleased] - 2026-05-28

### 📡 统一前端自适应路由：`track_mode` 与测试规范大对齐

为了让下游厂商和前端开发拥有“零猜测、高弹性、高度一致”的接口集成体验，我们对本地任务的交互链路以及配套集成测试用例做出了重大重构：

#### 任务配置与网关路由优化
- **`configs/tasks.yaml`**：将本地大模型 (`llm`)、TTS 语音合成 (`tts`) 和字幕对齐 (`subtitle`) 任务的 `track_mode` 统一由 `poll` 调整为 `ws`。前端调用提交接口时直接获得 `track_mode: ws` 推荐。
- **网关降级无缝兼容**：网关底层依然全面保留并向下兼容普通的 HTTP `/status` 状态查询接口。无论前端选择建立 WS 长连接（由网关在后台代劳轮询推送），还是强行采用 HTTP 周期轮询，底层均能无缝执行。

#### 示范用例双轴对齐重构 (`test/20260527/`)
- **`test_llm_local_ws.py` [NEW]**：本地 Ollama 离线大模型自适应 WS 推送测试，作为 WebSocket 优先接入的最佳实践样本。
- **`test_hybrid_audio_ws.py` [MODIFY]**：TTS/Subtitle 综合任务 WS 优先自适应测试，读取 `track_mode` 智能分流，完美支持配置变更后的自动降级。
- **`test_llm_local_poll.py` [MODIFY]**：大模型本地任务纯 HTTP 轮询测试用例，作为纯 HTTP REST 降级/兜底规范样本。
- **`test_hybrid_audio_poll.py` [MODIFY]**：配音与字幕强制对齐 HTTP 轮询测试用例，作为纯 HTTP REST 降级/兜底规范样本。

#### 文档同步更新
- **`CLAUDE.md`**：更新“协议透传与统一接口原则”段落，清晰拆解网关在 WS 伪装与 HTTP 降级方面的架构蓝图；并在“测试脚本”中完整归档了最新的 20260527 统一重构版集成用例矩阵。
- **`测试报告.md`**：同步更新了集成测试矩阵列表，清晰界定“自适应 WS（最佳实践）”与“纯 HTTP 轮询（降级兜底）”的双轨定位。

---

## [Unreleased] - 2026-05-27

### 🔄 重大架构重构：Provider 模块化

将 `src/core/providers/` 从扁平结构重构为按类型分类的模块化架构：

#### 新增模块结构
- **`src/core/providers/llm/`** - LLM 提供商模块
  - `base.py` - LLM 提供商基类
  - `manager.py` - LLM 提供商管理器
  - `ollama.py` - Ollama 本地推理支持
  - `openai_compatible.py` - OpenAI 兼容接口支持

- **`src/core/providers/audio/`** - 音频提供商模块
  - `base.py` - 音频提供商基类
  - `index_tts.py` - IndexTTS 语音合成支持
  - `manager.py` - 音频提供商管理器

- **`src/core/providers/image/`** - 图像提供商模块
  - `base.py` - 图像提供商基类
  - `comfyui.py` - ComfyUI 图像生成支持
  - `manager.py` - 图像提供商管理器

#### 删除旧文件
- `src/core/providers/base.py`
- `src/core/providers/manager.py`
- `src/core/providers/mock_test.py`
- `src/core/providers/ollama.py`
- `src/core/providers/openai_compatible.py`
- `configs/llm_config.yaml.example`

### ✨ 新功能与改进

#### Executor 精简优化
- **`image.py`** - 代码精简约 80%，移除冗余逻辑，使用新的 Provider 架构
- **`subtitle.py`** - 代码精简约 85%，优化 HTTP 轮询机制
- **`tts.py`** - 代码精简约 82%，使用新的 Audio Provider

#### 路径翻译增强 (`src/utils/path_helper.py`)
- 新增非路径字段排除列表，避免误译（如 MIME 类型 `video/h264-mp4`、数学表达式 `a/2` 等）
- 排除字段：`format`, `expression`, `sampler_name`, `scheduler`, `mode`, `type`, `device`, `codec`, `pix_fmt`, `method`, `crop_position`, `keep_proportion`, `upscale_method`
- 扩展路径识别字段：新增 `unet_name`, `vae_name`, `clip_name1`, `clip_name2`, `filename_prefix`, `model_name`
- 优化路径检测逻辑：仅当值包含斜杠/反斜杠且匹配 AIGC 权重后缀时才进行翻译

#### 配置改进
- **`config.yaml`** - 默认启用 ComfyUI 和 TTS 服务自动启动
- **`configs/llm_config.yaml`** - 注释掉 mock_test_provider，生产环境更干净

#### Bug 修复
- **`service_controller.py`** - 修复 `auto_start_services` 配置为空时的处理问题（使用 `or []` 确保返回值）
- **`scheduler.py`** - 更新 import 路径以适配新的 Provider 模块化结构

### 📝 文档更新
- **`CLAUDE.md`** - 添加重要警告：本地 Ollama 只能使用非流式接口，流式响应需使用 `/interface/llm/generate-stream`

---

## 技术细节

### 重构原因
1. **可扩展性** - 按类型分类便于新增提供商类型
2. **代码隔离** - 各类型提供商独立管理，降低耦合
3. **统一接口** - 每种类型都有独立的 base 基类和 manager 管理器
4. **代码精简** - Executor 层移除 provider 相关逻辑，职责更清晰

### 迁移说明
- 现有配置无需修改，Provider 管理器会自动加载新模块结构的提供商
- import 路径已自动更新：`src.core.providers.manager` → `src.core.providers.llm.manager`