"""biome-screentime: export Apple Screen Time (Biome App.InFocus) as app sessions or events."""
import argparse
import collections
import csv
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional

from . import __version__
from .infocus import (DEFAULT_SOURCE, SYSTEM_APPS, Device, Session, devices, events, records, sessions,
                      to_datetime)
from .protobuf import text


def _parse_time(s: str) -> float:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.astimezone()  # naive = local time
    return (t - dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)).total_seconds()


def _fmt(cocoa: float, tz: str) -> str:
    t = to_datetime(cocoa)
    if tz == "local":
        return t.astimezone().isoformat(timespec="seconds")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _names(pairs: List[str]) -> Dict[str, str]:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        if not v:
            raise SystemExit(f"--name expects ID=NAME, got {p!r}")
        out[k.strip().upper()] = v.strip()
    return out


def _label(d: Device, names: Dict[str, str]) -> str:
    return names.get(d.short_id, names.get(d.id.upper(), "mac" if d.id == "local" else "remote-" + d.short_id))


def _select(all_devices: List[Device], wanted: List[str], names: Dict[str, str]) -> List[Device]:
    if not wanted:
        return all_devices
    w = {x.upper() for x in wanted}
    return [d for d in all_devices if {d.id.upper(), d.short_id, _label(d, names).upper()} & w]


def _writer(fmt: str, out, columns: List[str]):
    if fmt == "csv":
        w = csv.writer(out)
        w.writerow(columns)
        return lambda row: w.writerow(row), lambda: None
    if fmt == "jsonl":
        return lambda row: out.write(json.dumps(dict(zip(columns, row))) + "\n"), lambda: None
    rows = []
    return lambda row: rows.append(dict(zip(columns, row))), lambda: json.dump(rows, out, indent=1)


def cmd_devices(a) -> int:
    names = _names(a.name)
    for d in devices(a.source):
        ss = sessions(events(d))
        if not ss:
            print(f"{_label(d, names):<16} {d.short_id:<9} no app sessions")
            continue
        top = collections.Counter()
        for s in ss:
            top[s.app] += s.seconds
        span = f"{_fmt(ss[0].start, a.tz)[:10]} … {_fmt(ss[-1].end, a.tz)[:16]}"
        print(f"{_label(d, names):<16} {d.short_id:<9} {len(ss):>6} sessions  {span}")
        for app, secs in top.most_common(a.top):
            print(f"{'':<27}{secs / 3600:7.1f} h  {app}")
    return 0


def cmd_sessions(a) -> int:
    names = _names(a.name)
    state: Dict[str, float] = {}
    if a.state and os.path.exists(a.state):
        with open(a.state) as f:
            state = json.load(f)
    since = _parse_time(a.since) if a.since else None
    until = _parse_time(a.until) if a.until else None
    exclude = () if a.include_system else tuple(SYSTEM_APPS) + tuple(a.exclude)
    rows: List[Session] = []
    new_state = dict(state)
    for d in _select(devices(a.source), a.device, names):
        for s in sessions(events(d), exclude=exclude):
            if since is not None and s.start < since or until is not None and s.end > until:
                continue
            if s.start < state.get(d.id, float("-inf")):
                continue
            rows.append(s)
            new_state[d.id] = max(new_state.get(d.id, float("-inf")), s.end)
    rows.sort(key=lambda s: (s.start, s.device))
    labels = {d.id: _label(d, names) for d in devices(a.source)}
    out = open(a.output, "w", newline="") if a.output else sys.stdout
    columns = ["start", "end", "seconds", "app", "device", "device_id"]
    write, finish = _writer(a.format, out, columns)
    for s in rows:
        write([_fmt(s.start, a.tz), _fmt(s.end, a.tz), round(s.seconds, 3), s.app, labels.get(s.device, s.device), s.device])
    finish()
    if a.output:
        out.close()
    if a.state:
        with open(a.state, "w") as f:
            json.dump(new_state, f)
    print(f"{len(rows)} sessions", file=sys.stderr)
    return 0


def cmd_events(a) -> int:
    names = _names(a.name)
    out = open(a.output, "w", newline="") if a.output else sys.stdout
    write, finish = _writer(a.format, out, ["time", "app", "starting", "version", "device", "device_id"])
    for d in _select(devices(a.source), a.device, names):
        for e in sorted(events(d), key=lambda e: e.time):
            write([_fmt(e.time, a.tz), e.app, e.starting, e.version, _label(d, names), d.id])
    finish()
    if a.output:
        out.close()
    return 0


def cmd_inspect(a) -> int:
    names = _names(a.name)
    for d in _select(devices(a.source), a.device, names):
        recs = list(records(d))
        print(f"== {_label(d, names)} ({d.id}): {len(recs)} records")
        for created, rec in recs[-a.limit:]:
            parts = []
            for field in sorted(rec):
                for wire, v in rec[field]:
                    if wire == 2:
                        v = text(v) or f"<{len(v)} bytes>"
                    elif wire == 1 and 4e8 < v < 1.3e9:
                        v = _fmt(v, a.tz)
                    parts.append(f"{field}={v}")
            print("  ", _fmt(created, a.tz) if created else "-", " ".join(parts))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="biome-screentime", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"App.InFocus stream folder or a copy of it (default: {DEFAULT_SOURCE})")
    p.add_argument("--tz", choices=["utc", "local"], default="utc", help="timezone of output timestamps (default: utc)")
    p.add_argument("--name", action="append", default=[], metavar="ID=NAME",
                   help="name a device by its short id (from `devices`), e.g. 1A2B3C4D=iphone; repeatable")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("devices", help="list devices with session counts, date range and top apps")
    d.add_argument("--top", type=int, default=5)
    d.set_defaults(func=cmd_devices)

    for name, func, help_ in [("sessions", cmd_sessions, "app sessions: start, end, seconds, app, device"),
                              ("events", cmd_events, "raw focus events: app gained/lost focus")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("--format", choices=["csv", "jsonl", "json"], default="csv")
        s.add_argument("-o", "--output", help="write to a file instead of stdout")
        s.add_argument("--device", action="append", default=[], metavar="ID_OR_NAME",
                       help="only these devices (id, short id or --name); repeatable")
        s.set_defaults(func=func)
        if name == "sessions":
            s.add_argument("--since", help="ISO time; sessions starting before it are skipped")
            s.add_argument("--until", help="ISO time; sessions ending after it are skipped")
            s.add_argument("--exclude", action="append", default=[], metavar="BUNDLE_ID",
                           help="drop sessions of this app (repeatable); StandBy/lock screen are dropped by default")
            s.add_argument("--include-system", action="store_true", help="keep StandBy, lock screen and screensaver")
            s.add_argument("--state", metavar="FILE",
                           help="incremental mode: only sessions that started after the last run; progress kept in FILE")

    i = sub.add_parser("inspect", help="show the last raw records (decoded protobuf fields) per device")
    i.add_argument("--limit", type=int, default=10)
    i.add_argument("--device", action="append", default=[], metavar="ID_OR_NAME")
    i.set_defaults(func=cmd_inspect)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except (FileNotFoundError, PermissionError) as e:
        print(f"biome-screentime: {e}", file=sys.stderr)
        if isinstance(e, PermissionError) or "Full Disk Access" in str(e):
            print("Give the app you run this from (e.g. Terminal) Full Disk Access, or point --source at a copy.",
                  file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
