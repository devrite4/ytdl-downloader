#!/usr/bin/env python3
"""
Download all videos from a YouTube channel.
Phase 1: Crawl the channel and build a SQLite inventory.
Phase 2: Download pending videos in random order with safe delays.

Resumable — safe to kill and restart at any time.

Usage:
    python download_youtube_channel.py crawl       <channel_url> [db_path]
    python download_youtube_channel.py download    [--limit=N]   [db_path] [output_dir]
    python download_youtube_channel.py download-one <video_id>   [db_path] [output_dir]
    python download_youtube_channel.py integrity                 [output_dir]
    python download_youtube_channel.py status                    [db_path]

Examples:
    python download_youtube_channel.py crawl https://www.youtube.com/@SomeChannel
    python download_youtube_channel.py download
    python download_youtube_channel.py download --limit=5
    python download_youtube_channel.py download-one dQw4w9WgXcQ
    python download_youtube_channel.py status
"""

import sys
import time
import random
import logging
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_DB      = Path.cwd() / "yt_inventory.db"
DEFAULT_OUT_DIR = Path.cwd() / "youtube_downloads"

FORMAT          = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
OUTPUT_TEMPLATE = "%(upload_date>%Y-%m-%d)s - %(title).50s [%(id)s].%(ext)s"

DELAY_MIN    = 60    # seconds between each download
DELAY_MAX    = 180
BATCH_SIZE   = 4    # extra pause every N downloads
BATCH_PAUSE  = 120  # seconds for batch pause
HOURLY_EVERY = 30   # minutes — extra long pause interval
HOURLY_PAUSE = 300  # seconds (5 min) for the 30-min pause

CDT         = ZoneInfo("America/Chicago")
QUIET_START = 21   # 9pm CDT — no new downloads after this hour
QUIET_END   = 0    # midnight CDT — resume

# ── DB helpers ─────────────────────────────────────────────────────────────────

def open_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id            TEXT PRIMARY KEY,
            title         TEXT,
            upload_date   TEXT,
            duration_sec  INTEGER,
            channel_url   TEXT,
            status        TEXT    DEFAULT 'pending',
            file_path     TEXT,
            error         TEXT,
            crawled_at    TEXT,
            downloaded_at TEXT
        )
    """)
    con.commit()
    return con


def upsert_video(con: sqlite3.Connection, row: dict) -> None:
    con.execute("""
        INSERT INTO videos (id, title, upload_date, duration_sec, channel_url, crawled_at)
        VALUES (:id, :title, :upload_date, :duration_sec, :channel_url, :crawled_at)
        ON CONFLICT(id) DO UPDATE SET
            title        = excluded.title,
            upload_date  = excluded.upload_date,
            duration_sec = excluded.duration_sec,
            crawled_at   = excluded.crawled_at
    """, row)


def mark_done(con: sqlite3.Connection, video_id: str, file_path: str) -> None:
    con.execute("""
        UPDATE videos SET status='done', file_path=?, downloaded_at=? WHERE id=?
    """, (file_path, datetime.now(timezone.utc).isoformat(), video_id))
    con.commit()


def mark_failed(con: sqlite3.Connection, video_id: str, error: str) -> None:
    con.execute("""
        UPDATE videos SET status='failed', error=?, downloaded_at=? WHERE id=?
    """, (error[-500:], datetime.now(timezone.utc).isoformat(), video_id))
    con.commit()


def pending_videos(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT id, title FROM videos WHERE status IN ('pending', 'failed')
        ORDER BY RANDOM()
    """).fetchall()

# ── Quiet hours ────────────────────────────────────────────────────────────────

def wait_if_quiet_hours() -> None:
    now = datetime.now(CDT)
    if now.hour >= QUIET_START:
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (midnight - now).total_seconds()
        logging.info("Quiet hours (9pm–midnight CDT). Sleeping %.0f mins until midnight...", secs / 60)
        time.sleep(secs)

# ── Phase 1: Crawl ─────────────────────────────────────────────────────────────

def videos_url(channel_url: str) -> str:
    url = channel_url.rstrip("/")
    if not url.endswith("/videos"):
        url += "/videos"
    return url


def cmd_crawl(channel_url: str, db_path: Path) -> None:
    crawl_url = videos_url(channel_url)
    logging.info("Crawling %s (Shorts excluded via /videos tab)", crawl_url)
    con = open_db(db_path)

    result = subprocess.run(
        [
            "yt-dlp",
            "--flat-playlist",
            "--match-filter", "duration > 60",
            "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(duration)s",
            "--no-warnings",
            crawl_url,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logging.error("Crawl failed:\n%s", result.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        vid_id      = parts[0]
        title       = parts[1] if len(parts) > 1 else ""
        upload_date = parts[2] if len(parts) > 2 else ""
        duration    = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        upsert_video(con, {
            "id":           vid_id,
            "title":        title,
            "upload_date":  upload_date,
            "duration_sec": duration,
            "channel_url":  channel_url,
            "crawled_at":   now,
        })
        count += 1

    con.commit()
    con.close()
    logging.info("Crawl complete. %d videos written to %s", count, db_path)

# ── Phase 2: Download ──────────────────────────────────────────────────────────

def find_downloaded_file(video_id: str, output_dir: Path) -> str | None:
    for f in output_dir.iterdir():
        if f.is_file() and f"[{video_id}]" in f.name:
            return str(f)
    return None


def download_video(video_id: str, output_dir: Path) -> tuple[bool, str]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    result = subprocess.run(
        [
            "yt-dlp",
            "--format", FORMAT,
            "--merge-output-format", "mkv",
            "--remux-video", "mkv",
            "--output", str(output_dir / OUTPUT_TEMPLATE),
            "--add-metadata",
            "--embed-thumbnail",
            "--no-playlist",
            "--retries", "10",
            "--fragment-retries", "10",
            "--retry-sleep", "5",
            "--trim-filenames", "200",
            "--no-warnings",
            url,
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        path = find_downloaded_file(video_id, output_dir) or ""
        return True, path
    return False, result.stderr


def cmd_download(db_path: Path, output_dir: Path, limit: int | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    con = open_db(db_path)
    queue = pending_videos(con)

    if not queue:
        logging.info("Nothing to download. Run 'crawl' first or everything is done.")
        con.close()
        return

    if limit:
        queue = queue[:limit]

    total = len(queue)
    logging.info("%d videos to download (random order).", total)

    last_hourly_pause = time.monotonic()

    for i, row in enumerate(queue, start=1):
        wait_if_quiet_hours()

        vid_id = row["id"]
        title  = row["title"] or vid_id

        existing = find_downloaded_file(vid_id, output_dir)
        if existing:
            logging.info("[%d/%d] SKIP (file exists) %s", i, total, title)
            mark_done(con, vid_id, existing)
            continue

        logging.info("[%d/%d] Downloading: %s  (%s)", i, total, title, vid_id)
        ok, payload = download_video(vid_id, output_dir)

        if ok:
            mark_done(con, vid_id, payload)
            logging.info("[%d/%d] OK  %s", i, total, vid_id)
        else:
            mark_failed(con, vid_id, payload)
            logging.warning("[%d/%d] FAIL %s\n%s", i, total, vid_id, payload[-300:])

        if i < total:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            logging.info("Waiting %.1fs ...", delay)
            time.sleep(delay)

            if i % BATCH_SIZE == 0:
                logging.info("Batch pause: cooling down for %ds ...", BATCH_PAUSE)
                time.sleep(BATCH_PAUSE)

            elapsed = (time.monotonic() - last_hourly_pause) / 60
            if elapsed >= HOURLY_EVERY:
                logging.info("30-min pause: cooling down for %ds ...", HOURLY_PAUSE)
                time.sleep(HOURLY_PAUSE)
                last_hourly_pause = time.monotonic()

    con.close()
    logging.info("Download run complete.")


def cmd_download_one(video_id: str, db_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    con = open_db(db_path)

    existing = find_downloaded_file(video_id, output_dir)
    if existing:
        logging.info("Already downloaded: %s", existing)
        mark_done(con, video_id, existing)
        con.close()
        return

    logging.info("Downloading single video: %s", video_id)
    ok, payload = download_video(video_id, output_dir)

    if ok:
        mark_done(con, video_id, payload)
        logging.info("OK: %s", payload)
    else:
        mark_failed(con, video_id, payload)
        logging.error("FAILED:\n%s", payload)

    con.close()

# ── Phase 3: Integrity check ──────────────────────────────────────────────────

def cmd_integrity(output_dir: Path) -> None:
    files = sorted(output_dir.glob("*.mkv")) + sorted(output_dir.glob("*.mp4"))
    if not files:
        print("No video files found.")
        return

    print(f"Checking {len(files)} files ...\n")
    ok_count = 0
    bad = []

    for i, f in enumerate(files, start=1):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type,duration",
             "-of", "default=noprint_wrappers=1", str(f)],
            capture_output=True, text=True,
        )
        ok = result.returncode == 0 and result.stdout.strip()
        status = "OK  " if ok else "FAIL"
        print(f"[{i:>3}/{len(files)}] {status}  {f.name[:80]}")
        if ok:
            ok_count += 1
        else:
            bad.append((f, result.stderr.strip() or "no video stream found"))

    print(f"\n{'='*60}")
    print(f"OK: {ok_count}  |  FAILED: {len(bad)}  |  TOTAL: {len(files)}")
    if bad:
        print("\nCorrupt files:")
        for f, err in bad:
            print(f"  {f.name}\n    {err}\n")

# ── Phase 4: Status ────────────────────────────────────────────────────────────

def cmd_status(db_path: Path) -> None:
    if not db_path.exists():
        print("No inventory found at", db_path)
        sys.exit(1)
    con = open_db(db_path)
    rows = con.execute("SELECT status, COUNT(*) as n FROM videos GROUP BY status").fetchall()
    total = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    print(f"\nInventory: {db_path}")
    print(f"{'Status':<12} {'Count':>8}")
    print("-" * 22)
    for r in rows:
        print(f"{r['status']:<12} {r['n']:>8}")
    print("-" * 22)
    print(f"{'TOTAL':<12} {total:>8}\n")
    con.close()

# ── Entry point ────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def check_ytdlp() -> None:
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, check=True)
        logging.info("yt-dlp %s", r.stdout.strip())
    except FileNotFoundError:
        print("ERROR: yt-dlp not found.  pip install yt-dlp")
        sys.exit(1)


def main() -> None:
    setup_logging()

    if len(sys.argv) < 2 or sys.argv[1] not in ("crawl", "download", "download-one", "integrity", "status"):
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "crawl":
        if len(sys.argv) < 3:
            print("Usage: crawl <channel_url> [db_path]")
            sys.exit(1)
        check_ytdlp()
        channel_url = sys.argv[2]
        db_path = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_DB
        cmd_crawl(channel_url, db_path)

    elif cmd == "download":
        check_ytdlp()
        args       = [a for a in sys.argv[2:] if not a.startswith("--limit")]
        limit_args = [a for a in sys.argv[2:] if a.startswith("--limit")]
        limit      = int(limit_args[0].split("=")[1]) if limit_args else None
        db_path    = Path(args[0]) if len(args) > 0 else DEFAULT_DB
        output_dir = Path(args[1]) if len(args) > 1 else DEFAULT_OUT_DIR
        cmd_download(db_path, output_dir, limit=limit)

    elif cmd == "download-one":
        if len(sys.argv) < 3:
            print("Usage: download-one <video_id> [db_path] [output_dir]")
            sys.exit(1)
        check_ytdlp()
        video_id   = sys.argv[2]
        db_path    = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_DB
        output_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_OUT_DIR
        cmd_download_one(video_id, db_path, output_dir)

    elif cmd == "integrity":
        output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT_DIR
        cmd_integrity(output_dir)

    elif cmd == "status":
        db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB
        cmd_status(db_path)


if __name__ == "__main__":
    main()
