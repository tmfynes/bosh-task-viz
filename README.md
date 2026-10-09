# bosh-task-viz

Live BOSH task visualizer for Cloud Foundry (basement / `homelab1.fynesy.com`).

SSHes to Ops Manager using a bound user-provided service, runs `bosh` there
(read-only, never shells out to a local `bosh` binary), and renders a live
D3 time-series of every processing BOSH task: how long each chore takes
along the rollout, with noise stripped from `bosh task <id> --debug`.

## Run locally (demo mode, no Ops Manager needed)

```
PORT=8080 DEMO=1 python app.py
```

This replays `fixtures/task-84098.events.jsonl` and
`fixtures/task-84098.debug.redacted.log` so the Gantt and debug drawer have
something to show without a real SSH connection. `DEMO=1` is also implied
automatically whenever `VCAP_SERVICES` is absent.

Open http://localhost:8080.

## Run against a real Ops Manager

Bind a **user-provided service** named `ops-manager-ssh` with credentials:

```json
{
  "host": "om1.homelab1.fynesy.com",
  "ip": "192.168.1.20",
  "port": 22,
  "user": "ubuntu",
  "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n"
}
```

Prefer a JSON file so the private key never lands in shell history:

```
cf cups ops-manager-ssh -p ops-manager-ssh.json
# already exists?
cf uups ops-manager-ssh -p ops-manager-ssh.json
cf push bosh-task-viz
```

The matching public key must already be in `ubuntu@om1` `~/.ssh/authorized_keys` (OM sshd is publickey-only). See `ops-manager-ssh.example.json`.

App key generated for basement (public half only):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBo4ajjOBduDqGLAQyzwMmgHMR0V5Ve4Nq9n+1/Jf1zL bosh-task-viz@basement
```

The app connects over SSH (paramiko, `host` then falls back to `ip`),
reconnects on failure, and runs every command as `bash -lc 'bosh <args>'`
(falling back to sourcing `/home/ubuntu/.profile` first if `bosh` isn't on
PATH in a non-login shell). The private key is loaded straight from the
credentials string into memory -- it is never logged, written to a
world-readable path, or echoed back in any API response.

## What it does

- Polls `bosh tasks --recent=40 --json` every second for the task list.
- Auto-tracks every task with `state == processing`; a user can also pin
  any recent/done/error task via the pin icon (`POST /api/tasks/<id>/track`
  / `/untrack`).
- For each tracked task, follows `bosh task <id> --event` (NDJSON) to build
  a tree of "chores" -- one parent bar per `(stage, task, index)`, with
  nested sub-chore bars for drain/stop/install-packages/start phases -- and
  `bosh task <id> --debug` (noise-filtered, secrets redacted) for the
  bottom log drawer. Concurrent debug streams are capped at 8.
- Serves `GET /api/stream` (SSE) with a `snapshot` event every second
  (task list + ssh/bosh health + chores for tracked tasks) plus a
  heartbeat comment every 15s; the frontend also polls `/api/tasks` every
  2s as a fallback.

## Files

- `app.py` -- Flask app, background poller, SSE stream, REST API.
- `om_ssh.py` -- VCAP_SERVICES credential lookup, paramiko SSH session.
- `parse_bosh.py` -- pure functions: NDJSON events -> chore tree, debug
  log noise filter. No I/O, fully unit tested.
- `static/` -- dark ops-console SPA (vanilla JS + D3 v7, vendored at
  `static/d3.min.js`, no CDN fetch at runtime).

## Tests

```
python -m unittest discover -s tests -v
```

Parses both fixtures and asserts the chore tree (e.g. `Creating missing
vms` ~56s, nested `starting jobs`) and the debug filter (PEM blocks and
SQL dropped, secret values redacted, INFO/WARN/ERROR/FATAL kept).

## Out of scope

Read-only. No Apply Changes, no `bosh create`/`delete`, no writes to the
director.
