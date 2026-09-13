# Deploying PARE

Three things get deployed, on two machines. Nothing here is required to *run*
PARE from a terminal — see [`QUICKSTART.md`](QUICKSTART.md) for that.

## The daemon, as a user service

The unit that is actually deployed is
[`deploy/systemd/pare-daemon.service`](deploy/systemd/pare-daemon.service).
Edit `WorkingDirectory`, `ExecStart` and any `Environment=` lines to match your
checkout, then:

```bash
cp deploy/systemd/pare-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pare-daemon
loginctl enable-linger "$USER"    # so it starts at boot, not at first login
```

Verify against the file rather than from memory — these two drift:

```bash
systemctl --user show -p FragmentPath --value pare-daemon
diff "$(systemctl --user show -p FragmentPath --value pare-daemon)" \
     deploy/systemd/pare-daemon.service
```

`pare-cli` is run interactively in a terminal, not as a service.

## The bench Pi

The status server and kiosk units, the drive, and the by-hand bring-up that
proves each layer separately: [`bench/INSTALL.md`](bench/INSTALL.md) and
[`bench/KIOSK-UBUNTU.md`](bench/KIOSK-UBUNTU.md). Deploys are driven by
[`scripts/bench_deploy.sh`](scripts/bench_deploy.sh); diagnose with
[`scripts/bench_doctor.sh`](scripts/bench_doctor.sh).

Editing anything under `bench/` changes nothing at the bench until a deploy
runs — the units serving the screen are the installed copies under
`/etc/systemd/system/`, not the files in the repo. `/opt/pare/DEPLOYED_FROM`
records which commit is actually out there.

## Shutting the bench Pi down

The Pi 5 has an onboard power button beside the USB-C jack, and it is registered
as a real input device (`pwr_button`, `event0`) with logind's default
`HandlePowerKey=poweroff`. **In the bench enclosure it is not reachable**, which
is why there are two other routes.

### The touchscreen control

The status page carries a `SHUT DOWN` button: tap once to arm, tap again within
five seconds to confirm, and it reverts on its own if you walk away. It POSTs to
`/power/off` on the local status server.

Two properties make that endpoint safe, and **both must hold** — if you change
either, re-read the other:

- The status server binds `127.0.0.1:8080`, so only something on this Pi can
  reach it.
- It refuses cross-origin POSTs. The realistic attacker is not a person on the
  network but a workbench object rendered in the kiosk's iframe, which is a
  different origin.

Authorization is `deploy/polkit/50-pare-poweroff.rules`, which grants `pare`
power-off **only** — not reboot, suspend or hibernate — for both
`org.freedesktop.login1.power-off` and `…-power-off-multiple-sessions`. The
second matters because the kiosk holds a logind session, so it is usually the
one that fires.

Handoff to the workbench is a **button, not a timer**. It used to redirect after
2.5 seconds, which meant this page — the only screen with the shutdown control,
and the only one that still works when ArcticBase is down — was visible for two
and a half seconds at a time, with no way back under `--kiosk`.

### A physical button on GPIO

Worth having, because it works when the screen is frozen, Chromium has died, the
network is down, or the Pi is already off — none of which a web page can do.

Add to `/boot/firmware/config.txt`:

```
dtoverlay=gpio-shutdown
```

The overlay ships with the kernel — on this Ubuntu image it lives at
`/boot/firmware/current/overlays/gpio-shutdown.dtbo`, not the
`/boot/firmware/overlays/` path most Pi documentation names. Default pin is
GPIO3, so wire a momentary button between **header pin 5 (GPIO3) and pin 6
(GND)** — they are adjacent, so a 2-pin header works.

**Two things to check before committing to the wiring:**

- **Does it wake the Pi, not just halt it?** On Pi 4 and earlier, pulling GPIO3
  low wakes the board from halt, which is the reason that pin is the default.
  **I have not verified this on a Pi 5**, whose power path goes through a PMIC.
  Test it: halt the Pi, press the button, see whether it comes back. If it does
  not, a clean shutdown still leaves you power-cycling at the USB-C end, and the
  only fix is a switch upstream.
- **GPIO2/3 are the Pi's own I2C pins** and `dtparam=i2c_arm=on` is set. Nothing
  appears to use them — the touchscreen reports as `10-0038`, i.e. bus 10, and
  the Tigard does I2C over USB — but confirm before wiring. Any other pin works
  via `dtoverlay=gpio-shutdown,gpio_pin=N`, at the cost of the wake behaviour,
  which only GPIO3 has.

## ArcticBase

The workbench host, run under Docker on the inference server:
[`deploy/arcticbase/README.md`](deploy/arcticbase/README.md). The override in
that directory supplies the `build:` stanza the shipped compose file comments
out, and the upload cap — both load-bearing, for reasons that file records.
