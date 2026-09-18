# Bridge v2 API

`incypher_bridge` v2 是一个 FastAPI + asyncio 的多 Agent raw-TCP 中间层。
每个 worker 使用一个 connector；connector 的本地 endpoint 在 upstream
generation 更替时保持稳定。

## 启动服务

```bash
uv run python -m incypher_bridge serve \
  --api-host 127.0.0.1 \
  --api-port 8765 \
  --state-dir .cypher_bridge/bridge \
  --env-file .env.local \
  --max-handshakes 4
```

team key 从 `.env.local` 的 `CYPHER_TEAM_KEY` 读取。

## HTTP API

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务健康检查 |
| POST | `/connectors` | 创建 connector 并启动 PoW 连接 |
| GET | `/connectors` | 列出 connector |
| GET | `/connectors/{connector_id}` | 查询 connector 状态 |
| GET | `/connectors/{connector_id}/events` | 读取 connector 事件 |
| POST | `/connectors/{connector_id}/reconnect` | 强制新 upstream generation |
| DELETE | `/connectors/{connector_id}` | 停止并删除 connector |

FastAPI 文档：

```text
http://127.0.0.1:8765/docs
```

## 创建 Connector

```bash
curl -sS -X POST http://127.0.0.1:8765/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "connector_id": "worker-a",
    "target": "47.236.162.54:30068",
    "auto_reconnect": true
  }'
```

返回示例：

```json
{
  "connector_id": "worker-a",
  "endpoint": "tcp://127.0.0.1:46385",
  "state": "connecting",
  "generation": 0,
  "bind_host": "127.0.0.1",
  "bind_port": 46385
}
```

轮询状态：

```bash
curl -sS http://127.0.0.1:8765/connectors/worker-a
```

直到：

```json
{
  "state": "ready",
  "generation": 1
}
```

之后 worker 连接本地 endpoint：

```python
import socket

s = socket.create_connection(("127.0.0.1", 46385))
print(s.recv(4096))
```

## Python Client

```python
import asyncio
from incypher_bridge.client import BridgeClient

async def main():
    async with BridgeClient("http://127.0.0.1:8765") as client:
        await client.create_connector(
            connector_id="worker-a",
            target="47.236.162.54:30068",
            auto_reconnect=True,
        )
        info = await client.wait_ready("worker-a")
        print(info["endpoint"])

asyncio.run(main())
```

## 生命周期语义

- 一个 connector 同时只允许一个本地 agent 连接。
- agent 断开后 upstream 保留，upstream 数据进入 pending buffer。
- upstream 断开时：
  - 当前本地 agent 收到 EOF；
  - connector endpoint 保持不变；
  - `generation` 递增；
  - auto reconnect 开启时重新连接并重新过 PoW。
- worker 不需要因为 upstream 断开而退出；它应记录本轮结果、重置协议状态并重新连接同一个 endpoint。
- `POST /connectors/{id}/reconnect` 可以强制结束当前 generation 并创建新 generation。

## 目录

```text
.cypher_bridge/bridge/connectors/<connector_id>/
├── connector.json
├── events.jsonl
├── challenge_to_agent.bin
└── agent_to_challenge.bin
```
