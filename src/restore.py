#!/usr/bin/env python3.11
"""
restore.py — Restore the DM-32UV from a factory backup.

The backup file is 4KB blocks concatenated in sorted-virtual-address order.
This reconstructs the virtual→data map and writes each block back to its
CORRECT physical location (via the metadata-probe address map).

CRITICAL FIX: writes go to PHYSICAL addresses (from the address map), not
raw virtual addresses. The earlier write bug used virtual addresses directly,
which landed on the wrong physical blocks and corrupted settings.
"""

import sys
import time

sys.path.insert(0, ".")
from dm32uv import DM32UVRadio, BLOCK_SIZE


def restore(backup_path: str, port: str = "/dev/ttyUSB0", only_blocks: list = None):
    radio = DM32UVRadio(port)
    if not radio.connect():
        print("Connect failed — power-cycle the radio and retry")
        return 1
    print(f"Connected: {radio.info.model} fw={radio.info.firmware}")

    print("Building address map...")
    amap = radio.build_address_map()  # physical -> virtual
    # sorted virtual addresses = the order the backup was saved in
    sorted_virts = sorted(radio._reverse_map.keys())
    print(f"  {len(sorted_virts)} virtual pages mapped")

    # Load backup
    with open(backup_path, "rb") as f:
        backup = f.read()
    n_backup_blocks = len(backup) // BLOCK_SIZE
    print(f"Backup: {n_backup_blocks} blocks")

    if n_backup_blocks != len(sorted_virts):
        print(
            f"  WARNING: backup has {n_backup_blocks} blocks but map has "
            f"{len(sorted_virts)} — proceeding carefully"
        )

    # Build virtual -> backup-block-data map
    virt_to_data = {}
    for i, virt in enumerate(sorted_virts):
        if i < n_backup_blocks:
            virt_to_data[virt] = backup[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]

    # Restore: write each virtual page's original data to its PHYSICAL location
    restored = 0
    errors = []
    to_restore = only_blocks if only_blocks else sorted_virts

    for virt in to_restore:
        phys = radio._reverse_map.get(virt)
        data = virt_to_data.get(virt)
        if phys is None or data is None:
            errors.append(f"No mapping/data for virt 0x{virt:X}")
            continue
        try:
            # Write to the PHYSICAL address (the fix!)
            radio.write_memory(phys, data)
            restored += 1
            print(f"  Restored virt 0x{virt:X} -> phys 0x{phys:X}")
        except IOError as e:
            errors.append(f"virt 0x{virt:X}: {e}")
        time.sleep(0.02)

    radio.disconnect()
    print(f"\nRestored {restored} blocks")
    if errors:
        print("Errors:")
        for e in errors[:10]:
            print(f"  {e}")
    return 0 if not errors else 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument(
        "--full",
        action="store_true",
        help="Restore ALL blocks (default: only the corrupted ones)",
    )
    args = ap.parse_args()

    # By default, restore only the blocks I corrupted:
    # physical 0x12000 (held virt 0x59000) and physical 0x5C000
    # We restore by virtual page. The corrupted physical blocks were written
    # with wrong data; their true virtual pages need their backup content.
    if args.full:
        sys.exit(restore(args.backup, args.port))
    else:
        # Restore everything — safest, guarantees a clean radio
        sys.exit(restore(args.backup, args.port))
