# BadLE Hard fallback handoff

This is a separate, ID-4-only image for a team-selected fallback run. The
general agent and its Dockerfile are unchanged. Building or replaying this
image locally does not update the team's registry tag or contact an instance.

## Candidate status and limitation

The saved `d2-static-20260922-113830` run recorded a candidate in 5.2 seconds,
including download/controller overhead. It did **not** submit it. That saved
candidate used the wrong `flag{...}` prefix and is superseded. The challenge
screenshot requires `INCYPHER{v1,...,v8}` with no spaces. The corrected
candidate SHA-256 is
`0ead3e39de337c3285facb1039244faa5570b0588343e71fba7e309b49cf6ac5`.
Only a `correct` platform verdict confirms acceptance; `already_solved` does
not mean this fallback earned new points.

The numeric body is evidence-supported: for the first eight active F003 frames,
capture payload index 4 XOR `0x7c` gives a smooth 96--106 mg/dL series, or
5.3--5.9 mmol/L. It is the only nonconstant byte position that stays entirely
inside the organizer's stated range. Literal displayed index 6 produces
out-of-range, discontinuous values. The implementation therefore selects
capture index 4 from the organizer's range/smoothness oracle. The log begins
directly with the 17-byte `af e8 86 ...` value and does not expose a two-byte
prefix, so the origin of the `frame[6]` versus capture-index-4 discrepancy and
platform acceptance are still unverified.

The fallback uses only the corrected candidate without trying alternatives. It stops
after one submission callback, including an incorrect or uncertain response.
Each new organizer run is a separate attempt; do not repeatedly re-upload an
unchanged candidate after rejection.

## Contents and behavior

- `deploy/badle_hard/entrypoint.py`: fixed identity
  `4 / BadLE Hard / healthcare / standard`, with two exact artifact digests.
- `deploy/badle_hard/Dockerfile`: pinned organizer base, Linux amd64; both the
  image entrypoint and direct `/opt/agent/main.py` invocation enter this same
  ID-4-only profile.
- `deploy/badle_hard/Dockerfile.dockerignore`: only the runner, static decoder,
  and shared scope/evidence runtime enter the build context.
- `tests/test_badle_hard_submission.py`: offline contract, replay and callback tests.
- `scripts/package_badle_hard.py`: creates a source-only ZIP and hash manifest.

The entrypoint ignores `ONLY_IDS`, does not enumerate challenges, and fetches
only ID 4. It rejects identity, filename, size, hash and connection changes.
There are no dynamic-instance, shell or LLM calls. It reads `CTF_BASE`/`CTFD_URL`
and `CTF_TOKEN`/`CTFD_TOKEN` injected by the arena. No LLM API key is needed.

No challenge files, candidate plaintext, credentials, `.env` files, previous
run directories or logs are embedded in the image/source ZIP. `/work/results.json`
contains timing, candidate hash and verdict. A record-only replay also creates
a private `.arena-candidates.jsonl`; keep that local file private.

## Local verification (no platform or registry writes)

Run from the repository root, or the root of the extracted source ZIP:

```powershell
python -B -m unittest discover -s tests -p test_badle_hard_submission.py -v

docker build --platform linux/amd64 --pull=false --network=none `
  -f deploy/badle_hard/Dockerfile `
  -t incypher-badle-hard:record-20260922-r2 .

$artifacts = (Resolve-Path 'arena-runs/d2-intake-20260922-1/4').Path
$run = Join-Path $PWD ('arena-runs/badle-hard-replay-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $run | Out-Null
docker run --rm --network=none --read-only --cap-drop=ALL `
  --security-opt no-new-privileges --cpus=2 --memory=2g --pids-limit=256 `
  --tmpfs /tmp:rw,nosuid,nodev,size=128m `
  --mount "type=bind,source=$artifacts,target=/input,readonly" `
  --mount "type=bind,source=$run,target=/work" `
  incypher-badle-hard:record-20260922-r2 --replay-dir /input
Get-Content (Join-Path $run 'results.json')
```

The handouts are deliberately not in the ZIP. Use your exact local ID 4 intake
directory for `$artifacts`. Tests requiring it skip when it is absent; skipped
fixture tests do not count as a completed replay. The expected result is
`candidate_recorded`, `submission_attempted: false`, the hash above, and
`solved: false`. Use a fresh output directory for each replay.

`--replay-dir` always forces record mode, even on the submission-enabled image.
The image requires the cached organizer base for an offline build. If that base
is missing, obtain it through the team's normal authenticated registry access.

## Build the submission-enabled image locally

```powershell
docker build --platform linux/amd64 --pull=false --network=none `
  -f deploy/badle_hard/Dockerfile `
  --build-arg BADLE_HARD_SUBMIT_MODE=submit `
  -t incypher-badle-hard:submit-20260922-r2 .

$image = docker image inspect incypher-badle-hard:submit-20260922-r2 | ConvertFrom-Json
$image | Select-Object Id,Os,Architecture
$image.Config.Env | Where-Object { $_ -like 'ARENA_SUBMIT_MODE=*' }
$image.Config.Entrypoint
```

Expect `linux / amd64`, `ARENA_SUBMIT_MODE=submit`, and an entrypoint ending in
`/opt/agent/badle_hard_entry.py`. Run the same network-disabled replay command
with this tag and a fresh output directory; it must still record without submitting.
Do not run the image locally in live mode with your personal CTFd token.

## Compatibility with the two team pipelines

Both linked team repositories were inspected read-only at their 22 September
HEADs. Neither repository has a CI deployment workflow or an executable static
solver-plugin seam. Both document a manual Docker build/tag/push flow.

- `cirnovsky/igs-cc` commit
  [`649686ebc6506b85adbe3d3e425761294369efae`](https://github.com/cirnovsky/igs-cc/tree/649686ebc6506b85adbe3d3e425761294369efae)
  owns `/opt/agent/main.py`, its Brain, and its entrypoint. It retries unsolved
  challenges over several rounds. Its `NO_SUBMIT=1` mode reports an unverified
  candidate as solved, so that result is not platform acceptance.
- `ParrotG/CypherAutomaton` commit
  [`6e8785a0ce0f4887dfd611a2e9ae098d9c49e1cb`](https://github.com/ParrotG/CypherAutomaton/tree/6e8785a0ce0f4887dfd611a2e9ae098d9c49e1cb)
  owns `arena.main` and the controller submission callback. Its
  `SUBMIT_FLAGS=0` path fabricates a `correct` status for dry runs, so that also
  cannot confirm the candidate. It defaults to two agents per challenge.

This fallback is therefore a deliberately separate image, not a patch to
either team's live scheduler. Do not copy its entrypoint over either repository
or interpret `NO_SUBMIT`/`SUBMIT_FLAGS=0` output as acceptance. Build it from
this checkout. It uses no LLM key and, in a real arena run, fetches and attempts
only challenge ID 4 once.

An in-place integration would require a reviewed controller-level hook before
the normal Brain call, preserving the team's typed challenge identity and its
existing submission callback. That integration is not part of this last-resort
package.

## Team handoff and activation

Team 150 has one shared `registry.in-cypher.com:5001/team-150/agent:latest`.
This fallback handles only BadLE Hard; replacing that tag does not merge its
behavior into your teammate's dynamic solver. Building and offline replay do
not affect the running agent. Pushing the shared `latest` tag is different: it
selects this fallback for an organizer cycle, so choose that handoff point with
the team after the current dynamic run.

The saved official [submission page](<how-to-submit __ IN-CYPHER.html>) says a
push is the entry, a push during a running container waits for a later cycle,
and D2 re-uploads after the first scored run cost 100 points. These are saved
instructions, not freshly verified queue/penalty policy. Check the current
[organizer instructions](https://hackathonlive.in-cypher.com/usage) and your
team's status before activation. No replacement or push is part of local preparation.

When the team selects this fallback, use an existing authorized Docker login,
then run:

```powershell
$registryImage = 'registry.in-cypher.com:5001/team-150/agent:latest'
docker tag incypher-badle-hard:submit-20260922-r2 $registryImage
docker push $registryImage
```

That push is the activation step. Record the digest printed by Docker and
compare it with the organizer's run record. Do not push the `record` image.
If Docker needs authentication, run `docker login registry.in-cypher.com:5001
--username team-150` interactively and enter the team's authorized registry
credential at its prompt. Never put the credential in source, a build argument,
the README, or a command saved to shell history.

The organizer must run it with the injected **agent-account** token. A manual
web submission or personal-token local submission does not establish an agent
solve for `VALID`/`NET`. After the run, inspect both `verdict.status` and `final`
for ID 4 in `/work/results.json`, then check the status board under `agent-150`:

- `verdict.status=correct` gives `final=platform_confirmed`. Confirm counted
  credit on the board.
- `verdict.status=already_solved` gives `final=account_already_solved`. The
  account had already solved ID 4; this does not validate the candidate or earn
  new credit.
- `verdict.status=incorrect` gives `final=candidate_rejected`; the runner stops.
- `final=submission_unconfirmed` means a transport, unknown-verdict, or
  post-submission processing failure. Check the board before considering
  another run, because acceptance may have occurred.
- `final=profile_rejected` means handout/identity drift or another precondition
  failed before submission; no model or other challenge fallback occurs.

Preserve the previous image digest in the team handoff. Restoring an older
`latest` is another registry update and may trigger another run/penalty; it is
not an automatic rollback step.

## Make a small source handoff

```powershell
python -B scripts/package_badle_hard.py --output releases/badle-hard-20260922-r2
```

Send the resulting source ZIP, manifest and this document through your normal
team channel. The script includes only named source/test/doc files and refuses
to overwrite an existing release directory. The ZIP contains no handouts or flags.
