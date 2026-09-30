# UART + power-cycle spike — findings (2026-09-27)

Hands-on session before designing the interactive UART pane (direction 1) and a
power-cycle tool. Everything below is measured against the real bench, not inferred.

## Setup as it actually is
- **Target:** a repurposed SCENESTEK cube PCB (company defunct). MediaTek/Ralink
  MT7628 (APSoC 7628_MP), 128 MB DDR, 32 MB SPI flash (Winbond W25Q256FV),
  U-Boot 1.1.3 / Ralink UBoot 4.3.0.18, Linux 3.10.14 "SCENESTEK LINUX BUILD 1090".
  HLK/Hi-Link module on board. Console: `ttyS0,115200n8`.
- **Tigard UART channel: if00 (ttyUSB0), NOT if01.** Verified by passive listen on
  both channels while powered: if00 carried live output, if01 was silent. The
  deployed worker was configured for if01 and read nothing. See memory
  `tigard-uart-is-if01` (now corrected).
- **Power switch:** DSD TECH SH-UR04A 4-ch USB relay, CP2102 bridge, ttyUSB2,
  9600 8N1. AT command set: `AT+CHn=1` on, `AT+CHn=0` off, `AT+CHn=?` query,
  `AT` -> `OK`. Avoid `AT+BAUD=` (changes the board's own rate). Target's + lead
  is on **NC1**, so `AT+CH1=1` cuts power, `AT+CH1=0` restores it. Contacts rated
  AC 110-250V / DC up to 30V, 600W/ch — 12V target well within.
- **Relay identity:** generic serial `0001`; address it by USB by-path
  (`platform-xhci-hcd.0-usb-0:2:1.0`), not by-id.

## What works
- Relay power-cycle: `AT+CH1=1`, wait, `AT+CH1=0` — target observably powers down
  and boots. Round-trip on the AT command ~0.3s.
- Full boot captured on if00 through a cycle: U-Boot + kernel, 7916 bytes, clean
  115200, CRLF line endings, standard printable console text. Saved:
  `docs/superpowers/2026-09-27-scenestek-boot-if00.log`.

## Facts that shape the design
- **Line endings are CRLF.** The pane's render and any keystroke send must handle
  `\r\n` correctly; Enter should send `\r` (U-Boot/busybox convention).
- **Boot burst is fast and continuous** at 115200 — the pane must render a fast
  stream without dropping; `console_detect_baud` during a cycle would land the
  burst cleanly (relay + baud-scan are the intended pairing).
- **Interrupt windows exist:** "Hold on the RESET button to enter the RECOVERY
  mode" and a U-Boot countdown — future value for bootloader access, and a reason
  keystroke timing (send during the countdown) matters.
- **MTD layout is exposed** (Bootloader/Config/Factory/Firmware1/Firmware2/Data1/
  Data2 on a 32MB W25Q256FV) — the artifact-drive target for later dump tools.
- **Idle target still chatters:** `mqtt:: Connection refused` repeats when up, so
  "silent capture" honesty and non-silent capture were both exercised.

## Immediate fix needed (independent of the design)
The deployed `pare-hardware-mcp` worker's `PARE_HW_DEVICE` points at if01 (silent).
For this bench it must be if00. Affects the Pi systemd unit, the repo copy
`systemd/pare-hardware-mcp.service`, and the if01 references in `workers.yaml`
comments and the bench-integration spec/plan.

## Two things to design next
1. **Interactive UART pane (direction 1):** keystroke send via `console_send`
   (raw vs line mode — the boot log's CRLF and the U-Boot prompt argue for raw
   per-key with Enter = `\r`), in-pane `console_open`, and a baud-detect action.
2. **Power-cycle as a hardware-worker tool:** today the relay is driven by an
   ad-hoc script. It belongs in `pare-hardware-mcp` as a gated tool (e.g.
   `power_cycle` / `power_set`), high-risk, so PARE can cycle the target as part
   of a flow (e.g. cycle -> baud-detect -> capture boot). Needs its own device
   declaration for the relay, separate from the UART device.
