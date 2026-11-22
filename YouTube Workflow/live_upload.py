"""Rank live highlights and enforce the local per-stream upload authorization."""
import re
import shutil
import time
from urllib.parse import parse_qs, urlsplit

WINDOW = 600
MIN_SCORE = 40
FREE_BYTES = 8 * 1024 ** 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS live_uploads (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, channel TEXT NOT NULL,
    cap INTEGER NOT NULL, deadline REAL NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL, next_pick REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_candidates (
    file TEXT PRIMARY KEY, live TEXT NOT NULL, score INTEGER NOT NULL,
    start REAL NOT NULL, end REAL NOT NULL, approved REAL
);
"""


def canonical_url(value):
    if not isinstance(value, str):
        raise ValueError("Paste a YouTube Live or Twitch link.")
    url = urlsplit(value.strip())
    host = url.hostname
    if url.scheme != "https" or url.username or url.password or url.port:
        raise ValueError("Paste an HTTPS YouTube Live or Twitch link.")
    if host in ("www.twitch.tv", "twitch.tv"):
        name = url.path.strip("/").lower()
        if re.fullmatch(r"[a-z0-9_]{3,25}", name):
            return "https://www.twitch.tv/" + name
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        video = parse_qs(url.query).get("v", [""])[0] if url.path == "/watch" else ""
        if host == "youtu.be" or url.path.startswith("/live/"):
            video = url.path.rstrip("/").split("/")[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            return "https://www.youtube.com/watch?v=" + video
    raise ValueError("Use a Twitch channel link or a YouTube Live link with a video ID.")


def stop(connection, run_id, message):
    connection.execute("UPDATE live_uploads SET status='stopped',detail=? WHERE id=?", (message, run_id))
    connection.execute("""
        UPDATE clips SET status='review',detail=? WHERE source=?
        AND (status='live_candidate' OR (status='approved' AND id IN (
            SELECT file FROM live_candidates WHERE approved IS NOT NULL
        )))
    """, (message, "live:" + run_id))


def authorized(connection, clip_id):
    rule = connection.execute("""
        SELECT r.status,r.deadline FROM live_candidates c JOIN live_uploads r ON c.live=r.id
        WHERE c.file=? AND c.approved IS NOT NULL
    """, (clip_id,)).fetchone()
    return rule is None or (rule["status"] in ("active", "ended") and rule["deadline"] > time.time())


def check_stop(rule, account, data):
    if rule["deadline"] <= time.time():
        return "Live upload authorization expired."
    if not account or account["channel_id"] != rule["channel"]:
        return "The destination channel changed."
    if shutil.disk_usage(data).free < FREE_BYTES:
        return "Live recording stopped because disk space is low."
    return ""


def collect(connection, run_id, clips, data):
    for item in clips:
        clip_id = item.get("file", "")
        if item.get("error") or not isinstance(clip_id, str) or not re.fullmatch(r"[0-9a-f]{32}", clip_id) or not (data / f"{clip_id}.mp4").is_file():
            continue
        try:
            start, end, score = float(item["start"]), float(item["end"]), int(item["viral"])
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        if not 0 <= start < end < float("inf") or not 0 <= score <= 100:
            continue
        connection.execute(
            "INSERT OR IGNORE INTO clips(id,source,title,hook,status) VALUES(?,?,?,?,'live_candidate')",
            (clip_id, "live:" + run_id, item.get("title") or "Live highlight", item.get("hook") or ""),
        )
        connection.execute(
            "INSERT OR IGNORE INTO live_candidates(file,live,score,start,end) VALUES(?,?,?,?,?)",
            (clip_id, run_id, score, start, end),
        )


def choose(connection, rule, now):
    used = connection.execute("""
        SELECT COUNT(*) FROM live_candidates c JOIN live_uploads r ON c.live=r.id
        WHERE r.channel=? AND c.approved>?
    """, (rule["channel"], now - 86400)).fetchone()[0]
    previous = [(r["start"], r["end"]) for r in connection.execute(
        "SELECT start,end FROM live_candidates WHERE live=? AND approved IS NOT NULL", (rule["id"],)
    )]
    remaining = max(0, rule["cap"] - used)
    rows = connection.execute("""
        SELECT c.* FROM live_candidates c JOIN clips ON clips.id=c.file
        WHERE c.live=? AND clips.status='live_candidate' ORDER BY c.score DESC,c.start
    """, (rule["id"],)).fetchall()
    for row in rows:
        start, end = row["start"], row["end"]
        overlaps = any(
            min(end, b) - max(start, a) >= .5 * min(end-start, b-a) for a,b in previous
        )
        if remaining and row["score"] >= MIN_SCORE and not overlaps:
            connection.execute("UPDATE live_candidates SET approved=? WHERE file=?", (now, row["file"]))
            connection.execute("UPDATE clips SET status='approved',channel=?,detail=? WHERE id=?",
                               (rule["channel"], "Approved by your live-session setting", row["file"]))
            previous.append((start, end))
            remaining -= 1
    connection.execute("""
        UPDATE clips SET status='review',detail='Available for manual review' WHERE source=? AND status='live_candidate'
    """, ("live:" + rule["id"],))
