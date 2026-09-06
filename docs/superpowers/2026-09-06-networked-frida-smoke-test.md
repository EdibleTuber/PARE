# Smoke test: moving `frida` to the laptop

**Date:** 2026-09-06
**Spec:** [`specs/2026-09-05-networked-workers-design.md`](specs/2026-09-05-networked-workers-design.md) §9 step 5
**Why this is the proof:** every claim in that spec has so far been checked against
loopback, a killed subprocess, or a discard port. This is the first time a worker runs
on a machine the daemon does not own, over a network hop that can genuinely degrade.

## What this actually tests, and what it does not

It answers three questions, in order of how likely they are to bite:

1. **Does it work at all** — handshake, tool registration, a real attach driven from a
   machine that cannot see the emulator.
2. **Is hook-event polling usable over a tailnet hop.** Genuinely unknown. This is the
   risk the spec flags, and the reason step 5 exists before the hardware worker.
3. **Do the failure paths behave on a real degraded link** rather than on a process
   somebody killed. Killing a process is not the same event as a laptop sleeping: one
   closes the socket, the other drops the SYN.

It does **not** test large payloads (§7.3 is still open), and it does not test the
bench approval channel (that is D1, ArcticBase).

---

## Part 1 — On the laptop

### 1.1 `git pull` is not enough

Pulling gets the code. It does **not** install the new dependency, and this branch added
one:

```
pare-worker-kit @ git+https://github.com/EdibleTuber/pare-worker-kit.git@v0.1.1
```

So:

```bash
cd ~/path/to/pare-frida-mcp
git pull
pip install -e ".[dev]"        # <-- the part git pull does not do
```

Two things to know about that install:

- It pulls `pare-worker-kit` from GitHub. **It does not install `agent_core`**, and that
  is deliberate — the daemon side stays on the server. Some tests skip without it; that
  is correct, not a failure.
- If it fails with *"cannot be a direct reference"*, the checkout predates
  `allow-direct-references = true` in `pyproject.toml`. Pull again.

### 1.2 Confirm the worker can serve HTTP before involving the network

```bash
AGENT_WORKER_TRANSPORT=http AGENT_WORKER_HOST=127.0.0.1 AGENT_WORKER_PORT=9101 \
  pare-frida-mcp
```

Expect uvicorn to start. Then in another shell:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9101/mcp
```

Any HTTP status proves it is listening; the MCP handshake is the daemon's job. Stop it.

### 1.3 Check the refusal that protects an unauthenticated worker

```bash
AGENT_WORKER_TRANSPORT=http AGENT_WORKER_HOST=0.0.0.0 AGENT_WORKER_PORT=9101 \
  pare-frida-mcp; echo "exit=$?"
```

**Expect a non-zero exit** and a message naming the wildcard. This worker has no
authentication — the bind address is the access control — so a wildcard bind would
expose every frida tool to every network the laptop is on, including whatever coffee-shop
wifi it joins next. If this *succeeds*, stop and say so; something is wrong.

### 1.4 Find the tailnet address

```bash
tailscale ip -4
ip -4 addr show tailscale0 | grep inet
```

Use the **interface name**, not the address, so a tailnet re-address does not silently
strand the worker.

### 1.5 The unit

**Generate it; do not paste it.** An earlier version of this doc gave a template with
`User=<you>` and `ExecStart=/full/path/to/venv/bin/pare-frida-mcp`, and the placeholders
survived into a real unit file. systemd reported:

```
Process: ExecStart=/full/path/to/venv/bin/pare-frida-mcp (code=exited, status=217/USER)
```

`217/USER` means it could not resolve the `User=` line. It never got as far as the
binary, which would then have failed `203/EXEC` for the same reason. A placeholder that
looks fillable is a placeholder that gets shipped, so substitute the values instead.

**With the venv active**, compute the values into variables and LOOK at them before
anything is written:

```bash
SVC_USER="$(id -un)"
SVC_BIN="$(command -v pare-frida-mcp)"
echo "user=[$SVC_USER]"
echo "bin =[$SVC_BIN]"
ip -4 -o addr show tailscale0 | awk '{print "tailscale0 -> " $4}'
```

All three must be non-empty, and neither of the first two may contain a `$`. An empty
`bin` means the venv is not active or `pip install -e .` has not run; no `tailscale0`
line means tailscaled is not up yet.

Then write the unit from those variables:

```bash
sudo tee /etc/systemd/system/pare-frida-mcp.service > /dev/null <<EOF
[Unit]
Description=PARE frida worker
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Environment=AGENT_WORKER_TRANSPORT=http
Environment=AGENT_WORKER_HOST=tailscale0
Environment=AGENT_WORKER_PORT=9101
ExecStart=${SVC_BIN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Note the **unquoted** `<<EOF`: the shell expands `$(whoami)` and `$(command -v ...)` as it
writes. Quoting it (`<<'EOF'`) would put the literal `$(...)` in the file and reproduce
exactly the failure this replaces.

Now make the shell PROVE it substituted, rather than eyeballing it. This exact check is
here because eyeballing failed in practice: a unit went live containing the literal
`User=$(whoami)`, and it took a `systemctl cat` and a journal dump to see it.

```bash
if grep -q '\$' /etc/systemd/system/pare-frida-mcp.service; then
  echo "STILL HAS PLACEHOLDERS — do not enable"
  grep -n '\$' /etc/systemd/system/pare-frida-mcp.service
else
  echo clean
  sudo systemctl daemon-reload
  sudo systemctl enable --now pare-frida-mcp
fi
```

If it says STILL HAS PLACEHOLDERS, the heredoc is not being expanded — usually because
the text is being pasted into an editor rather than run, or because the heredoc got
quoted. Read the two `echo` values from the previous step and type them into the file
literally; that works every time.

Then:

```bash
systemctl status pare-frida-mcp
sudo ss -tlnp | grep 9101        # MUST show the tailnet IP, never 0.0.0.0
```

That last line is the one worth actually reading.

**Check all three `Environment=` lines survived.** A unit missing
`AGENT_WORKER_PORT` starts and immediately exits 1 with "AGENT_WORKER_PORT is required
when serving over http" -- the kit refusing to invent a default port, which is correct
but looks like a crash if you are not expecting it:

```bash
grep -c '^Environment=' /etc/systemd/system/pare-frida-mcp.service   # must be 3
```

**If it still will not start**, the exit code names the cause:

| Code | Means | Usually |
|---|---|---|
| `217/USER` | `User=` unresolvable | placeholder left in, or a typo'd username |
| `203/EXEC` | `ExecStart=` not executable | wrong path, or the venv moved |
| `1` with a wildcard message | the worker refused the bind | `AGENT_WORKER_HOST` resolved to `0.0.0.0` — this is the refusal working |
| `1` with "AGENT_WORKER_PORT is required" | no port in the unit | an `Environment=` line was dropped — expect 3 |
| `1`, no output | look at `journalctl -u pare-frida-mcp -n 50` | usually a missing dependency from a skipped `pip install` |

`systemctl cat pare-frida-mcp` is the command that ends the guessing: it prints what
systemd actually parsed, including any drop-in under `.service.d/` that editing the main
file would never touch. `sudo journalctl -u pare-frida-mcp -n 30` names the specific
failure -- for 217 it says "Failed to determine user credentials", which points at the
`User=` line and nothing else.

Note that `User=root` does NOT cause 217; root always resolves. If you see 217, the
value is unresolvable for some other reason -- a literal placeholder, a typo, or quotes
around the name.

### 1.6 The emulator

`frida-server` must be running on the device and `adb devices` must show it **on the
laptop**. The worker talks to frida locally; only PARE crosses the network.

---

## Part 2 — On the inference server

### 2.1 Point `workers.yaml` at the laptop

```yaml
  frida:
    endpoint: http://100.x.y.z:9101/mcp     # the laptop's tailnet address
    transport: streamable_http
    connect_timeout: 20
    read_timeout: 60
    risk_default: low
    autoload: false        # REQUIRED — see below
    capability_tags: [mobile, dynamic, android, frida]
```

Keep the existing `risk_overrides` pins. They are the only tier link not under the
worker's control, and a worker you now reach over a network is exactly the one that
needs them.

`autoload: false` is not ergonomics. A sleeping laptop does not refuse a connection, it
drops the SYN, so an autoloading networked worker costs its full connect timeout at
every daemon boot.

### 2.2 Run the harness

```bash
python scripts/smoke_networked_frida.py http://100.x.y.z:9101/mcp
```

It needs no inference server and no PARE daemon — it drives `agent_core` directly, so a
failure is the worker or the link, never the model.

---

## Part 3 — What to look for

| Check | Pass looks like |
|---|---|
| Handshake | tools listed, count matches the local build |
| Identity | `server_version` is **frida's** version, not `1.29.1` |
| Risk tiers | every tool carries a tier in `_meta` |
| Attach | a real session id, from a server that cannot see the emulator |
| **Poll latency** | the number that decides whether this is usable |
| Liveness | `reachable` goes False after the laptop is unplugged |
| Approvals | a held `scope: session` approval is gone after link loss |

### The one that matters

**Hook-event polling latency.** If a poll round-trip is comparable to loopback, this
design works. If it is hundreds of milliseconds, the co-pilot loop gets sluggish and the
hardware worker inherits that. The harness prints a distribution, not an average — a
p95 that is ten times the median means an unusable link that *looks* fine on average.

### Deliberately unplugging the laptop

Do this at the end. Turn off wifi, or `sudo systemctl stop pare-frida-mcp`. They are
different events and both are worth seeing: stopping the service closes the socket,
turning off wifi drops the SYN, and only the second resembles a sleeping laptop.

Within ~30s (the probe interval) `/worker list` should show **UNREACHABLE** with the
endpoint. Then `/worker unload frida` must say the remote process keeps running and
its attachments survive — because they do.
