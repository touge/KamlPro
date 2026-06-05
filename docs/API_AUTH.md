# Kalm API 认证使用指南

## 概述

Kalm 所有 API 接口均受 Bearer Token 保护，包括 HTTP 请求和 WebSocket 连接。

---

## 配置

在 `config.yaml` 中配置允许的 Token 列表：

```yaml
api_server:
  tokens:
    - "your-secret-token-1"
    - "your-secret-token-2"
```

- **留空或不配置**：跳过认证（仅建议本地开发使用）
- **配置多个 token**：任意一个均可通过验证

---

## HTTP 请求认证

所有 HTTP API 请求需在 Header 中携带 `Authorization` 字段：

```
Authorization: Bearer your-secret-token-1
```

### 示例

```bash
# curl 示例
curl -H "Authorization: Bearer your-secret-token-1" http://127.0.0.1:7000/interface/status

# Python 示例
import requests
headers = {"Authorization": "Bearer your-secret-token-1"}
response = requests.get("http://127.0.0.1:7000/interface/status", headers=headers)
```

### 错误响应

- **401 Unauthorized**：Token 无效或未提供
```json
{"detail": "Invalid authentication token"}
```

---

## WebSocket 连接认证

WebSocket 连接通过 **HTTP Header** 传递 Token（不会在 URL 中暴露），支持两种方式：

### 方式 1：Authorization Header（标准）

```
Authorization: Bearer your-secret-token-1
```

### 方式 2：x-token Header（简化）

```
x-token: your-secret-token-1
```

> **注意：** WebSocket 不支持在 URL query 参数中传递 token（会明文暴露），一律通过 Header 传递。

### 错误响应

认证失败时，服务端会先完成 WebSocket 握手（accept），然后立即以关闭码 **4001** 关闭连接，
客户端通过捕获关闭事件即可判断认证结果：

| 关闭码 | 原因 | 说明 |
|--------|------|------|
| 4001 | `Missing or invalid authentication token` | Token 无效或未提供 |
| 其他 | - | 网络异常、服务重启等，非认证问题 |

**客户端判断逻辑：**
- 收到 `onopen` → 认证通过，连接正常
- 收到 `onclose` 且 `code === 4001` → 认证失败，检查 Token
- 收到 `onclose` 且 `code !== 4001` 或连接超时 → 网络/服务异常

---

## Swagger UI 测试

FastAPI 内置的 `/docs` 页面支持直接在 UI 中输入 Token：

1. 访问 `http://127.0.0.1:7000/docs`
2. 点击右上角 **Authorize** 按钮
3. 输入 Bearer Token（只需输入 token 值，不要加 "Bearer " 前缀）
4. 点击 **Authorize**
5. 点击 **Close**
6. 现在可以直接在页面上测试所有 HTTP API，请求会自动携带 Token

> **注意：** WebSocket 接口无法在 Swagger UI 中直接测试认证，需通过代码连接。

---

## 前端集成示例

### JavaScript (fetch)

```javascript
const TOKEN = "your-secret-token-1";

async function apiCall(endpoint) {
  const response = await fetch(endpoint, {
    headers: {
      "Authorization": `Bearer ${TOKEN}`
    }
  });
  return response.json();
}

// 使用
apiCall("http://127.0.0.1:7000/interface/status").then(console.log);
```

### JavaScript (浏览器 WebSocket)

浏览器原生 WebSocket API 故意屏蔽了自定义 Header 的能力（如 `Authorization`）。为了避免将 Token 放在 URL 中暴露给服务器日志（Nginx/网关），我们全面采用了 **子协议 (Subprotocol) 伪装 Header 方案**。

这是一种将 Token 隐藏在 `Sec-WebSocket-Protocol` 请求头中的安全最佳实践，浏览器会自动为您处理底层的 HTTP Header 拼接。

```javascript
const TOKEN = "your-secret-token-1";
const url = "ws://127.0.0.1:7000/interface/queue/ws";

// 强烈推荐：将 'kalm-auth' 作为第一个协议，Token 作为第二个协议传入
const ws = new WebSocket(url, ['kalm-auth', TOKEN]);

ws.onopen = () => {
    console.log("WebSocket 连接且鉴权成功！");
};

ws.onclose = (event) => {
    if (event.code === 4001) {
        console.error("鉴权失败：Token 无效");
    }
};
```

### Node.js (ws 库)

```javascript
const WebSocket = require('ws');
const TOKEN = 'your-secret-token-1';

const ws = new WebSocket('ws://127.0.0.1:7000/interface/queue/ws', {
  headers: {
    'Authorization': `Bearer ${TOKEN}`
  }
});

ws.on('open', () => console.log('认证通过，已连接'));

ws.on('close', (code, reason) => {
  if (code === 4001) {
    console.error('认证失败:', reason.toString());
  } else {
    console.log('连接关闭:', code, reason.toString());
  }
});

ws.on('message', (data) => console.log('消息:', data.toString()));
```

### Python (requests)

```python
import requests

TOKEN = "your-secret-token-1"
BASE_URL = "http://127.0.0.1:7000"
headers = {"Authorization": f"Bearer {TOKEN}"}

# 获取状态
response = requests.get(f"{BASE_URL}/interface/status", headers=headers)
print(response.json())
```

### Python (websockets)

```python
import asyncio
import websockets

TOKEN = "your-secret-token-1"

async def main():
    try:
        async with websockets.connect(
            "ws://127.0.0.1:7000/interface/queue/ws",
            extra_headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            print("认证通过，已连接")
            async for message in ws:
                print(message)
    except websockets.exceptions.ConnectionClosed as e:
        if e.code == 4001:
            print(f"认证失败: {e.reason}")
        else:
            print(f"连接关闭: code={e.code} reason={e.reason}")

asyncio.run(main())
```

---

## 安全建议

1. **生产环境务必配置 Token**：不要留空
2. **使用强随机 Token**：建议 32 位以上随机字符串
3. **定期轮换 Token**：修改 `config.yaml` 后重启服务即可生效
4. **限制网络暴露**：建议只绑定 `127.0.0.1` 或通过反向代理暴露
5. **HTTPS/WSS**：生产环境建议配合 Nginx 等使用 HTTPS，防止 Header 被窃听
6. **浏览器 WebSocket 限制**：浏览器原生 API 不支持自定义 Header，建议通过短 token + query 参数方案

---

## 常见问题

### Q: 忘记 Token 怎么办？
查看 `config.yaml` 文件中 `api_server.tokens` 配置的值。

### Q: 如何临时关闭认证？
将 `api_server.tokens` 设为空列表 `[]` 或删除该配置，重启服务。

### Q: 401 错误怎么解决？
1. 确认 Token 值正确（无多余空格）
2. 确认 Header 格式为 `Authorization: Bearer <token>`

### Q: 浏览器 WebSocket 怎么传 Header？
浏览器原生 `new WebSocket()` 不支持自定义 Header（如 `Authorization`）。解决方案：
- **纯前端项目（推荐）**：使用子协议数组 `new WebSocket(url, ['kalm-auth', token])`。后端会自动拦截并解析。
- **服务端 Node.js / Python**：直接在建连时传入 `Authorization: Bearer <token>` 即可。

### Q: 多个客户端用不同 Token 可以吗？
可以，`tokens` 列表中的任何一个都能通过验证。