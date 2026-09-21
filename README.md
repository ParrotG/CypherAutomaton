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
3. files:   local file or directory     -> 每个 worker 获得独立副本（agent 可在 workspace 内修改副本），原文件不变，无 PoW
```

### 命令占位符约定

README 命令中的尖括号占位符表示需要按实际环境替换；替换时请连同尖括号一起替换：

- `<HOST>`、`<PORT>`：raw TCP 题目地址和端口，例如 `10.0.0.1:30068`。
- `<TARGET>`：交给 Agent/Scheduler 的 target，可为 raw TCP、HTTP(S) URL、文件或目录。
- `<TARGET_URL>`：HTTP(S) 题目 URL。
- `<CHALLENGE_FILES_DIR>`：本地 challenge 文件或目录路径。
- `<TASK_ID>`：任务 id，例如 `Zip` 或 `pwn-task`。
- `<TASK_DESCRIPTION_FILE>`：宿主机上的题目描述文件路径。
- `<RUN_ID>`：worker run id，例如 `w0001`。
- `<ATTEMPT_ID>`：scheduler attempt id，形如 `attempt-YYYYMMDDTHHMMSSZ-xxxxxx`。
- `<RUN_DIR>`：Agent run/state 目录路径，例如 `.cypher_bridge/worker/<RUN_ID>`。
- `<CONNECTOR_ID>`：bridge connector id，例如 `worker-a`。
- `<LOCAL_BRIDGE_PORT>`：bridge 为本 connector 分配的本地 TCP 端口。
- `<ENV_FILE>`：私密 env 文件路径，默认 `.env.local`。
- `<BRIDGE_API_PORT>`：Bridge HTTP API 端口，默认 `8765`。
- `<BRIDGE_STATE_DIR>`：Bridge 状态目录，默认 `.cypher_bridge/bridge`。
- `<MAX_HANDSHAKES>`：Bridge 最大并发 PoW 握手数，默认 `4`。
- `<STATE_DIR>`：运行状态目录，例如 `.cypher_bridge/scheduler`。
- `<N_CONCURRENT>`、`<N_TOTAL>`：并发 worker 上限和总 worker 上限。
- `<REVIEW_FEEDBACK>`：人工 review 时反馈给 Agent 的失败原因或建议。
- `<HOST_CHALLENGE_FILES_DIR>`、`<HOST_TASK_DESCRIPTION_FILE>`：Docker bind mount 使用的宿主机绝对路径。

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
  --api-port <BRIDGE_API_PORT> \
  --state-dir <BRIDGE_STATE_DIR> \
  --env-file <ENV_FILE> \
  --max-handshakes <MAX_HANDSHAKES>
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
curl -X POST http://127.0.0.1:<BRIDGE_API_PORT>/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "connector_id": "<CONNECTOR_ID>",
    "target": "<HOST>:<PORT>",
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

s = connect("<HOST>", <PORT>)
# 省略 team_key 时从 .env.local 安全加载
```

或连接 bridge 本地 endpoint：

```python
from solver import connect_bridge

s = connect_bridge(<LOCAL_BRIDGE_PORT>)
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
  --run-dir <RUN_DIR> \
  --agent-endpoint tcp://127.0.0.1:<LOCAL_BRIDGE_PORT> \
  --description-file <TASK_DESCRIPTION_FILE>
```

直接处理 URL 或文件任务：

```bash
uv run python -m incypher_agent run \
  --run-dir <RUN_DIR> \
  --target <TARGET> \
  --description-file <TASK_DESCRIPTION_FILE>
```

真实模型连通性检查：

```bash
uv run python -m incypher_agent smoke --env-file <ENV_FILE>
```

查看状态：

```bash
uv run python -m incypher_agent show --run-dir <RUN_DIR>
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
  --task-id <TASK_ID> \
  --target <HOST>:<PORT> \
  --description-file <TASK_DESCRIPTION_FILE> \
  --max-concurrent-workers <N_CONCURRENT> \
  --max-total-workers <N_TOTAL>
```

URL：

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <TARGET_URL> \
  --description-file <TASK_DESCRIPTION_FILE>
```

文件/目录：

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <CHALLENGE_FILES_DIR> \
  --description-file <TASK_DESCRIPTION_FILE>
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
uv run python -m incypher_scheduler candidates --task-id <TASK_ID>
```

反馈成功：

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --result success
```

反馈失败：

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --result failed \
  --feedback "<REVIEW_FEEDBACK>"
```

`review` 不传 `--attempt-id` 时自动选择包含该 `run_id` 的最新 attempt；
也可以显式指定：

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --attempt-id <ATTEMPT_ID> \
  --result success
```

立即退出模式：

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <CHALLENGE_FILES_DIR> \
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

### 前置准备

在 `docker run` 之前，先根据目标选择准备动作：

- 只要运行 Bridge：可以 `docker build -t incypher-agent-stack .` 后直接 `docker run`，也可以直接 `docker compose up --build`。
- 运行 Scheduler/Agent（容器内会启动 embedded bridge）：至少先执行一次 `docker build -t incypher-agent-stack .`；不需要 compose。
- 需要常驻 Bridge 服务、供其他进程通过 `--bridge-url` 连接，或需要长期管理 connector：执行 `docker compose up --build`。Compose 会构建同一镜像并启动 Bridge，端口限制在 `127.0.0.1:8765`。
- 运行前先执行 `mkdir -p .cypher_bridge`，确保该目录存在且当前宿主用户可写；同时确保 `.env.local` 已存在且权限为 `0600`。
- 在 rootless Docker / user namespace remap 环境中，容器内 root 写入 bind mount 后，宿主机可能看到 `nobody:nogroup` 所有、`agent/` 为 `0700`，从而无法直接查看。给 Scheduler/Agent 的 `docker run` 增加 `--user "$(id -u):$(id -g)"` 可让输出文件归当前宿主用户。

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

由于 Scheduler 会在容器内启动 bwrap sandbox worker，通常需要额外权限，并建议使用宿主 UID/GID。

```bash
docker run --rm -it \
  --privileged \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/.cypher_bridge:/app/.cypher_bridge" \
  -v "$PWD/.env.local:/app/.env.local:ro" \
  -v "<HOST_CHALLENGE_FILES_DIR>:/challenge-files:ro" \
  -v "<HOST_TASK_DESCRIPTION_FILE>:/description.md:ro" \
  incypher-agent-stack \
  incypher-scheduler run \
    --task-id <TASK_ID> \
    --target /challenge-files \
    --description-file /description.md \
    --env-file /app/.env.local \
    --max-concurrent-workers <N_CONCURRENT> \
    --max-total-workers <N_TOTAL> \
    --no-wait-verification
```

说明：

- `<HOST_CHALLENGE_FILES_DIR>`、`<HOST_TASK_DESCRIPTION_FILE>` 需替换为宿主机绝对路径；如果没有题目描述文件，可删除对应的 `-v` 和 `--description-file`。
- Docker 模式推荐默认加 `--no-wait-verification`：否则 worker 会等待容器内 `verification.json`，跨容器人工 review 操作更麻烦；需要人工验证时删除该参数，并通过挂载到同一 state 目录的 `incypher-scheduler review` 写入验证结果。
- 如果宿主机允许较细粒度权限，也可以尝试：

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
