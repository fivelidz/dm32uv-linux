#!/usr/bin/env python3.11
"""
verify_write.py — Self-verification harness for codeplug writes.

Does a FULL before/after diff of the ENTIRE codeplug around a write, so we
can see EXACTLY which blocks/bytes change — not just spot-check one block.

This is how we catch corruption ourselves before trusting a write.

Usage:
    python3.11 verify_write.py           # dry diagnostic: read full, show map
    python3.11 verify_write.py --test-cb # read full, do CB write, diff, restore
"""

import sys
import time

sys.path.insert(0, ".")
from dm32uv import DM32UVRadio, BLOCK_SIZE


def read_all(radio):
    """Read the full codeplug as {physical_addr: bytes}."""
    radio.build_address_map()
    snapshot = {}
    for phys in sorted(radio._address_map.keys()):
        snapshot[phys] = radio.read_memory(phys, BLOCK_SIZE)
        time.sleep(0.02)
    return snapshot


def diff_snapshots(before, after):
    """Return list of (phys, virt, n_bytes_changed, first_offset)."""
    diffs = []
    for phys in sorted(before.keys()):
        b = before[phys]
        a = after.get(phys)
        if a is None:
            diffs.append((phys, None, -1, -1))  # block disappeared
            continue
        if b != a:
            n = sum(1 for x, y in zip(b, a) if x != y)
            first = next(i for i, (x, y) in enumerate(zip(b, a)) if x != y)
            diffs.append((phys, None, n, first))
    return diffs


def main():
    radio = DM32UVRadio("/dev/ttyUSB0")
    if not radio.connect():
        print("Connect failed — power-cycle the radio.")
        return 1
    print(f"Connected: {radio.info.model} fw={radio.info.firmware}")

    if "--test-cb" not in sys.argv:
        # Just show the address map
        radio.build_address_map()
        print(f"\nAddress map: {len(radio._address_map)} blocks (physical -> virtual)")
        print("Key virtual pages:")
        for virt in [0x4000, 0x12000, 0x42000, 0x44000, 0x5C000, 0x67000]:
            phys = radio._reverse_map.get(virt)
            print(
                f"  virt 0x{virt:X} -> phys {'0x%X' % phys if phys else 'NOT MAPPED'}"
            )
        radio.disconnect()
        return 0

    # Full test: read, write CB, read, diff
    from codeplug import make_cb_channels, build_channel_edits, build_zone_edits

    print("\n[1/4] Reading FULL codeplug (before)...")
    before = read_all(radio)
    print(f"  {len(before)} blocks captured")

    print("[2/4] Writing CB channels...")
    chans = make_cb_channels()
    edits = {**build_channel_edits(chans), **build_zone_edits("UHF-CB", len(chans))}
    result = radio.apply_edits(edits, verify=True)
    print(f"  blocks_written={result['blocks_written']} verified={result['verified']}")

    print("[3/4] Reading FULL codeplug (after)...")
    after = read_all(radio)

    print("[4/4] Diffing...")
    diffs = diff_snapshots(before, after)
    print(f"\n{'=' * 60}")
    print(f"BLOCKS CHANGED BY THE WRITE: {len(diffs)}")
    print(f"{'=' * 60}")
    for phys, virt, n, first in diffs:
        # What virtual page does this physical block hold?
        vpage = radio._address_map.get(phys)
        vp = f"0x{vpage:X}" if vpage is not None else "?"
        print(
            f"  phys 0x{phys:06X} (virt page {vp}): "
            f"{n} bytes changed, first at offset 0x{first:X}"
        )

    # EXPECTED: only the channel bank (virt 0x12000) and zone bank (virt
    # 0x5C000) should change. Anything else = corruption.
    expected_virt = {0x12000, 0x5C000}
    unexpected = []
    for phys, virt, n, first in diffs:
        vpage = radio._address_map.get(phys)
        if vpage not in expected_virt:
            unexpected.append((phys, vpage))

    print()
    if unexpected:
        print(f"⚠️  CORRUPTION DETECTED — {len(unexpected)} unexpected blocks changed:")
        for phys, vpage in unexpected:
            print(f"    phys 0x{phys:X} = virt page 0x{vpage:X} (should NOT change!)")
    else:
        print("✓ CLEAN — only channel + zone banks changed, as expected.")

    radio.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
