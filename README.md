# biome-screentime-exporter

Export Apple **Screen Time** from macOS as plain app **sessions** (CSV, JSON or JSONL), for **this Mac and
every device synced to it**: iPhone, iPad, other Macs.

```
$ biome-screentime --name 1A2B3C4D=iphone sessions --since 2026-01-05
start,end,seconds,app,device,device_id
2026-01-05T08:00:00Z,2026-01-05T08:10:00Z,600.0,com.example.chat,iphone,1A2B3C4D-…
2026-01-05T08:10:00Z,2026-01-05T08:42:30Z,1950.0,com.example.music,iphone,1A2B3C4D-…
```

It reads the same data Screen Time itself uses: Apple's **Biome** `App.InFocus` stream, one record each
time an app gains or loses focus. Point it at the stream (or a copy) and you get sessions you can load
into a database, a spreadsheet, Grafana, ActivityWatch, or your own scripts.

- **No dependencies**: standard library only, works with the Mac's built-in `/usr/bin/python3` (3.9+)
- **Both on-disk formats** of Apple's SEGB files, with CRC checks so a format change fails loudly
  instead of producing wrong numbers
- **Synced devices**: iPhone/iPad usage arrives through iCloud when *Share Across Devices* is on
- **Incremental mode** for scheduled exports (only new sessions each run)
- **Offline**: reads local files, never touches the network

## Requirements

- macOS with Screen Time on. For iPhone/iPad data, turn on **Settings → Screen Time → Share Across
  Devices** on each device.
- **Full Disk Access** for whatever runs it, because the stream lives in a protected folder
  (`~/Library/Biome/streams/restricted/App.InFocus`). Add Terminal (or your app) under **System Settings →
  Privacy & Security → Full Disk Access**, or copy the folder somewhere readable and use `--source`.

## Install

```bash
uv tool install git+https://github.com/SonyCore/biome-screentime-exporter
# or
pipx install git+https://github.com/SonyCore/biome-screentime-exporter
```

Or run it from a clone without installing:

```bash
git clone https://github.com/SonyCore/biome-screentime-exporter
cd biome-screentime-exporter
PYTHONPATH=src python3 -m biome_screentime devices
```

## Usage

```bash
biome-screentime devices                       # which devices are there, how much data, top apps
biome-screentime sessions > sessions.csv       # every session, CSV
biome-screentime sessions --format jsonl --since 2026-11-01 --until 2026-11-08
biome-screentime --name 1A2B3C4D=iphone sessions --device iphone --tz local
biome-screentime sessions --state ~/.screentime-state.json -o new.csv   # only what's new since last run
biome-screentime events --format json          # raw focus events (gained/lost focus)
biome-screentime inspect --limit 5             # raw decoded records, for debugging
```

| Option | |
|---|---|
| `--source DIR` | the stream folder, or a copy of it (default `~/Library/Biome/streams/restricted/App.InFocus`) |
| `--name ID=NAME` | name a device by the short id that `devices` prints (repeatable). This Mac is `mac` |
| `--tz utc\|local` | timestamps in UTC (`…Z`, default) or your local offset |
| `sessions --device X` | only these devices (id, short id or name); repeatable |
| `sessions --exclude BUNDLE_ID` | drop an app; StandBy, lock screen and screensaver are dropped by default |
| `sessions --include-system` | keep StandBy / lock screen / screensaver |
| `sessions --state FILE` | incremental: skip sessions already exported, remember progress in FILE |
| `--format csv\|jsonl\|json`, `-o FILE` | output format and destination |

### Session columns

| column | |
|---|---|
| `start`, `end` | ISO 8601 |
| `seconds` | duration |
| `app` | bundle id (`com.openai.chat`). Most have a dot, a few don't (imo is `imoimiphone`) |
| `device` | `mac`, your `--name`, or `remote-<short id>` |
| `device_id` | `local` or the device UUID |

### As a library

```python
from biome_screentime import devices, events, sessions, to_datetime

for device in devices("~/Library/Biome/streams/restricted/App.InFocus"):
    for s in sessions(events(device)):
        print(device.short_id, s.app, to_datetime(s.start), round(s.seconds))
```

## How it works

```
App.InFocus/
  local/<files>                       this Mac
  remote/<DEVICE-UUID>/<files>        each synced device
  …/tombstone/                        deletion markers (skipped)
```

Each file is a **SEGB** container, named after the time it was created:

- **v2** (current): `SEGB` header, records from byte 32, a trailer at the end of the (preallocated) file
  with one entry per record: end offset, state (1 live, 3 deleted, 4 unused) and a timestamp. Every
  record starts with a CRC32 of its payload.
- **v1** (older): `SEGB` at byte 52, then length-prefixed records, 8-byte aligned.

Each live record is a protobuf message:

| field | meaning |
|---|---|
| 3 | `1` app came into focus, `0` it left |
| 4 | event time (seconds since 2001-01-01 UTC) |
| 6 | app bundle id |
| 9 | app version |

Sessions are built by pairing focus-in/focus-out per device. A session ends when its app loses focus or
another app gains it. Sessions under 1 s or over 4 h (a screen left on) are dropped.

## Accuracy

Checked against Apple's own Screen Time screen over a full week of iPhone use: per-app weekly totals
matched to **within 3 minutes**.

## Caveats

- **iPhone/iPad data is only as fresh as iCloud sync.** Events reach the Mac in batches, sometimes hours
  late. When they arrive they keep their original times, so nothing is lost, but recent hours can be
  empty until the next sync. System Settings may show fresher numbers, because it reads a separate
  Screen Time database. That database sits in a protected macOS data vault that even Full Disk Access
  can't open.
- **Mac time includes idle time while an app is in front.** macOS records focus until the screen locks
  or sleeps.
- **Copying the stream?** Biome writes into its preallocated files without changing their modification
  time, so `rsync -a` will skip changed files. Use `rsync -a --checksum`, or backdate your copies before
  syncing.
- Field numbers come from observing real data (macOS/iOS 17–27). If Apple changes the format, `inspect`
  shows the raw records.

## Related

- ActivityWatch importers push the same stream into ActivityWatch buckets. This tool is a plain
  exporter, so the sessions can go anywhere.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests build synthetic SEGB files (both layouts, deleted records, unused slots, CRC errors), so no
real Screen Time data is needed or included.

## License

MIT, see [LICENSE](LICENSE).
