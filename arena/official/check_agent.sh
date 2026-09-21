#!/usr/bin/env bash
# check_agent.sh — validate an agent image against the IN-CYPHER Arena contract.
#
# The SAME script the organizers run on your submission, so a local PASS means it will
# run in the arena. It starts your image under the exact production sandbox.
#
#   ./check_agent.sh <image> [CTF_TOKEN] [seconds]
#
#   ./check_agent.sh my-agent:latest                      # structural checks only
#   ./check_agent.sh my-agent:latest ctfd_xxx 90          # + live run against the platform
#
set -uo pipefail
IMG="${1:?usage: check_agent.sh <image> [CTF_TOKEN] [seconds]}"
TOKEN="${2:-}"
WINDOW="${3:-60}"
CTF_BASE="${CTF_BASE:-https://hackathon.in-cypher.com}"
LLM_ENV="${LLM_ENV:-}"           # organizers point this at an env-file; contestants export LLM_API_KEY
ONLY_IDS="${ONLY_IDS:-78}"        # one quick web challenge keeps the check short
NAME="agentcheck-$$"

pass=0; fail=0; warn=0
ok(){   printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
no(){   printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
wr(){   printf '  \033[33mWARN\033[0m  %s\n' "$1"; warn=$((warn+1)); }
cleanup(){ docker rm -f "$NAME" >/dev/null 2>&1; [ -n "${WORKDIR_HOST:-}" ] && rm -rf "$WORKDIR_HOST"; }
trap cleanup EXIT

echo "=== IN-CYPHER agent image check: $IMG ==="

# ---------------------------------------------------------------- 1. image present
if docker image inspect "$IMG" >/dev/null 2>&1; then
  ok "image found locally"
elif docker pull -q "$IMG" >/dev/null 2>&1; then
  ok "image pulled from registry"
else
  no "image not found and could not be pulled — nothing else can be checked"; exit 1
fi

ARCH=$(docker image inspect "$IMG" --format '{{.Architecture}}')
if [ "$ARCH" = "amd64" ]; then ok "architecture amd64 (matches the arena)"
else no "architecture is '$ARCH' — the arena is x86_64; rebuild with: docker build --platform linux/amd64 ..."; fi

SIZE=$(docker image inspect "$IMG" --format '{{.Size}}')
SIZE_MB=$((SIZE/1000000))
if [ "$SIZE_MB" -lt 4000 ]; then ok "image size ${SIZE_MB}MB (under the 4GB guideline)"
else wr "image size ${SIZE_MB}MB is large — it will be slow to pull for every run"; fi

# ---------------------------------------------------------------- 2. start command
ENTRY=$(docker image inspect "$IMG" --format '{{json .Config.Entrypoint}}')
CMD=$(docker image inspect "$IMG" --format '{{json .Config.Cmd}}')
if [ "$ENTRY" != "null" ] || [ "$CMD" != "null" ]; then
  ok "declares a start command (entrypoint=$ENTRY cmd=$CMD)"
else
  no "no ENTRYPOINT or CMD — the arena cannot start your agent"
fi

USER_=$(docker image inspect "$IMG" --format '{{.Config.User}}')
[ -z "$USER_" ] && wr "image runs as root (allowed, but all capabilities are dropped anyway)" \
                || ok "image runs as user '$USER_'"

# ---------------------------------------------------------------- 3. hardened start
# These are the EXACT flags the arena uses. If your image needs anything more, it fails here.
WORKDIR_HOST=$(mktemp -d); chmod 777 "$WORKDIR_HOST"   # outlives the container; writable under cap-drop=ALL
RUNARGS=(--name "$NAME" --cap-drop=ALL --security-opt no-new-privileges:true
         --read-only -v "$WORKDIR_HOST:/work" --tmpfs /tmp:rw,size=256m
         --cpus 2 --memory 2g --pids-limit 256 --network bridge
         -e CTF_BASE="$CTF_BASE" -e ONLY_IDS="$ONLY_IDS" -e MAX_STEPS=6)
[ -n "$TOKEN" ] && RUNARGS+=(-e CTF_TOKEN="$TOKEN")
if [ -n "$LLM_ENV" ] && [ -f "$LLM_ENV" ]; then
  RUNARGS+=(--env-file "$LLM_ENV")
elif [ -n "${LLM_API_KEY:-}" ]; then
  RUNARGS+=(-e LLM_API_KEY="$LLM_API_KEY")
  [ -n "${LLM_BASE_URL:-}" ] && RUNARGS+=(-e LLM_BASE_URL="$LLM_BASE_URL")
  [ -n "${LLM_MODEL:-}" ]    && RUNARGS+=(-e LLM_MODEL="$LLM_MODEL")
elif [ -n "$TOKEN" ]; then
  wr "no LLM_API_KEY in your environment — an LLM-based agent will get HTTP 401 below"
fi

docker rm -f "$NAME" >/dev/null 2>&1
if docker run -d "${RUNARGS[@]}" "$IMG" >/dev/null 2>&1; then
  ok "starts under the production sandbox (cap-drop ALL, no-new-privileges, read-only root)"
else
  no "failed to start under the production sandbox — check you don't need extra privileges"
  exit 1
fi

# ---------------------------------------------------------------- 4. survives / behaves
sleep 10
STATE=$(docker inspect "$NAME" --format '{{.State.Status}}' 2>/dev/null)
EXITC=$(docker inspect "$NAME" --format '{{.State.ExitCode}}' 2>/dev/null)
OOM=$(docker inspect "$NAME" --format '{{.State.OOMKilled}}' 2>/dev/null)
EARLYLOG=$(docker logs "$NAME" 2>&1)
LOGBYTES=$(printf '%s' "$EARLYLOG" | wc -c)

# A write to the read-only root is the single most common porting mistake: the container
# may keep running while every write silently fails, so look for it explicitly.
if printf '%s' "$EARLYLOG" | grep -qiE 'read-only file system|readonly file system'; then
  no "tried to write outside /work and /tmp — the root filesystem is read-only"
  printf '%s' "$EARLYLOG" | grep -iE 'read-only file system' | head -2 | sed 's/^/        /'
fi

if [ "$OOM" = "true" ]; then
  no "OOM-killed — your agent exceeded the 2GB memory cap"
elif [ "$STATE" = "running" ]; then
  ok "still running after 10s (no crash loop)"
elif [ -z "$TOKEN" ] && printf '%s' "$EARLYLOG" | grep -qiE 'CTF_TOKEN|access token|credential'; then
  ok "exits without CTF_TOKEN, as expected (pass a token to exercise it properly)"
elif [ "$LOGBYTES" -eq 0 ]; then
  no "exited within 10s having produced no output at all — it does not look like an agent"
elif [ "$EXITC" = "0" ]; then
  wr "exited cleanly within 10s — fine only if it really finished its work"
else
  no "exited with code $EXITC within 10s — it is crashing on startup"
  echo "        --- last log lines ---"
  printf '%s' "$EARLYLOG" | tail -12 | sed 's/^/        /'
fi

# ---------------------------------------------------------------- 5. live behaviour
if [ -n "$TOKEN" ]; then
  echo "  ...running ${WINDOW}s to observe real behaviour"
  sleep "$WINDOW"

  LOG=$(docker logs "$NAME" 2>&1)
  if echo "$LOG" | grep -qiE 'auth=token|challenges to attempt|/api/agent/v1'; then
    ok "talks to the platform API with the injected token"
  else
    wr "no sign it called the platform API (is it using CTF_TOKEN and CTF_BASE?)"
  fi
  if echo "$LOG" | grep -qiE 'error 1010|Redirecting|/login'; then
    no "platform auth looks wrong — send a browser User-Agent AND Content-Type: application/json"
  fi

  if [ -s "$WORKDIR_HOST/results.json" ]; then
    if python3 -c "import json; json.load(open('$WORKDIR_HOST/results.json'))" >/dev/null 2>&1; then
      ok "wrote a valid JSON /work/results.json"
    else
      no "/work/results.json exists but is not valid JSON"
    fi
  else
    wr "no /work/results.json yet — expected if it had not finished a challenge in ${WINDOW}s"
  fi

  read -r CPU MEM <<<"$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' "$NAME" 2>/dev/null)"
  [ -n "${CPU:-}" ] && ok "resource use within caps (cpu $CPU, mem $MEM)"
else
  wr "no CTF_TOKEN given — skipped the live platform checks (pass one to run them)"
fi

echo
echo "=== $pass passed · $fail failed · $warn warnings ==="
[ "$fail" -gt 0 ] && { echo "Fix the FAIL items — the arena runs exactly these flags."; exit 1; }
echo "Looks good: this image will run in the arena."
exit 0
