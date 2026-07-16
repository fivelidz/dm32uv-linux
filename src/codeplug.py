"""
codeplug.py — DM-32UV codeplug encoder (channels + zones → binary).

Produces byte-identical output to qdmr for analog FM channels. Based on the
exact binary format extracted from qdmr's dm32uv_codeplug.cc.

Memory map (virtual addresses):
    channels  0x12000  (48-byte records, count as uint16 LE at 0x12000)
    zones     0x5C000  (145-byte records, count as uint8 at 0x5C000)
"""

from __future__ import annotations
from dataclasses import dataclass, field

# ── Memory addresses ───────────────────────────────────────────────
CHANNEL_BANK = 0x12000
ZONE_BANK = 0x5C000
BLOCK_SIZE = 0x1000
CHANNEL_SIZE = 0x30  # 48 bytes
ZONE_SIZE = 0x91  # 145 bytes
CHANNELS_IN_BLOCK0 = 84
CHANNELS_PER_BLOCK = 85
ZONES_PER_BLOCK = 28


@dataclass
class CPChannel:
    """A channel for encoding."""

    name: str
    rx_mhz: float
    tx_mhz: float = 0.0
    mode: str = "FM"  # FM | DMR
    power: str = "High"  # Low | Medium | High
    wide: bool = False  # False=narrow 12.5k, True=wide 25k
    rx_only: bool = False
    squelch: int = 3
    color_code: int = 1
    time_slot: int = 1


# ── Encoding helpers ───────────────────────────────────────────────


def bcd8_le(val: int) -> bytes:
    """8-nibble packed BCD as little-endian uint32. val = freq_Hz // 10."""
    digits = f"{val:08d}"  # MSD first, 8 digits
    word = 0
    for i, ch in enumerate(digits):
        word |= int(ch) << (28 - 4 * i)
    return word.to_bytes(4, "little")


def encode_name(name: str, size: int = 16) -> bytes:
    """Latin-1, fixed size, 0x00-padded."""
    b = name.encode("latin-1", errors="ignore")[:size]
    return b.ljust(size, b"\x00")


POWER_MAP = {"Low": 0, "Medium": 1, "High": 2}


def encode_channel(ch: CPChannel) -> bytes:
    """Encode one channel into a 48-byte record (qdmr-exact)."""
    rec = bytearray(48)  # clear()

    # 0x00: name (16 bytes, Latin-1, 0x00-padded)
    rec[0x00:0x10] = encode_name(ch.name, 16)

    # 0x10: RX frequency (BCD8 LE, Hz/10)
    rx_hz = int(round(ch.rx_mhz * 1_000_000))
    rec[0x10:0x14] = bcd8_le(rx_hz // 10)

    # 0x14: TX frequency (BCD8 LE) or 0xFFFFFFFF if rx_only
    if ch.rx_only or (ch.tx_mhz == 0 and ch.rx_mhz == 0):
        rec[0x14:0x18] = b"\xff\xff\xff\xff"
    else:
        tx_hz = int(round((ch.tx_mhz or ch.rx_mhz) * 1_000_000))
        rec[0x14:0x18] = bcd8_le(tx_hz // 10)

    # 0x18: channelType(bit4: FM=0/DMR=1) | rxOnly(bit3) | power(bit1-2) | lone(bit0)
    ctype = 1 if ch.mode == "DMR" else 0
    power = POWER_MAP.get(ch.power, 2)
    rec[0x18] = (ctype << 4) | ((1 if ch.rx_only else 0) << 3) | ((power & 0b11) << 1)

    # 0x19: bandwidth(bit7: 0=narrow/1=wide); scanListIndex(5 bits @bit2)=0
    rec[0x19] = (1 << 7) if ch.wide else 0

    # 0x1A: admitCriterion(2 bits @bit4 = Always=0); preventTalkaround(bit7)=0
    rec[0x1A] = 0

    # 0x1C: squelchLevel(4 bits @bit4)
    rec[0x1C] = (ch.squelch & 0xF) << 4

    # 0x1D: DMR fields — timeslot(bit4), colorcode(4 bits @bit0)
    if ch.mode == "DMR":
        rec[0x1D] = ((1 if ch.time_slot == 2 else 0) << 4) | (ch.color_code & 0xF)

    # 0x21, 0x23: rxTone, txTone = 0xFFFF (none)
    rec[0x21:0x23] = b"\xff\xff"
    rec[0x23:0x25] = b"\xff\xff"

    return bytes(rec)


def encode_zone(name: str, channel_indices: list[int]) -> bytes:
    """Encode a zone (145 bytes). channel_indices are 0-based global indices."""
    rec = bytearray(ZONE_SIZE)
    rec[0x00:0x10] = encode_name(name, 16)
    count = min(64, len(channel_indices))
    rec[0x10] = count
    for i in range(count):
        # channel ref = index + 1, uint16 LE, at 0x11 + i*2
        ref = channel_indices[i] + 1
        rec[0x11 + i * 2 : 0x13 + i * 2] = ref.to_bytes(2, "little")
    return bytes(rec)


def channel_address(index: int) -> int:
    """Virtual address of channel record `index` (0-based)."""
    if index < CHANNELS_IN_BLOCK0:
        return CHANNEL_BANK + 0x10 + index * CHANNEL_SIZE
    index2 = index - CHANNELS_IN_BLOCK0
    block = 1 + index2 // CHANNELS_PER_BLOCK
    slot = index2 % CHANNELS_PER_BLOCK
    return CHANNEL_BANK + block * BLOCK_SIZE + slot * CHANNEL_SIZE


# ── Build the codeplug edits ───────────────────────────────────────


def build_channel_edits(channels: list[CPChannel]) -> dict[int, bytes]:
    """Return {virtual_address: bytes} edits to write the channel list.

    This writes the channel count + each channel record. The caller applies
    these as a read-modify-write on the existing codeplug blocks.
    """
    edits: dict[int, bytes] = {}

    # Channel count (uint16 LE at 0x12000)
    edits[CHANNEL_BANK] = len(channels).to_bytes(2, "little")

    # Each channel record
    for i, ch in enumerate(channels):
        addr = channel_address(i)
        edits[addr] = encode_channel(ch)

    return edits


def build_zone_edits(zone_name: str, num_channels: int) -> dict[int, bytes]:
    """Return edits to create one zone containing all channels."""
    edits: dict[int, bytes] = {}
    # Zone bank header: count byte = 1 zone
    edits[ZONE_BANK] = b"\x01"
    # Zone record at 0x5C010
    indices = list(range(num_channels))
    edits[ZONE_BANK + 0x10] = encode_zone(zone_name, indices[:64])
    return edits


def make_cb_channels() -> list[CPChannel]:
    """Australian UHF CB — 80 analog FM channels."""
    channels = []
    for ch in range(1, 81):
        freq = 476.4250 + (ch - 1) * 0.0125
        channels.append(
            CPChannel(
                name=f"CB{ch:02d}",
                rx_mhz=round(freq, 4),
                tx_mhz=round(freq, 4),
                mode="FM",
                power="High",
                wide=False,  # 12.5 kHz narrow (CB standard)
                rx_only=False,
                squelch=3,
            )
        )
    return channels
