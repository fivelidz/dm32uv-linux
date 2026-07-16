"""baofeng_mcp.dm32uv — Native Python driver for the Baofeng DM-32UV DMR radio.

Implements the serial protocol for reading/writing codeplugs without the
official CPS software. Based on:
  - The reverse-engineered DM32-Protocol-Spec (github.com/infamy/DM32-Protocol-Spec)
  - The qdmr C++ implementation (github.com/hmatuschek/qdmr)

Protocol summary:
  1. Connect at 115200 baud, DTR=high, RTS=low
  2. Handshake: PSEARCH → PASSSTA → SYSINFO → V-frames → PROGRAM
  3. Build address map (physical ↔ virtual 4KB block mapping)
  4. Read/write 4KB blocks via R/W commands
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

try:
    import serial as pyserial
except ImportError:
    pyserial = None  # type: ignore

BAUD = 115200
TIMEOUT = 1.0  # seconds for most operations
WRITE_TIMEOUT = 5.0  # seconds for write operations
BLOCK_SIZE = 0x1000  # 4096 bytes


# ── Data Models ────────────────────────────────────────────────────


@dataclass
class RadioInfo:
    model: str = ""
    firmware: str = ""
    build_date: str = ""
    codeplug_range: tuple[int, int] = (0, 0)
    callsign_range: tuple[int, int] = (0, 0)


@dataclass
class Channel:
    index: int = 0
    name: str = ""
    rx_freq_mhz: float = 0.0
    tx_freq_mhz: float = 0.0
    mode: str = "Analog"  # Analog | Digital
    power: str = "High"  # Low | Medium | High
    bandwidth: str = "Narrow"  # Narrow | Wide
    rx_only: bool = False
    squelch: int = 0
    color_code: int = 0
    time_slot: int = 1
    tx_tone: str = ""
    rx_tone: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "rx_freq_mhz": round(self.rx_freq_mhz, 4),
            "tx_freq_mhz": round(self.tx_freq_mhz, 4),
            "mode": self.mode,
            "power": self.power,
            "bandwidth": self.bandwidth,
            "rx_only": self.rx_only,
            "squelch": self.squelch,
            "color_code": self.color_code,
            "time_slot": self.time_slot,
            "tx_tone": self.tx_tone,
            "rx_tone": self.rx_tone,
        }


# ── Encoding Helpers ───────────────────────────────────────────────


def encode_frequency_bcd(freq_mhz: float) -> bytes:
    """Encode frequency in MHz as 4-byte BCD little-endian.

    The radio stores frequency as an 8-digit decimal number representing
    Hz/10, packed as BCD nibbles into a uint32 little-endian word.
    E.g., 145.350 MHz = 14535000 (Hz/10) → 0x14653500 → LE: 00 35 65 14
    """
    val = int(round(freq_mhz * 1_000_000)) // 10  # Hz/10 as integer
    # Extract 8 decimal digits
    digits = []
    for _ in range(8):
        digits.append(val % 10)
        val //= 10
    digits.reverse()  # now digits[0] is most significant
    # Pack into uint32: digit[0] in the highest nibble
    packed = 0
    for d in digits:
        packed = (packed << 4) | d
    return packed.to_bytes(4, "little")


def decode_frequency_bcd(data: bytes) -> float:
    """Decode 4-byte BCD little-endian to frequency in MHz."""
    if data == b"\xff\xff\xff\xff":
        return 0.0  # invalid/no TX
    val = int.from_bytes(data, "little")
    result = 0
    multiplier = 1
    for shift in range(0, 32, 4):
        digit = (val >> shift) & 0xF
        if digit > 9:
            digit = 0  # corrupt BCD
        result += digit * multiplier
        multiplier *= 10
    return (result * 10) / 1_000_000.0  # Hz/10 → MHz


def decode_name(data: bytes) -> str:
    """Decode a channel/zone name from raw bytes."""
    # Null-terminated ASCII, padded with 0xFF
    name = data.split(b"\x00")[0]
    return name.replace(b"\xff", b"").decode("ascii", errors="ignore").strip()


def encode_name(name: str, size: int = 16) -> bytes:
    """Encode a name into a fixed-size field."""
    buf = bytearray(b"\xff" * size)
    name_bytes = name.encode("ascii", errors="ignore")[: size - 1]
    buf[0 : len(name_bytes)] = name_bytes
    buf[len(name_bytes)] = 0x00
    return bytes(buf)


# ── Channel Binary Parsing ─────────────────────────────────────────


def parse_channel(data: bytes, index: int = 0) -> Channel:
    """Parse a 48-byte channel record into a Channel object."""
    ch = Channel(index=index)
    ch.name = decode_name(data[0x00:0x10])
    ch.rx_freq_mhz = decode_frequency_bcd(data[0x10:0x14])
    ch.tx_freq_mhz = decode_frequency_bcd(data[0x14:0x18])

    # Byte 0x18: mode, rx_only, power, lone_worker
    mode_val = (data[0x18] >> 4) & 0x0F
    ch.mode = "Digital" if mode_val in (1, 3) else "Analog"
    ch.rx_only = bool(data[0x18] & 0x08)
    power_val = (data[0x18] >> 1) & 0x03
    ch.power = ["Low", "Medium", "High"][power_val] if power_val <= 2 else "High"

    # Byte 0x19: bandwidth, scan
    ch.bandwidth = "Wide" if (data[0x19] & 0x80) else "Narrow"

    # Byte 0x1C: squelch (bits 4-7)
    ch.squelch = (data[0x1C] >> 4) & 0x0F

    # Byte 0x1D: color code (bits 0-3), time slot (bit 4)
    ch.color_code = data[0x1D] & 0x0F
    ch.time_slot = 2 if (data[0x1D] & 0x10) else 1

    return ch


def encode_channel(ch: Channel) -> bytes:
    """Encode a Channel into a 48-byte record."""
    data = bytearray(48)
    # Name
    data[0x00:0x10] = encode_name(ch.name, 16)
    # Frequencies
    data[0x10:0x14] = encode_frequency_bcd(ch.rx_freq_mhz)
    if ch.rx_only:
        data[0x14:0x18] = b"\xff\xff\xff\xff"
    else:
        data[0x14:0x18] = encode_frequency_bcd(ch.tx_freq_mhz or ch.rx_freq_mhz)

    # Byte 0x18: mode | rx_only | power | lone_worker
    mode_val = 0x10 if ch.mode == "Digital" else 0x00  # bits 4-7
    rx_only = 0x08 if ch.rx_only else 0x00
    power_val = {"Low": 0x00, "Medium": 0x02, "High": 0x04}.get(ch.power, 0x04)
    data[0x18] = mode_val | rx_only | power_val

    # Byte 0x19: bandwidth
    if ch.bandwidth == "Wide":
        data[0x19] |= 0x80

    # Byte 0x1C: squelch (bits 4-7)
    data[0x1C] |= (ch.squelch & 0x0F) << 4

    # Byte 0x1D: color code (bits 0-3), time slot (bit 4)
    data[0x1D] |= ch.color_code & 0x0F
    if ch.time_slot == 2:
        data[0x1D] |= 0x10

    return bytes(data)


# ── Radio Driver ───────────────────────────────────────────────────


class DM32UVRadio:
    """Native serial driver for the Baofeng DM-32UV."""

    def __init__(self, port: str = "/dev/ttyUSB0"):
        self.port_path = port
        self._serial: pyserial.Serial | None = None
        self.info = RadioInfo()
        self._address_map: dict[int, int] = {}  # physical → virtual
        self._reverse_map: dict[int, int] = {}  # virtual → physical

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # ── Low-level serial ───────────────────────────────────────────

    def _open(self) -> bool:
        if pyserial is None:
            raise RuntimeError("pyserial not installed")
        try:
            self._serial = pyserial.Serial(self.port_path, BAUD, timeout=TIMEOUT)
            # CRITICAL: DM-32UV needs RTS HIGH, DTR LOW for the PSEARCH handshake.
            # (Verified empirically — RTS high is what makes the radio respond
            # with \x06DP570UV. This matches qdmr issue #946: asserting DTR or
            # de-asserting RTS wedges the handshake.)
            self._serial.rts = True
            self._serial.dtr = False
            time.sleep(0.5)  # CH340 settle time
            return True
        except Exception:
            self._serial = None
            return False

    def _close(self):
        if self._serial:
            # Reset the radio's USB-serial adapter by dropping DTR
            try:
                self._serial.dtr = False
                time.sleep(0.5)
            except Exception:
                pass
            self._serial.close()
            self._serial = None

    def _write(self, data: bytes):
        assert self._serial is not None
        self._serial.write(data)
        self._serial.flush()

    def _read(self, n: int) -> bytes:
        assert self._serial is not None
        return self._serial.read(n)

    def _clear_input(self):
        if self._serial:
            self._serial.reset_input_buffer()

    # ── Handshake ──────────────────────────────────────────────────

    def connect(self) -> bool:
        """Full connection sequence: handshake + identify + program mode."""
        if not self._open():
            return False

        # PSEARCH — identify radio (retry up to 3 times)
        identified = False
        for attempt in range(3):
            time.sleep(0.5)
            self._clear_input()
            self._write(b"PSEARCH")
            resp = self._read(8)
            if len(resp) == 8 and resp[0:1] == b"\x06":
                model = resp[1:8].decode("ascii", errors="ignore")
                self.info.model = model
                identified = True
                break
            time.sleep(0.5)

        if not identified:
            self._close()
            return False

        time.sleep(0.1)

        # PASSSTA — password/status
        self._write(b"PASSSTA")
        resp = self._read(3)
        if len(resp) < 3 or resp[0:1] != b"P":
            self._close()
            return False

        time.sleep(0.1)

        # SYSINFO
        self._write(b"SYSINFO")
        resp = self._read(1)
        if resp != b"\x06":
            self._close()
            return False

        time.sleep(0.1)

        # V-frame 0x01: firmware version
        self._write(b"\x56\x00\x00\x00\x01")
        resp = self._read(3)
        if len(resp) >= 3 and resp[0:1] == b"V":
            fw_len = resp[2]
            fw_data = self._read(fw_len)
            self.info.firmware = fw_data.decode("ascii", errors="ignore")

        time.sleep(0.1)

        # V-frame 0x0A: codeplug memory range
        self._write(b"\x56\x00\x00\x00\x0a")
        resp = self._read(3)
        if len(resp) >= 3 and resp[0:1] == b"V":
            data_len = resp[2]
            data = self._read(data_len)
            if len(data) >= 8:
                start = struct.unpack("<I", data[0:4])[0]
                end = struct.unpack("<I", data[4:8])[0]
                self.info.codeplug_range = (start, end)

        time.sleep(0.1)

        # Enter programming mode
        self._write(b"\xff\xff\xff\xff\x0cPROGRAM")
        resp = self._read(1)
        if resp != b"\x06":
            self._close()
            return False

        time.sleep(0.01)

        self._write(b"\x02")
        resp = self._read(8)
        # Expect 8 bytes (content varies)

        time.sleep(0.01)

        self._write(b"\x06")
        resp = self._read(1)
        if resp != b"\x06":
            self._close()
            return False

        return True

    def disconnect(self):
        """Exit programming mode and close the port."""
        self._close()
        self._address_map.clear()
        self._reverse_map.clear()

    # ── Memory Read/Write ──────────────────────────────────────────

    def read_memory(self, address: int, length: int) -> bytes:
        """Read `length` bytes from `address` (physical address)."""
        cmd = b"\x52" + struct.pack("<I", address)[:3] + struct.pack("<H", length)
        self._write(cmd)
        # Read response header: W + addr(3) + len(2)
        header = self._read(6)
        if len(header) < 6 or header[0:1] != b"W":
            raise IOError(f"Bad read response: {header.hex() if header else 'timeout'}")
        resp_len = struct.unpack("<H", header[4:6])[0]
        data = self._read(resp_len)
        if len(data) < resp_len:
            raise IOError(f"Short read: expected {resp_len}, got {len(data)}")
        return data

    def write_memory(self, address: int, data: bytes):
        """Write `data` to a PHYSICAL `address`.

        Command format (from qdmr WriteRequest):
          'W' + address(3 bytes LE) + length(2 bytes LE) + payload
        Response: ACK (0x06). Chunks to <=4096 bytes, aligned to 0x1000.

        IMPORTANT: `address` is the PHYSICAL address (from the address map),
        the same address space read_memory() uses. Do NOT pass a raw virtual
        codeplug address here — translate it via _reverse_map first.
        """
        offset = 0
        remaining = len(data)
        while remaining > 0:
            if (address & 0xFFF) != 0:
                n = min(remaining, 0x1000 - (address & 0xFFF))
            else:
                n = min(remaining, 0x1000)
            chunk = data[offset : offset + n]
            cmd = bytearray()
            cmd.append(0x57)  # 'W'
            cmd += struct.pack("<I", address)[:3]  # 3-byte address LE
            cmd += struct.pack("<H", n)  # 2-byte length LE
            cmd += chunk
            self._write(bytes(cmd))
            resp = self._read(1)
            if resp != b"\x06":
                raise IOError(
                    f"Write NAK at 0x{address:X}: {resp.hex() if resp else 'timeout'}"
                )
            address += n
            offset += n
            remaining -= n
            time.sleep(0.02)

    def write_block(self, address: int, data: bytes, metadata: int = 0):
        """Compatibility shim — writes via write_memory (metadata ignored)."""
        self.write_memory(address, data)

    # ── Address Map ────────────────────────────────────────────────

    def build_address_map(self, progress_cb=None) -> dict[int, int]:
        """Probe all 4KB blocks to build the physical→virtual address map.

        Each block's last byte (offset 0xFFF) contains a prefix that
        identifies the virtual page. Returns {physical_addr: virtual_addr}.
        """
        if not self.info.codeplug_range[1]:
            raise RuntimeError("Not connected or no codeplug range")
        self._address_map.clear()
        self._reverse_map.clear()

        start, end = self.info.codeplug_range
        for addr in range(start, end + 1, BLOCK_SIZE):
            try:
                metadata = self.read_memory(addr + 0xFFF, 1)[0]
            except IOError:
                continue
            if metadata == 0x00 or metadata == 0xFF:
                continue  # unmapped
            virtual = metadata << 12
            self._address_map[addr] = virtual
            self._reverse_map[virtual] = addr
            if progress_cb:
                progress_cb(addr, start, end)
            time.sleep(0.005)  # 5ms between probes

        return dict(self._address_map)

    def physical_to_virtual(self, phys: int) -> int | None:
        return self._address_map.get(phys)

    def virtual_to_physical(self, virt: int) -> int | None:
        return self._reverse_map.get(virt)

    def read_virtual(self, virtual_addr: int, length: int) -> bytes:
        """Read from a virtual address (translates via the address map)."""
        # Find the physical block containing this virtual address
        block_virt = virtual_addr & ~0xFFF  # align to 4KB
        phys = self._reverse_map.get(block_virt)
        if phys is None:
            raise IOError(f"Virtual address 0x{block_virt:06X} not mapped")
        offset = virtual_addr & 0xFFF
        data = self.read_memory(phys + offset, length)
        return data

    # ── Channel Operations ─────────────────────────────────────────

    def read_channels(self) -> list[Channel]:
        """Read all channels from the radio."""
        if not self._address_map:
            self.build_address_map()

        # Find channel blocks (virtual addresses 0x12000 - 0x41FFF)
        channel_blocks = []
        for virt in sorted(self._reverse_map.keys()):
            if 0x12000 <= virt < 0x42000:
                channel_blocks.append(virt)

        if not channel_blocks:
            return []

        # Read first block to get channel count
        first_phys = self._reverse_map[channel_blocks[0]]
        first_block = self.read_memory(first_phys, BLOCK_SIZE)
        channel_count = struct.unpack("<I", first_block[0:4])[0]
        time.sleep(0.025)

        channels = []
        for block_idx, virt in enumerate(channel_blocks):
            if len(channels) >= channel_count:
                break
            phys = self._reverse_map[virt]
            block_data = self.read_memory(phys, BLOCK_SIZE)
            time.sleep(0.025)

            # First block: channels start at offset 0x10; others at 0x00
            offset = 0x10 if block_idx == 0 else 0x00
            while offset + 48 <= BLOCK_SIZE and len(channels) < channel_count:
                ch_data = block_data[offset : offset + 48]
                ch = parse_channel(ch_data, index=len(channels) + 1)
                if not ch.name and ch.rx_freq_mhz == 0:
                    break  # empty slot
                channels.append(ch)
                offset += 48

        return channels

    def read_codeplug_block(self, virtual_addr: int) -> bytes:
        """Read a full 4KB block at a virtual address."""
        phys = self._reverse_map.get(virtual_addr)
        if phys is None:
            raise IOError(f"Virtual address 0x{virtual_addr:06X} not mapped")
        return self.read_memory(phys, BLOCK_SIZE)

    def read_full_codeplug(self, progress_cb=None) -> dict[int, bytes]:
        """Read the entire codeplug into a {virtual_addr: data} dict."""
        if not self._address_map:
            self.build_address_map(progress_cb)

        codeplug = {}
        for phys, virt in sorted(self._address_map.items()):
            codeplug[virt] = self.read_memory(phys, BLOCK_SIZE)
            time.sleep(0.025)
            if progress_cb:
                progress_cb(virt, len(codeplug), len(self._address_map))

        return codeplug

    # ── Write path (VIRTUAL-address aware) ─────────────────────────

    def apply_edits(self, edits: dict, verify: bool = True, progress_cb=None) -> dict:
        """Apply {virtual_address: bytes} edits, translating to PHYSICAL.

        THE CRITICAL FIX: codeplug edits are specified by VIRTUAL address
        (0x12000 = channels, 0x5C000 = zones). But the radio stores those
        pages at different PHYSICAL addresses (found via the metadata probe).
        Writes MUST go to the physical address, or they clobber whatever
        real data lives at the physical location = corruption.

        For each affected 4KB virtual block:
          1. Translate virtual block -> physical (via _reverse_map)
          2. Read the current physical block
          3. Overlay edits at their in-block offsets
          4. Write back to the PHYSICAL address
          5. Verify by reading the physical address back
        """
        if not self._address_map:
            self.build_address_map()

        # Group edits by 4KB virtual block
        blocks: dict = {}
        for vaddr, data in edits.items():
            vblock = vaddr & ~0xFFF
            blocks.setdefault(vblock, []).append((vaddr, data))

        errors = []
        written = 0
        n = len(blocks)

        for i, (vblock, block_edits) in enumerate(sorted(blocks.items())):
            # Translate virtual block -> physical
            phys = self._reverse_map.get(vblock)
            if phys is None:
                errors.append(
                    f"Virtual block 0x{vblock:X} not mapped — cannot write "
                    f"safely (would corrupt). Skipped."
                )
                continue

            # Read current physical block, overlay edits, write back
            try:
                current = bytearray(self.read_memory(phys, BLOCK_SIZE))
            except IOError as e:
                errors.append(f"Read-before-write failed 0x{phys:X}: {e}")
                continue

            for vaddr, data in block_edits:
                off = vaddr & 0xFFF
                current[off : off + len(data)] = data

            try:
                self.write_memory(phys, bytes(current))
                written += 1
            except IOError as e:
                errors.append(f"Write failed phys 0x{phys:X}: {e}")
                continue

            if verify:
                try:
                    rb = self.read_memory(phys, BLOCK_SIZE)
                    for vaddr, data in block_edits:
                        off = vaddr & 0xFFF
                        if rb[off : off + len(data)] != data:
                            errors.append(f"Verify mismatch virt 0x{vaddr:X}")
                except IOError as e:
                    errors.append(f"Verify read failed 0x{phys:X}: {e}")

            if progress_cb:
                progress_cb(vblock, i + 1, n)

        return {
            "blocks_written": written,
            "verified": verify and len(errors) == 0,
            "errors": errors,
        }
