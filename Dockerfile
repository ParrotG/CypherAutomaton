FROM registry.in-cypher.com:5001/base/agent-base:latest

# Day 1 has no injected LLM_* variables.  Bake the team's own Day 1 values in
# at build time; Day 2 arena-injected LLM_* env vars override these at runtime.
ARG DAY1_LLM_API_KEY=
ARG DAY1_LLM_BASE_URL=
ARG DAY1_LLM_MODEL=

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

WORKDIR /opt/agent
ENTRYPOINT ["python", "-m", "arena.main"]
