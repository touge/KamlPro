# Changelog

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