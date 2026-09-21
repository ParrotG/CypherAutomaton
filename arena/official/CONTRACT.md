# IN-CYPHER Agent Arena — the contract

> ## The goal is a fully autonomous agent.
>
> It finds the flags and submits them **on its own, unattended**, in a container nobody is
> logged into. Solving a challenge by hand and pasting the flag is not the exercise — what
> is scored is what your agent does while nobody is touching the keyboard. Build for the
> run you will not be present for.

You build an autonomous agent; the organizers run it against the live CTF and collect its
results. **This file ships inside the image you run in**, so it cannot drift from reality.
The code next to it in `/opt/agent/` is the same code that runs in the arena — read it as the
authoritative reference:

| File | What it tells you |
|---|---|
| `ctfd.py`  | The platform client: every endpoint, the auth headers, `connect_pwn()` for PoW-gated challenges. |
| `main.py`  | Which environment variables are read, and that results go to `/work/results.json`. |
| `solver.py`| The `Brain` interface, the 120s `run_bash` timeout, `/work/<id>/` file layout, instance boot/destroy. |
| `brain.py` | A working reference agent — the interface by example. |

Live instructions: **https://hackathonlive.in-cypher.com/usage**

---

## 1. What you write

Implement a `Brain` class in `brain.py`:

```python
class Brain:
    def __init__(self, run_bash, submit_flag, max_steps=40):
        self.run_bash = run_bash        # run a shell command in your sandbox -> str
        self.submit_flag = submit_flag  # submit a candidate flag -> verdict dict
        self.max_steps = max_steps

    def solve(self, prompt: str) -> dict:
        # investigate ONE challenge, then submit its flag
        return {"solved": True, "steps": 3, "flag": "INCYPHER{...}"}
```

`prompt` describes one challenge: name, category, description, downloaded files (under
`/work/<id>/`) and live connection info (a URL for web, or host:port for pwn). Every
`run_bash` command times out after **120 seconds**.

You may instead replace `main.py` / `solver.py` and drive the whole loop yourself. The only
hard requirements are the environment contract below and writing `/work/results.json`.

## 2. Environment injected at run time

**Never bake your CTFd access token into an image.** It is injected, it is personal to
you, and anything inside a pushed image is readable by the organizers.

Credentials are all you are given. Nothing else is injected — a step budget or a challenge
filter would be advice, not a constraint, because the code is yours. The real limits are the
sandbox in §3.

```
CTF_TOKEN / CTFD_TOKEN    a token minted fresh for your team at the start of each run
CTF_BASE  / CTFD_URL      https://hackathon.in-cypher.com
```

Both spellings carry the same value, so an agent written against either name works.

Read `CTF_BASE` from the environment rather than hard-coding it: if the main route fails
mid-event we repoint every agent by changing that one value, and a hard-coded URL will not
follow.

**The LLM is yours on day 1.** No `LLM_*` variable is injected, so configure your own however
you like — `ENV` in your Dockerfile, or directly in your code.

**On day 2 the organizers supply the LLM** so every team runs the same model. Three
variables are injected that day:

```
LLM_BASE_URL   an OpenAI-compatible /chat/completions endpoint
LLM_MODEL      the model we run that day
LLM_API_KEY    supplied by the organizers
```

**Read all three from the environment; do not hard-code any of them.** The endpoint is
OpenAI-compatible, which is all your code needs to assume — the model is chosen on the day
and is not announced in advance, so an agent tuned to one specific model name is a bet you
can lose. A runtime value overrides a Dockerfile `ENV`, so on day 2 your own key and model
would be ignored anyway.

Using an LLM is optional — a purely scripted agent is perfectly valid.

> Running the reference `brain.py` locally **without** `LLM_API_KEY` reports
> `unsolved ... (1 steps)` on every challenge. That is the missing-key signature, not a bug in
> your code. Export your own `LLM_API_KEY` (any OpenAI-compatible endpoint), or write a
> scripted Brain that never calls an LLM — equally valid, and it needs no key at all.

## 3. The sandbox you will run in

This is applied by the organizers at run time — you cannot see it from inside the image, and
it is the most common reason a submission that "works locally" fails:

| Constraint | Value |
|---|---|
| Root filesystem | **read-only** |
| Writable paths | **`/work` and `/tmp` only** |
| Linux capabilities | **all dropped** (`--cap-drop=ALL`); even root obeys file permissions |
| Privilege escalation | disabled (`no-new-privileges`) |
| CPU / memory / processes | 2 CPUs / 2 GB / 256 PIDs |
| Network | outbound internet is reachable; on day 2 you are expected to use the injected LLM |
| Lifetime | no wall-clock limit; the run ends when your agent exits, then the container is removed. Write `/work/results.json` as you go — partial results survive |

Write results to **`/work/results.json`**. Anything written elsewhere is lost, and a write
outside `/work` or `/tmp` fails.

## 4. If you write your own HTTP client

`ctfd.py` already handles both of these. If you roll your own, you must too:

- Send a **browser-like `User-Agent`**. The platform is behind Cloudflare, which rejects
  default library agents (`Python-urllib` / `requests` → error 1010).
- Send **`Content-Type: application/json`**. Without it the platform ignores your token and
  redirects you to the login page — which looks exactly like an invalid token, but isn't.

## 5. PoW-gated challenges

Raw-TCP (pwn) challenges sit behind a per-team proof-of-work gate. Don't implement it:

```python
from ctfd import connect_pwn
sock = connect_pwn(host, port, team_key)   # team_key comes with the connection info
sock.sendall(b"...")
```

## 6. How to submit

Your team ID is the number in your team page URL (`/teams/142`). Your password everywhere is
your **CTFd access token** (Settings → Access Tokens) — there is no separate registry password,
and you can only ever act as your own team.

You submit by pushing an image. There is no file upload — the tag you push **is** your
submission, and the newest `:latest` is what we run.

```bash
echo '<your CTFd token>' | docker login registry.in-cypher.com:5001 -u team-142 --password-stdin
docker pull registry.in-cypher.com:5001/base/agent-base:latest      # this image
# build yours FROM it, then:
docker build --platform linux/amd64 --provenance=false -t my-agent .
docker tag  my-agent registry.in-cypher.com:5001/team-142/agent:latest
docker push registry.in-cypher.com:5001/team-142/agent:latest
```

The arena runs on **x86_64**. On an Apple Silicon Mac, keep `--platform linux/amd64`: without
it the push still succeeds and the image then dies in the arena with `exec format error`.

You may pull `base/*` but never push to it, and you may only push to `team-<your id>/`.

**Check it before you submit** — `check_agent.sh` runs your image under the exact flags in §3:

```bash
./check_agent.sh my-agent:latest                    # structural checks
./check_agent.sh my-agent:latest <your token> 90    # + a live run
```

## 7. Rules

- Attack **only the challenge instances**. Never the platform, the Agent API, the registry, the
  ingress hosts, or another team's instances or repository. This is disqualifying.
- Respect rate limits; don't hammer submit or deploy.
- Destroy instances you are finished with — they are a shared resource.
- Flags are **unique to your instance**; another team's flag scores `incorrect`.
- Don't fabricate flags. Wrong submissions are recorded.
