# KamlPro ComfyUI 全局进度 (Overall Progress) 测试与集成报告

## 1. 背景与痛点 (Problem Statement)

原生 ComfyUI 通过 WebSocket 提供的进度推送（`type: "progress"`）采用的是**单节点绝对值**机制。这种设计在复杂的商业级工作流中会暴露严重的用户体验问题：

1. **进度回跳（横跳）**：当工作流中存在多个采样器（例如：本测试中的 `cover3sizes.json` 包含 3 个平行的 `KSampler`），前端收到的进度会表现为 `0->100%`，然后再跳回 `0->100%`，反复三次。
2. **切片内部回跳**：部分视频流或批处理节点（如 AnimateDiff sliding window）会在单一节点内部触发多次迭代，导致进度条在未进入下一个节点前就发生归零倒退。
3. **开局进度丢失（真空期）**：工作流前期的轻量级计算节点（如模型加载、参数构建等）本身不产生 `progress` 事件。这导致任务启动前几秒甚至几十秒内，前端进度条死死卡在 `0%`，直到遇到第一个 `KSampler` 才会瞬间飙升至 `45%`。

---

## 2. KamlPro 解决方案 (The KamlPro Solution)

为了对下游客户端提供完全屏蔽底层细节的友好 API，KamlPro 代理层内置了 **`ComfyProgressInterceptor` (智能进度拦截器)**。下游客户端不再需要解析底层的节点跳跃逻辑。

### 核心机制：
*   **全生命周期监听**：不仅仅监听 `progress`，还将 `executing` 和 `execution_cached` 纳入进度驱动。确保轻量级节点在执行和命中缓存时，进度条能够出现平滑起步（例如：`0% -> 4% -> 8% ...`）。
*   **DAG 节点统筹算法**：根据底层返回的字典自动计算工作流总节点数（`total_nodes`），将单节点进度映射为全局总体占比。
    `全局进度 = (已完成节点总数 + 当前节点的子进度) / 总节点数 * 100%`
*   **高水位单调递增锁 (High-Water Mark)**：通过算法强制保证进度百分比严格单调递增，即使底层发生极其复杂的内部切片重置，对外的 `percent` 也只会前进，绝不后退。
*   **芝诺微增量逼近 (Zeno's Approach)**：当检测到底层进度回跳（切片循环）时，利用剩余空间做微弱的极限逼近（如：`+0.5%` 或 `+0.1%`），让用户感知到系统仍在努力计算，而不是死机。

### 🌟 零侵害后向兼容 (100% Backwards Compatibility)：
KamlPro 的进度拦截器采用了“**增量附加（Additive）**”设计：
*   **不吞咽原生数据**：所有 ComfyUI 原生的 `progress`、`executing` 等底层 WebSocket 事件都会被原封不动地直接透传给客户端。
*   **平滑过渡**：如果老版本客户端尚未升级代码，它们仍会按照旧逻辑去监听原生 `progress`，完全可以正常工作（尽管体验仍是来回跳的）。当它们收到新下发的 `overall_progress` 事件时，因为未被代码识别，会直接静默忽略。这意味着下游厂商可以在**不修改一行老代码**的前提下，随时随地渐进式切换到新逻辑。

---

## 3. 下游客户端集成指南 (Integration Guide)

下游应用前端（如 `aigc-tools`、APP 客户端等）在连接 KamlPro 的 WebSocket 后，**应忽略原生的 `progress` 字段，直接监听 Kaml 独家推送的 `overall_progress` 字段。**

### 消息结构示例：

```json
{
  "type": "overall_progress",
  "data": {
    "percent": 65,
    "message": "执行中 (65%)"
  }
}
```

### 客户端渲染伪代码：

```javascript
ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    
    if (msg.type === "overall_progress") {
        // 直接无脑喂给 UI 进度条组件，绝对丝滑、不跳退
        myProgressBar.value = msg.data.percent;
        myStatusText.innerText = msg.data.message;
    }
};
```

---

## 4. 测试用例验证 (Test Validation)

我们在当前目录提供了用于复现和验证此特性的集成测试用例：
*   **工作流配置**：`comfyui_workflows/cover3sizes.json` (包含 24 个节点，其中有 3 个相互平行的核心采样器)。
*   **测试脚本**：`test_cover3sizes.py`。

### 运行方式：

确保 KamlPro 主服务 (main.py) 已经启动，且处于最新代码状态，然后在终端运行：
```bash
python test/20260605/test_cover3sizes.py
```

### 测试预期结果分析：

1.  **前期节点期**：开局不卡顿，随着 `CLIPLoader` 等节点的瞬间执行，进度条会匀速上升至 45% 左右。
2.  **采样期 (KSampler 1)**：进入第一个采样器，进度条从 45% 稳步上涨到 60%。
3.  **接力期 (KSampler 2 & 3)**：原生 ComfyUI 在此时会再次爆出 0% 的进度，但控制台显示的 `[合成全局进度]` 会接力在 60% 的基础上继续攀升至 88%、99%。
4.  **兜底收尾**：任务收尾时，必定稳定触达 `100% 执行完成` 并自动下载所有的目标输出资产。

> **结论**：本机制已彻底抹平了 ComfyUI 的前端工程痛点，完全达到了商业级、工业级的平滑展示标准。
