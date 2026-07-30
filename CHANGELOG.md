# Changelog

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