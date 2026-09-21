FROM registry.in-cypher.com:5001/base/agent-base:latest

# Day 1 has no injected LLM_* variables.  Bake the team's own Day 1 values in
# at build time; Day 2 arena-injected LLM_* env vars override these at runtime.
ARG DAY1_LLM_API_KEY=<REDACTED>
ARG DAY1_LLM_BASE_URL=https://openrouter.ai/api/v1
ARG DAY1_LLM_MODEL=deepseek/deepseek-v4.1-flash

ENV INCLUDE_SOLVED=0
ENV MAX_ATTEMPTS=5
ENV CONTEXT_WINDOW_TOKENS=1048576
ENV MAX_ATTEMPT_TOKENS=1200000
ENV MAX_CONCURRENT_CHALLENGES=2

ENV PYTHONPATH=/opt/agent \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
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
