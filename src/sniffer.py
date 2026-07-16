#!/usr/bin/env python3.11
"""
DM-32UV Serial Sniffer — Captures all traffic between the CPS and radio.

This creates a PTY (pseudo-terminal) that the CPS connects to. All traffic
is forwarded to the real radio AND logged to a file. This lets us see the
exact protocol the CPS uses, so we can replicate it in Python.

USAGE:
  1. Run this script:   python3.11 sniffer.py
  2. It prints a virtual port path (e.g., /dev/pts/3)
  3. Point Wine's COM1 at that path:
     ln -sf /dev/pts/3 ~/.wine-baofeng/dosdevices/com1
  4. Launch the CPS:    ./launch_cps.sh
  5. In CPS: select COM1, click "Read Radio"
  6. Watch the capture scroll by. When done, Ctrl-C.
  7. The log is at /tmp/dm32_capture.log
"""

import os
import pty
import select
import serial
import sys
import time
from datetime import datetime

REAL_PORT = "/dev/ttyUSB0"
# Try both baud rates — the CPS will set the correct one via the CH340
BAUD = 115200
LOG_FILE = "/tmp/dm32_capture.log"


def main():
    # Create PTY pair
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    print(f"\n{'=' * 60}")
    print(f"  DM-32UV Serial Sniffer")
    print(f"{'=' * 60}")
    print(f"\n  Virtual port: {slave_name}")
    print(f"  Real radio:   {REAL_PORT}")
    print(f"  Log file:     {LOG_FILE}")
    print(f"\n  Next steps:")
    print(f"    1. Point COM1 at this port:")
    print(f"       ln -sf {slave_name} ~/.wine-baofeng/dosdevices/com1")
    print(f"    2. Launch CPS: ./tools/baofeng_cps/launch_cps.sh")
    print(f"    3. In CPS: COM1 → Program → Read Radio")
    print(f"    4. Watch the capture below")
    print(f"    5. Ctrl-C when done")
    print(f"\n{'=' * 60}\n", flush=True)

    # Open the real radio
    try:
        radio = serial.Serial(REAL_PORT, BAUD, timeout=0.01)
    except Exception as e:
        print(f"ERROR: Cannot open {REAL_PORT}: {e}")
        sys.exit(1)

    log = open(LOG_FILE, "w")

    def log_msg(direction, data):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        hex_str = " ".join(f"{b:02x}" for b in data)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        line = f"{ts} {direction:12s} [{len(data):4d}]  {hex_str}  |{asc}|"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    try:
        while True:
            r, _, _ = select.select([master_fd, radio.fd], [], [], 0.1)
            if master_fd in r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    print("\n[CPS disconnected]")
                    break
                if data:
                    log_msg("CPS->RADIO", data)
                    radio.write(data)
                    radio.flush()
            if radio.fd in r:
                data = radio.read(65536)
                if data:
                    log_msg("RADIO->CPS", data)
                    os.write(master_fd, data)
    except KeyboardInterrupt:
        print("\n\n[Sniffer stopped]")
    finally:
        log.close()
        radio.close()
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except Exception:
            pass
        print(f"Capture saved to {LOG_FILE}")


if __name__ == "__main__":
    main()
