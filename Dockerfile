FROM registry.in-cypher.com:5001/base/agent-base:latest

# Day 1 has no injected LLM_* variables.  Pass the team's own Day 1/2 values
# via --build-arg; Day 2 arena-injected LLM_* env vars override these at runtime.
# Keep the defaults empty: never commit real API keys into the repository.
ARG DAY1_LLM_API_KEY=
ARG DAY1_LLM_BASE_URL=
ARG DAY1_LLM_MODEL=

ENV PYTHONPATH=/opt/agent \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ARENA_MODE=auto \
    INCLUDE_SOLVED=0 \
    SUBMIT_FLAGS=1 \
    MAX_STEPS=1000000 \
    MAX_ATTEMPTS=5 \
    MAX_CONCURRENT_CHALLENGES=2 \
    PRELOAD_WORKERS=2 \
    CONTEXT_WINDOW_TOKENS=1048576 \
    CONTEXT_RESERVE_TOKENS=32768 \
    MAX_ATTEMPT_TOKENS=1200000 \
    MAX_PLAIN_REPLIES=5 \
    MAX_TOOL_OUTPUT=16000 \
    AGENTS_PER_CHALLENGE=2 \
    DYNAMIC_AGENTS_PER_CHALLENGE=2 \
    CHALLENGE_TIME_LIMIT_SECONDS=3600 \
    RENEW_INTERVAL_SECONDS=60 \
    VIEW_IMAGE_MAX_SIDE=1024 \
    LLM_API_KEY=${DAY1_LLM_API_KEY} \
    LLM_BASE_URL=${DAY1_LLM_BASE_URL} \
    LLM_MODEL=${DAY1_LLM_MODEL}

RUN pip install --no-cache-dir "openai>=1.0,<2" "Pillow>=10.0" "numpy>=1.26" "pydicom>=2.4"

# Official base already provides /opt/agent/ctfd.py and the toolchain.
COPY arena /opt/agent/arena

# Some arena runners invoke /opt/agent/main.py directly rather than honoring
# the image ENTRYPOINT.  Replace the base main.py with a thin shim into the
# arena scheduler so both invocation styles run the same code.
COPY arena/entrypoint.py /opt/agent/main.py

WORKDIR /opt/agent
ENTRYPOINT ["python", "/opt/agent/main.py"]
