"""Select and schedule highlights from the creator roster."""
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_upload import MIN_SCORE

INDIA = timezone(timedelta(hours=5, minutes=30))
PER_CREATOR = 3
PER_DAY = 20
SPACING = timedelta(minutes=30)
LIVE_PICK_DELAY = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_profiles (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, campaign TEXT NOT NULL,
    hashtags TEXT NOT NULL, disclosure INTEGER NOT NULL, mode TEXT NOT NULL,
    channel TEXT, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS creator_candidates (
    clip TEXT PRIMARY KEY, creator TEXT NOT NULL, source TEXT NOT NULL,
    attribution TEXT NOT NULL, score INTEGER NOT NULL, start REAL NOT NULL,
    end REAL NOT NULL, ready REAL NOT NULL, day TEXT, selected REAL
);
CREATE INDEX IF NOT EXISTS creator_daily ON creator_candidates(day, creator);
"""

DYLAN_URL = "https://www.youtube.com/watch?v=LEAdEczRNEc"
DYLAN_CAMPAIGN = "https://contentrewards.com/c/campaigns/39753a1a-f1f3-491b-a8f6-0cfee0af0c97"


def day_at(timestamp):
    return datetime.fromtimestamp(timestamp, INDIA).date().isoformat()


def collect(db, profile, source, attribution, items, data, ready):
    for item in items:
        key = item.get("file")
        if item.get("error") or not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            continue
        if not (data / f"{key}.mp4").is_file():
            continue
        try:
            score = int(item["viral"])
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not 0 <= score <= 100 or not math.isfinite(end) or not 0 <= start < end:
            continue
        title = str(item.get("title") or "").strip()[:100]
        if not title or title in {"Live clip", "Live highlight"} or re.fullmatch(r"Clip \d+", title):
            score = 0
            title = f"{profile['name']} highlight"
        status = "creator_candidate" if score >= MIN_SCORE and profile["mode"] != "review" else "review"
        detail = "Below the automatic selection threshold." if score < MIN_SCORE else ""
        db.execute("""
            INSERT OR IGNORE INTO clips(id,source,title,hook,status,detail) VALUES(?,?,?,?,?,?)
        """, (key, source, title, str(item.get("hook") or "").strip(), status, detail))
        db.execute("""
            INSERT OR IGNORE INTO creator_candidates(clip,creator,source,attribution,score,start,end,ready)
            VALUES(?,?,?,?,?,?,?,?)
        """, (key, profile["id"], source, attribution, score, start, end, ready))


def overlaps(db, candidate):
    for prev in db.execute("""
        SELECT start,end FROM creator_candidates WHERE source=? AND selected IS NOT NULL
    """, (candidate["source"],)):
        shared = min(candidate["end"], prev["end"]) - max(candidate["start"], prev["start"])
        if shared >= .5 * min(candidate["end"] - candidate["start"], prev["end"] - prev["start"]):
            return True
    return False


def release_slot(db, channel, now):
    first = datetime.fromtimestamp(now, INDIA) + SPACING
    candidate = first.replace(second=0, microsecond=0)
    candidate += timedelta(minutes=30 - candidate.minute % 30)
    existing = [
        datetime.fromisoformat(row["publish_at"])
        for row in db.execute("""
            SELECT publish_at FROM clips WHERE channel=? AND publish_at IS NOT NULL
            AND (status IN ('approved','uploading','scheduled','check')
                 OR (status='removed' AND removed_status='scheduled'))
        """, (channel,))
    ]
    while candidate.date().isoformat() == day_at(now):
        if all(abs(candidate - moment) >= SPACING for moment in existing):
            return candidate.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        candidate += SPACING
    return None


def select(db, channel, now):
    today = day_at(now)
    counts = {row["creator"]: row["total"] for row in db.execute("""
        SELECT creator,COUNT(*) AS total FROM creator_candidates WHERE day=? GROUP BY creator
    """, (today,))}
    used = sum(counts.values())
    if used >= PER_DAY:
        return
    for row in db.execute("""
        SELECT c.*,p.mode,p.channel,p.disclosure FROM creator_candidates c
        JOIN creator_profiles p ON p.id=c.creator JOIN clips v ON v.id=c.clip
        WHERE v.status='creator_candidate' AND c.ready<=? AND c.selected IS NULL AND p.channel=?
        ORDER BY c.score DESC, c.ready, c.start, c.clip
    """, (now, channel)).fetchall():
        if row["mode"] == "review" or counts.get(row["creator"], 0) >= PER_CREATOR:
            continue
        if overlaps(db, row):
            db.execute("UPDATE clips SET status='review',detail='Overlaps a selected clip.' WHERE id=?", (row["clip"],))
            continue
        scheduled = row["mode"] == "schedule" and not row["disclosure"]
        publish_at = release_slot(db, channel, now) if scheduled else None
        if scheduled and not publish_at:
            continue
        detail = "Selected for scheduled public release." if scheduled else "Selected for private upload."
        if row["disclosure"]:
            detail += " Verify the paid-promotion disclosure in YouTube Studio before publishing."
        db.execute("UPDATE clips SET status='approved',channel=?,publish_at=?,detail=? WHERE id=?",
                   (channel, publish_at, detail, row["clip"]))
        db.execute("UPDATE creator_candidates SET day=?, selected=? WHERE clip=?", (today, now, row["clip"]))
        counts[row["creator"]] = counts.get(row["creator"], 0) + 1
        used += 1
        if used >= PER_DAY:
            break


def authorized(db, clip, channel):
    row = db.execute("""
        SELECT p.channel,p.mode,p.disclosure,v.publish_at,c.day FROM creator_candidates c
        JOIN creator_profiles p ON p.id=c.creator JOIN clips v ON v.id=c.clip
        WHERE c.clip=? AND c.selected IS NOT NULL
    """, (clip,)).fetchone()
    if row is None:
        return True
    return row["channel"] == channel and row["mode"] != "review" and row["day"] == day_at(time.time()) and (
        not row["publish_at"] or (row["mode"] == "schedule" and not row["disclosure"])
    )


def description(row, profile):
    pieces = [row["title"]]
    if row["hook"] and row["hook"].casefold() != row["title"].casefold():
        pieces.append(row["hook"])
    attribution = profile["attribution"] or profile["url"]
    pieces.append(f"Source: {profile['name']}" + (f" — {attribution}" if attribution else ""))
    if profile["hashtags"]:
        pieces.append(profile["hashtags"])
    if profile["disclosure"]:
        pieces.insert(0, "#Ad")
    return "\n\n".join(pieces)
