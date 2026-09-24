# IN-CYPHER Arena Agent

本仓库是 IN-CYPHER Hackathon 的自主 CTF Agent 开源实现。最终提交物是一个 Docker 镜像，能够在官方 Agent Arena 中无人干预地获取题目、解题，并通过官方 CTF Agent API 提交 flag。

---

## 1. 项目目标

按照官方 `CONTRACT.md`，本项目最终提交一个可被 Arena 自动运行的 Docker 镜像：

- 读取运行时注入的 `CTF_BASE` / `CTF_TOKEN`；
- 使用官方 CTF Agent API 获取题单；
- 加载题目描述和附件；
- 自主解题；
- 通过官方 `client.submit()` 提交 flag；
- 将结果写入 `/work/results.json`。

官方提交方式：

```text
registry.in-cypher.com:5001/team-<team_id>/agent:latest
```

当前实现位于：

```text
arena/
```

---

## 2. 当前目录结构

```text
.
├── Dockerfile                  # 基于官方 agent-base 的提交镜像
├── .dockerignore
├── .env.local.example          # 本地开发配置模板
├── README.md
├── docs/
│   └── 工程说明.md
├── arena/
│   ├── entrypoint.py           # /opt/agent/main.py 兼容入口
│   ├── main.py                 # 调度器 / 容器入口
│   ├── platform.py             # 官方 CTF Agent API 封装与 token 刷新
│   ├── solver.py               # 单题编排、超时/重排、多 agent 协作
│   ├── brain.py                # Brain 接口
│   ├── run_once.py             # 单题 CLI
│   ├── smoke.py                # LLM 连通性冒烟
│   ├── api_probe.py            # 只读平台 API 探测
│   ├── official/               # 官方 /opt/agent 文件快照
│   └── runtime/
│       ├── model.py            # OpenAI-compatible / OpenRouter 客户端
│       ├── loop.py             # model-tool loop + context/token 预算
│       ├── tools.py            # run_bash / submit_flag / view_image / editor
│       ├── connection_proxy.py # raw TCP 多 agent 串行 gateway
│       ├── events.py           # JSONL 事件日志
│       └── summary.py          # attempt summary
└── tests/                      # 单元测试
```

---

## 3. 外部权威材料

必须优先参考以下材料：

| 来源 | 用途 |
|---|---|
| `https://hackathonlive.in-cypher.com/` | 提交入口、重跑入口 |
| `https://hackathonlive.in-cypher.com/usage` | 官方使用说明 |
| `https://hackathonlive.in-cypher.com/status` | 运行/提交状态板 |
| `https://hackathonlive.in-cypher.com/dashboard` | 资源 dashboard |
| `arena/official/CONTRACT.md` | 官方契约，最权威 |
| `arena/official/ctfd.py` | 官方平台客户端与 API 行为 |
| `arena/official/main.py` / `solver.py` / `brain.py` | 官方参考 harness |
| `arena/official/check_agent.sh` | 官方镜像检查脚本 |
| OpenRouter 模型信息 | `deepseek/deepseek-v4.1-flash`：text+image，1M context |

官方基础镜像：

```text
registry.in-cypher.com:5001/base/agent-base:latest
```

当前固定 digest 见 `arena/BASELINE.md`：

```text
sha256:d3c707c6187f49a8b5f0ba617b9590cce72a18676d93343a93098726c7723224
```

重新拉取/解包官方文件：

```bash
docker pull registry.in-cypher.com:5001/base/agent-base:latest

mkdir -p /tmp/official-agent
docker run --rm --entrypoint tar \
  registry.in-cypher.com:5001/base/agent-base:latest \
  -cf - -C /opt agent | tar -xf - -C /tmp/official-agent
```

---

## 4. 本地配置 `.env.local`

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

常用字段：

```dotenv
# 官方平台（本地 live test 用）
CTF_BASE=https://hackathon.in-cypher.com
CTF_TOKEN=ctfd_...

# 本地模型 provider 选择：deepseek 或 openrouter
LLM_PROVIDER=deepseek

# DeepSeek 直连
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# OpenRouter
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-v4.1-flash

# Loop / context
# MAX_STEPS 仅作为不可达保险丝；实际由 token/context 预算控制
MAX_STEPS=1000000
MAX_ATTEMPTS=3
CONTEXT_WINDOW_TOKENS=1000000
CONTEXT_RESERVE_TOKENS=8000
MAX_ATTEMPT_TOKENS=1000000
MAX_PLAIN_REPLIES=3

# 动态实例后台续期间隔（秒）
RENEW_INTERVAL_SECONDS=60

# flag 提交开关：0 = 拦截并模拟成功，1 = 真实提交
SUBMIT_FLAGS=1

# 调度并发
MAX_CONCURRENT_CHALLENGES=2
PRELOAD_WORKERS=2
```

说明：

- `deepseek-flash` 与 `deepseek/deepseek-v4.1-flash` 是同一目标模型的不同 provider 表示；
- OpenRouter 路径默认会自动附带：
  ```json
  "reasoning": {"effort": "high"},
  "provider": {"order": ["deepseek"], "allow_fallbacks": true}
  ```
- `ARENA_MODE` 未设置时默认 `auto`：题单里存在非 Practice 题则跑 competition，只有 Practice 题则跑 practice；也可显式设置 `practice` / `competition` 覆盖。
- Day 1 arena **不会注入 `LLM_*`**，因此镜像必须通过 Dockerfile 构建参数内置自己的 Day 1 LLM 配置；Day 2 arena 注入的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 会在运行时覆盖镜像内置值。
- 官方 arena 始终注入 `CTF_TOKEN` / `CTF_BASE`。

---

## 5. 常用开发命令

### 5.1 本地测试

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

### 5.2 LLM 冒烟

```bash
set -a; . ./.env.local; set +a
.venv/bin/python -m arena.smoke
```

### 5.3 只读 API 探测

```bash
set -a; . ./.env.local; set +a
.venv/bin/python -m arena.api_probe
```

### 5.4 单题端到端

```bash
set -a; . ./.env.local; set +a

.venv/bin/python -m arena.run_once \
  --challenge-id 90 \
  --max-steps 1000000 \
  --max-attempts 3 \
  --work-root /tmp/arena-once
```

### 5.5 全局调度器（本地）

```bash
set -a; . ./.env.local; set +a

.venv/bin/python -m arena.main
```

常用环境变量：

```dotenv
ARENA_MODE=practice
INCLUDE_SOLVED=1
SUBMIT_FLAGS=0
ONLY_IDS=17,19,24,90
CATEGORIES=web,pwn
MAX_CONCURRENT_CHALLENGES=2
AGENTS_PER_CHALLENGE=2
DYNAMIC_AGENTS_PER_CHALLENGE=2
CHALLENGE_TIME_LIMIT_SECONDS=3600
```

调度策略：

- 从 `list_challenges()` 获取题单；
- 对每个题再调用 `client.challenge(id)` 加载完整 description / files；
- 按分值从低到高排序；
- 静态题先预下载文件；
- 只要静态和动态题都有剩余，保持 **1 个动态 + 剩余静态** 的并发结构；
- 动态题全局最多 1 个实例；
- 每题完成写 `/work/results.json`，包含 `timings`。

---

## 6. 日志与结果

运行时目录：

```text
/work/
├── results.json                          # 总结果 + 每题耗时
└── <challenge_id>/
    ├── events.jsonl                      # 完整 JSONL 事件日志
    ├── attempts/
    │   └── 001/
    │       └── summary.json              # 触限/出错后的 attempt summary
    └── ...                               # 下载文件 / 临时文件
```

事件类型包括：

```text
run_start
model_request
model_reply
tool_call
tool_result
assistant_plain
context_limit
model_error
run_end
```

注意：

- `events.jsonl` 可能包含 flag、题目输出、脚本内容；
- 当前**不做日志脱敏**；
- 不要把 `events.jsonl` 提交到仓库或共享；
- 建议本地测试使用独立 Docker volume，运行后导出并删除。

---

## 7. Docker 构建与运行

### 7.1 构建

```bash
DOCKER_BUILDKIT=0 docker build -t arena-test \
  --build-arg DAY1_LLM_API_KEY="$OPENROUTER_API_KEY" \
  --build-arg DAY1_LLM_BASE_URL="$OPENROUTER_BASE_URL" \
  --build-arg DAY1_LLM_MODEL="$OPENROUTER_MODEL" .
```

BuildKit 正常时也可用：

```bash
docker build --platform linux/amd64 --provenance=false -t arena-test \
  --build-arg DAY1_LLM_API_KEY="$OPENROUTER_API_KEY" \
  --build-arg DAY1_LLM_BASE_URL="$OPENROUTER_BASE_URL" \
  --build-arg DAY1_LLM_MODEL="$OPENROUTER_MODEL" .
```

### 7.2 本地官方沙箱参数运行

```bash
docker run --rm \
  --cap-drop=ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  -v "$PWD/arena-work:/work" \
  --tmpfs /tmp:rw,size=1g \
  --cpus 8 \
  --memory 8g \
  --pids-limit 1024 \
  --network bridge \
  --env-file "$PWD/.env.local" \
  -e ARENA_MODE=auto \
  -e INCLUDE_SOLVED=1 \
  -e SUBMIT_FLAGS=0 \
  -e MAX_CONCURRENT_CHALLENGES=6 \
  -e MAX_ATTEMPTS=1 \
  arena-test
```

正式提交时使用官方 `team-<id>/agent:latest`：

```bash
echo "$CTF_TOKEN" | docker login registry.in-cypher.com:5001 -u team-<id> --password-stdin

docker tag arena-test:latest \
  registry.in-cypher.com:5001/team-<id>/agent:latest

docker push registry.in-cypher.com:5001/team-<id>/agent:latest
```

当前官方 registry 上的 `:latest` 可能是旧 digest；重新 push 前先更新本地镜像。

---

## 8. 已实现的重要行为

- 官方 `ctfd.py` 全量 API 接入；
- 完整 challenge 加载（description/files）；
- 静态题文件预加载；
- 并发调度：`1 dynamic + N static`；
- 动态实例全局锁与 finally destroy，并在 attempt 运行期间后台 `renew`；
- 平台 API 瞬时错误自动重试；
- 累计 token 预算 + 连续无 tool call 保险丝；
- `ARENA_MODE=auto` 自动区分 Day 1 practice / Day 2 competition；
- 单题内默认 2 个并行 agent；每个 agent 独立 workdir，互不读取对方文件；
- 单题所有 agent 退出后，将各自 summary 追加到同一个 `summary.txt`，写入时用线程锁串行化；
- raw TCP 动态题若开启多 agent，会启动本地串行 TCP gateway，PoW 也由 gateway 处理；
- `CHALLENGE_TIME_LIMIT_SECONDS` 默认 3600 秒；超时后停止 agent、destroy dynamic，并把题目重新排到队尾；
- bash 子进程可以正常使用 `curl`/`wget` 等外网工具，生产 sandbox 的 `--network bridge` 已允许出网；
- `max_steps` + `context_window_tokens` 双预算；
- 失败 attempt 生成 deterministic summary，支持下一 attempt 继承；
- `SUBMIT_FLAGS=0` 拦截提交并模拟成功；
- OpenRouter `reasoning.effort=high` + provider deepseek + allow_fallbacks；
- Bash 策略拦截根目录扫描和读取自身日志；
- `events.jsonl` 不再每次复制完整 messages 历史。

---

## 9. 已知限制

1. `arena/official/` 是官方 base 的只读快照，仅作为契约和参考保留。
2. `events.jsonl` 可能很大，当前只避免记录完整 messages 历史，尚未做轮转/压缩。
3. Bash 策略是工具层启发式限制，不是完整 OS 沙箱。
4. `already_solved` 不能证明本次 flag 正确；官方 API 对已解 Practice 题可能直接返回 already_solved。
5. 动态题全局只允许一个实例，是总耗时瓶颈。
6. 单题超时后会重新排到队尾；如果题目持续超时，会一直循环，需要外部停止或后续增加全局 deadline。
7. 本地 Docker 容器随本机关机会停止；长跑应部署在远端主机或官方 runner。
8. `SUBMIT_FLAGS=0` 仅用于本地测试，会拦截并模拟提交成功，不能用于正式计分。

---

## 10. 开发建议

优先阅读：

1. `docs/工程说明.md`
2. `arena/official/CONTRACT.md`
3. `arena/main.py`
4. `arena/solver.py`
5. `arena/runtime/loop.py`
6. `arena/runtime/model.py`
7. `arena/runtime/tools.py`
8. `arena/official/ctfd.py`

建议流程：

1. 用 `SUBMIT_FLAGS=0` 在本地 Docker 中做短 smoke；
2. 用 `check_agent.sh` 做结构检查；
3. 只在确认全链路正常后再 push 到官方 registry；
4. 比赛计分 run 开始后，避免无意义地重复 push。
