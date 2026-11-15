"""Tests on synthetic SEGB files (no real Screen Time data). Run: python3 -m unittest discover -s tests"""
import csv
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from biome_screentime import SEGBError, devices, events, read_segb, sessions  # noqa: E402
from biome_screentime.cli import main  # noqa: E402

T0 = 815_000_000.0  # Cocoa seconds, Nov 2026


def varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def record(app, starting, t, version="1.0"):
    return (varint(2 << 3) + varint(1) + varint(3 << 3) + varint(1 if starting else 0)
            + varint(4 << 3 | 1) + struct.pack("<d", t)
            + varint(6 << 3 | 2) + varint(len(app)) + app.encode()
            + varint(9 << 3 | 2) + varint(len(version)) + version.encode())


def segb_v2(payloads, states=None, size=4096, corrupt=()):
    """Like the real files: preallocated size, trailer newest-first, plus an empty slot (offset 0, state 4)."""
    states = states or [1] * len(payloads)
    body, entries = b"", []
    for i, p in enumerate(payloads):
        while len(body) % 4:
            body += b"\0"
        crc = zlib.crc32(p) & 0xFFFFFFFF
        if i in corrupt:
            crc ^= 0xFFFF
        body += struct.pack("<II", crc, 0) + p
        entries.append(struct.pack("<iid", len(body), states[i], T0))
    entries.append(struct.pack("<iid", 0, 4, T0))       # unused slot
    trailer = b"".join(reversed(entries))
    header = b"SEGB" + struct.pack("<id", len(entries), T0) + b"\0" * 16
    pad = size - len(header) - len(body) - len(trailer)
    return header + body + b"\0" * pad + trailer


def segb_v1(payloads):
    body = b""
    for p in payloads:
        body += struct.pack("<iidd", len(p), 1, T0, T0) + b"\0" * 8 + p
        while len(body) % 8:
            body += b"\0"
    return struct.pack("<i", 56 + len(body)) + b"\0" * 48 + b"SEGB" + body


FOCUS = [  # (app, starting, seconds after T0)
    ("com.example.chat", True, 0), ("com.example.chat", False, 600),
    ("com.example.notes", True, 700),
    ("com.apple.springboard.stand-by", True, 1000),      # ends notes; StandBy itself excluded
    ("com.apple.springboard.stand-by", False, 4000),
    ("com.example.music", True, 4100), ("com.example.music", False, 4400),
]


def write_tree(root, layout="v2"):
    payloads = [record(a, s, T0 + dt) for a, s, dt in FOCUS]
    blob = segb_v2(payloads) if layout == "v2" else segb_v1(payloads)
    for sub in ("local", "remote/1A2B3C4D-0000-4000-8000-000000000000"):
        os.makedirs(os.path.join(root, sub, "tombstone"))
        with open(os.path.join(root, sub, str(int(T0 * 1e6))), "wb") as f:
            f.write(blob)
        with open(os.path.join(root, sub, "tombstone", "1"), "wb") as f:
            f.write(b"not an app event stream")
    return payloads


class SEGBTests(unittest.TestCase):
    def test_v2_and_v1_layouts(self):
        payloads = [record("a.b", True, T0), record("a.b", False, T0 + 5)]
        with tempfile.TemporaryDirectory() as tmp:
            for name, blob in (("v2", segb_v2(payloads)), ("v1", segb_v1(payloads))):
                p = os.path.join(tmp, name)
                with open(p, "wb") as f:
                    f.write(blob)
                self.assertEqual([pl for _, pl in read_segb(p)], payloads, name)

    def test_deleted_records_are_skipped(self):
        payloads = [record("a.b", True, T0), record("a.b", False, T0 + 5)]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f")
            with open(p, "wb") as f:
                f.write(segb_v2(payloads, states=[3, 1]))
            self.assertEqual([pl for _, pl in read_segb(p)], payloads[1:])

    def test_many_crc_failures_raise(self):
        payloads = [record("a.b", True, T0 + i) for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f")
            with open(p, "wb") as f:
                f.write(segb_v2(payloads, corrupt=range(6)))
            with self.assertRaises(SEGBError):
                list(read_segb(p))


class SessionTests(unittest.TestCase):
    def test_devices_events_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            devs = devices(tmp)
            self.assertEqual([d.id for d in devs], ["local", "1A2B3C4D-0000-4000-8000-000000000000"])
            self.assertEqual(devs[1].short_id, "1A2B3C4D")
            evts = list(events(devs[0]))
            self.assertEqual(len(evts), len(FOCUS))
            got = [(s.app, s.seconds) for s in sessions(evts)]
            self.assertEqual(got, [("com.example.chat", 600), ("com.example.notes", 300), ("com.example.music", 300)])

    def test_include_system_keeps_standby(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, layout="v1")
            apps = [s.app for s in sessions(events(devices(tmp)[0]), exclude=())]
            self.assertIn("com.apple.springboard.stand-by", apps)


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_sessions_csv_names_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            state = os.path.join(tmp, "state.json")
            code, out, _ = self.run_cli("--source", tmp, "--name", "1A2B3C4D=phone", "sessions", "--state", state)
            self.assertEqual(code, 0)
            rows = list(csv.DictReader(io.StringIO(out)))
            self.assertEqual(len(rows), 6)
            self.assertEqual({r["device"] for r in rows}, {"mac", "phone"})
            self.assertTrue(rows[0]["start"].endswith("Z"))
            _, out2, _ = self.run_cli("--source", tmp, "sessions", "--state", state)
            self.assertEqual(list(csv.DictReader(io.StringIO(out2))), [])    # nothing new

    def test_json_and_device_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            _, out, _ = self.run_cli("--source", tmp, "sessions", "--format", "json", "--device", "local")
            data = json.loads(out)
            self.assertEqual({r["device_id"] for r in data}, {"local"})
            self.assertEqual(data[0]["app"], "com.example.chat")

    def test_missing_source_is_a_clear_error(self):
        code, _, err = self.run_cli("--source", "/nonexistent/App.InFocus", "devices")
        self.assertEqual(code, 1)
        self.assertIn("Full Disk Access", err)


if __name__ == "__main__":
    unittest.main()
