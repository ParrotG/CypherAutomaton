# IN-CYPHER autonomous agent stack

本项目为 IN-CYPHER Hackathon 提供一套可独立运行的自主解题基础设施：

- `incypher_bridge`：连接 raw TCP + PoW 题目的多 Agent 中间层。
- `incypher_agent`：最小 model-tool loop，默认使用 DeepSeek-compatible API。
- `incypher_scheduler`：task-blind 调度器，创建 bridge connector 和 worker，
  维护并发/总 worker 上限；支持 raw TCP、HTTP(S) URL、本地文件/目录。
- Blackboard：任务级共享记录板，供并发 worker 和后续 worker 拉取已完成摘要。
- Docker：提供可部署的 Bridge 服务和 Scheduler/Agent 运行镜像。

当前支持的题目输入方式：

```text
1. raw TCP: HOST:PORT / `nc HOST PORT`  -> bridge 完成 PoW
2. URL:     http://... / https://...    -> worker 直接访问，无 PoW
3. files:   local file or directory     -> 每个 worker 独立只读副本，无 PoW
```

---

## 目录结构

```text
.
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── uv.toml
├── .env.local                 # 0600 私密配置
├── .env.local.example
├── README.md
├── docs/
│   └── 工程说明.md
├── src/
│   ├── bridge.py              # python bridge.py ... 包装
│   ├── solver.py              # 兼容 solver.connect / connect_bridge
│   ├── incypher_bridge/       # FastAPI + asyncio bridge
│   ├── incypher_agent/        # minimal model-tool agent
│   └── incypher_scheduler/    # task-blind scheduler
└── tests/
```

---

## 本地安装

需要 Python 3.10+，推荐 `uv`：

```bash
uv venv --python 3.10
uv sync
```

初始化 Agent 通用 Python 工具环境：

```bash
uv run python -m incypher_agent sandbox-init
```

默认工具环境位于：

```text
.cypher_bridge/agent-tools/
```

预装：

```text
pwntools
requests
pycryptodome
z3-solver
pyelftools
virtualenv
```

Agent 可以在 workspace 内创建自己的 venv 添加依赖：

```bash
python -m virtualenv .venv
.venv/bin/pip install <package>
```

---

## 配置 `.env.local`

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

必需：

```dotenv
CYPHER_TEAM_KEY=...
DEEPSEEK_API_KEY=...
```

可选：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
CYPHER_THINKING=disabled
```

规则：

- `.env.local` 必须是 `0600`。
- `DEEPSEEK_API_KEY` 只从 `.env.local` 读取，不读取系统环境变量，也没有多级 fallback。
- API key 缺失、为空、含空白或明显过短时会报错退出。
- 模型名不在 `.env.local` 中配置；模型名通过 `--model` 或 `config.py` 默认值决定。
- `CYPHER_TEAM_KEY` 只用于本地 HMAC PoW，不会发送到远端，也不会写入日志。
- 不要把 `.env.local` 放入 Agent workspace。

---

## Bridge v2

Bridge 是一个 FastAPI + asyncio 服务，负责管理 raw TCP connector、PoW、
upstream generation、本地 Agent endpoint 和事件日志。

### 启动

```bash
uv run python -m incypher_bridge serve \
  --api-host 127.0.0.1 \
  --api-port 8765 \
  --state-dir .cypher_bridge/bridge \
  --env-file .env.local \
  --max-handshakes 4
```

### HTTP API

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/connectors` | 创建 connector |
| GET | `/connectors` | 列出 connector |
| GET | `/connectors/{id}` | 查询 connector |
| GET | `/connectors/{id}/events` | 读取事件 |
| POST | `/connectors/{id}/reconnect` | 强制新 upstream generation |
| DELETE | `/connectors/{id}` | 停止并删除 connector |

创建 connector：

```bash
curl -X POST http://127.0.0.1:8765/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "connector_id": "worker-a",
    "target": "47.236.162.54:30068",
    "auto_reconnect": true
  }'
```

响应中的 `endpoint` 是 Agent 要连接的本地 TCP 地址。

### Connector 生命周期

- 一个 connector 同时只允许一个本地 Agent 连接。
- Agent 断开后 upstream 保留。
- 没有 Agent 时 upstream 数据进入 bounded pending buffer。
- upstream 断开时：
  - 当前 Agent 收到 EOF；
  - connector endpoint 保持不变；
  - generation 递增；
  - auto-reconnect 开启时自动重新 PoW。
- 一个 connector 可以经历多轮 upstream generation。

### 直接使用 solver

```python
from solver import connect

s = connect("47.236.162.54", 30068)
# 省略 team_key 时从 .env.local 安全加载
```

或连接 bridge 本地 endpoint：

```python
from solver import connect_bridge

s = connect_bridge(46385)
```

---

## Agent

Agent 是一个最小 model-tool loop：

- `bash`
- `str_replace_editor`
- `report_flag`

### Sandbox

默认使用 `bubblewrap`：

- 每个 bash 调用独立 PID/UTS/IPC namespace；
- 私有 `/tmp`；
- 只读写挂载 workspace 到 `/workspace`；
- 项目根目录、`.env.local`、其他 worker workspace 不可见；
- shared blackboard 挂载到 `/blackboard`；
- 模型 API key 保留在宿主机 AgentLoop。

### 运行

通过 bridge 提供的 endpoint：

```bash
uv run python -m incypher_agent run \
  --run-dir .cypher_bridge/worker \
  --agent-endpoint tcp://127.0.0.1:46385 \
  --description-file ./task.md
```

直接处理 URL 或文件任务：

```bash
uv run python -m incypher_agent run \
  --run-dir .cypher_bridge/worker \
  --target /workspace/challenge_files \
  --description-file ./task.md
```

真实模型连通性检查：

```bash
uv run python -m incypher_agent smoke
```

查看状态：

```bash
uv run python -m incypher_agent show --run-dir .cypher_bridge/worker
```

### Flag 与验证

平台提交格式统一为：

```text
INCYPHER{...}
```

Agent 可以报告：

```text
INCYPHER{ad74b76d}
flag{ad74b76d}
ad74b76d
```

程序会归一化为：

```text
INCYPHER{ad74b76d}
```

默认 `report_flag` 不立即结束，而是：

```text
report_flag
-> candidate.json
-> WAITING_VERIFICATION
-> 等待人工 verification.json
    success -> flag.json，exit 0
    failed  -> feedback 注入同一上下文，继续工作
```

如果不需要人工验证，可在 Agent 或 Scheduler 上加：

```text
--no-wait-verification
```

此时 `report_flag` 写 `flag.json` 并立即退出 0。

---

## Scheduler

`incypher_scheduler` 是 task-blind 调度器：

- 不分析题目；
- 维护 worker 上限；
- worker 退出后在总上限内补位；
- 每个 scheduler run 使用独立 attempt 目录；
- raw TCP 时启动 embedded bridge；
- URL/文件任务不启动 bridge。

### 使用

Raw TCP：

```bash
uv run python -m incypher_scheduler run \
  --task-id pwn-task \
  --target 47.236.162.54:30068 \
  --description-file ./task.md \
  --max-concurrent-workers 2 \
  --max-total-workers 4
```

URL：

```bash
uv run python -m incypher_scheduler run \
  --task-id web-task \
  --target https://target.example/challenge \
  --description-file ./task.md
```

文件/目录：

```bash
uv run python -m incypher_scheduler run \
  --task-id file-task \
  --target ./downloaded-files \
  --description-file ./task.md
```

### Attempts 与 Worker

每次运行创建：

```text
.cypher_bridge/scheduler/tasks/<task_id>/attempts/<attempt_id>/workers/wNNNN/
```

同一 task_id 多次运行不会覆盖旧 worker 目录。

### 人工验证

默认等待人工验证。

查看候选：

```bash
uv run python -m incypher_scheduler candidates --task-id Zip
```

反馈成功：

```bash
uv run python -m incypher_scheduler review \
  --task-id Zip \
  --run-id w0001 \
  --result success
```

反馈失败：

```bash
uv run python -m incypher_scheduler review \
  --task-id Zip \
  --run-id w0001 \
  --result failed \
  --feedback "INCYPHER{f989c670} rejected; try the central directory CRC"
```

`review` 不传 `--attempt-id` 时自动选择包含该 `run_id` 的最新 attempt；
也可以显式指定：

```bash
--attempt-id attempt-20260919T184322Z-cd59f7
```

立即退出模式：

```bash
uv run python -m incypher_scheduler run \
  --task-id Zip \
  --target ./sample_challenges/files/Zip \
  --no-wait-verification
```

---

## Blackboard

任务级目录：

```text
.cypher_bridge/scheduler/tasks/<task_id>/blackboard/
└── records/
```

每个 worker sandbox 中挂载：

```text
/blackboard
```

Agent 可随时拉取过去记录：

```bash
bb-read
ls /blackboard/records
cat /blackboard/records/*
```

记录一个完成的工作单元：

```bash
printf 'Status: success\nSummary: ...\n' | bb-write
```

`bb-write` 使用临时文件 + `os.replace` 原子写入，一记录一文件。

Prompt 规则：

```text
After a meaningful unit of work concludes, briefly record any outcome
that may be useful to other agents. Do not log routine intermediate steps.
```

Scheduler 会在 worker 正常或异常退出时写一条极小系统记录：

```json
{
  "attempt_id": "attempt-...",
  "worker_id": "w0001",
  "exit_code": 0,
  "started_at": "...",
  "finished_at": "...",
  "run_dir": "..."
}
```

---

## Docker 部署

Docker 用于部署本项目程序，不用于把 Agent workspace 变成容器。

### 构建

```bash
docker build -t incypher-agent-stack .
```

镜像内包含：

- Python 3.10
- FastAPI / asyncio / httpx / openai
- bubblewrap
- 常用 CTF 工具
- Bridge、Agent、Scheduler 三个 entrypoint

### 运行 Bridge

```bash
docker run --rm \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/.env.local:/app/.env.local:ro" \
  -v "$PWD/.cypher_bridge/docker-bridge:/data/bridge" \
  incypher-agent-stack
```

### Docker Compose

`compose.yaml` 提供了一个最小 Bridge 常驻服务：

```bash
docker compose up --build
```

Compose 不是必需的。它只解决 Bridge 常驻服务的端口、env 和 state volume；
Scheduler/Agent 需要动态目标参数，而且依赖 bubblewrap 的 namespace 能力，
直接 `docker run` 更清晰。

### 在 Docker 中运行 Scheduler/Agent

由于 Scheduler 会在容器内启动 bwrap sandbox worker，通常需要额外权限。
例如：

```bash
docker run --rm -it \
  --privileged \
  -v "$PWD/.cypher_bridge:/app/.cypher_bridge" \
  -v "$PWD/.env.local:/app/.env.local:ro" \
  -v "$PWD/sample_challenges/files/Zip:/challenge-files:ro" \
  incypher-agent-stack \
  incypher-scheduler run \
    --task-id Zip \
    --target /challenge-files \
    --env-file /app/.env.local \
    --max-concurrent-workers 1 \
    --max-total-workers 1
```

如果宿主机允许较细粒度权限，也可以尝试：

```text
--security-opt seccomp=unconfined
--cap-add SYS_ADMIN
```

具体权限取决于宿主机 Docker 配置和内核 user namespace 设置。

---

## 数据布局

```text
.cypher_bridge/
├── agent-tools/                         # 共享只读 Python 工具环境
├── scheduler/
│   ├── bridge/
│   │   └── connectors/<connector_id>/
│   └── tasks/<task_id>/
│       ├── blackboard/
│       │   └── records/
│       └── attempts/
│           └── <attempt_id>/
│               └── workers/
│                   └── w0001/
│                       ├── worker.log
│                       └── agent/
│                           ├── state.json
│                           ├── events.jsonl
│                           ├── heartbeat
│                           ├── candidate.json
│                           ├── verification.json
│                           ├── flag.json
│                           ├── artifacts/
│                           └── workspace/
└── ...
```

---

## 安全

- `.env.local` 必须为 `0600`。
- Bridge HTTP API 默认只绑定 `127.0.0.1`；Docker 中默认 CMD 绑定 `0.0.0.0`，建议端口映射仍限制为 `127.0.0.1:8765:8765`。
- bwrap sandbox 内不挂载项目根目录和 `.env.local`。
- Blackboard 只保存任务级记录，不要把 team key、API key 写入记录。
- Agent bash 和 artifacts 可能包含题目数据，请勿把私密文件放入 workspace。
- Docker 中运行 Scheduler/Agent 时，bwrap 可能需要更高权限；只应在可信环境使用。

---

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

覆盖：

- PoW 解析与求解；
- Bridge v2 HTTP API、concurrent connector、upstream reconnect；
- URL / 文件 / raw TCP target；
- Attempt / worker 生命周期；
- Blackboard 原子写入和系统记录；
- Agent sandbox、tools、context limit；
- 模型连接错误退避重试；
- candidate 归一化、人工验证反馈和同上下文恢复。

进一步设计说明见：

```text
docs/工程说明.md
```
