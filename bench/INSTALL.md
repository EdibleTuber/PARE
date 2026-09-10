# Setting up `pare-bench` (the Pi)

Step 4 of the ArcticBase/artifacts design. Concrete addresses for this bench:

| | |
|---|---|
| Pi | `pare-bench` — `100.97.133.126`, Ubuntu 26.04.1 LTS, aarch64, Python 3.14.4 |
| inference server / daemon host / ArcticBase | `agenthost` — `100.82.222.92`, ArcticBase on `:2929` |
| bench screen | DSI ribbon (`card1-DSI-1` connected; HDMI unplugged) |
| login user | `pare`, uid 1000, `/home/pare`, `/bin/bash` |

**Surveyed on the real device 2026-09-10, so several steps below are already
done.** What is genuinely still needed is marked. In particular `pare` is a real
login user, not the `nologin` service account this guide first assumed — so one
account covers both the status page and the kiosk, and `useradd` is not needed.

The Pi needs **no venv, no pip, no PARE package**. The status page is stdlib-only
and was verified importing under `python3 -S` with no site-packages, and it parses
as 3.9 syntax so Bullseye works as well as Bookworm. Two files and system
`python3` is the whole dependency.

## 0. On the server, first

ArcticBase must be reachable from the Pi. It was previously bound to loopback:

```bash
ARCTIC_BASE_HOST=0.0.0.0 ARCTIC_BASE_MAX_UPLOAD_BYTES=8388608 ...
```

**Be aware what that changes.** `0.0.0.0` makes an unauthenticated workbench host
reachable from everything on the LAN, not only the tailnet. §7 takes the tailnet
as the trust boundary, so this is consistent with the design — but if you want it
tighter, bind to `100.82.222.92` specifically and loopback access goes away.

`ARCTIC_BASE_MAX_UPLOAD_BYTES=8388608` matters (§5.1): the default is 2 GB, and a
2 GB object makes every labelled checkpoint a 2 GB tarball. Note the measured
caveat — that cap is **not** applied to the JSON `POST /objects` path, which is why
PARE publishes in two calls.

Verify from the Pi, not from the server:

```bash
curl -m3 http://100.82.222.92:2929/api/health     # expect {"status":"ok",...}
```

## 1. Copy the two things the Pi needs

From the repo checkout on `agenthost`:

```bash
ssh pare-bench 'sudo mkdir -p /opt/pare/bench /opt/pare/scripts'
scp bench/__init__.py bench/status_server.py pare-bench:/tmp/
scp scripts/bench_doctor.sh pare-bench:/tmp/
ssh pare-bench 'sudo mv /tmp/__init__.py /tmp/status_server.py /opt/pare/bench/ && \
                sudo mv /tmp/bench_doctor.sh /opt/pare/scripts/ && \
                sudo chmod +x /opt/pare/scripts/bench_doctor.sh'
```

Plain `scp`, not `scp -O`. Legacy SCP mode requires execution of the remote user's
shell, so the path is re-parsed remotely; OpenSSH ≥9.0 defaults to SFTP and that
default is the safe one.

## 2. The service user — ALREADY DONE

`pare` exists as a real login user (uid 1000, `/home/pare`, `/bin/bash`), so no
`useradd` is needed and the same account can run both units.

Two prerequisites were checked on the device and are already satisfied:

- **`tailscale status --json` works as `pare`** — it is already the tailscale
  operator, having been the user that ran `tailscale up`. No
  `tailscale set --operator=` needed. This matters because the status page's
  first probe shells out to it, and without access the *network* probe goes red
  while the network is perfectly fine.
- **`pare` is in `video`, `render` and `input`** — cage's prerequisites, so no
  `usermod` and no re-login needed.

## 3. The status page

The unit file has to reach `/etc/systemd/system/` — copying the code to
`/opt/pare` is not enough, and `systemctl enable` answers "does not exist" if you
only did the latter:

```bash
sudo install -m 644 ~/pare-bench/systemd/pare-bench-status.service \
    /etc/systemd/system/pare-bench-status.service
sudo systemctl daemon-reload
sudo systemctl enable --now pare-bench-status
systemctl --no-pager status pare-bench-status | head -12
curl -s localhost:8080/status.json | head -c 400; echo
```

Then confirm the **network** probe specifically, because it is the one with a
sandbox-shaped failure mode:

```bash
curl -s localhost:8080/status.json |
  python3 -c "import json,sys; p=json.load(sys.stdin)['probes'][0]; print(p['state'], p['detail'])"
```

It must say `ok`. If it reports a tailscale permission error, `ReadWritePaths`
did not cover the socket — check `systemctl show pare-bench-status -p ReadWritePaths`
and where `tailscaled.sock` actually is on your system.

You should see four probes. Expect `network` and `arcticbase` green immediately.
`heartbeat` goes green once the PARE daemon has beaten at least once, and `project`
adopts whatever project the daemon reports.

## 4. The kiosk

This bench runs **Ubuntu Server**, which has no X, no Wayland and no browser, so
the kiosk is `cage` + Chromium rather than a desktop session. It also needs a
different user from the status page.

**Follow [`KIOSK-UBUNTU.md`](KIOSK-UBUNTU.md) instead of copying the unit
straight in.** I had no access to the Pi and could not test any of it, so that
file proves one layer at a time — a failure tells you which layer instead of
leaving you with a blank screen and four candidates.

The home URL is `http://127.0.0.1:8080/` — the Pi's **own** page, never one served
by `agenthost`. §8.1: cold-boot the Pi with the server down and a remote home URL
shows Chromium's own interstitial, in kiosk mode, to someone holding two probes.

## 5. The artifact drive — NOT POSSIBLE YET

There is no external drive attached: `lsblk` shows only the 59 GB SD card
(`/boot/firmware` + `/`), and `/mnt/bench-store` is not a mountpoint. Nothing to
mount until a drive is plugged in, so this section is for when one is.


`/etc/fstab`, and `nofail` is not optional (§8.4) — without it a Pi booted at the
bench with the drive unplugged drops to an emergency shell, which is a brick on a
machine with no keyboard:

```
UUID=<uuid>  /mnt/bench-store  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2
```

ext4 explicitly: FAT32 fails at 4 GiB with `EFBIG`, and a firmware dump crosses that.

Then name the drive, so one project's stick is distinguishable from another's:

```bash
ssh pare-bench 'sudo sh -c "uuidgen > /mnt/bench-store/.bench-store-id"'
```

## 6. Check it

```bash
ssh pare-bench 'PARE_SERVER=http://100.82.222.92:2929 /opt/pare/scripts/bench_doctor.sh'
```

Five probes, naming which of the five candidates is at fault. It is deliberately
daemon-independent and dependency-free, so it works in the state you most need it —
the daemon being down, or the status page itself having failed.

Expect probes 4 (worker on `:9100`) and 5 (drive) to fail until
`pare-hardware-mcp` exists and a drive is mounted. That is step 5.

## What is NOT done by this

- **`pare-hardware-mcp`** does not exist yet — `/mnt/secondary/projects/pare-hardware-mcp`
  is an empty directory. Nothing listens on `:9100` and no tool produces an artifact.
- Do **not** add `RequiresMountsFor` to that worker's unit when you write it (§8.4).
  With the drive absent the unit never starts, the port never listens, and
  `/worker list` prints `UNREACHABLE` — the same string it prints for "the Pi is
  off". Start unconditionally and fail at call time naming the mountpoint.
