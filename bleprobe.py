#!/usr/bin/env python3
"""Windows BLE bridge for the ZKC42V e-paper tag (port of mac/BLEProbe.swift).

Uses `bleak` (WinRT backend). The tag is addressed by its BLE MAC address
(XX:XX:XX:XX:XX:XX) as printed by `scan`, not a CoreBluetooth UUID.

Commands:
  bleprobe.py scan [seconds]              Discover nearby BLE devices
  bleprobe.py send <ADDR> <frame.bin>     Flash a frame to the tag
  bleprobe.py partial <ADDR> <old.bin> <new.bin>  Windowed experimental update
  bleprobe.py seq <ADDR> <listen> <c:hex>...  Sequential writes with notify listen
  bleprobe.py inspect <ADDR>              Dump GATT profile
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
from pathlib import Path

from epaper_partial import Rect, diff_rects, extract_plane_rect, rects_area

CMD_CHAR = "62750002-D828-918D-FB46-B6C11C675AEC"
RAW_CHUNK = 233
BleStep = tuple[bytes, bool, float, str]


def rle_compress(data: bytes) -> bytes:
    """Port of BLEProbe.swift rleCompress (matches epdiy.cn rle.js)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        run_len = 1
        while i + run_len < n and run_len < 130 and data[i + run_len] == data[i]:
            run_len += 1
        if run_len >= 3:
            out.append(0x80 | (run_len - 3))
            out.append(data[i])
            i += run_len
        else:
            start = i
            length = 0
            while i < n and length < 127:
                if i + 2 < n and data[i] == data[i + 1] and data[i] == data[i + 2]:
                    break
                length += 1
                i += 1
            if length == 0:
                out.append(0x00)
                out.append(data[i])
                i += 1
            else:
                out.append(length - 1)
                out.extend(data[start : start + length])
    return bytes(out)


def rle_chunks(plane: bytes, plane_flag: int, chunk: int = 233) -> list[bytes]:
    """Split an RLE plane at code boundaries; prefix each chunk with WRITE_IMG(0x30, flag)."""
    chunks: list[bytes] = []
    i = 0
    start = 0
    while i < len(plane):
        control = plane[i]
        code_len = 2 if (control & 0x80) else (int(control) + 2)
        if i - start + code_len > chunk and i > start:
            chunks.append(plane[start:i])
            start = i
        i += code_len
    if i > start:
        chunks.append(plane[start:i])
    out: list[bytes] = []
    for idx, c in enumerate(chunks):
        flag = 0x04 | plane_flag | (0x02 if idx == 0 else 0x00)
        out.append(b"\x30" + bytes([flag]) + c)
    return out


def raw_command(command: int) -> bytes:
    """EPD-nRF5 SEND_COMMAND envelope (raw SSD16xx command)."""
    return bytes((0x03, command))


def raw_data(data: bytes) -> bytes:
    """EPD-nRF5 SEND_DATA envelope (raw SSD16xx data)."""
    if not data:
        raise ValueError("raw data payload must not be empty")
    return bytes((0x04,)) + data


def _u16le(value: int) -> bytes:
    if not 0 <= value <= 0x1FF:
        raise ValueError(f"SSD1619 Y coordinate out of range: {value}")
    return bytes((value & 0xFF, (value >> 8) & 0x01))


def _window_steps(rect, label: str) -> list[BleStep]:
    """Build SSD1619 window and counter setup for one plane write."""
    x0 = rect.x_byte
    x1 = x0 + rect.byte_width - 1
    y0 = rect.y
    y1 = rect.y + rect.h - 1
    return [
        (raw_command(0x11), True, 0.03, f"{label} DATA_ENTRY"),
        (raw_data(b"\x03"), False, 0.03, ""),
        (raw_command(0x44), True, 0.03, f"{label} X_WINDOW {x0}..{x1}"),
        (raw_data(bytes((x0, x1))), False, 0.03, ""),
        (raw_command(0x45), True, 0.03, f"{label} Y_WINDOW {y0}..{y1}"),
        (raw_data(_u16le(y0) + _u16le(y1)), False, 0.03, ""),
        (raw_command(0x4E), True, 0.03, f"{label} X_COUNTER {x0}"),
        (raw_data(bytes((x0,))), False, 0.03, ""),
        (raw_command(0x4F), True, 0.03, f"{label} Y_COUNTER {y0}"),
        (raw_data(_u16le(y0)), False, 0.03, ""),
    ]


def _plane_data_steps(data: bytes, label: str, chunk: int = RAW_CHUNK) -> list[BleStep]:
    if chunk <= 0:
        raise ValueError("raw data chunk must be positive")
    steps: list[BleStep] = []
    for index in range(0, len(data), chunk):
        part = data[index : index + chunk]
        steps.append((raw_data(part), False, 0.03, f"{label} DATA {len(part)}B"))
    return steps


def build_partial_steps(
    old_frame: tuple[int, int, bytes, bytes],
    new_frame: tuple[int, int, bytes, bytes],
    *,
    init_param: int = 0x02,
    chunk: int = RAW_CHUNK,
    partial_ctrl2: int = 0xFF,
    merge_gap_bytes: int = 0,
) -> tuple[list[Rect], list[BleStep]]:
    """Build a raw SSD1619 partial-refresh plan without touching BLE.

    The GR5513 firmware must expose EPD-nRF5 SEND_COMMAND (0x03) and
    SEND_DATA (0x04).  ``partial_ctrl2=0xFF`` is the SSD16xx Gen2 partial
    candidate; it is intentionally an explicit parameter because the tag's
    panel/LUT still needs hardware validation.
    """
    old_w, old_h, old_black, old_red = old_frame
    new_w, new_h, new_black, new_red = new_frame
    if (old_w, old_h) != (new_w, new_h):
        raise ValueError("old and new frame dimensions differ")
    if not 0 <= partial_ctrl2 <= 0xFF:
        raise ValueError("partial_ctrl2 must fit in one byte")
    rects = diff_rects(
        new_w,
        new_h,
        old_black,
        old_red,
        new_black,
        new_red,
        merge_gap_bytes=merge_gap_bytes,
    )
    if not rects:
        return rects, []

    steps: list[BleStep] = [
        (bytes((0x01, init_param)), True, 0.30, f"INIT model=0x{init_param:02x}"),
        (bytes((0x31, 0x00, 0x00)), True, 0.10, "SET_SLOT slot=0"),
    ]
    for index, rect in enumerate(rects):
        prefix = f"RECT {index + 1}/{len(rects)} {rect.x},{rect.y} {rect.w}x{rect.h}"
        for plane_name, plane_command, plane in (
            ("BW", 0x24, new_black),
            ("RED", 0x26, new_red),
        ):
            label = f"{prefix} {plane_name}"
            steps.extend(_window_steps(rect, label))
            steps.append((raw_command(plane_command), True, 0.03, f"{label} RAM"))
            steps.extend(
                _plane_data_steps(
                    extract_plane_rect(plane, new_w, new_h, rect),
                    label,
                    chunk,
                )
            )

    steps.extend(
        [
            (raw_command(0x21), True, 0.03, "UPDATE_CTRL1"),
            (raw_data(b"\x80\x00"), False, 0.03, ""),
            (raw_command(0x22), True, 0.03, f"UPDATE_CTRL2 0x{partial_ctrl2:02x}"),
            (raw_data(bytes((partial_ctrl2,))), False, 0.03, ""),
            (raw_command(0x20), True, 1.00, "MASTER_ACTIVATE"),
        ]
    )
    return rects, steps


def read_frame(path: str) -> tuple[int, int, bytes, bytes]:
    """Parse ZKEPD1 frame: magic, <w LE, h LE, black plane, red plane."""
    data = Path(path).read_bytes()
    if len(data) <= 10 or data[:7] != b"ZKEPD1\n":
        raise ValueError(f"bad frame file: {path}")
    w, h = struct.unpack("<HH", data[7:11])
    plane = (w * h) // 8
    if len(data) != 11 + plane * 2:
        raise ValueError(f"frame size mismatch: {len(data)} != 11 + {plane}*2")
    return w, h, data[11 : 11 + plane], data[11 + plane : 11 + plane * 2]


def hex_bytes(s: str) -> bytes:
    h = s.replace(" ", "")
    if len(h) % 2 != 0 or not h:
        raise ValueError(f"bad hex: {s!r}")
    return bytes.fromhex(h)


def find_char(services, uuid: str):
    for svc in services:
        for ch in svc.characteristics:
            if ch.uuid.lower() == uuid.lower():
                return ch
    return None


async def cmd_scan(seconds: int) -> int:
    from bleak import BleakScanner

    devices: dict[str, tuple] = {}

    def cb(device, adv):
        devices[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    print(f"=== {len(devices)} unique devices ===")
    rows = []
    for addr, (dev, adv) in devices.items():
        rssi = getattr(adv, "rssi", None) if adv is not None else None
        if rssi is None:
            rssi = getattr(dev, "rssi", None)
        name = ""
        if dev is not None:
            name = dev.name or ""
        if adv is not None and not name:
            name = getattr(adv, "local_name", "") or ""
        mfr = getattr(adv, "manufacturer_data", None) if adv is not None else None
        mfr_hex = "".join(b.hex() for b in mfr.values()) if mfr else ""
        uuids = [str(u) for u in (getattr(adv, "service_uuids", []) or [])] if adv is not None else []
        rows.append((rssi if rssi is not None else -999, name, addr, uuids, mfr_hex))
    for rssi, name, addr, uuids, mfr_hex in sorted(rows, key=lambda r: r[0], reverse=True):
        svc = ",".join(uuids) if uuids else "-"
        print(f"{rssi:4d} dBm | {name} | {addr} | {svc}")
        if mfr_hex:
            print(f"        mfr={mfr_hex}")
    return 0


async def cmd_inspect(addr: str) -> int:
    from bleak import BleakClient

    async with BleakClient(addr, timeout=30) as client:
        print(f"connected: {addr}")
        for svc in client.services:
            print(f"service {svc.uuid}")
            for ch in svc.characteristics:
                props = ch.properties
                extra = ""
                if "read" in props:
                    try:
                        val = await client.read_gatt_char(ch)
                        asc = "".join(
                            chr(b) if 32 <= b < 127 else "."
                            for b in val
                        )
                        extra = f" value={val.hex()} '{asc}'"
                    except Exception:
                        pass
                print(f"  char {ch.uuid} [{','.join(props)}]{extra}")
    return 0


async def _run_ble_steps(addr: str, steps: list[BleStep], *, hold_seconds: int = 20) -> int:
    """Execute a prepared command sequence with the existing retry policy."""
    from bleak import BleakClient, BleakScanner

    print(f"total {len(steps)} steps, starting ...")
    for i, (data, resp, _, label) in enumerate(steps):
        if label:
            print(f"+ {label}")
        if i < 3 or i == len(steps) - 1 or i % 50 == 0:
            print(f"  step {i + 1}/{len(steps)} ({len(data)}B, {'resp' if resp else 'nresp'})")

    # Warm Windows' BLE device cache before connecting (direct connect is flaky).
    try:
        await BleakScanner().discover(timeout=4)
    except Exception:
        pass

    last_err = None
    for attempt in range(1, 4):
        try:
            async with BleakClient(addr, timeout=30) as client:
                ch = find_char(client.services, CMD_CHAR)
                if ch is None:
                    print("command characteristic not found", file=sys.stderr)
                    return 1
                for svc in client.services:
                    for c in svc.characteristics:
                        if "notify" in c.properties or "indicate" in c.properties:
                            try:
                                await client.start_notify(c)
                            except Exception:
                                pass
                await asyncio.sleep(1.0)
                print(f"connected (attempt {attempt}), sending ...")
                t0 = time.time()
                for i, (data, resp, delay, label) in enumerate(steps):
                    await client.write_gatt_char(ch, data, response=resp)
                    if i in (0, 1, 2, len(steps) - 1) or i % 50 == 0:
                        suffix = f" {label}" if label else ""
                        print(f"[+{time.time() - t0:.0f}s] step {i + 1}/{len(steps)}{suffix}")
                    await asyncio.sleep(delay)
                print(f"all writes done, holding BLE {hold_seconds}s while EPD refreshes ...")
                await asyncio.sleep(hold_seconds)
                return 0
        except Exception as e:
            last_err = e
            print(f"attempt {attempt} failed: {e}", file=sys.stderr)
            await asyncio.sleep(3)
    print(f"send failed after retries: {last_err}", file=sys.stderr)
    return 1


async def cmd_send(addr: str, frame_path: str, init_param: int = 0x02) -> int:
    w, h, black, red = read_frame(frame_path)
    black_rle = rle_compress(black)
    red_rle = rle_compress(red)
    chunks = rle_chunks(black_rle, 0) + rle_chunks(red_rle, 1)
    print(f"frame: {w}x{h}, black {len(black)}B -> RLE {len(black_rle)}B, red {len(red)}B -> RLE {len(red_rle)}B")

    steps: list[BleStep] = []
    steps.append((bytes([0x01, init_param]), True, 0.30, f"INIT model=0x{init_param:02x}"))
    steps.append((bytes([0x31, 0x00, 0x00]), True, 0.10, "SET_SLOT slot=0"))
    for i, c in enumerate(chunks):
        last = i == len(chunks) - 1
        steps.append((c, False, 0.50 if last else 0.05, "WRITE_IMG" if i == 0 else ""))
    steps.append((bytes([0x05]), True, 1.0, "REFRESH"))
    return await _run_ble_steps(addr, steps)


async def cmd_partial(
    addr: str,
    old_frame_path: str,
    new_frame_path: str,
    *,
    init_param: int = 0x02,
    allow_red: bool = False,
    max_area: float = 0.35,
    max_rects: int = 32,
    partial_ctrl2: int = 0xFF,
) -> int:
    """Send an experimental SSD1619 windowed update, with full fallback."""
    new_frame = read_frame(new_frame_path)
    old_path = Path(old_frame_path)
    if not old_path.is_file():
        print("previous frame missing; falling back to full refresh")
        return await cmd_send(addr, new_frame_path, init_param=init_param)

    try:
        old_frame = read_frame(old_frame_path)
        rects, steps = build_partial_steps(
            old_frame,
            new_frame,
            init_param=init_param,
            partial_ctrl2=partial_ctrl2,
        )
    except ValueError as exc:
        print(f"partial plan invalid ({exc}); falling back to full refresh")
        return await cmd_send(addr, new_frame_path, init_param=init_param)

    if not rects:
        print("frame unchanged; skipped BLE push")
        return 0

    red_changed = old_frame[3] != new_frame[3]
    area_ratio = rects_area(rects) / (new_frame[0] * new_frame[1])
    if red_changed and not allow_red:
        print("red plane changed; conservative full-refresh fallback (use --allow-red to test partial BWR)")
        return await cmd_send(addr, new_frame_path, init_param=init_param)
    if area_ratio > max_area or len(rects) > max_rects:
        print(
            f"dirty area {area_ratio:.1%} / {len(rects)} rects exceeds partial limits; "
            "falling back to full refresh"
        )
        return await cmd_send(addr, new_frame_path, init_param=init_param)

    print(
        f"partial frame: {new_frame[0]}x{new_frame[1]}, rects={len(rects)}, "
        f"dirty area={area_ratio:.1%}, red={'yes' if red_changed else 'no'}"
    )
    rc = await _run_ble_steps(addr, steps)
    if rc != 0:
        print("partial BLE sequence failed; retrying known-good full refresh", file=sys.stderr)
        return await cmd_send(addr, new_frame_path, init_param=init_param)
    return rc


async def cmd_seq(addr: str, listen: int, pairs: list[tuple[str, bytes]]) -> int:
    from bleak import BleakClient

    async with BleakClient(addr, timeout=30) as client:
        for cid, data in pairs:
            ch = find_char(client.services, cid)
            if ch is None:
                print(f"char {cid} not found, skipping", file=sys.stderr)
                continue
            resp = bool(getattr(ch, "write", None))
            print(f"WRITE -> {cid}: {data.hex()}")
            await client.write_gatt_char(ch, data, response=resp)
            await asyncio.sleep(1.5)
        print(f"listening {listen}s ...")
        await asyncio.sleep(listen)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "scan":
        seconds = int(args[1]) if len(args) > 1 else 12
        return asyncio.run(cmd_scan(seconds))
    if cmd == "send":
        if len(args) < 3:
            print("usage: bleprobe.py send <ADDR> <frame.bin> [--initparam <hex>]")
            return 2
        init_param = 0x02
        i = 3
        while i < len(args):
            if args[i] == "--initparam" and i + 1 < len(args):
                init_param = hex_bytes(args[i + 1])[0]
                i += 2
            else:
                i += 1
        return asyncio.run(cmd_send(args[1], args[2], init_param=init_param))
    if cmd == "partial":
        if len(args) < 4:
            print(
                "usage: bleprobe.py partial <ADDR> <old-frame.bin> <new-frame.bin> "
                "[--allow-red] [--initparam <hex>] [--max-area <ratio>] "
                "[--max-rects <n>] [--partial-ctrl2 <hex>]"
            )
            return 2
        init_param = 0x02
        allow_red = False
        max_area = 0.35
        max_rects = 32
        partial_ctrl2 = 0xFF
        i = 4
        try:
            while i < len(args):
                if args[i] == "--allow-red":
                    allow_red = True
                    i += 1
                elif args[i] == "--initparam" and i + 1 < len(args):
                    init_param = hex_bytes(args[i + 1])[0]
                    i += 2
                elif args[i] == "--max-area" and i + 1 < len(args):
                    max_area = float(args[i + 1])
                    i += 2
                elif args[i] == "--max-rects" and i + 1 < len(args):
                    max_rects = int(args[i + 1])
                    i += 2
                elif args[i] == "--partial-ctrl2" and i + 1 < len(args):
                    partial_ctrl2 = hex_bytes(args[i + 1])[0]
                    i += 2
                else:
                    print(f"unknown or incomplete option {args[i]}", file=sys.stderr)
                    return 2
        except (ValueError, IndexError) as exc:
            print(f"bad partial option: {exc}", file=sys.stderr)
            return 2
        if not 0 < max_area <= 1 or max_rects <= 0:
            print("--max-area must be in (0,1] and --max-rects must be positive", file=sys.stderr)
            return 2
        return asyncio.run(
            cmd_partial(
                args[1],
                args[2],
                args[3],
                init_param=init_param,
                allow_red=allow_red,
                max_area=max_area,
                max_rects=max_rects,
                partial_ctrl2=partial_ctrl2,
            )
        )
    if cmd == "seq":
        if len(args) < 4:
            print("usage: bleprobe.py seq <ADDR> <listen> <char:hex> ...")
            return 2
        listen = int(args[2])
        pairs = []
        for pair in args[3:]:
            cid, _, hexs = pair.partition(":")
            pairs.append((cid, hex_bytes(hexs)))
        return asyncio.run(cmd_seq(args[1], listen, pairs))
    if cmd == "inspect":
        if len(args) < 2:
            print("usage: bleprobe.py inspect <ADDR>")
            return 2
        return asyncio.run(cmd_inspect(args[1]))
    print(f"unknown command {cmd!r}", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
