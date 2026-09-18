# IN-CYPHER temporary bridge

本项目包含两个可独立使用的组件：

- `incypher_bridge`：面向 IN-CYPHER raw TCP 题目的本地桥接层，负责读取 team key、完成 team-key-bound PoW、维持 upstream 连接，并在本地提供 Agent 可直接连接的 TCP 端口和人类管理接口。
- `incypher_agent`：配套的最小自主 Agent 单元，通过 DeepSeek-compatible API 驱动一个 model-tool loop，使用 `bash`、`str_replace_editor`、`report_flag` 三个工具解题。

当前适用题型：

- raw TCP + PoW 题目，由 bridge 完成 PoW。
- web、download、纯文件分析等题目，由 Agent 直接访问或处理，不走 bridge 的 raw TCP 转发。

---

## 目录结构

```text
.
├── pyproject.toml
├── uv.toml
├── .env.local                 # 0600 私密配置
├── .env.local.example
├── README.md
├── docs/
│   └── 工程说明.md
├── src/
│   ├── bridge.py              # python bridge.py ... 包装
│   ├── solver.py              # 兼容官方 solver.connect 的入口
│   ├── incypher_bridge/       # PoW / TCP bridge
│   └── incypher_agent/        # 最小 model-tool Agent
└── tests/
```

---

## 安装

需要 Python 3.10+。推荐使用 `uv`：

```bash
uv venv --python 3.10
uv sync
```

`uv` 缓存默认放在项目内 `.uv-cache/`。

---

## 配置 `.env.local`

复制示例文件并设置权限：

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

必需配置：

```dotenv
CYPHER_TEAM_KEY=...
DEEPSEEK_API_KEY=...
```

可选配置：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
CYPHER_THINKING=disabled
```

配置规则：

- `.env.local` 必须是 `0600`，否则会报错；可用 `--allow-insecure-key-file` 临时绕过。
- `DEEPSEEK_API_KEY` 只从 `.env.local` 读取，不读取系统环境变量，也没有多级 fallback。
- API key 缺失、为空、包含空白或明显过短时，CLI 会报错并退出。
- 模型名不在 `.env.local` 中配置。模型名只通过 `--model` 或 `src/incypher_agent/config.py` 中的默认值设置。
- `CYPHER_TEAM_KEY` 只用于本地 HMAC 计算，不会发送到远端，也不会写入日志。
- `DEEPSEEK_API_KEY` 用于调用模型 API，不会写入 Agent 状态文件。

---

## 启动 Bridge

```bash
uv run python -m incypher_bridge serve \
  --target 47.236.162.54:30092 \
  --challenge-id overflow-ward \
  --category pwn \
  --description-file /path/to/overflow-ward.md \
  --duration 1h
```

`--target` 支持以下格式：

```text
HOST:PORT
[IPv6]:PORT
nc -v HOST PORT
```

常用参数：

```text
--challenge-id       稳定题目 ID，建议始终显式指定
--run-id             本次运行 ID，省略时自动生成
--description-file   题目描述文件，会复制到 challenge 目录
--description-text   直接内联题目描述
--duration           运行时限，例如 30m、1h、01:00:00
--no-timeout         不设置运行时限
--bind               本地监听地址，默认 127.0.0.1
--listen-port        本地 Agent 端口，默认自动分配
--max-sessions       最大活跃会话数，默认 4
--max-handshakes     最大并发 PoW 握手数，默认 1
--no-auto-reconnect  upstream 断开后不自动重连
```

启动成功后会输出：

```text
Challenge: overflow-ward
Run:       run-...
State:     ready
Target:    47.236.162.54:30092
Agent:     tcp://127.0.0.1:46385
```

---

## Agent 接入

Agent 连接 bridge 的本地端口，不需要 team key，也不执行 PoW。

```python
from solver import connect_bridge

s = connect_bridge(46385)
banner = s.recv(4096)
```

也可以直接使用 socket：

```python
import socket

s = socket.create_connection(("127.0.0.1", 46385))
```

题目描述可以通过以下方式交给 Agent：

- 读取 `.cypher_bridge/challenges/<challenge_id>/challenge.md`
- 读取 run 目录中的 `run.json`
- 执行：
  ```bash
  uv run python -m incypher_bridge context \
    --challenge-id overflow-ward \
    --run-id <run_id> \
    --json
  ```

Bridge 不会把题目描述混入 raw TCP 协议流。

---

## 人类管理 Bridge

```bash
# 列出 challenge / run
uv run python -m incypher_bridge list

# 查看 run 状态
uv run python -m incypher_bridge status \
  --challenge-id overflow-ward \
  --run-id <run_id>

# 查看题目描述、目标地址、Agent 端点
uv run python -m incypher_bridge context \
  --challenge-id overflow-ward \
  --run-id <run_id>

# 查看事件，可跟随
uv run python -m incypher_bridge observe --follow \
  --challenge-id overflow-ward \
  --run-id <run_id>

# 直接 tail 人类可读日志
tail -f .cypher_bridge/challenges/overflow-ward/runs/<run_id>/transcript.log

# 人类调试发包（会写入真实 upstream，谨慎）
uv run python -m incypher_bridge send --text 'help\n' \
  --challenge-id overflow-ward \
  --run-id <run_id>

# 手动重连 / 停止
uv run python -m incypher_bridge reconnect \
  --challenge-id overflow-ward \
  --run-id <run_id>

uv run python -m incypher_bridge stop \
  --challenge-id overflow-ward \
  --run-id <run_id>
```

不传 `--challenge-id` / `--run-id` 时，默认使用 `active.json` 中最新启动的 run。

---

## 多会话

一个 `serve` 进程可以管理同一题目下的多条独立会话。每条会话有独立的 upstream、本地 Agent 端口、缓存和日志。

创建子会话：

```bash
uv run python -m incypher_bridge session-create \
  --challenge-id example \
  --run-id primary \
  --new-run-id worker-a
```

创建接口异步返回；轮询到目标会话 `state=ready` 且 `agent_endpoint` 非空后，让 Agent 连接该端口。

查看会话：

```bash
uv run python -m incypher_bridge sessions \
  --challenge-id example \
  --run-id primary
```

管理子会话：

```bash
uv run python -m incypher_bridge status \
  --challenge-id example --run-id worker-a --json

uv run python -m incypher_bridge reconnect \
  --challenge-id example --run-id worker-a

uv run python -m incypher_bridge stop \
  --challenge-id example --run-id worker-a
```

说明：

- 相同 `run_id` 重复创建是幂等的，不会重复建立 upstream。
- 每个会话同时只允许一个本地 Agent 连接；第二个连接会收到 `busy`。
- 子会话共享同一目标地址，但远端容器或题目状态不保证隔离。
- 停止主 run 会关闭全部子会话。
- 子会话创建不会修改 `active.json`。

---

## 运行 Agent

真实模型连通性检查：

```bash
uv run python -m incypher_agent smoke
```

`smoke` 会真实调用一次模型 API，不做伪装。

先由 bridge 启动一个 run，然后运行 Agent：

```bash
uv run python -m incypher_agent run \
  --challenge-id overflow-ward \
  --run-id <run_id> \
  --max-seconds 3600 \
  --context-window-tokens 1000000 \
  --context-reserve-tokens 8000
```

Agent 会从 `run.json` 读取：

- `agent_endpoint`
- `description_text`
- `challenge_md`
- `challenge_id`
- `run_id`

完整参数可查看：

```bash
uv run python -m incypher_agent run --help
```

查看 Agent 状态：

```bash
uv run python -m incypher_agent show \
  --challenge-id overflow-ward \
  --run-id <run_id>
```

不做真实模型调用，仅检查配置：

```bash
uv run python -m incypher_agent doctor \
  --challenge-id overflow-ward \
  --run-id <run_id>
```

---

## Agent 预算

Agent 的工作预算不使用模型调用次数或工具调用次数上限。调用次数只作为统计信息记录。

工作预算由上下文窗口控制：

```text
context_limit_tokens =
    context_window_tokens - context_reserve_tokens
```

Agent 在每次模型调用前估算下一次发送给 API 的输入 token 总量：

- 如果前一次 API 调用返回了 `prompt_tokens`，用实际值作为基线；
- 新追加的 model reply、tool result、continuation message 用本地估算；
- 当估算值达到或超过 `context_limit_tokens` 时，Agent 停止工作。

停止时写入：

```text
.cypher_bridge/challenges/<challenge_id>/runs/<run_id>/agent/escalation.json
```

并返回 exit code `10`。

其他结束条件：

- `report_flag`：成功，exit code `0`。
- `max_seconds` 超时：exit code `10`。
- 模型调用连续失败：exit code `50`。
- 用户 Ctrl-C：exit code `130`。

Context 参数：

```text
--context-window-tokens   模型上下文窗口大小，默认 1000000
--context-reserve-tokens  为下一次模型输出和工具输出预留，默认 8000
```

实际模型上下文窗口不同的时候，请按模型能力调整这两个参数。

---

## Bridge 行为

- 每条会话一个 upstream，一个本地 Agent 端口。
- Agent 断开后 upstream 保持。
- 没有 Agent 连接时，upstream 返回的数据进入 bounded pending buffer，并在下一个 Agent 连接时重放。
- upstream 断开时，本地 Agent 会收到 EOF；bridge 默认自动重连并重新过 PoW。
- 可用 `--no-auto-reconnect` 关闭自动重连，或用 `reconnect` 手动重连。
- 默认时限 1 小时；`--duration 30m`、`--duration 01:00:00` 或 `--no-timeout` 可调整。
- 默认只监听 `127.0.0.1`。Agent 在 Docker 中时，建议使用 host 网络或 `host.docker.internal`，不要直接改成 `0.0.0.0`。

---

## 数据布局

```text
.cypher_bridge/
├── active.json
├── c-<pid>-<rand>.sock
└── challenges/
    └── <challenge_id>/
        ├── challenge.json
        ├── challenge.md
        └── runs/
            └── <run_id>/
                ├── run.json
                ├── events.jsonl
                ├── transcript.log
                ├── challenge_to_agent.bin
                ├── agent_to_challenge.bin
                └── agent/
                    ├── state.json
                    ├── events.jsonl
                    ├── heartbeat
                    ├── escalation.json
                    ├── flag.json
                    ├── agent.log
                    ├── artifacts/
                    └── workspace/
```

---

## 直接使用 solver 形态

项目提供与官方 helper 形态相近的入口：

```python
from solver import connect

s = connect("47.236.162.54", 30092, team_key)
# 省略 team_key 时从当前工作目录 .env.local 安全加载
s = connect("47.236.162.54", 30092)
```

`connect` 返回 `PrefixedSocket`，会重放 PoW 握手期间已消费的初始服务字节。

---

## 安全

- `.env.local` 必须为 `0600`。
- bridge 默认只监听 `127.0.0.1`。
- control socket 权限为 `0600`。
- team key 不发送到远端，不写入 metadata，不写入日志。
- Agent bash 工具的 `HOME` 指向 workspace，且不会通过环境变量继承 team key；但 bash 不是文件系统沙箱，不要把私密文件暴露在可访问路径中。
- 人类 `send` 会写入真实 upstream；正式自主运行时只给 Agent 本地端口。
- Agent workspace 和 artifacts 可能包含题目数据，请勿把私密 `.env.local` 放入 workspace。

---

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

测试使用本地 mock PoW gate，不需要真实题目或真实模型。

真实目标连接探测：

```bash
uv run python -m tests.bridge_connection_probe \
  --host HOST --port PORT --sessions 3
```

该探测只发送 PoW 答案，不发送题目命令，不调用模型或 Agent。

开发说明和内部设计见 [docs/工程说明.md](docs/工程说明.md)。
