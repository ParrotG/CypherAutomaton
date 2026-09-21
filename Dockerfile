FROM registry.in-cypher.com:5001/base/agent-base:latest

ENV PYTHONPATH=/opt/agent \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "openai>=1.0,<2"

# Official base already provides /opt/agent/ctfd.py and the toolchain.
COPY arena /opt/agent/arena

WORKDIR /opt/agent
ENTRYPOINT ["python", "-m", "arena.main"]
