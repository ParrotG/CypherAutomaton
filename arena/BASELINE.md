# Official agent-base baseline

This directory vendors the official IN-CYPHER agent base files used as the
starting point for the arena submission.

- Registry image: `registry.in-cypher.com:5001/base/agent-base:latest`
- Pinned digest: `sha256:d3c707c6187f49a8b5f0ba617b9590cce72a18676d93343a93098726c7723224`
- Architecture: `linux/amd64`
- Extracted from `/opt/agent/` inside the image.

Official files:

- `CONTRACT.md`
- `README.md`
- `ctfd.py`
- `main.py`
- `solver.py`
- `brain.py`
- `local_test.py`
- `check_agent.sh`
- `SHA256SUMS`

The official image remains the source of truth for these files. Update this
directory by re-pulling the pinned digest and re-copying `/opt/agent/`.
