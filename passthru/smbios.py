"""SMBIOS memory blobs (type 16 array, type 17 devices, type 6 modules).

QEMU's `-smbios type=17,...` is a single global structure, so every DIMM is
passed as its own binary blob (`-smbios file=...`). Handles match the ones
QEMU uses itself (array 0x1000) so its type 19 still points at our array.
Layout and byte format mirror the blobs that already work in the win10 VM.
"""

import struct

ARRAY_HANDLE = 0x1000
DEVICE_HANDLE = 0x1100
MODULE_HANDLE = 0x0600
SLOTS = ("A1", "B1", "C1", "D1")
SPEED_MTS = 2400
VOLTAGE_MV = 1200

# GiB -> (Crucial part number, ranks)
PARTS = {
    4: ("CT4G4DFS824A.C8FE", 1),
    8: ("CT8G4DFS824A.C8FE", 1),
    16: ("CT16G4DFD824A.C16FE", 2),
}

# Which sticks go in which slots for each total (GiB). None = empty slot.
LAYOUTS = {
    8: (4, 4, None, None),
    12: (4, 4, 4, None),
    16: (8, 8, None, None),
    24: (8, 8, 4, 4),
    32: (8, 8, 8, 8),
    48: (16, 16, 8, 8),
    64: (16, 16, 16, 16),
}


def layout_for(total_gib):
    """Sticks per slot for a RAM size, or None if it can't be built from 4 DIMMs."""
    if total_gib in LAYOUTS:
        return LAYOUTS[total_gib]
    # Largest sticks first, at most four, sizes from PARTS.
    left, sticks = total_gib, []
    for size in sorted(PARTS, reverse=True):
        while left >= size and len(sticks) < len(SLOTS):
            sticks.append(size)
            left -= size
    if left:
        return None
    return tuple(sticks + [None] * (len(SLOTS) - len(sticks)))


def _strings(*items):
    out = b"".join(s.encode("ascii") + b"\0" for s in items)
    return out + b"\0"


def type16(device_count=len(SLOTS), max_gib=128):
    body = struct.pack(
        "<BBHBBBIHHQ",
        16, 0x17, ARRAY_HANDLE,
        0x03,  # location: system board
        0x03,  # use: system memory
        0x03,  # error correction: none
        max_gib * 1024 * 1024,  # max capacity, KiB
        0xFFFE,  # no error information
        device_count,
        0,  # extended maximum capacity (unused)
    )
    return body + b"\0\0"


def type17(index, size_gib, serial):
    slot = SLOTS[index]
    if size_gib is None:
        size_mb, speed, mtype, detail, rank = 0, 0, 0x02, 0x0004, 0
        maker = part = serial = "NO DIMM"
        mv = 0
    else:
        part, rank = PARTS[size_gib]
        size_mb, speed, mtype, detail = size_gib * 1024, SPEED_MTS, 0x1A, 0x4080  # synchronous, unbuffered
        maker, mv = "Crucial", VOLTAGE_MV
    body = struct.pack(
        "<BBHHHHHHBBBBBHHBBBBBIHHHH",
        17, 0x28, DEVICE_HANDLE + index,
        ARRAY_HANDLE, 0xFFFE,
        64, 64,  # total / data width, bits
        size_mb,
        0x09,  # form factor: DIMM
        0,  # device set
        1, 2,  # device locator, bank locator (string numbers)
        mtype, detail, speed,
        3, 4, 5, 6,  # manufacturer, serial, asset tag, part number
        rank,
        0,  # extended size
        speed,  # configured speed
        mv, mv, mv,  # min / max / configured voltage
    )
    return body + _strings(
        "DIMM_" + slot, "CHANNEL " + slot[0], maker, serial, slot, part,
    )


def _size_code(size_gib):
    if size_gib is None:
        return 0x7F  # not installed
    return (size_gib * 1024).bit_length() - 1  # 2^n MB


def type6(index, size_gib):
    code = _size_code(size_gib)
    body = struct.pack(
        "<BBHBBBHBBB",
        6, 0x0C, MODULE_HANDLE + index,
        1,  # socket designation string
        0xFF,  # bank connections: none
        0,  # current speed: unknown
        0x0002,  # current memory type: other
        code, code,
        0,  # error status
    )
    return body + _strings("DIMM_" + SLOTS[index])


def default_serials(seed):
    """Stable per-VM DIMM serials. seed: any string (VM uuid)."""
    import hashlib
    h = hashlib.sha256(seed.encode()).hexdigest().upper()
    return [h[i * 8:i * 8 + 6] + SLOTS[i] for i in range(len(SLOTS))]


def build(total_gib, serials):
    """{filename: bytes} for one VM. Raises ValueError if size is not buildable."""
    lay = layout_for(total_gib)
    if lay is None:
        raise ValueError(f"{total_gib} ГБ нельзя собрать из 4 планок {sorted(PARTS)} ГБ")
    files = {"t16_array.bin": type16()}
    for i, size in enumerate(lay):
        tag = SLOTS[i].lower()
        files[f"t17_{tag}.bin"] = type17(i, size, serials[i])
        files[f"t06_{tag}.bin"] = type6(i, size)
    return files


# ---- parsing (used by checks/selftest) ---------------------------------

def parse(blob):
    """Split one structure into (type, length, handle, formatted, strings)."""
    if len(blob) < 4:
        raise ValueError("слишком короткая структура")
    typ, length, handle = struct.unpack_from("<BBH", blob)
    if length > len(blob):
        raise ValueError("длина заголовка больше файла")
    tail = blob[length:]
    if not tail.endswith(b"\0\0"):
        raise ValueError("нет двойного нуля в конце")
    raw = tail[:-2] if tail != b"\0\0" else b""
    strings = [s.decode("ascii") for s in raw.split(b"\0")] if raw else []
    if raw.endswith(b"\0"):
        raise ValueError("пустая строка в таблице строк")
    return typ, length, handle, blob[:length], strings


def dimm_size_mb(blob):
    typ, length, _, fmt, _ = parse(blob)
    if typ != 17:
        raise ValueError("не type 17")
    size = struct.unpack_from("<H", fmt, 0x0C)[0]
    if size == 0x7FFF:
        return struct.unpack_from("<I", fmt, 0x1C)[0]
    return size * 1024 if size & 0x8000 else size
