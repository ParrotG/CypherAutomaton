# Isolated BadLE Hard fallback. No challenge files, flags, or credentials copied.
FROM registry.in-cypher.com:5001/base/agent-base@sha256:d3c707c6187f49a8b5f0ba617b9590cce72a18676d93343a93098726c7723224
ARG BADLE_HARD_SUBMIT_MODE=record
ENV ARENA_SUBMIT_MODE=${BADLE_HARD_SUBMIT_MODE} \
    PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="BadLE Hard static fallback" \
    org.incypher.challenge-id="4" \
    org.incypher.default-submit-mode=${BADLE_HARD_SUBMIT_MODE} \
    org.incypher.candidate-status="unsubmitted-index-mapping-inferred"
COPY deploy/arena/arena_runtime.py deploy/arena/d2_badle.py /opt/agent/
COPY deploy/badle_hard/entrypoint.py /opt/agent/badle_hard_entry.py
# Some arena runners invoke /opt/agent/main.py directly instead of honoring
# the image entrypoint. Keep that path on the same ID-4-only profile.
COPY deploy/badle_hard/entrypoint.py /opt/agent/main.py
ENTRYPOINT ["python3", "-B", "/opt/agent/badle_hard_entry.py"]
CMD []
