# Bench verification, 2026-09-13

Ruling 6 of the phase-1 build deferred every hardware check to one consolidated
pass, because the bench's Tailscale SSH auth expired mid-plan. This is that pass.
It is **partial**: no target was transmitting, so everything needing traffic is
still open.

**Provenance.** The tree was rsynced to `/tmp/hwverify` on `pare-bench` and run
against the system python (which carries pyserial 3.5). `session.py`'s sha256 on
the Pi matched the local file exactly, so the code measured is the code committed
— `/opt/pare/DEPLOYED_FROM` says nothing about code in a venv, which is why the
hash is recorded rather than the path.

## Verified — and only measurable here

### The baud scan does not disturb the target

This is the one that mattered. `B8` says the scan must change speed on the **held
descriptor**, never close and reopen, because reopening re-asserts DTR and would
reboot a DTR-reset board once per candidate rate — never letting it stay up long
enough to emit a clean sample.

**The test suite passes against a close-and-reopen implementation.** Reopening the
same `Serial` object preserves object identity, and `os.open` hands back the
descriptor it just released, so `_serial is serial_obj` and `fileno() == fd_before`
both hold for the mutant. No software check can separate the two.

Measured during a two-rate scan, sampling `TIOCMGET` and `tcgetattr` every ~4 ms:

```
fd before=3 after=3   reopen calls=0
DTR changes: 0 / 178 samples
RTS changes: 0 / 178 samples
distinct termios speeds seen mid-scan: [9600, 115200]
restored=True  final_baud=115200  (entry was 115200)
```

Zero DTR transitions. The speed change lands at the termios layer on the live
descriptor — both rates observed — and the entry rate is restored afterwards.

### The reported DTR/RTS state is honest

`console_open` was asked for `dtr=False, rts=False`; the kernel's `TIOCMGET`
reports `DTR: False, RTS: False`. Worth checking rather than assuming, because
pyserial's own attributes are a cache of what was *requested* — the session
already reports `rts: null` under `rtscts` precisely because pyserial never
applies it there.

Note the standing caveat this does **not** overturn: `open(2)` itself asserts the
modem lines on a real UART before any ioctl runs. This confirms the steady state
after open, not that a board never twitches during it.

### Exclusivity

A second `open` against the same `by-id` path was refused, naming the holder.

## Corrected: 250000 is not a chip-refused rate

An earlier review framed the `ValueError`/`OverflowError` restore path as
protecting against "a rate the chip refuses", using 250000 as the example. **The
FT2232H accepted it** (`rejected={}`). FTDI parts synthesise arbitrary rates from
divisors rather than matching a fixed table, so that path needs a rate the
*kernel* refuses. The defensive code is right; the example was wrong, and the
restore path remains unexercised on hardware.

## Still open — every one needs a target wired and powered

Both Tigard channels opened cleanly at 115200 and **both were silent**.

1. **Which channel is the UART.** `if00` vs `if01`. The whole point of Level 2.
2. **Boot-log capture**, and `dropped == 0` across one — the last line of the
   plan's Definition of Done.
3. **Reader throughput at 921600** against `READ_TIMEOUT=0.05` / `READ_CHUNK=4096`.
4. **Detection against real traffic** — scoring has only ever seen synthetic bytes.
5. **The CTS stall.** Wire RTS/CTS to a target that never asserts CTS and confirm
   the 5 s bounded refusal, its message, and that the session survives. This is
   the case the write path was rebuilt around across three fix rounds.
6. **The short-write count** on a real UART. Measured on a slow-draining pty
   (64000 reported, 73728 delivered); a user-facing claim about bytes reaching a
   board rests on it.
7. **`framing_errors`**, still unimplemented — calibrating it needs a
   deliberately floated ground, which is also item 5's rig.

## Bench state at time of writing

Drive mounted (117 G, 116 G free); sentinel rotated to `22951728-…` with the old
value kept at `.bench-store-id.prev`. Tigard attached, both channels enumerating
with stable `by-id` paths. No target attached.

**The drive was found unmounted earlier today.** It had lost power; when power
returned it enumerated cleanly as `/dev/sda1`, but 25 minutes after boot — long
after `nofail,x-systemd.device-timeout=10` had given up. Nothing re-triggers the
mount, and nothing complains. `scripts/bench_doctor.sh` would have caught it;
nothing else does.
