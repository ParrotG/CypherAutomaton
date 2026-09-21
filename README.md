# IN-CYPHER autonomous agent stack

This project provides a standalone autonomous challenge-solving infrastructure for the IN-CYPHER Hackathon:

- `incypher_bridge`: a multi-agent middle layer for raw TCP + PoW challenges.
- `incypher_agent`: a minimal model-tool loop, using a DeepSeek-compatible API by default.
- `incypher_scheduler`: a task-blind scheduler that creates bridge connectors and workers,
  maintains concurrent/total worker limits, and supports raw TCP, HTTP(S) URL, and local file/directory targets.
- Blackboard: a task-level shared record board that lets concurrent workers and later workers
  pull summaries of completed work.
- Docker: provides a deployable Bridge service and Scheduler/Agent runtime images.

Currently supported challenge input modes:

```text
1. raw TCP: HOST:PORT / `nc HOST PORT`  -> bridge handles PoW
2. URL:     http://... / https://...    -> worker accesses directly, no PoW
3. files:   local file or directory     -> each worker gets an independent copy (the agent may modify its workspace copy); original files stay unchanged; no PoW
```

### Command placeholder convention

Angle-bracket placeholders in README commands must be replaced with values from your actual environment. Replace the angle brackets themselves as well:

- `<HOST>`, `<PORT>`: raw TCP challenge host and port, e.g. `10.0.0.1:30068`.
- `<TARGET>`: target passed to Agent/Scheduler; can be raw TCP, an HTTP(S) URL, a file, or a directory.
- `<TARGET_URL>`: HTTP(S) challenge URL.
- `<CHALLENGE_FILES_DIR>`: local challenge file or directory path.
- `<TASK_ID>`: task id, e.g. `Zip` or `pwn-task`.
- `<TASK_DESCRIPTION_FILE>`: path to the challenge description file on the host.
- `<RUN_ID>`: worker run id, e.g. `w0001`.
- `<ATTEMPT_ID>`: scheduler attempt id, formatted like `attempt-YYYYMMDDTHHMMSSZ-xxxxxx`.
- `<RUN_DIR>`: Agent run/state directory path, e.g. `.cypher_bridge/worker/<RUN_ID>`.
- `<CONNECTOR_ID>`: bridge connector id, e.g. `worker-a`.
- `<LOCAL_BRIDGE_PORT>`: local TCP port assigned by the bridge for this connector.
- `<ENV_FILE>`: private env file path, default `.env.local`.
- `<BRIDGE_API_PORT>`: Bridge HTTP API port, default `8765`.
- `<BRIDGE_STATE_DIR>`: Bridge state directory, default `.cypher_bridge/bridge`.
- `<MAX_HANDSHAKES>`: maximum concurrent PoW handshakes for the Bridge, default `4`.
- `<STATE_DIR>`: runtime state directory, e.g. `.cypher_bridge/scheduler`.
- `<N_CONCURRENT>`, `<N_TOTAL>`: concurrent worker limit and total worker limit.
- `<REVIEW_FEEDBACK>`: failure reason or guidance returned to the Agent during manual review.
- `<HOST_CHALLENGE_FILES_DIR>`, `<HOST_TASK_DESCRIPTION_FILE>`: absolute host paths used for Docker bind mounts.

---

## Directory layout

```text
.
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── uv.toml
├── .env.local                 # 0600 private configuration
├── .env.local.example
├── README.md
├── docs/
│   └── 工程说明.md
├── src/
│   ├── bridge.py              # python bridge.py ... wrapper
│   ├── solver.py              # compatible solver.connect / connect_bridge
│   ├── incypher_bridge/       # FastAPI + asyncio bridge
│   ├── incypher_agent/        # minimal model-tool agent
│   └── incypher_scheduler/    # task-blind scheduler
└── tests/
```

---

## Local installation

Requires Python 3.10+; `uv` is recommended:

```bash
uv venv --python 3.10
uv sync
```

Initialize the shared Agent Python tool environment:

```bash
uv run python -m incypher_agent sandbox-init
```

The default tool environment is located at:

```text
.cypher_bridge/agent-tools/
```

Preinstalled packages:

```text
pwntools
requests
pycryptodome
z3-solver
pyelftools
virtualenv
```

The Agent may create its own venv inside the workspace to add dependencies:

```bash
python -m virtualenv .venv
.venv/bin/pip install <package>
```

---

## Configuring `.env.local`

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

Required:

```dotenv
CYPHER_TEAM_KEY=...
DEEPSEEK_API_KEY=...
```

Optional:

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
CYPHER_THINKING=disabled
```

Rules:

- `.env.local` must be `0600`.
- `DEEPSEEK_API_KEY` is read only from `.env.local`; it is not read from process environment variables, and there is no multi-level fallback.
- Missing, empty, whitespace-containing, or obviously too-short API keys cause the program to error out.
- The model name is not configured in `.env.local`; it is determined by `--model` or the default value in `config.py`.
- `CYPHER_TEAM_KEY` is used only for local HMAC PoW. It is never sent to the remote and never written to logs.
- Do not put `.env.local` inside the Agent workspace.

---

## Bridge v2

Bridge is a FastAPI + asyncio service responsible for raw TCP connectors, PoW,
upstream generations, local Agent endpoints, and event logs.

### Starting

```bash
uv run python -m incypher_bridge serve \
  --api-host 127.0.0.1 \
  --api-port <BRIDGE_API_PORT> \
  --state-dir <BRIDGE_STATE_DIR> \
  --env-file <ENV_FILE> \
  --max-handshakes <MAX_HANDSHAKES>
```

### HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Health check |
| POST | `/connectors` | Create a connector |
| GET | `/connectors` | List connectors |
| GET | `/connectors/{id}` | Query a connector |
| GET | `/connectors/{id}/events` | Read events |
| POST | `/connectors/{id}/reconnect` | Force a new upstream generation |
| DELETE | `/connectors/{id}` | Stop and delete a connector |

Create a connector:

```bash
curl -X POST http://127.0.0.1:<BRIDGE_API_PORT>/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "connector_id": "<CONNECTOR_ID>",
    "target": "<HOST>:<PORT>",
    "auto_reconnect": true
  }'
```

The `endpoint` in the response is the local TCP address the Agent should connect to.

### Connector lifecycle

- A connector allows only one local Agent connection at a time.
- The upstream is kept alive after the Agent disconnects.
- When no Agent is connected, upstream data enters a bounded pending buffer.
- When the upstream disconnects:
  - the current Agent receives EOF;
  - the connector endpoint remains unchanged;
  - the generation is incremented;
  - if auto-reconnect is enabled, PoW is performed again automatically.
- A connector can go through multiple upstream generations.

### Using solver directly

```python
from solver import connect

s = connect("<HOST>", <PORT>)
# If team_key is omitted, it is loaded securely from .env.local
```

Or connect to a local bridge endpoint:

```python
from solver import connect_bridge

s = connect_bridge(<LOCAL_BRIDGE_PORT>)
```

---

## Agent

Agent is a minimal model-tool loop:

- `bash`
- `str_replace_editor`
- `report_flag`

### Sandbox

Bubblewrap is used by default:

- each bash call gets its own PID/UTS/IPC namespace;
- private `/tmp`;
- the workspace is mounted read-write at `/workspace`;
- the project root, `.env.local`, and other worker workspaces are not visible;
- the shared blackboard is mounted at `/blackboard`;
- the model API key remains in the host-side AgentLoop.

### Running

Through an endpoint provided by Bridge:

```bash
uv run python -m incypher_agent run \
  --run-dir <RUN_DIR> \
  --agent-endpoint tcp://127.0.0.1:<LOCAL_BRIDGE_PORT> \
  --description-file <TASK_DESCRIPTION_FILE>
```

Handle a URL or file task directly:

```bash
uv run python -m incypher_agent run \
  --run-dir <RUN_DIR> \
  --target <TARGET> \
  --description-file <TASK_DESCRIPTION_FILE>
```

Real model connectivity check:

```bash
uv run python -m incypher_agent smoke --env-file <ENV_FILE>
```

View state:

```bash
uv run python -m incypher_agent show --run-dir <RUN_DIR>
```

### Flags and verification

The platform submission format is always:

```text
INCYPHER{...}
```

The Agent may report:

```text
INCYPHER{ad74b76d}
flag{ad74b76d}
ad74b76d
```

The program normalizes it to:

```text
INCYPHER{ad74b76d}
```

By default, `report_flag` does not terminate immediately. Instead:

```text
report_flag
-> candidate.json
-> WAITING_VERIFICATION
-> wait for manual verification.json
    success -> flag.json, exit 0
    failed  -> feedback injected into the same context, continue working
```

If manual verification is not needed, add this to Agent or Scheduler:

```text
--no-wait-verification
```

In that case, `report_flag` writes `flag.json` and exits 0 immediately.

---

## Scheduler

`incypher_scheduler` is a task-blind scheduler:

- it does not analyze the challenge;
- it maintains worker limits;
- after a worker exits, it starts a replacement while under the total limit;
- each scheduler run uses an independent attempt directory;
- for raw TCP it starts an embedded bridge;
- for URL/file tasks it does not start a bridge.

### Usage

Raw TCP:

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <HOST>:<PORT> \
  --description-file <TASK_DESCRIPTION_FILE> \
  --max-concurrent-workers <N_CONCURRENT> \
  --max-total-workers <N_TOTAL>
```

URL:

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <TARGET_URL> \
  --description-file <TASK_DESCRIPTION_FILE>
```

File/directory:

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <CHALLENGE_FILES_DIR> \
  --description-file <TASK_DESCRIPTION_FILE>
```

### Attempts and Workers

Each run creates:

```text
.cypher_bridge/scheduler/tasks/<task_id>/attempts/<attempt_id>/workers/wNNNN/
```

Running the same task_id multiple times does not overwrite old worker directories.

### Manual verification

Manual verification is the default.

List candidates:

```bash
uv run python -m incypher_scheduler candidates --task-id <TASK_ID>
```

Report success:

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --result success
```

Report failure:

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --result failed \
  --feedback "<REVIEW_FEEDBACK>"
```

When `--attempt-id` is not passed, `review` automatically selects the newest attempt containing that `run_id`.
It can also be specified explicitly:

```bash
uv run python -m incypher_scheduler review \
  --task-id <TASK_ID> \
  --run-id <RUN_ID> \
  --attempt-id <ATTEMPT_ID> \
  --result success
```

Immediate-exit mode:

```bash
uv run python -m incypher_scheduler run \
  --task-id <TASK_ID> \
  --target <CHALLENGE_FILES_DIR> \
  --no-wait-verification
```

---

## Blackboard

Task-level directory:

```text
.cypher_bridge/scheduler/tasks/<task_id>/blackboard/
└── records/
```

It is mounted in each worker sandbox at:

```text
/blackboard
```

The Agent may pull previous records at any time:

```bash
bb-read
ls /blackboard/records
cat /blackboard/records/*
```

Record a completed unit of work:

```bash
printf 'Status: success\nSummary: ...\n' | bb-write
```

`bb-write` uses a temporary file plus `os.replace` for atomic writes, one record per file.

Prompt rule:

```text
After a meaningful unit of work concludes, briefly record any outcome
that may be useful to other agents. Do not log routine intermediate steps.
```

The Scheduler writes a minimal system record when a worker exits normally or abnormally:

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

## Docker deployment

Docker is used to deploy this project's programs; it is not used to turn the Agent workspace into a container.

### Prerequisites

Before `docker run`, choose the preparation steps based on what you want to run:

- Bridge only: either run `docker build -t incypher-agent-stack .` and then `docker run`, or directly run `docker compose up --build`.
- Scheduler/Agent (an embedded bridge starts inside the container): run `docker build -t incypher-agent-stack .` at least once; Compose is not required.
- A long-running Bridge service used by other processes via `--bridge-url`, or long-term connector management: run `docker compose up --build`. Compose builds the same image and starts Bridge, with the port restricted to `127.0.0.1:8765`.
- Before running, first execute `mkdir -p .cypher_bridge` to ensure the directory exists and is writable by the current host user. Also ensure `.env.local` exists and has mode `0600`.
- In rootless Docker / user namespace remap environments, after container root writes to a bind mount, the host may see `nobody:nogroup` ownership and an `agent/` directory with mode `0700`, making it inaccessible. Adding `--user "$(id -u):$(id -g)"` to the Scheduler/Agent `docker run` makes output files owned by the current host user.

### Build

```bash
docker build -t incypher-agent-stack .
```

The image includes:

- Python 3.10
- FastAPI / asyncio / httpx / openai
- bubblewrap
- common CTF tools
- Bridge, Agent, and Scheduler entrypoints

### Running the Bridge

```bash
docker run --rm \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/.env.local:/app/.env.local:ro" \
  -v "$PWD/.cypher_bridge/docker-bridge:/data/bridge" \
  incypher-agent-stack
```

### Docker Compose

`compose.yaml` provides a minimal long-running Bridge service:

```bash
docker compose up --build
```

Compose is optional. It only solves the port, env, and state volume for a long-running Bridge service;
Scheduler/Agent need dynamic target arguments and depend on bubblewrap namespace capabilities,
so running them directly with `docker run` is clearer.

### Running Scheduler/Agent in Docker

Because Scheduler starts bwrap sandbox workers inside the container, extra privileges are usually needed, and using the host UID/GID is recommended.

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

Notes:

- Replace `<HOST_CHALLENGE_FILES_DIR>` and `<HOST_TASK_DESCRIPTION_FILE>` with absolute host paths. If there is no challenge description file, remove the corresponding `-v` and `--description-file`.
- In Docker mode, adding `--no-wait-verification` is recommended by default; otherwise the worker waits for `verification.json` inside the container, and cross-container manual review is more cumbersome. If manual verification is needed, remove that flag and write the verification result through `incypher-scheduler review` against the same mounted state directory.
- If the host allows finer-grained permissions, you can also try:

```text
--security-opt seccomp=unconfined
--cap-add SYS_ADMIN
```

The exact permissions depend on the host Docker configuration and kernel user namespace settings.

---

## Data layout

```text
.cypher_bridge/
├── agent-tools/                         # shared read-only Python tool environment
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

## Security

- `.env.local` must be `0600`.
- The Bridge HTTP API binds to `127.0.0.1` by default; the Docker default CMD binds to `0.0.0.0`, so keep the port mapping restricted to `127.0.0.1:8765:8765`.
- The bwrap sandbox does not mount the project root or `.env.local`.
- The Blackboard stores task-level records only; do not write team keys, API keys, or other secrets into records.
- Agent bash and artifacts may contain challenge data; do not place private files in the workspace.
- Running Scheduler/Agent in Docker may require elevated privileges for bwrap; use it only in trusted environments.

---

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

Coverage:

- PoW parsing and solving;
- Bridge v2 HTTP API, concurrent connectors, upstream reconnect;
- URL / file / raw TCP targets;
- attempt / worker lifecycle;
- Blackboard atomic writes and system records;
- Agent sandbox, tools, and context limit;
- model connection error backoff and retry;
- candidate normalization, manual verification feedback, and same-context recovery.

For further design details, see:

```text
docs/工程说明.md
```
