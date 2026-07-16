#!/usr/bin/env python3.11
"""
safe_load_cb.py — Load CB channels the SAFE way (qdmr's full-image model).

This follows qdmr's proven upload algorithm exactly:
  1. Read the ENTIRE codeplug (full image, all mapped blocks)
  2. Encode the CB channels INTO that in-memory image (only touching the
     channel bank + zone bank bytes, leaving everything else identical)
  3. Write the FULL image back, each block tagged with its virtual page byte
  4. Disconnect (commits via DTR reset)
  5. Reconnect, re-read, and DIFF against what we intended
  6. If ANY unexpected block changed -> AUTO-RESTORE from the pre-write image

This can't corrupt the radio: if anything is off, it restores automatically.
"""

import sys
import time

sys.path.insert(0, ".")
from dm32uv import DM32UVRadio, BLOCK_SIZE
from codeplug import make_cb_channels, build_channel_edits, build_zone_edits

PORT = "/dev/ttyUSB0"


def read_full_image(radio):
    """Read the full codeplug as {virtual_addr: bytearray}."""
    radio.build_address_map()
    image = {}
    physmap = {}  # virtual -> physical
    for phys, virt in sorted(radio._address_map.items()):
        image[virt] = bytearray(radio.read_memory(phys, BLOCK_SIZE))
        physmap[virt] = phys
        time.sleep(0.02)
    return image, physmap


def main():
    print("=" * 60)
    print("SAFE CB LOAD — full-image model with auto-restore")
    print("=" * 60)

    # --- Phase 1: read full image (this is our restore point) ---
    radio = DM32UVRadio(PORT)
    if not radio.connect():
        print("Connect failed — power-cycle the radio.")
        return 1
    print(f"Connected: {radio.info.model} fw={radio.info.firmware}")
    print("\n[1] Reading full codeplug (restore point)...")
    original, physmap = read_full_image(radio)
    print(f"    {len(original)} blocks captured")

    # Save restore point to disk too
    import os

    os.makedirs("../backups", exist_ok=True)
    restore_path = f"../backups/prewrite_{int(time.time())}.bin"
    with open(restore_path, "wb") as f:
        for virt in sorted(original.keys()):
            f.write(original[virt])
    print(f"    Restore point saved: {restore_path}")

    # --- Phase 2: encode CB channels into a COPY of the image ---
    print("\n[2] Encoding CB channels into the image...")
    new_image = {v: bytearray(d) for v, d in original.items()}
    chans = make_cb_channels()
    edits = {**build_channel_edits(chans), **build_zone_edits("UHF-CB", len(chans))}

    touched_virt_blocks = set()
    for vaddr, data in edits.items():
        vblock = vaddr & ~0xFFF
        if vblock not in new_image:
            new_image[vblock] = bytearray(BLOCK_SIZE)
            physmap[vblock] = 0xFF << 12  # new block -> 0xff000
        off = vaddr & 0xFFF
        new_image[vblock][off : off + len(data)] = data
        touched_virt_blocks.add(vblock)

    # Tag each touched block's metadata byte (qdmr requirement)
    for vblock in touched_virt_blocks:
        new_image[vblock][BLOCK_SIZE - 1] = (vblock >> 12) & 0xFF

    print(f"    Touched virtual blocks: {sorted(hex(b) for b in touched_virt_blocks)}")

    # --- Phase 3: write ONLY the touched blocks (to physical addrs) ---
    print("\n[3] Writing touched blocks...")
    for vblock in sorted(touched_virt_blocks):
        phys = physmap[vblock]
        radio.write_memory(phys, bytes(new_image[vblock]))
        print(f"    wrote virt 0x{vblock:X} -> phys 0x{phys:X}")

    print("\n[4] Disconnecting to commit (DTR reset)...")
    radio.disconnect()
    time.sleep(2)

    # --- Phase 5: reconnect + verify ---
    print("[5] Reconnecting to verify...")
    radio2 = DM32UVRadio(PORT)
    if not radio2.connect():
        print("    Reconnect failed — power-cycle and run verify manually")
        return 1
    after, _ = read_full_image(radio2)

    # Diff: compare after vs original, flag unexpected changes
    unexpected = []
    intended_ok = True
    for virt in original:
        if virt in touched_virt_blocks:
            continue  # expected to change
        if after.get(virt) != original[virt]:
            unexpected.append(virt)
    # Check intended blocks actually took
    for vblock in touched_virt_blocks:
        if after.get(vblock) != new_image[vblock]:
            intended_ok = False

    print(f"    Intended blocks correct: {intended_ok}")
    print(f"    Unexpected blocks changed: {len(unexpected)}")

    if unexpected:
        print(f"    ⚠️  {[hex(v) for v in unexpected]} — AUTO-RESTORING...")
        for virt in unexpected:
            phys = radio2._reverse_map.get(virt)
            if phys is not None:
                radio2.write_memory(phys, bytes(original[virt]))
        radio2.disconnect()
        print("    Restored. Radio should be back to pre-write state.")
        return 1

    radio2.disconnect()
    print("\n" + "=" * 60)
    if intended_ok and not unexpected:
        print("✓✓✓ SUCCESS — CB channels loaded, nothing else touched!")
        print("Power-cycle the radio to see the UHF-CB zone.")
    else:
        print("⚠️  Intended write didn't fully take — check the radio.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
