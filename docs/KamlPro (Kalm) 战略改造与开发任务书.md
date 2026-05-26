# KamlPro (Kalm) 战略改造与开发任务书

本文件详尽记录了将 **KamlPro (Kalm)** 升级为 **EchoDrama GUI V3 统一后端控制网关** 的大改造开发任务。通过将所有大模型（含本地与云端 API）、绘图、配音、字幕任务收归网关进行统一调度排队与显存动态管理，客户端与服务端的集成界限将更加清晰。

---

## ── 目录 (Table of Contents) ──
1. [改造核心目标](#1-改造核心目标)
2. [联调方案与独立开发优势](#2-联调方案与独立开发优势)
3. [第零阶段：平台自适应、级联配置与配置中心模块化重构](#25-第零阶段平台自适应级联配置与配置中心模块化重构-已完成)
4. [第一阶段：LLM Provider 插件化体系改造](#3-第一阶段llm-provider-插件化体系改造-已完成)
5. [第二阶段：混合流式与并发调度机制](#4-第二阶段混合流式与并发调度机制-已完成)
6. [第三阶段：GET /models 模型资产接口升级](#5-第三阶段get-models-模型资产接口升级)
7. [测试验证与冒烟测试方案](#6-测试验证与冒烟测试方案-已完成)
8. [配套技术文档](#7-配套技术文档)

---

## 1. 改造核心目标
1. **单一 LLM 入口抽象**：PyQt6 客户端无论是调用本地模型还是线上 API（OpenAI, DeepSeek, Gemini），统一请求 KamlPro 的 `/interface/llm/generate` 和 `/generate-stream` 端点。
2. **凭证完全安全物理隔离**：所有的 API 密钥完全配置在服务端的 `config.yaml` 或环境变量中，杜绝客户端的明文 Key 泄漏。
3. **消除重复逻辑代码**：利用本地 Ollama 完全兼容 OpenAI `/v1/chat/completions` 标准接口协议的特性，合并本地与云端大模型的执行器（Executor）调用路径，精简 80% 的网络交互代码。

---

## 2. 联调方案与独立开发优势

> [!NOTE]
> **将改造计划独立编写在 KamlPro 目录下的优势：**
> 1. **清晰的"前后端隔离"边界**：KamlPro 作为纯中转控制站，理应独立开发与独立演进。在 KamlPro 目录中管理任务可避免客户端 UI 代码与其产生物理耦合。
> 2. **杜绝"黑盒式"联调噩梦**：通过在网关下先使用 `test/` 目录的测试脚本（如 `test_comfyui.py`, `test_tts.py`）进行**全自动 Mock 或直接冒烟测试**，确保网关接口 100% 工作无误。
> 3. **极佳的排障隔离性**：如果在联调中发生错误，若网关测试脚本是通过的，说明 Bug 100% 存在于客户端 UI 信号层；若网关测试不通过，则是服务端路由问题。排查效率提升 10 倍。

---

## 2.5 第零阶段：平台自适应、级联配置与配置中心模块化重构 (已完成)

### 2.5.1 改造逻辑
- **OS 级联自适应**：合并 `ComfyUI_Windows`、`ComfyUI_Linux` 为通用 `ComfyUI`，合并 `TTS_Windows`、`TTS_Linux` 为通用 `TTS`。
- **级联覆盖合并机制**：最外层配置默认公共参数，`platforms` 下仅对特定 OS 执行差异化覆盖。`service_controller.py` 加载时全自动深度合并，遵循 DRY 开发原则。
- **构建高内聚 `utils/` 工具库**：抽象独立的 `json_helper.py`（进行 LLM NDJSON 清洗与括号匹配解析）与 `path_helper.py`（进行跨操作系统文件路径与 ComfyUI 流程图 JSON 智能翻译），杜绝 Executors 内部臃肿。
- **配置层极致精简与完全模块化**：将任务定义（`tasks`）、子进程（`services`）、LLM 提供商（`llm_providers` / `llm_config`）与模型/图像后缀过滤（`model_extensions`）解耦出主 `config.yaml`。在 `configs/` 子目录下统一按键值映射（`key: "configs/filename.yaml"`) 进行扁平化声明。配置加载器实现**智能字典拆箱（Smart Unwrapper）**，做到"子文件有 key 具高读写性"与"装载后主配置扁平"的优雅共存。

### 2.5.2 改造清单 (DONE)
- [x] **重载并迁移服务注册表至 [configs/services.yaml](configs/services.yaml)**：全面改为级联默认+特异覆盖架构，并从根目录清理冗余。
- [x] **模块化子配置文件创建**：将大模型注册表、AIGC 常见过滤后缀、服务任务映射关系独立装载到 `configs/` 目录下。
- [x] **极致精简核心映射表 [config.yaml](config.yaml)**：大幅清理主配置膨胀，直接通过直接文件路径进行优雅键值绑定映射。
- [x] **升级解析器 [service_controller.py](src/core/service_controller.py)**：默认读取 `configs/services.yaml`，并提供对根目录 `services.yaml` 的平滑向下兼容。
- [x] **升级加载器 [yaml_config_loader.py](src/logic/yaml_config_loader.py)**：重构并新增递归 `_resolve_sub_configs` 深度字典合并（`_deep_merge`） and 子配置同名键自动拆解展开逻辑。
- [x] **创建 Utils 辅助函数包 [src/utils](src/utils)**：编写完备的 `json_helper.py`、`path_helper.py` 及统一入口 `__init__.py`，修复了 `path_helper.py` 遗留的模块导入 bug，为下阶段 LLM 执行器大升级与路径翻译打下稳健基础。

---

## 3. 第一阶段：LLM Provider 插件化体系改造 (已完成)

### 3.1 改造背景

原始代码在 `executors/llm.py` 中将 Ollama 请求地址硬编码写死，既无法切换 Provider，也无法在不修改核心代码的情况下扩展新的云端 API。本阶段将其彻底重构为**基于装饰器自发现的插件化 Provider 体系**。

### 3.2 架构设计

```
src/core/providers/
├── __init__.py          ← 导出公共接口
├── base.py              ← BaseLLMProvider 抽象基类
├── manager.py           ← LLMProviderManager（自动发现 + 注册表）
├── ollama.py            ← OllamaProvider（@register_provider("ollama")）
└── openai_compatible.py ← OpenAICompatibleProvider（@register_provider("openai_compatible")）
```

**核心设计原则：**

1. **严格配置驱动**：`configs/llm_config.yaml` 中每个 provider 条目**必须**显式声明 `type` 字段。若缺失，网关启动时立即抛出 `RuntimeError` 并终止，绝不做隐式推导或容错。
2. **装饰器自动发现**：新增任意 Provider 只需在 `src/core/providers/` 目录下新建 `.py` 文件，使用 `@register_provider("名称")` 装饰类，系统启动时自动通过 `pkgutil` 包扫描完成注册，**无需手动修改任何注册表代码**。
3. **统一抽象接口**：所有 Provider 继承 `BaseLLMProvider`，实现 `generate()` 与 `generate_stream()` 两个标准方法，上层 Executor 无感知差异。

### 3.3 配置规范

`configs/llm_config.yaml` 示例：

```yaml
api_concurrency_limit: 2

providers:
  ollama:
    type: ollama          # 必填！缺失则启动报错
    base_url: "http://127.0.0.1:11434"
    api_key: ""
    models:
      - name: "gemma3:12b"
        tier: "sentinel"
        display_name: "Gemma 3 (本地离线 / 12B)"

  deepseek:
    type: openai_compatible   # 必填！
    base_url: "https://api.deepseek.com"
    api_key: "sk-xxxxxxxx"
    models:
      - name: "deepseek-v4-flash"
        tier: "value"
        display_name: "DeepSeek V4 Flash (极速 / 降本首选)"
```

### 3.4 扩展新 Provider 的方法

只需三步，无需改动任何已有代码：

```python
# src/core/providers/my_new_provider.py
from .base import BaseLLMProvider
from .manager import register_provider

@register_provider("my_provider_type")
class MyNewProvider(BaseLLMProvider):
    def generate(self, model, messages, **kwargs):
        ...
    def generate_stream(self, model, messages, **kwargs):
        ...
```

然后在 `configs/llm_config.yaml` 对应条目中写 `type: my_provider_type`，即可生效。

### 3.5 改造清单 (DONE)
- [x] **新建 `src/core/providers/base.py`**：定义 `BaseLLMProvider` 抽象基类，规范 `generate` / `generate_stream` 接口签名。
- [x] **新建 `src/core/providers/manager.py`**：实现 `LLMProviderManager`，含 `@register_provider` 装饰器与 `pkgutil` 自动包扫描发现逻辑。
- [x] **新建 `src/core/providers/ollama.py`**：实现 `OllamaProvider`，对接 Ollama 原生 `/api/generate` 与 `/api/chat` 协议。
- [x] **新建 `src/core/providers/openai_compatible.py`**：实现 `OpenAICompatibleProvider`，对接标准 OpenAI `/v1/chat/completions` 协议。同时透明支持 **DeepSeek `reasoning_content` 字段**，自动将推理过程包裹在 `<think>…</think>` 标签中透传给客户端。
- [x] **严格配置校验**：`LLMProviderManager` 初始化时逐条检查 `type` 字段，缺失则 `RuntimeError` 快速失败。
- [x] **重构 `src/core/executors/llm.py`**：移除硬编码 Ollama 地址，改为通过 `LLMProviderManager` 按 model → provider 映射进行动态分发。

---

## 4. 第二阶段：混合流式与并发调度机制 (已完成)

### 4.1 改造背景

原有 `/generate-stream` 接口在有并发任务占用槽位时，新请求无法优雅排队等待，只能返回错误。本阶段实现**混合流式传输与并发调度**的完整机制，使流式请求在排队期间也能保持连接活跃，并在槽位释放后无缝接管。

### 4.2 核心机制

```
客户端 SSE 长连接
       │
       ▼
  [排队等待阶段] ──── 每 500ms 发送心跳 {"status":"queued","position":N}
       │
       │  获得执行槽位
       ▼
  [流式推理阶段] ──── 逐 Token 实时推送 {"response":"...","done":false}
       │
       │  推理完成
       ▼
  [结束帧推送] ────── {"response":"","done":true}
```

**并发控制**：`api_concurrency_limit`（在 `configs/llm_config.yaml` 中配置）限制同时执行的 LLM 任务数。超出限制的请求进入 FIFO 队列排队，按序获得槽位执行。

**核心 API 端点：**
- `POST /interface/llm/generate` — 非流式任务提交，轮询 `/status/{task_id}` 获取结果。
- `GET /interface/llm/generate-stream?prompt=...&model=...` — 混合流式端点，SSE 长连接，自动处理排队 + 推理全流程。

### 4.3 改造清单 (DONE)
- [x] **实现混合流式 SSE 端点**：在 `src/api/routes/llm.py` 中实现支持排队心跳 + 推理流式推送的完整 `generate-stream` 端点。
- [x] **并发信号量控制**：以 `asyncio.Semaphore` 为核心，实现 `api_concurrency_limit` 限制与 FIFO 公平排队。
- [x] **流式数据帧统一标准**：无论 Ollama 还是 OpenAI Compatible Provider，均输出统一的 NDJSON 帧格式 `{"response":"...","done":bool}`。
- [x] **DeepSeek 推理内容透传**：`reasoning_content` 字段自动包裹 `<think>…</think>` 标签，透明混入正常流式响应序列中。

> [!NOTE]
> 完整的架构说明、数据流图、并发场景分析与测试代码，详见配套设计文档：
> **[`docs/KamlPro_混合流式与并发调度机制设计文档.md`](KamlPro_混合流式与并发调度机制设计文档.md)**

---

## 5. 第三阶段：GET /models 模型资产接口升级

### 5.1 改造逻辑
在 `src/api/routes/llm.py` 中，重构 `/interface/llm/models` 端点。

### 5.2 任务清单 (TODO)
- [ ] 读取 `config.yaml` 中的 `llm_config.models` 数组。
- [ ] 直接向前端抛出这个格式化的统一注册表 JSON 数组。
- [ ] 支持按 `tier` 或 `provider` 进行自动聚类，支持前端的极简拉取显示。

---

## 6. 测试验证与冒烟测试方案 (已完成)

### 6.1 混合流式并发测试客户端 `test/test_hybrid_stream.py` (DONE)

本次开发已实现完整的 **2×2 矩阵测试脚本**，覆盖以下四种典型场景：

| 场景 | 流式模式 | 并发状态 | 验证内容 |
|------|----------|----------|----------|
| 场景 1 | 流式（Stream） | 空闲（无排队） | 直连流式推理，逐 Token 输出 |
| 场景 2 | 流式（Stream） | 满队（有任务占槽） | 心跳排队等待 → 槽位释放 → 无缝接管流式推理 |
| 场景 3 | 非流式（Sync） | 空闲（无排队） | 提交任务 → 轮询 `/status` → 获取完整结果 |
| 场景 4 | 非流式（Sync） | 满队（有任务占槽） | 验证任务在队列中正常排序消费 |

**测试脚本特性：**
- `[HH:MM:SS.fff]` 高精度毫秒级时间戳，精确观察每个接口的耗时。
- 动态从 `configs/llm_config.yaml` 读取 `api_concurrency_limit`，无需硬编码并发数。
- 占槽任务使用异步后台 Stream Reader 真实维持 SSE 连接，确保槽位真实被占用。
- GBK/Windows 终端完全兼容（无 emoji 字符），可在 Windows 命令行直接运行。

### 6.2 待编写测试 (TODO)
- [ ] **`test/test_cloud_llm.py`**：云端大模型（DeepSeek / OpenAI）非流式与流式完整测试。
- [ ] **`test/test_provider_discovery.py`**：验证 Provider 自动发现注册机制的单元测试。
- [ ] **容错并发压测**：提交 10+ 并发任务，验证 FIFO 队列平滑串行消费与 `TaskManager` 内存状态一致性。

---

## 7. 配套技术文档

| 文档名称 | 路径 | 内容摘要 |
|----------|------|----------|
| 混合流式与并发调度机制设计文档 | [`docs/KamlPro_混合流式与并发调度机制设计文档.md`](KamlPro_混合流式与并发调度机制设计文档.md) | Provider 插件架构、SSE 心跳排队机制、并发调度数据流图、完整测试代码（可直接交付客户参考） |
| 战略改造与开发任务书（本文档） | `docs/KamlPro (Kalm) 战略改造与开发任务书.md` | 各阶段改造目标、任务清单、完成状态追踪 |
