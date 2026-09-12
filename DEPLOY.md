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

## ArcticBase

The workbench host, run under Docker on the inference server:
[`deploy/arcticbase/README.md`](deploy/arcticbase/README.md). The override in
that directory supplies the `build:` stanza the shipped compose file comments
out, and the upload cap — both load-bearing, for reasons that file records.
