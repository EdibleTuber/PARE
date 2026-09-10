# Setting up `pare-bench` (the Pi)

Step 4 of the ArcticBase/artifacts design. Concrete addresses for this bench:

| | |
|---|---|
| Pi | `pare-bench` — `100.97.133.126` |
| inference server / daemon host / ArcticBase | `agenthost` — `100.82.222.92`, ArcticBase on `:2929` |

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

## 2. The service user

The units run as `User=pare`. Either create it:

```bash
ssh pare-bench 'sudo useradd -r -s /usr/sbin/nologin pare'
```

…or edit `User=`/`Group=` in the status unit to the Pi's existing login user.

**The status page's first probe shells out to `tailscale status --json`, and a
service user cannot do that by default** — the local API socket is privileged.
Grant it explicitly:

```bash
sudo tailscale set --operator=pare
```

Then check it as that user, because getting this wrong makes the *network* probe
red while the network is fine:

```bash
sudo -u pare tailscale status --json | head -c 80
```

If it fails, the probe now reports stderr and names both causes rather than
guessing — but it is better to fix it here than to read about it on the screen.

**The kiosk runs as a different user.** See §4: it needs a real login user with a
home directory, which a `nologin` service account is not.

## 3. The status page

```bash
scp bench/systemd/pare-bench-status.service pare-bench:/tmp/
ssh pare-bench 'sudo mv /tmp/pare-bench-status.service /etc/systemd/system/ && \
                sudo systemctl daemon-reload && \
                sudo systemctl enable --now pare-bench-status'
ssh pare-bench 'systemctl --no-pager status pare-bench-status | head -5'
ssh pare-bench 'curl -s localhost:8080/status.json | head -c 400; echo'
```

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

## 5. The artifact drive

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
