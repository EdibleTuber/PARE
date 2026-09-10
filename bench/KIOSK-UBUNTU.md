# The kiosk on Ubuntu Server, by hand first

**I could not test any of this.** No access to the Pi, no ARM machine, no screen.
So do not enable the unit and hope — work down this list, which proves one layer
at a time. When something fails you will know which layer, instead of staring at
a blank screen with four candidate causes.

Ubuntu Server has no X, no Wayland and no browser. `cage` is a Wayland kiosk
compositor: one client, fullscreen, no window manager, no desktop. That is a much
smaller surface than a desktop environment installed to show a single page.

## 0. Which user — ALREADY SATISFIED

An earlier draft of this file said the kiosk needed a different user from the
status page. That was written before I could see the device, assuming `pare`
would be a `nologin` service account. It is not: surveyed as uid 1000 with
`/home/pare` and `/bin/bash`, so it is a real login user and one account runs
both units. The unit says `User=pare`.

The group prerequisites are also already met — `pare` is in `video`, `render`
and `input`, so no `usermod` and no re-login. Confirm if you like:

```bash
id | tr ',' '\n' | grep -E 'video|input|render'
```

The screen is on the DSI ribbon (`card1-DSI-1` reports `connected`; both HDMI
outputs are disconnected), so cage will drive that panel.

## 1. Install

```bash
sudo apt update
sudo apt install -y cage          # universe
sudo snap install chromium
```

Check both landed where the unit expects:

```bash
command -v cage                   # expect /usr/bin/cage
command -v chromium               # expect /snap/bin/chromium
```

If either path differs, fix `ExecStart` in the unit rather than symlinking.

## 2. Prove cage can drive the screen at all

From a **console session on the Pi itself** (not over SSH — cage needs the seat):

```bash
cage -- chromium --kiosk --ozone-platform=wayland http://127.0.0.1:8080/
```

| Symptom | Cause |
|---|---|
| `could not create backend` | no seat/DRM access — groups not applied, or you are over SSH |
| Chromium exits immediately | missing `--ozone-platform=wayland`; it looked for X and found none |
| black screen, cage running | display not detected; check `/sys/class/drm/*/status` |
| page loads | you are done with this step |

If it works over the console but you want it at boot, continue. **Do not skip to
the unit if this step failed** — the unit adds systemd, PAM and tty handling on
top, and debugging four layers at once is what this file exists to avoid.

## 3. Stop the console blanking the screen before cage starts

§8.3: a blank screen looks identical to a crash, and this machine has no keyboard
to wake. Two separate mechanisms, and the unit only handles one:

The unit runs `setterm --blank 0 --powersave off`, which affects the **active** tty.
Kernel framebuffer blanking can fire before that, so also add to the kernel
cmdline — on Ubuntu for Pi that is `/boot/firmware/cmdline.txt`, appended to the
existing single line, not a new one:

```
consoleblank=0
```

Reboot for it to take effect.

`xset s off` and `xset -dpms` from the earlier X-based draft do nothing here —
they are X clients, and there is no X server. cage does not blank or idle-suspend
on its own.

## 4. Install the unit

```bash
sudo cp pare-bench-kiosk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now getty@tty1        # cage owns tty1; Conflicts= also handles this
sudo systemctl enable --now pare-bench-kiosk
journalctl -u pare-bench-kiosk -n 40 --no-pager
```

## 5. Prove the failure modes, not just the happy path

The page exists for when things are broken, so check it renders broken states.
With the unit running:

```bash
# ArcticBase unreachable -> arcticbase FAIL, heartbeat/project "not checked"
sudo iptables -I OUTPUT -d 100.82.222.92 -p tcp --dport 2929 -j REJECT
#  ... watch the screen ...
sudo iptables -D OUTPUT -d 100.82.222.92 -p tcp --dport 2929 -j REJECT
```

The screen should name **`FIRST FAILURE: ARCTICBASE`** and show the two later
probes as `not checked` — never as `ok`, and never as a guess. And the tick in the
top right should keep counting: that moving element is how "the screen is alive"
is answerable from three feet away.

Also confirm the local-service failure path, which is the one a remote page could
never report:

```bash
sudo systemctl stop pare-bench-status
```

The screen should say **LOCAL STATUS SERVICE NOT ANSWERING** rather than continuing
to show stale green probes. That is the whole point of §8.1.
