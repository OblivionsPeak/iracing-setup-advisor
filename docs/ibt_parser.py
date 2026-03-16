#!/usr/bin/env python3
"""
iRacing IBT telemetry file parser.
Reads binary .ibt files and returns all channels as numpy arrays.
"""

import struct
import numpy as np
from pathlib import Path

HEADER_SIZE        = 112
DISK_SUB_HDR_SIZE  = 32
VAR_HEADER_SIZE    = 144

# irsdk type id → numpy dtype string
VAR_DTYPES = {
    0: 'S1',    # char
    1: '?',     # bool
    2: '<i4',   # int32
    3: '<u4',   # uint32 / bitField
    4: '<f4',   # float32
    5: '<f8',   # float64
}


def parse_ibt(filepath):
    """
    Parse an iRacing .ibt telemetry file.

    Returns
    -------
    channels     : dict  {name: np.ndarray}
    session_info : str   YAML blob from iRacing
    tick_rate    : int   samples per second (usually 60)
    record_count : int   total sample count
    """
    with open(filepath, 'rb') as f:
        raw = f.read()

    if len(raw) < HEADER_SIZE + DISK_SUB_HDR_SIZE:
        raise ValueError("File is too small to be a valid .ibt file.")

    # ── Main header (112 bytes) ──────────────────────────────────────────────
    # 10 ints (40 bytes) + pad[2] (8 bytes) = 48 bytes
    # followed by 4 × irsdk_varBuf (each 16 bytes) = 64 bytes  → total 112
    (ver, status, tick_rate,
     session_info_update, session_info_len, session_info_offset,
     num_vars, var_header_offset,
     num_buf, buf_len, _pad0, _pad1) = struct.unpack_from('<12i', raw, 0)

    # varBuf[0]: (tickCount=4, bufOffset=4, pad=8) at byte 48
    data_buf_offset = struct.unpack_from('<4i', raw, 48)[1]

    # ── Disk sub-header (32 bytes at 112) ────────────────────────────────────
    # time_t(8) + double(8) + double(8) + int(4) + int(4)
    _start_date, _start_time, _end_time, lap_count, record_count = \
        struct.unpack_from('<qddii', raw, HEADER_SIZE)

    # ── Session info YAML ────────────────────────────────────────────────────
    session_info = (
        raw[session_info_offset: session_info_offset + session_info_len]
        .rstrip(b'\x00')
        .decode('utf-8', errors='replace')
    )

    # ── Variable headers (144 bytes each) ────────────────────────────────────
    var_headers = []
    for i in range(num_vars):
        base = var_header_offset + i * VAR_HEADER_SIZE
        var_type, var_off, count = struct.unpack_from('<3i', raw, base)
        name = raw[base + 16: base + 48].rstrip(b'\x00').decode('utf-8', errors='replace')
        unit = raw[base + 112: base + 144].rstrip(b'\x00').decode('utf-8', errors='replace')
        var_headers.append({
            'type': var_type, 'offset': var_off,
            'count': count, 'name': name, 'unit': unit,
        })

    # ── Safety: data must come after all headers ──────────────────────────────
    min_data_start = var_header_offset + num_vars * VAR_HEADER_SIZE
    if data_buf_offset < min_data_start:
        data_buf_offset = min_data_start

    # ── Data rows ────────────────────────────────────────────────────────────
    available = len(raw) - data_buf_offset
    record_count = min(record_count, available // buf_len)
    data_bytes = raw[data_buf_offset: data_buf_offset + record_count * buf_len]

    # Shape: (record_count, buf_len) — each row is one tick of all variables
    data_arr = np.frombuffer(data_bytes, dtype=np.uint8).reshape(record_count, buf_len)

    channels = {}
    for vh in var_headers:
        tc = vh['type']
        if tc not in VAR_DTYPES:
            continue
        dt   = np.dtype(VAR_DTYPES[tc])
        cnt  = vh['count']
        off  = vh['offset']
        size = dt.itemsize * cnt

        if off + size > buf_len:
            continue  # corrupt header entry

        # Slice the column bytes out of every row, reinterpret as the target dtype
        col = data_arr[:, off: off + size].copy()   # C-contiguous copy
        arr = np.frombuffer(col.tobytes(), dtype=dt)

        channels[vh['name']] = arr.reshape(record_count, cnt) if cnt > 1 else arr

    return channels, session_info, tick_rate, record_count


def parse_ibt_bytes(data):
    """
    Parse an iRacing .ibt telemetry file from raw bytes.
    Same as parse_ibt() but accepts bytes instead of a filepath.
    """
    raw = bytes(data)

    if len(raw) < HEADER_SIZE + DISK_SUB_HDR_SIZE:
        raise ValueError("File is too small to be a valid .ibt file.")

    # ── Main header (112 bytes) ──────────────────────────────────────────────
    # 10 ints (40 bytes) + pad[2] (8 bytes) = 48 bytes
    # followed by 4 × irsdk_varBuf (each 16 bytes) = 64 bytes  → total 112
    (ver, status, tick_rate,
     session_info_update, session_info_len, session_info_offset,
     num_vars, var_header_offset,
     num_buf, buf_len, _pad0, _pad1) = struct.unpack_from('<12i', raw, 0)

    # varBuf[0]: (tickCount=4, bufOffset=4, pad=8) at byte 48
    data_buf_offset = struct.unpack_from('<4i', raw, 48)[1]

    # ── Disk sub-header (32 bytes at 112) ────────────────────────────────────
    # time_t(8) + double(8) + double(8) + int(4) + int(4)
    _start_date, _start_time, _end_time, lap_count, record_count = \
        struct.unpack_from('<qddii', raw, HEADER_SIZE)

    # ── Session info YAML ────────────────────────────────────────────────────
    session_info = (
        raw[session_info_offset: session_info_offset + session_info_len]
        .rstrip(b'\x00')
        .decode('utf-8', errors='replace')
    )

    # ── Variable headers (144 bytes each) ────────────────────────────────────
    var_headers = []
    for i in range(num_vars):
        base = var_header_offset + i * VAR_HEADER_SIZE
        var_type, var_off, count = struct.unpack_from('<3i', raw, base)
        name = raw[base + 16: base + 48].rstrip(b'\x00').decode('utf-8', errors='replace')
        unit = raw[base + 112: base + 144].rstrip(b'\x00').decode('utf-8', errors='replace')
        var_headers.append({
            'type': var_type, 'offset': var_off,
            'count': count, 'name': name, 'unit': unit,
        })

    # ── Safety: data must come after all headers ──────────────────────────────
    min_data_start = var_header_offset + num_vars * VAR_HEADER_SIZE
    if data_buf_offset < min_data_start:
        data_buf_offset = min_data_start

    # ── Data rows ────────────────────────────────────────────────────────────
    available = len(raw) - data_buf_offset
    record_count = min(record_count, available // buf_len)
    data_bytes = raw[data_buf_offset: data_buf_offset + record_count * buf_len]

    # Shape: (record_count, buf_len) — each row is one tick of all variables
    data_arr = np.frombuffer(data_bytes, dtype=np.uint8).reshape(record_count, buf_len)

    channels = {}
    for vh in var_headers:
        tc = vh['type']
        if tc not in VAR_DTYPES:
            continue
        dt   = np.dtype(VAR_DTYPES[tc])
        cnt  = vh['count']
        off  = vh['offset']
        size = dt.itemsize * cnt

        if off + size > buf_len:
            continue  # corrupt header entry

        # Slice the column bytes out of every row, reinterpret as the target dtype
        col = data_arr[:, off: off + size].copy()   # C-contiguous copy
        arr = np.frombuffer(col.tobytes(), dtype=dt)

        channels[vh['name']] = arr.reshape(record_count, cnt) if cnt > 1 else arr

    return channels, session_info, tick_rate, record_count
