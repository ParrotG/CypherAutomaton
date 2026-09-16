# IN-CYPHER temporary bridge

一个无第三方运行时依赖的临时中间层，用于 IN-CYPHER Hackathon 的 raw-TCP
题目：

1. 从安全位置读取 team key；
2. 解析题目连接返回的 `nonce` / `bits`；
3. 自动完成 team-key-bound proof-of-work；
4. 维持到题目的 upstream TCP 连接；
5. 在本地暴露一个 Agent 可直接连接的 TCP 端点；
6. 按 `challenge_id/run_id` 持久化运行记录，并提供人类可读接口。

官方 ADK / `solver` 尚未发布，因此 PoW 目前按几种最常见编码尝试，默认优先：

```python
mac = hmac.new(team_key.encode(), nonce.encode(), hashlib.sha256).digest()
sha256(mac + str(i).encode()).digest()
```

## 目录结构

```text
.
├── pyproject.toml
├── uv.toml
├── .env.local                    # 0600 私密文件，含 CYPHER_TEAM_KEY / DEEPSEEK_API_KEY
├── .env.local.example
├── README.md
├── docs/
│   └── 工程说明.md
├── src/
│   ├── bridge.py                 # python bridge.py ... 包装
│   ├── solver.py                 # 兼容官方 solver.connect
│   ├── incypher_bridge/          # PoW / TCP bridge
│   └── incypher_agent/           # minimal model-tool agent unit
│       ├── cli.py
│       ├── client.py
│       ├── config.py
│       ├── control.py
│       ├── duration.py
│       ├── paths.py
│       ├── pow.py
│       ├── session.py
│       └── transcript.py
└── tests/
    ├── mock_gate.py
    ├── test_bridge.py
    └── test_pow.py
```

项目采用 `src` 布局，`uv sync` 会把 `src/incypher_bridge`、`solver.py`、
`bridge.py` 安装进虚拟环境，因此可以在项目根目录直接执行：

```bash
uv run python -m incypher_bridge ...
```

## 安装

```bash
uv venv --python 3.10
uv sync
```

uv 缓存默认配置在项目内 `.uv-cache/`，适合受限环境。

## team key

优先读取顺序：

1. `--team-key-file` / `INCYPHER_TEAM_KEY_FILE`；
2. 当前环境变量 `CYPHER_TEAM_KEY`、`INCYPHER_TEAM_KEY`、`TEAM_KEY`；
3. `--env-file` 指定的私密 env 文件，默认 `./.env.local`；
4. `~/.config/incypher/team_key`。

私密文件必须是 `0600`，否则会报错；可用 `--allow-insecure-key-file` 临时绕过。
team key 只用于本地 HMAC 计算，不会发送到远端，也不会写入日志。

当前项目使用自己目录下的 `.env.local`，权限为 `0600`。里面需要有：

```dotenv
CYPHER_TEAM_KEY=...
DEEPSEEK_API_KEY=...
CYPHER_MODEL=deepseek-flash
```

## 启动一个 challenge / run

`challenge_id` 表示题目，`run_id` 表示一次部署和解题过程。强烈建议每次启动时
显式指定 `challenge_id`，让同一题目的多次部署/重连都落在同一个 challenge 下。

```bash
uv run python -m incypher_bridge serve \
  --target 47.236.162.54:30092 \
  --challenge-id overflow-ward \
  --category pwn \
  --description-file /path/to/overflow-ward.md \
  --duration 1h
```

`--run-id` 可省略，默认自动生成，例如：

```text
run-20260916T145544Z-2da959
```

也支持平台里粘贴过来的格式：

```bash
--target "nc -v 47.236.162.54 30092"
```

成功后会显示：

```text
Challenge: overflow-ward
Run:       run-20260916T145544Z-2da959
State:     ready
Target:    47.236.162.54:30092
Agent:     tcp://127.0.0.1:46385
...
Challenge description: .cypher_bridge/challenges/overflow-ward/challenge.md
```

## Agent 接入

Agent 只连接本地 bridge，不需要 team key，也不需要实现 PoW：

```python
from solver import connect_bridge

s = connect_bridge(46385)
print(s.recv(4096))
```

或使用普通 socket：

```python
import socket
s = socket.create_connection(("127.0.0.1", 46385))
```

Agent 可以直接从 run 目录读取题目描述：

```text
.cypher_bridge/challenges/<challenge_id>/challenge.md
.cypher_bridge/challenges/<challenge_id>/runs/<run_id>/run.json
```

`run.json` 中包含：

- `challenge_id`
- `run_id`
- `challenge_name`
- `category`
- `description_text`
- `description_file`
- `challenge_md`
- `agent_endpoint`
- `target`
- `state`
- `pow_summary`
- 收发字节统计

人类或 Agent 也可以直接使用：

```bash
uv run python -m incypher_bridge context \
  --challenge-id overflow-ward \
  --run-id <run_id> \
  --json
```


## Minimal Agent Unit（阶段 A）

阶段 A 实现了一个最小 model-tool loop：

- 工具只有 `bash`、`str_replace_editor`、`report_flag`。
- 使用真实 DeepSeek API，默认模型 `deepseek-flash`。
- 不借鉴 smolagents / Temporal。
- 当前只做硬超限上报：`max_seconds` / `max_model_calls` / `max_tool_calls`。
- 不做偏离检测、卡死检测、多任务检测，parent scheduler 暂缓。
- 模型只是普通 assistant message 不会结束，只有 `report_flag` 或硬超限才结束。

先确认 API key 与模型连通：

```bash
uv run python -m incypher_agent smoke
```

运行一个已经由 bridge 建立的 run：

```bash
uv run python -m incypher_agent run \
  --challenge-id overflow-ward \
  --run-id <run_id> \
  --max-seconds 3600 \
  --max-model-calls 200 \
  --max-tool-calls 500
```

它会读取 `run.json` 中的：

- `agent_endpoint`
- `description_text` / `challenge_md`
- `challenge_id` / `run_id`

并在以下位置写入状态：

```text
.cypher_bridge/challenges/<challenge_id>/runs/<run_id>/agent/
├── state.json
├── events.jsonl
├── heartbeat
├── escalation.json
├── flag.json
└── workspace/
```

硬超限时写入 `escalation.json` 并返回 exit code `10`：

```json
{
  "reason": "hard_limit",
  "detail": "max_seconds exceeded ...",
  "model_calls": 12,
  "tool_calls": 34
}
```

查看 agent 状态：

```bash
uv run python -m incypher_agent show \
  --challenge-id overflow-ward \
  --run-id <run_id>
```

## 人类管理接口

```bash
# 列出所有 challenge / run
uv run python -m incypher_bridge list

# 查看某个 run 状态
uv run python -m incypher_bridge status \
  --challenge-id overflow-ward \
  --run-id <run_id>

# 查看题目描述、目标、Agent 端点
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

# 强制重连 / 停止
uv run python -m incypher_bridge reconnect --challenge-id overflow-ward --run-id <run_id>
uv run python -m incypher_bridge stop --challenge-id overflow-ward --run-id <run_id>
```

不传 `--challenge-id` / `--run-id` 时，会使用 `active.json` 中最近启动的 run。

## 数据布局

```text
.cypher_bridge/
├── active.json                              # 最近启动的 run
├── c-<pid>-<rand>.sock                      # 短路径 Unix control socket
└── challenges/
    └── <challenge_id>/
        ├── challenge.json                   # 题目元数据
        ├── challenge.md                     # 题目描述副本
        └── runs/
            └── <run_id>/
                ├── run.json                 # run 状态、PoW、端点、统计
                ├── events.jsonl             # 机器可读事件
                ├── transcript.log           # 人类可读事件
                ├── challenge_to_agent.bin   # 题目 -> Agent 原始流
                └── agent_to_challenge.bin   # Agent -> 题目原始流
```

## 行为说明

- 一个 run 维持一个 upstream，本地端口同一时间只接受一个 Agent；多余的连接会收到
  `busy`。
- Agent 断开后 upstream 仍保持；没有 Agent 时收到的 upstream 数据会缓存到
  `--max-pending-bytes`，并在下一个 Agent 连接时重放。
- upstream 断开时，本地 Agent 会收到 EOF；bridge 默认自动重连并重新过 PoW。
  可用 `--no-auto-reconnect` 关闭，或手动 `reconnect`。
- 默认时限 1 小时，可用 `--duration 30m`、`--duration 01:00:00` 或
  `--no-timeout`。
- 默认只绑定 `127.0.0.1`。如果 Agent 在 Docker 中，建议使用 host 网络或
  `host.docker.internal`，不要随意改成 `0.0.0.0`。

## 直接使用官方形态的 solver

```python
from solver import connect

s = connect("47.236.162.54", 30092, team_key)
# 省略 team_key 时从当前工作目录/.env.local 安全加载
s = connect("47.236.162.54", 30092)
```

`connect` 返回 `PrefixedSocket`，会重放在 PoW 握手期间已消费掉的初始服务字节。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

包含本地 mock PoW gate、端到端 bridge relay、描述文件与 challenge/run 布局测试。

## 交接文档

其他 Agent 继续开发前请先读：

```text
docs/工程说明.md
```
