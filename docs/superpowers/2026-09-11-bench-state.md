# Bench state, 2026-09-11

Written before a context clear. **Every value here was read from the machine, not
recalled** — and each section names the command that re-reads it, because a
snapshot rots and a command does not.

## Machines

| | |
|---|---|
| `agenthost` | `100.82.222.92` / LAN `192.168.1.14`+`.16`, x86_64. Inference server, PARE daemon host, ArcticBase host. |
| `pare-bench` | `100.97.133.126`, Ubuntu 26.04.1, aarch64, Python 3.14.4. Screen on DSI (`card1-DSI-1`); HDMI unplugged. Login user `pare`, uid 1000. |

`tailscale status` · `ssh pare@100.97.133.126` (Tailscale SSH, ACL action is
`check`, so it needs a periodic browser re-auth and will refuse
non-interactively once expired).

**`tailscale ssh` does not work from `agenthost`** — tailscale is a snap there and
AppArmor forbids the snap exec'ing `/usr/bin/ssh`
(`apparmor="DENIED" ... name="/usr/bin/ssh"`). Plain `ssh` over the tailnet works.

## What is running

**ArcticBase** — Docker on `agenthost`, from `ArcticBase/docker-compose.yml` plus
`deploy/arcticbase/docker-compose.override.yml` (the override supplies the `build:`
stanza the shipped file comments out, and the 8 MiB cap).

```bash
curl -s -o /dev/null -w "api %{http_code}\n" http://127.0.0.1:2929/api/health
curl -s -o /dev/null -w "ui  %{http_code}\n" http://127.0.0.1:2929/
```

Both must be 200. The UI only exists because the Dockerfile's Node stage builds
it — running the backend directly serves the API and 404s every page.

**PARE daemon** — `systemctl --user status pare-daemon` on `agenthost`, from
`deploy/systemd/pare-daemon.service`. Lingering is on, so it starts at boot.
`PARE_ARCTICBASE_URL` is pinned in the unit to the **tailnet** address, not
loopback, because `publish_finding` builds its returned URL from it and the reader
is at the bench.

**Bench units** — `pare-bench-status` and `pare-bench-kiosk` on the Pi, cage +
Chromium on tty1. `getty@tty1` is `enabled/inactive`: that is correct, and it is
the recovery path — never disable it.

```bash
ssh pare@100.97.133.126 'cd ~/PARE && ./scripts/bench_deploy.sh --check'
cat /opt/pare/DEPLOYED_FROM      # on the Pi
```

## What is NOT done

- **`pare-hardware-mcp` does not exist.** `/mnt/secondary/projects/pare-hardware-mcp`
  is an empty directory — no git repo, no spec. §15 says it gets its own spec, and
  that spec must absorb the three open follow-ups below.
- ~~No artifact drive.~~ **Done 2026-09-11.** `/dev/sda1`, ext4,
  `LABEL=bench-store`, `UUID=61c3b8ce-eb78-4531-813d-505112165533`, 916 GiB with
  907 GiB free, mounted at `/mnt/bench-store` via fstab
  (`nofail,x-systemd.device-timeout=10`), owned by `pare`, and carrying
  `.bench-store-id` = `88dabc85-9d25-4acf-ad8b-d4c2335b4427` — that UUID is what
  goes into `workers.yaml` as `artifact_drive_id` when the hardware worker is
  declared.

  The disk is an HGST HTS721010A9E630: a 1 TB **7200 rpm 2.5" spinner**, and it
  **requires the powered hub**. On the Pi's own ports its spin-up surge exceeded
  what the enclosure requests (`bMaxPower 800mA`) and the bridge enumerated with
  no SATA target behind it — `Generic ATA/ATAPI Device`, 0 B, "Media removed",
  inconsistently between attempts. The same drive enumerated in 4 s on a desktop
  port. Diagnosed by moving it between machines, which cost nothing; `smartctl`
  would have told us less. If it ever reverts to that symptom, check the hub's
  power before suspecting the disk.

  Currently linked at **480M** — the hub is in a USB 2 port. Works, but caps a
  2 GB dump at about a minute instead of ten seconds.
- **No Tigard attached.** `lsusb` shows only a wireless receiver. `pare` is already
  in `dialout`, `spi`, `i2c`, `gpio`, `plugdev`, so no group work is needed when one
  arrives.
- **Descriptor publishing on dispatch** is unwired, deliberately: nothing declares
  `produces="artifact"` yet, so wiring it now would be unexercised code.

## Open follow-ups, all landing in the hardware worker

From `2026-09-07-artifact-contract-followups.md`; the five "worth sweeping" items
are done, these three are not:

1. **Containment** of a descriptor's `path` under the worker's `artifact_root`.
   `validate_descriptor(payload, *, worker, tool)` takes no root, so it is not
   expressible in that signature — it needs the `WorkerSpec` in hand.
2. **`host` is never checked against the WorkerSpec endpoint**, so a compromised
   worker can point retrieval at a machine of its choosing.
3. **Open the artifact with `O_NOFOLLOW|O_CREAT|O_EXCL`.** `artifact_path()` returns
   a path and holds no descriptor, so it cannot close the TOCTOU window or detect a
   hardlink. The wiring step is the first place that actually opens the file.

## Constraints that still bind

- Trust boundary is the tailnet plus the LAN. ArcticBase has **no application auth**
  and is bound `0.0.0.0`.
- `workers.yaml` never contains secrets; it is the trust anchor. `extra="forbid"`
  means one unknown key aborts the whole file — and
  `tests/test_workers_yaml_format_comment.py` fails if its key list drifts from
  `WorkerSpec`.
- Flash write is **one encapsulated `critical` tool**, never exposed primitives.
  Destructive tools get operator pins from day one.
- `pare-worker-kit` depends on `mcp` alone. Neither `agent_core` nor the kit imports
  the other in library code; shared wire constants are stated twice and guarded by a
  test on each side (CI asserts `4 collected, 0 skipped`).
- A missing `artifact_root` must fail closed.

## Versions, and why they need watching

| | |
|---|---|
| `agent_core` | `1.10.0` — pinned by PARE, installed in its venv |
| `pare-worker-kit` | `0.1.2` — pinned by all three workers |

Both needed a release before consumers could see merged work, and `agent_core`
sat at `1.9.0` through thirteen commits, so a stale pin was invisible by version
number alone. **Check `git log <tag>..origin/main` before assuming a consumer has
what was merged.**

## Loose ends

- `ssh.service`/`ssh.socket` on the Pi are `disabled` but still recorded `failed`
  (tailscaled owns port 22). `sudo systemctl reset-failed ssh.socket ssh.service`
  clears it. Moving openssh to `Port 2222` would give a fallback that does not
  depend on tailscaled — worth having on a machine at a bench.
- `consoleblank=0` is not in `/boot/firmware/cmdline.txt`; the kernel parameter
  currently reads `0` anyway. Adding it makes blanking-off independent of any unit
  having run.
- The Pi's checkout may be left on a detached `FETCH_HEAD` from a deploy;
  `git -C ~/PARE checkout main && git pull` puts it back.
