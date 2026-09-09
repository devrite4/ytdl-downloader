# ytdl-downloader

A polite, resumable YouTube channel downloader with a built-in SQLite inventory. Downloads all videos from a channel in random order with randomised delays between each download — making it virtually indistinguishable from a human casually watching videos.

---

## Features

- **Inventory first** — crawls the full channel and stores all video metadata in a local SQLite database before downloading anything
- **Random order** — videos are downloaded in a shuffled order each run, not sequentially
- **Randomised delays** — 60–180 seconds between each download, so no two requests follow the same pattern
- **Batch cooldowns** — extra 2-minute pause every 4 videos
- **30-minute cooldown** — additional 5-minute break every 30 minutes of active runtime
- **Quiet hours** — no downloads between 9pm and midnight CDT (easily configurable)
- **Resumable** — safe to kill and restart at any time; already-downloaded videos are skipped
- **Auto-retry** — failed videos are retried on the next run
- **Integrity check** — verify all downloaded files are not corrupt using `ffprobe`
- **1080p max, MKV format** — downloads the best available stream up to 1080p and wraps it in MKV without re-encoding, preserving original quality at smaller file sizes than MP4/H.264
- **No Shorts** — automatically targets the `/videos` tab and filters out videos under 60 seconds

---

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) — fast Python package manager
- [ffmpeg](https://ffmpeg.org/) — required for merging video and audio streams

### Install ffmpeg

```bash
# Ubuntu / Debian / WSL
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Setup

```bash
git clone https://github.com/your-username/ytdl-downloader
cd ytdl-downloader
uv sync
```

That's it. `uv` handles the Python environment and installs `yt-dlp` automatically.

---

## Usage

### Step 1 — Crawl the channel

Fetches all video metadata and writes it to a local SQLite inventory. No downloading yet.

```bash
uv run download_youtube_channel.py crawl https://www.youtube.com/@SomeChannel
```

### Step 2 — Check the inventory

```bash
uv run download_youtube_channel.py status
```

Output:
```
Inventory: yt_inventory.db
Status          Count
----------------------
pending           285
----------------------
TOTAL             285
```

### Step 3 — Start downloading

```bash
uv run download_youtube_channel.py download
```

Run this in a `tmux` session or with `nohup` so it survives terminal disconnects:

```bash
nohup uv run download_youtube_channel.py download > download.log 2>&1 &
tail -f download.log
```

### Download a specific video by ID

```bash
uv run download_youtube_channel.py download-one dQw4w9WgXcQ
```

### Test with a single video before running the full queue

```bash
uv run download_youtube_channel.py download --limit=1
```

### Run an integrity check on all downloaded files

```bash
uv run download_youtube_channel.py integrity
```

---

## How the delays work

Random delays are the core of why this tool works without getting flagged. Here is the full rhythm:

| Trigger | Delay |
|---|---|
| After every video | 60–180 seconds (random each time) |
| After every 4 videos | +2 minutes (batch cooldown) |
| Every 30 minutes of runtime | +5 minutes (cooling pause) |
| 9pm–midnight CDT | Full stop — resumes at midnight |

The combination of randomised wait times, shuffled download order, and multi-level cooldowns means traffic patterns look organic rather than automated. In testing across 285 videos downloaded over ~18 hours, zero rate-limit errors or blocks were encountered.

---

## Output

Videos are saved to `./youtube_downloads/` by default with this naming pattern:

```
YYYY-MM-DD - Video Title [videoId].mkv
```

The SQLite database (`yt_inventory.db`) tracks the status of every video:

| Status | Meaning |
|---|---|
| `pending` | Not yet downloaded |
| `done` | Successfully downloaded |
| `failed` | Failed — will be retried on next run |

You can query it directly:

```bash
sqlite3 yt_inventory.db "SELECT title, status FROM videos WHERE status='failed';"
```

---

## Configuration

All settings are at the top of `download_youtube_channel.py`:

```python
DELAY_MIN    = 60     # min seconds between downloads
DELAY_MAX    = 180    # max seconds between downloads
BATCH_SIZE   = 4      # extra pause every N videos
BATCH_PAUSE  = 120    # seconds for batch pause
HOURLY_EVERY = 30     # minutes between cooling pauses
HOURLY_PAUSE = 300    # seconds for cooling pause (5 min)
QUIET_START  = 21     # quiet hours start (9pm, 24h format)
```

---

## Why MKV over MP4?

YouTube's primary stream format is VP9, which is 30–40% more efficient than H.264 (MP4). Forcing MP4 output requires re-encoding VP9 → H.264, which wastes time and slightly degrades quality. MKV wraps the original VP9 stream as-is — same 1080p clarity, smaller file, no re-encoding. MKV plays natively on VLC and any modern media player.

---

## Disclaimer

This tool is intended for personal archival of content you have the right to download. Users are responsible for complying with YouTube's Terms of Service and applicable copyright law. The author does not condone downloading content without permission from the rights holder.

---

## License

This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this software, either in source code form or as a compiled binary, for any purpose, commercial or non-commercial, and by any means.

In jurisdictions that recognize copyright laws, the author or authors of this software dedicate any and all copyright interest in the software to the public domain.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to [https://unlicense.org](https://unlicense.org)
