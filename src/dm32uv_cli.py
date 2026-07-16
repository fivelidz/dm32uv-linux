#!/usr/bin/env python3.11
"""
dm32uv_cli.py — Command-line tool for the Baofeng DM-32UV on Linux.

The first working pure-Python programmer for the DM-32UV NRF firmware,
using the RTS=HIGH/DTR=LOW handshake discovery.

Usage:
    python3.11 dm32uv_cli.py detect              # Identify the radio
    python3.11 dm32uv_cli.py backup out.bin      # Read full codeplug to file
    python3.11 dm32uv_cli.py info                # Show firmware + memory range

IMPORTANT: Power-cycle the radio (off, wait 3s, on) before each session.
The radio only accepts the programming handshake once per power-on.
"""

import sys
import argparse

sys.path.insert(0, ".")
from dm32uv import DM32UVRadio  # noqa: E402


def cmd_detect(args):
    radio = DM32UVRadio(args.port)
    if radio.connect():
        print(f"Radio detected!")
        print(f"  Model:    {radio.info.model}")
        print(f"  Firmware: {radio.info.firmware}")
        print(
            f"  Codeplug: 0x{radio.info.codeplug_range[0]:X} - "
            f"0x{radio.info.codeplug_range[1]:X}"
        )
        radio.disconnect()
        return 0
    else:
        print("No radio detected.")
        print("  - Is the radio ON?")
        print("  - Power-cycle it (off, wait 3s, on) — the handshake is one-shot")
        print("  - Cable connected to /dev/ttyUSB0?")
        return 1


def cmd_info(args):
    return cmd_detect(args)


def cmd_backup(args):
    radio = DM32UVRadio(args.port)
    if not radio.connect():
        print("No radio detected. Power-cycle and retry.")
        return 1
    print(f"Connected: {radio.info.model} fw={radio.info.firmware}")
    print("Building address map...")
    amap = radio.build_address_map()
    print(f"  Mapped {len(amap)} blocks")
    print("Reading full codeplug...")

    def progress(virt, done, total):
        print(f"\r  {done}/{total} blocks", end="", flush=True)

    codeplug = radio.read_full_codeplug(progress_cb=progress)
    print()

    # Write to file (virtual-address ordered)
    with open(args.output, "wb") as f:
        for virt in sorted(codeplug.keys()):
            f.write(codeplug[virt])
    total_bytes = sum(len(v) for v in codeplug.values())
    print(f"Saved {total_bytes} bytes to {args.output}")
    radio.disconnect()
    return 0


def main():
    ap = argparse.ArgumentParser(description="DM-32UV Linux programmer")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="serial port")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("detect", help="Detect + identify the radio")
    sub.add_parser("info", help="Show radio info")
    b = sub.add_parser("backup", help="Read full codeplug to a file")
    b.add_argument("output", help="Output .bin file")
    args = ap.parse_args()

    if args.cmd == "detect":
        return cmd_detect(args)
    elif args.cmd == "info":
        return cmd_info(args)
    elif args.cmd == "backup":
        return cmd_backup(args)
    else:
        ap.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
