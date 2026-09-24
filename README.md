# Autonomous CTF Agent for IN-CYPHER Hackathon

Team: **INCYPHER AVENGER**

An autonomous CTF agent system built for the IN-CYPHER Hackathon. The system is designed to complete CTF tasks without human intervention: it discovers challenges, loads descriptions and attachments, solves tasks with an LLM and tool calls, submits flags through the official platform API, and writes results to `/work/results.json`. The project won first place in the competition.

![Competition Outcome](<assets/Competition Outcome.png>)

## 1. Project Brief

This project is the autonomous agent system developed by team **INCYPHER AVENGER** for the IN-CYPHER Hackathon. It is packaged as a Docker image and runs unattended against a live CTF platform.

The agent:

- Reads the platform endpoint and access token from runtime environment variables.
- Fetches the challenge list and the full description of every selected challenge.
- Downloads challenge attachments and prepares an isolated working directory.
- Runs an LLM-driven model-tool loop with shell access, image viewing, file editing, and flag submission.
- Handles static challenges and dynamic instance challenges.
- Submits flags through the official CTF Agent API.
- Persists progress and final timings to `/work/results.json`.

The image is built from:

```text
registry.in-cypher.com:5001/base/agent-base:latest
```

## 2. Configuration and Startup

### 2.1 Prerequisites

Required:

- Docker with `linux/amd64` image support.
- Network access to `registry.in-cypher.com:5001` and the challenge platform.
- A CTFd access token.
- An OpenAI-compatible LLM API key, or organizer-injected `LLM_*` variables in the runtime environment.

Optional for local development:

- Python 3.10+
- `uv`

The Docker runtime is expected to follow the competition sandbox:

- 2 CPUs
- 2 GB memory
- 256 processes
- read-only root filesystem
- writable `/work` and `/tmp`
- outbound network access

### 2.2 Runtime Environment Variables

Platform credentials:

| Variable | Default | Description |
|---|---:|---|
| `CTF_BASE` / `CTFD_URL` | — | Base URL of the CTF platform. |
| `CTF_TOKEN` / `CTFD_TOKEN` / `CTF_SESSION` | — | Fresh CTFd access token for the current run. |
| `WORK_ROOT` | `/work` | Root directory for challenge files and results. |
| `RESULTS_PATH` | `/work/results.json` | Final result file. |

Target selection:

| Variable | Default | Description |
|---|---:|---|
| `ARENA_MODE` | `auto` | `auto`, `practice`, or `competition`. `auto` selects `competition` when non-practice challenges exist, otherwise `practice`. |
| `INCLUDE_SOLVED` | `0` in Docker image | Include challenges already solved by the team. |
| `ONLY_IDS` | empty | Comma/space-separated challenge IDs to run. |
| `CATEGORIES` | empty | Comma/space-separated exact category names to include. |

Scheduling and concurrency:

| Variable | Default | Description |
|---|---:|---|
| `MAX_CONCURRENT_CHALLENGES` | `2` | Number of challenges processed concurrently. |
| `PRELOAD_WORKERS` | `2` | Parallel workers for static attachment preloading. |
| `AGENTS_PER_CHALLENGE` | `2` | Number of agents working in parallel on one challenge. |
| `DYNAMIC_AGENTS_PER_CHALLENGE` | `2` | Number of agents for one dynamic challenge. |
| `CHALLENGE_TIME_LIMIT_SECONDS` | `3600` | Per-challenge wall-clock deadline. |
| `RENEW_INTERVAL_SECONDS` | `60` | Background renewal interval for dynamic instances. |

Agent loop and budgets:

| Variable | Default | Description |
|---|---:|---|
| `MAX_STEPS` | `1000000` | Hard backstop for model calls per attempt. |
| `MAX_ATTEMPTS` | `5` | Maximum attempts per challenge. |
| `CONTEXT_WINDOW_TOKENS` | `1048576` | Model context window. |
| `CONTEXT_RESERVE_TOKENS` | `32768` | Reserved context tokens. |
| `MAX_ATTEMPT_TOKENS` | `1200000` | Cumulative token budget per attempt. |
| `MAX_PLAIN_REPLIES` | `5` | Stop after this many consecutive assistant replies without tool calls. |
| `MAX_TOOL_OUTPUT` | `16000` | Maximum characters returned from one shell command. |
| `VIEW_IMAGE_MAX_SIDE` | `1024` | Maximum rendered image side length for image analysis. |

Submission:

| Variable | Default | Description |
|---|---:|---|
| `SUBMIT_FLAGS` | `1` | `1` submits real flags. `0` intercepts and simulates success for local testing. |

LLM configuration:

| Variable | Default | Description |
|---|---:|---|
| `LLM_PROVIDER` | `deepseek` | Provider selection: `deepseek` or `openrouter`. |
| `LLM_API_KEY` | — | Explicit OpenAI-compatible API key. Highest priority. |
| `LLM_BASE_URL` | — | Explicit OpenAI-compatible base URL. Highest priority. |
| `LLM_MODEL` | — | Explicit model name. Highest priority. |
| `DEEPSEEK_API_KEY` | — | Direct DeepSeek API key. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Direct DeepSeek base URL. |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Direct DeepSeek model name. |
| `OPENROUTER_API_KEY` | — | OpenRouter API key. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter base URL. |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | OpenRouter model name. |

Build-time fallback LLM arguments:

| Build argument | Description |
|---|---|
| `DAY1_LLM_API_KEY` | Fallback API key embedded at build time. Overridden by runtime `LLM_API_KEY`. |
| `DAY1_LLM_BASE_URL` | Fallback base URL embedded at build time. |
| `DAY1_LLM_MODEL` | Fallback model embedded at build time. |

### 2.3 Local Configuration File

Copy the template and keep it private:

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

The local template contains the platform URL, access token, LLM provider settings, budgets, concurrency settings, and the submission toggle.

### 2.4 Build

```bash
set -a
. ./.env.local
set +a

docker build --platform linux/amd64 --provenance=false \
  -t incypher-agent \
  --build-arg DAY1_LLM_API_KEY="$OPENROUTER_API_KEY" \
  --build-arg DAY1_LLM_BASE_URL="$OPENROUTER_BASE_URL" \
  --build-arg DAY1_LLM_MODEL="$OPENROUTER_MODEL" .
```

### 2.5 Run Locally

Real-submission mode:

```bash
mkdir -p "$PWD/incypher-work"
chmod 777 "$PWD/incypher-work"

docker run -d --name incypher-agent \
  --cap-drop=ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  -v "$PWD/incypher-work:/work" \
  --tmpfs /tmp:rw,size=256m \
  --cpus 2 \
  --memory 2g \
  --pids-limit 256 \
  --network bridge \
  --env-file "$PWD/.env.local" \
  -e ARENA_MODE=auto \
  incypher-agent
```

Local no-submission test:

```bash
docker run -d --name incypher-agent-test \
  --cap-drop=ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  -v "$PWD/incypher-work:/work" \
  --tmpfs /tmp:rw,size=256m \
  --cpus 2 \
  --memory 2g \
  --pids-limit 256 \
  --network bridge \
  --env-file "$PWD/.env.local" \
  -e ARENA_MODE=auto \
  -e SUBMIT_FLAGS=0 \
  incypher-agent
```

### 2.6 Submit the Image

```bash
printf '%s' "$CTF_TOKEN" | docker login registry.in-cypher.com:5001 \
  --username "team-<team_id>" \
  --password-stdin

docker tag incypher-agent \
  "registry.in-cypher.com:5001/team-<team_id>/agent:latest"

docker push \
  "registry.in-cypher.com:5001/team-<team_id>/agent:latest"
```

The newest `:latest` image is what the platform runs.

### 2.7 Output

```text
/work/results.json
```

Runtime layout per challenge:

```text
/work/<challenge_id>/
├── shared/
├── attempts/
│   └── 001/
│       └── summary.txt
└── agents/
    ├── agent-0/
    │   ├── events.jsonl
    │   └── <challenge files>
    └── agent-1/
        ├── events.jsonl
        └── <challenge files>
```

## 3. Design and Architecture

### 3.1 Agent Design

The agent is implemented as a small model-tool loop.

Core components:

- `arena/entrypoint.py`: compatibility entrypoint for `python /opt/agent/main.py`.
- `arena/main.py`: global challenge scheduler.
- `arena/solver.py`: per-challenge orchestration, retries, timeout, and parallel agents.
- `arena/brain.py`: `Brain` implementation used by the solver.
- `arena/runtime/model.py`: OpenAI-compatible model client.
- `arena/runtime/loop.py`: model-tool loop and context/token budgets.
- `arena/runtime/tools.py`: `run_bash`, `submit_flag`, `view_image`, and `str_replace_editor`.
- `arena/runtime/connection_proxy.py`: local serialized TCP gateway for raw dynamic services.
- `arena/runtime/events.py`: JSONL event logging.
- `arena/runtime/summary.py`: deterministic attempt summaries.

Each agent receives one challenge prompt containing:

- challenge name, category, points, and type;
- full description;
- names of downloaded files;
- live connection information when applicable;
- previous attempt summary when applicable.

The model can call tools to inspect files, run shell commands, view images or DICOM files, edit scripts, and submit candidate flags. The runtime enforces context and token budgets, a step backstop, and a consecutive no-tool-call limit.

Multimodal analysis:

- `view_image` reads local PNG/JPEG/GIF/WebP images and DICOM files.
- DICOM files are rendered through `pydicom` and `numpy`.
- Images are resized, encoded, and attached to the next model turn as image content.
- Image token cost is estimated with a fixed budget instead of counting base64 bytes as text.

### 3.2 Runtime Environment

The image is designed for the competition sandbox:

- read-only root filesystem;
- writable `/work` and `/tmp`;
- no Linux capabilities;
- no privilege escalation;
- 2 CPUs, 2 GB memory, 256 PIDs;
- outbound network access.

The image entrypoint is:

```text
python /opt/agent/main.py
```

`/opt/agent/main.py` is a thin shim to `arena.main`, so the scheduler runs whether the runtime honors the image entrypoint or directly invokes `/opt/agent/main.py`.

Platform credentials are read at runtime. The platform client refreshes the token from `CTF_TOKEN`, `CTFD_TOKEN`, or `CTF_SESSION` before every platform call, so a freshly injected token is used for submissions.

### 3.3 Concurrency

Concurrency is configured at two levels:

- Global challenge concurrency: `MAX_CONCURRENT_CHALLENGES`, default `2`.
- Per-challenge agent concurrency: `AGENTS_PER_CHALLENGE`, default `2`.
- Dynamic challenges use `DYNAMIC_AGENTS_PER_CHALLENGE`, default `2`.

This yields up to `2 × 2 = 4` active agents under the default settings.

Isolation:

- challenge attachments are downloaded once into `/work/<id>/shared`;
- each agent copies attachments into `/work/<id>/agents/agent-N`;
- each agent has its own working directory, event log, and scratch files;
- the shell tool blocks parent-directory traversal and absolute `/work` access outside the agent workspace;
- file editing and image viewing resolve paths inside the agent workspace.

Dynamic challenges:

- at most one dynamic instance is live globally;
- raw TCP dynamic services are exposed to agents through a local serialized gateway;
- the gateway handles the proof-of-work gate and permits only one upstream session at a time;
- HTTP dynamic services are used directly;
- dynamic instances are renewed in the background and destroyed when the challenge attempt ends.

Summary sharing:

- agents do not communicate during solving;
- each agent writes its own summary when it exits;
- all summaries for an attempt are appended to the same `summary.txt` under a thread lock.

### 3.4 Task-Level Scheduling

Each challenge follows this control flow:

1. Prepare attachments.
2. Boot a dynamic instance if needed.
3. Start one or two agents in independent work directories.
4. Run the model-tool loop for each agent.
5. Stop when one agent solves the challenge or the time limit is reached.
6. Append agent summaries.
7. Destroy dynamic instances.
8. Retry up to `MAX_ATTEMPTS`.
9. On timeout, mark the challenge for requeue and move it to the tail of the global queue.

Per-challenge controls:

- `CHALLENGE_TIME_LIMIT_SECONDS` default `3600`;
- `MAX_ATTEMPTS` default `5`;
- `MAX_STEPS` default `1000000`;
- `CONTEXT_WINDOW_TOKENS` default `1048576`;
- `MAX_ATTEMPT_TOKENS` default `1200000`;
- `MAX_PLAIN_REPLIES` default `5`.

### 3.5 Global Scheduling

The global scheduler in `arena/main.py`:

1. Connects to the platform using environment credentials.
2. Fetches the challenge list.
3. Resolves the target mode (`auto`, `practice`, or `competition`).
4. Fetches the team's solved challenge IDs.
5. Selects targets by mode, solved state, optional IDs, and optional categories.
6. Loads the full challenge record for every selected challenge.
7. Sorts selected challenges by points ascending, then by ID.
8. Preloads static challenge attachments in parallel.
9. Runs the main scheduling loop.
10. Writes `/work/results.json` after each challenge finishes.

Scheduling policy:

- dynamic challenges are globally serialized to one live instance;
- if both static and dynamic work remain, one dynamic challenge is kept in flight and remaining concurrency slots are filled with static challenges;
- each challenge runs up to `AGENTS_PER_CHALLENGE` agents in parallel;
- timed-out challenges are appended back to the tail of their queue;
- result entries include start time, finish time, duration, attempts, agent count, and submission status.

## References

- https://www.imperial.ac.uk/about/global/singapore/research/in-cypher/in-cypher-hackathon/
- https://hackathon.in-cypher.com/how-to-play
- https://hackathonlive.in-cypher.com/
