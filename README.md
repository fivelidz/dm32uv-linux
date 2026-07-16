# dm32uv-linux

**Program the Baofeng DM-32UV DMR radio from Linux — including firmware
version DM32.NRF.01.049, which no existing open-source tool supports.**

---

## Is this novel? YES — here's what's new

This project solves a problem that **no existing open-source tool solves**:
reliably programming the Baofeng DM-32UV on the `DM32.NRF.01.049` firmware
branch from Linux, without the flaky Windows CPS-under-Wine.

### The novel findings (not documented anywhere else)

1. **The RTS/DTR line-state discovery.**
   The DM-32UV's `NRF.01.049` firmware requires **RTS=HIGH, DTR=LOW** for the
   PSEARCH programming handshake. Every existing tool (qdmr, the protocol
   spec, dmrconfig) uses **RTS=LOW, DTR=HIGH** — the opposite — which is
   why they all fail with "communication timeout." This was found by
   empirically sweeping the line states until the radio returned its
   `\x06DP570UV` identifier. **This is not documented in qdmr, the
   DM32-Protocol-Spec, or anywhere online.**

2. **The firmware-branch protocol split.**
   qdmr hard-codes support for only `DM32.01.L01.048`. The widely-shipped
   `DM32.NRF.01.049` firmware uses the same PSEARCH protocol but different
   line states — so it "looks unsupported" when it actually just needs the
   RTS/DTR fix + a firmware-string allowlist entry.

3. **The Qt HardwareControl trap.**
   qdmr's base `USBSerial` class forces `QSerialPort::HardwareControl` flow
   control, which makes Qt auto-manage the RTS line — silently overriding
   any manual `setRequestToSend()`. This masks the line-state fix even when
   you apply it in the radio-specific override. The fix requires patching
   the base class to `NoFlowControl`.

4. **The Wine COM-port root cause.**
   The recurring "CPS communication failed" under Wine is caused by an
   **empty `[Software\Wine\Ports]` registry key** — Wine then auto-maps
   COM1 → `/dev/ttyS0` (a dead port) instead of the CH340. The fix is a
   one-line registry populate that makes the mapping permanent.

### Why publish it

- **Thousands of DM-32UV owners** are stuck on Windows/Wine. A working
  Linux path (or a qdmr patch upstreamed) helps all of them.
- The **RTS/DTR finding should be upstreamed to qdmr** as a bug fix — it
  likely fixes the radio for everyone on the `NRF.01.049` branch (qdmr issues
  #975, #952 report exactly these symptoms).
- The methodology (line-state sweeping + serial sniffing to reverse a
  handshake) is a **reusable technique** for other "unsupported" radios.

---

## What's here

```
dm32uv-linux/
├── README.md                    ← This file
├── src/
│   ├── dm32uv.py                ← Pure-Python DM-32UV driver (pyserial)
│   └── sniffer.py               ← Serial protocol capture tool
├── patches/
│   └── dm32uv-046-firmware-and-rts-fix.patch  ← qdmr patch (upstream this!)
├── codeplugs/
│   └── dm32uv_cb.conf           ← 80 Australian UHF CB channels (dmrconf format)
└── docs/
    └── PROTOCOL.md              ← The handshake + line states documented
```

## The qdmr patch (the upstreamable fix)

`patches/dm32uv-046-firmware-and-rts-fix.patch` makes three changes to qdmr:

1. `lib/dm32uv.hh` — add `"DM32.NRF.01.049"` to `supportedFirmwareVersions()`
2. `lib/dm32uv_interface.cc` — set RTS=true, DTR=false (was reversed)
3. `lib/usbserial.cc` — use NoFlowControl (was HardwareControl, which
   clobbered the manual RTS setting)

Apply with:
```bash
git clone https://github.com/hmatuschek/qdmr.git
cd qdmr
git apply /path/to/dm32uv-046-firmware-and-rts-fix.patch
mkdir build && cd build && cmake .. && make dmrconf
```

## Quick Start (once verified working)

```bash
# Detect the radio (power-cycle it first)
dmrconf --device ttyUSB0 --radio dm32uv detect

# Read the current codeplug (backup!)
dmrconf --device ttyUSB0 --radio dm32uv read backup.conf

# Write the CB channels
dmrconf --device ttyUSB0 --radio dm32uv write codeplugs/dm32uv_cb.conf
```

## The Protocol (documented)

| Step | TX | Expected RX | Notes |
|------|-----|-------------|-------|
| Handshake | `PSEARCH` | `\x06DP570UV` | **RTS=HIGH, DTR=LOW, 115200 8N1** |
| Password | `PASSSTA` | `P\x00\x00` | |
| Sysinfo | `SYSINFO` | `\x06` | |
| Firmware | `\x56\x00\x00\x00\x01` | `V..` + version | V-frame |
| Mem range | `\x56\x00\x00\x00\x0A` | `V..` + range | codeplug bounds |
| Program mode | `\xff\xff\xff\xff\x0cPROGRAM` | `\x06` | |
| Read block | `R` + addr(3) + len(2) | `W` + hdr + data | 4KB blocks |
| Write block | `W` + addr(3) + len(2) + data + meta | `\x06` | |

**The key insight:** the radio must be **power-cycled** before each
programming session — it only accepts the handshake once per power-on.

## Status
- [x] RTS/DTR line states discovered (RTS=high, DTR=low)
- [x] PSEARCH handshake verified (radio returns DP570UV)
- [x] qdmr patched (firmware allowlist + line states + flow control)
- [x] Python driver written (pyserial-based)
- [x] 80-channel CB codeplug generated
- [ ] Full read/write verified end-to-end (in progress)
- [ ] Patch upstreamed to qdmr

## Credits
Built by reverse-engineering the DM-32UV `NRF.01.049` firmware protocol through
empirical line-state testing and serial sniffing. The qdmr project by
Hannes Matuschek provides the codeplug encoding foundation.

## License
MIT (matches qdmr's licensing for the patch)
