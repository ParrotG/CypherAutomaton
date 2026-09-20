FROM python:3.10-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        bubblewrap \
        build-essential \
        ca-certificates \
        curl \
        file \
        gawk \
        gdb \
        git \
        libc6-dbg \
        netcat-openbsd \
        patchelf \
        procps \
        socat \
        unzip \
        wget \
        xxd \
        zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md uv.lock /app/
COPY src /app/src

RUN pip install --no-cache-dir .

RUN mkdir -p /data /app/.cypher_bridge

EXPOSE 8765

CMD ["incypher-bridge", "serve", "--api-host", "0.0.0.0", "--api-port", "8765", "--state-dir", "/data/bridge", "--env-file", "/app/.env.local"]
