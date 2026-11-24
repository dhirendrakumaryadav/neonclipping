"""Watch a local folder, queue NeonClipping jobs and submit explicitly approved private or scheduled uploads."""
import hashlib
import uuid
import math
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import keyring
import live_upload
import creator_queue
import commentary_take
import montage
import review_insights
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HERE = Path(__file__).resolve().parent
ENGINE_DIR = HERE.parent / "app" if (HERE.parent / "app" / "app.py").is_file() else HERE.parent
ROOT = Path(os.environ.get("NEON_WORKFLOW_HOME", HERE))
DROP = ROOT / "Drop videos here"
COMMENTARY = ROOT / "Commentary"
CREATOR_DROP = ROOT / "Creator videos"
SPEECH_MODELS = Path(os.environ.get("NEON_WORKFLOW_SPEECH", HERE.parent / "Data" / "models"))
SETTINGS = ROOT / "Settings"
DATA = Path(os.environ.get("NEON_WORKFLOW_DATA", HERE.parent / "Data"))
NEON = os.environ.get("NEON_WORKFLOW_ENGINE", "http://127.0.0.1:8770")
PORT = int(os.environ.get("NEON_WORKFLOW_PORT", "8772"))
ORIGIN = f"http://127.0.0.1:{PORT}"
ENGINE_PORT = urlsplit(NEON).port or 80
LIBRARY_ORIGINS = {f"http://{host}:{ENGINE_PORT}" for host in ("127.0.0.1", "localhost")}
LIBRARY_ROUTE = re.compile(r"/review/library/[0-9a-f]{32}\Z")
CALLBACK = ORIGIN + "/oauth/callback"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
STORE = "NeonClipping.YouTubeWorkflow"
SECRET = secrets.token_urlsafe(32)
VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
REMOVABLE_CLIPS = ("review", "creator_candidate", "live_candidate", "uploaded", "scheduled")
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
LOCK = threading.Lock()
WAKE = threading.Event()
PENDING_SIGNINS = {}
NOTE = ""
SCHEDULE_LEAD = timedelta(minutes=15)
UPLOAD_LEAD = timedelta(minutes=2)


@contextmanager
def db():
    with closing(sqlite3.connect(ROOT / "queue.sqlite3", timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            yield connection


def setup():
    DROP.mkdir(parents=True, exist_ok=True)
    COMMENTARY.mkdir(parents=True, exist_ok=True)
    SETTINGS.mkdir(parents=True, exist_ok=True)
    CREATOR_DROP.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, source TEXT,
                job TEXT, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                hook TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'review',
                channel TEXT, youtube_id TEXT, detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS commentary_takes (
                id TEXT PRIMARY KEY, clip TEXT NOT NULL, created REAL NOT NULL, caption_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clip_packaging (
                clip TEXT PRIMARY KEY, title TEXT NOT NULL,
                cover_seconds REAL NOT NULL, cover_text TEXT NOT NULL DEFAULT ''
            );
            UPDATE clips SET status='check',
                detail='Upload was interrupted. Check YouTube Studio before trying again.'
                WHERE status='uploading';
        """)
        connection.executescript(live_upload.SCHEMA)
        connection.executescript(creator_queue.SCHEMA)
        source_columns = {c["name"] for c in connection.execute("PRAGMA table_info(sources)")}
        if "creator" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN creator TEXT")
        columns = {column["name"] for column in connection.execute("PRAGMA table_info(clips)")}
        if "publish_at" not in columns:
            connection.execute("ALTER TABLE clips ADD COLUMN publish_at TEXT")
        if "library_description" not in columns:
            connection.execute("ALTER TABLE clips ADD COLUMN library_description TEXT")
        if "removed_status" not in columns:
            connection.execute("ALTER TABLE clips ADD COLUMN removed_status TEXT")
        if "removed_at" not in columns:
            connection.execute("ALTER TABLE clips ADD COLUMN removed_at REAL")
        if "commentary_take" not in columns:
            connection.execute("ALTER TABLE clips ADD COLUMN commentary_take TEXT")


def scan(seen):
    now = time.monotonic()
    files = {path: None for path in DROP.iterdir()}
    with db() as connection:
        profiles = connection.execute("SELECT id FROM creator_profiles").fetchall()
    for profile in profiles:
        folder = CREATOR_DROP / profile["id"]
        if folder.is_dir() and not folder.is_symlink():
            files.update({path: profile["id"] for path in folder.iterdir()})
    for stale in seen.keys() - files:
        seen.pop(stale)
    for path in sorted(files):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO:
            continue
        info = path.stat()
        stamp = (info.st_size, info.st_mtime_ns)
        earlier = seen.get(path)
        if earlier is None or earlier[0] != stamp:
            seen[path] = (stamp, now, False)
            continue
        if earlier[2] or not stamp[0] or now - earlier[1] < 12:
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        info = path.stat()
        if stamp != (info.st_size, info.st_mtime_ns):
            continue
        with db() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources(id,filename,status,created,creator) VALUES(?,?,?,?,?)",
                (digest, path.name, "waiting", time.time(), files[path]),
            )
        seen[path] = (stamp, now, True)


def request(client, method, path, **kwargs):
    response = client.request(method, NEON + path, **kwargs)
    if response.status_code >= 400:
        if response.status_code < 500:
            raise ValueError(response.json().get("detail", f"NeonClipping HTTP {response.status_code}"))
        response.raise_for_status()
    return response.json()


def prepare_transcript(client, source_id, key):
    source = DATA / f"{source_id}.mp4"
    if not source.is_file():
        raise ValueError("NeonClipping's imported source is missing.")
    payload = {"id": source_id}
    if request(client, "POST", "/transcripts/load", json=payload).get("cached"):
        return
    with db() as connection:
        connection.execute("UPDATE sources SET detail=? WHERE id=?",
                           ("Transcribing locally before picking clips", key))
    with TemporaryDirectory(dir=ROOT) as temporary:
        output = Path(temporary) / "words.json"
        try:
            subprocess.run(
                [sys.executable, str(HERE / "prepare_transcript.py"), str(source), str(output),
                 str(SPEECH_MODELS), str(ENGINE_DIR)],
                check=True, timeout=7200, capture_output=True,
            )
        except (subprocess.SubprocessError, OSError):
            raise ValueError("Local transcription failed. Confirm the video has a readable audio track, then retry.")
        words = json.loads(output.read_text(encoding="utf-8"))
    if words:
        request(client, "POST", "/transcripts/import", json={**payload, "words": words})


def probe_video(video_path):
    bin_probe = HERE.parent / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"
    cmd = str(bin_probe) if bin_probe.is_file() else "ffprobe"
    result = subprocess.run([cmd, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)],
                            capture_output=True, text=True, check=True, timeout=15)
    metadata = json.loads(result.stdout)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    return int(video["width"]), int(video["height"]), float(metadata["format"]["duration"])


def is_finished_clip(video_path):
    try:
        width, height, duration = probe_video(video_path)
        return height > width and duration <= 120
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError):
        return False

def sync_clips(client):
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM sources WHERE status IN ('waiting','processing') ORDER BY created"
        ).fetchall()
    if not rows:
        return
    jobs = request(client, "GET", "/queue")["jobs"]
    by_source = {
        job["payload"].get("drop_workflow_id"): job for job in jobs
        if job["kind"] == "make"
    }
    queued_upload = False
    for row in rows:
        key = row["id"]
        job = by_source.get(key)
        try:
            if not job:
                if row["job"]:
                    raise ValueError("The associated NeonClipping queue entry is missing.")
                if queued_upload:
                    continue
                source_id = row["source"]
                if not source_id:
                    folder = CREATOR_DROP / row["creator"] if row["creator"] else DROP
                    path = folder / row["filename"]
                    if folder.is_symlink():
                        raise ValueError("Use the creator's original inbox folder.")
                    if not path.is_file() or path.is_symlink():
                        raise ValueError("Put the original video back in the drop folder and retry.")
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != key:
                            raise ValueError("The original video was replaced before import.")
                        stream.seek(0)
                        source_id = request(
                            client, "POST", "/upload",
                            files={"file": (path.name, stream, "application/octet-stream")},
                        )["id"]
                    with db() as connection:
                        connection.execute("UPDATE sources SET source=? WHERE id=?", (source_id, key))
                if not row["creator"] and is_finished_clip(DATA / f"{source_id}.mp4"):
                    title = Path(row["filename"]).stem.replace("-", " ").replace("_", " ")
                    with db() as connection:
                        connection.execute(
                            "INSERT OR IGNORE INTO clips(id,source,title,hook,status) VALUES(?,?,?,?,'review')",
                            (source_id, key, title, ""),
                        )
                        connection.execute("UPDATE sources SET status='done',detail='1 clip ready (direct vertical clip)' WHERE id=?", (key,))
                    continue
                prepare_transcript(client, source_id, key)
                job = request(
                    client, "POST", "/queue",
                    json={"kind": "make", "payload": {
                        "id": source_id, "drop_workflow_id": key, "mode": "fast",
                        "moment_profile": "balanced", "count": 5 if row["creator"] else 3,
                        "language": "en" if row["creator"] else None, "min_len": 15, "max_len": 45,
                        "aspect": "vertical", "resolution": 1080, "layout": "blur",
                        "caption_style": "clean", "hook_background": False,
                    }},
                )
                queued_upload = True
            result = job["result"]
            with db() as connection:
                if job["status"] == "done":
                    good = 0
                    profile = connection.execute("SELECT * FROM creator_profiles WHERE id=?", (row["creator"],)).fetchone()
                    if profile:
                        creator_queue.collect(connection, profile, key, "", result.get("clips", []), DATA, time.time())
                        good = connection.execute("SELECT COUNT(*) FROM creator_candidates WHERE source=?", (key,)).fetchone()[0]
                    for clip in ([] if profile else result.get("clips", [])):
                        clip_id = clip.get("file", "")
                        if clip.get("error") or not HEX_ID.fullmatch(clip_id):
                            continue
                        if not (DATA / f"{clip_id}.mp4").is_file():
                            continue
                        connection.execute(
                            "INSERT OR IGNORE INTO clips(id,source,title,hook) VALUES(?,?,?,?)",
                            (clip_id, key, clip.get("title") or "NeonClipping Short", clip.get("hook") or ""),
                        )
                        good += 1
                    total = len(result.get("clips", []))
                    label = f"{good} clips ready" + (f" ({total - good} failed)" if total > good else "")
                    connection.execute("UPDATE sources SET job=?,status=?,detail=? WHERE id=?",
                                       (job["id"], "done", label, key))
                elif job["status"] in ("error", "interrupted", "canceled"):
                    detail = result.get("error") or f"NeonClipping job {job['status']}."
                    connection.execute("UPDATE sources SET job=?,status='error',detail=? WHERE id=?",
                                       (job["id"], detail, key))
                else:
                    connection.execute(
                        "UPDATE sources SET job=?,status='processing',detail=? WHERE id=?",
                        (job["id"], result.get("stage") or job["status"], key),
                    )
        except ValueError as error:
            with db() as connection:
                connection.execute("UPDATE sources SET status='error',detail=? WHERE id=?",
                                   (str(error), key))


def saved_account():
    data = keyring.get_password(STORE, "account")
    return json.loads(data) if data else None


def youtube(account):
    creds = Credentials.from_authorized_user_info(account["token"], SCOPES)
    if not creds.valid:
        creds.refresh(GoogleRequest())
        account["token"] = json.loads(creds.to_json())
        keyring.set_password(STORE, "account", json.dumps(account))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def client_config():
    path = SETTINGS / "client_secret.json"
    if not path.is_file():
        raise HTTPException(409, "Save your Google Desktop OAuth client as Settings/client_secret.json first.")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))["installed"]
        if not config["client_id"].endswith(".apps.googleusercontent.com") or not config["client_secret"]:
            raise ValueError()
        return {"installed": {
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "auth_uri": AUTH_URL, "token_uri": TOKEN_URL, "redirect_uris": [CALLBACK],
        }}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Use the JSON downloaded for a Google OAuth Desktop app.")


def parse_publish_time(value):
    if not isinstance(value, str):
        raise ValueError("Choose a publication date and time.")
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError()
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError("Choose a valid publication time with a time zone.") from error


def approve_publish_time(value):
    if value is None:
        return None
    try:
        moment = parse_publish_time(value)
        if moment < datetime.now(timezone.utc) + SCHEDULE_LEAD:
            raise ValueError("Choose a publication time at least 15 minutes from now.")
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def automatic_authorized(connection, row):
    return live_upload.authorized(connection, row["id"]) and creator_queue.authorized(
        connection, row["id"], row["channel"]
    )


def upload_one():
    with db() as connection:
        row = connection.execute("SELECT * FROM clips WHERE status='approved' ORDER BY rowid LIMIT 1").fetchone()
    if not row:
        return
    key = row["id"]
    publish_at = row["publish_at"]
    publish_time = parse_publish_time(publish_at) if publish_at else None
    if publish_time and publish_time < datetime.now(timezone.utc) + UPLOAD_LEAD:
        with db() as connection:
            connection.execute(
                "UPDATE clips SET status='review',publish_at=NULL,detail=? WHERE id=?",
                ("Publication time is too close. Choose a new time and approve again.", key),
            )
        return
    with LOCK:
        with db() as connection:
            current = connection.execute("SELECT status FROM clips WHERE id=?", (key,)).fetchone()
            if not current or current["status"] != "approved":
                return
            row = connection.execute("SELECT * FROM clips WHERE id=?", (key,)).fetchone()
            if not automatic_authorized(connection, row):
                connection.execute("UPDATE clips SET status='review',publish_at=NULL,detail=? WHERE id=?",
                                   ("Automatic upload authorization ended.", key))
                return
        account = saved_account()
        if not account or row["channel"] != account["channel_id"]:
            with db() as connection:
                connection.execute("UPDATE clips SET status='review',detail=? WHERE id=?",
                                   ("Channel changed. Approve again for the connected channel.", key))
            return
        try:
            service = youtube(account)
            path = review_media_path(key, row["commentary_take"])
            if not path.is_file():
                raise FileNotFoundError()
            media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            visibility = {"privacyStatus": "private"}
            if publish_at:
                visibility["publishAt"] = publish_at
            desc = (
                f"{row['title']}\n\n"
                f"{row['hook']}\n\n"
                f"Source: Whatever You Steal in GTA 6, You Keep! (MrBeast Gaming)\n\n"
                f"#GTA6 #Shorts #GTA6Shorts #Gaming #GamingShorts #Viral #GTAVIFunny"
            )
            tags = ["GTA 6", "GTA 6 gameplay", "GTA 6 funny moments", "GTA 6 challenge", "MrBeast Gaming", "Shorts", "Gaming Shorts", "GTA Shorts"]
            with db() as connection:
                profile = connection.execute("""
                    SELECT p.*,c.attribution FROM creator_candidates c JOIN creator_profiles p ON p.id=c.creator
                    WHERE c.clip=?
                """, (key,)).fetchone()
            if profile:
                desc = creator_queue.description(row, profile)
                tags = ["GTA VI", profile["name"]]
            elif row["library_description"] is not None:
                desc, tags = row["library_description"], []
            if (row["library_description"] or "").startswith("#Ad\n") and not desc.startswith("#Ad\n"):
                desc = "#Ad\n" + desc
            upload = service.videos().insert(
                part="snippet,status", notifySubscribers=False, media_body=media,
                body={"snippet": {"title": row["title"], "description": desc, "tags": tags, "categoryId": "20"},
                      "status": visibility},
            )
        except Exception:
            with db() as connection:
                connection.execute("UPDATE clips SET status='review',detail=? WHERE id=?",
                                   ("Could not start the upload. Check the video and reconnect YouTube if needed.", key))
            return
        with db() as connection:
            connection.execute("UPDATE clips SET status='uploading',detail='' WHERE id=?", (key,))
    try:
        result = None
        while result is None:
            with db() as connection:
                if not automatic_authorized(connection, row):
                    connection.execute("UPDATE clips SET status='check',detail=? WHERE id=?", (
                        "Automatic uploads stopped during transfer. Check YouTube Studio.", key,
                    ))
                    return
            if publish_time and publish_time < datetime.now(timezone.utc) + UPLOAD_LEAD:
                with db() as connection:
                    connection.execute("UPDATE clips SET status='check',detail=? WHERE id=?", (
                        "Upload paused near the publication time. Check YouTube Studio before trying again.", key,
                    ))
                return
            progress, result = upload.next_chunk(num_retries=0 if publish_time else 3)
            if progress is not None:
                with db() as connection:
                    connection.execute("UPDATE clips SET detail=? WHERE id=?",
                                       (f"{progress.progress():.0%} uploaded", key))
        identifier = result["id"]
    except Exception:
        with db() as connection:
            connection.execute("UPDATE clips SET status='check',detail=? WHERE id=?",
                               ("Upload could not be confirmed. Check YouTube Studio to avoid a duplicate.", key))
        return
    status = "uploaded"
    detail = "Uploaded as private"
    if (profile and profile["disclosure"]) or (row["library_description"] or "").startswith("#Ad\n"):
        detail += ". Verify the paid-promotion disclosure in YouTube Studio before publishing."
    if publish_time:
        remote = result.get("status", {})
        try:
            matches = parse_publish_time(remote.get("publishAt")) == publish_time
        except ValueError:
            matches = False
        if remote.get("privacyStatus") == "private" and matches:
            status, detail = "scheduled", "Scheduled on YouTube"
        else:
            status, detail = "check", "Uploaded, but the requested schedule was not confirmed. Check YouTube Studio."
    with db() as connection:
        connection.execute("UPDATE clips SET status=?,youtube_id=?,detail=? WHERE id=?",
                           (status, identifier, detail, key))


def worker(stop):
    global NOTE
    seen = {}
    with httpx.Client(timeout=httpx.Timeout(120, connect=4), trust_env=False) as client:
        while not stop.is_set():
            try:
                scan(seen)
                sync_clips(client)
                NOTE = ""
            except (httpx.HTTPError, OSError):
                NOTE = "Waiting for NeonClipping or a file that is still being copied. The queue will retry."
            except Exception:
                NOTE = "The local queue hit an error. Check the workflow log."
            stop.wait(3)


def sync_live(client):
    with db() as connection:
        rule = connection.execute("SELECT * FROM live_uploads WHERE status='active' LIMIT 1").fetchone()
    if not rule:
        return
    run_id = rule["id"]
    reason = live_upload.check_stop(rule, saved_account(), DATA)
    if reason:
        with LOCK, db() as connection:
            live_upload.stop(connection, run_id, reason)
        try:
            request(client, "POST", f"/live/{run_id}/stop", json={})
        except (ValueError, httpx.HTTPError):
            pass
        return
    try:
        status = request(client, "GET", f"/live/{run_id}")
    except ValueError:
        status = {"status": "error", "error": "The recorder stopped or NeonClipping restarted."}
    ended = status.get("status") in ("ended", "stopped", "error") and not status.get("pending")
    now = time.time()
    with LOCK, db() as connection:
        latest = connection.execute("SELECT status FROM live_uploads WHERE id=?", (run_id,)).fetchone()
        if not latest or latest["status"] != "active":
            return
        live_upload.collect(connection, run_id, status.get("clips", []), DATA)
        if now >= rule["next_pick"] or ended:
            live_upload.choose(connection, rule, now)
            connection.execute("UPDATE live_uploads SET next_pick=? WHERE id=?", (now + live_upload.WINDOW, run_id))
        connection.execute("UPDATE live_uploads SET status=?,detail=? WHERE id=?", (
            "ended" if ended else "active", status.get("error") or status.get("stage") or "Watching the stream", run_id,
        ))
    WAKE.set()


def sync_creators(client):
    sessions = request(client, "GET", "/creator-live-sessions")["sessions"]
    account = saved_account()
    now = time.time()
    with LOCK, db() as connection:
        profiles = {row["id"]: row for row in connection.execute("SELECT * FROM creator_profiles")}
        for session in sessions:
            profile = profiles.get(session["creator"])
            if not profile or session.get("creator_url") != profile["url"]:
                continue
            finished = session["status"] in {"ended", "stopped", "error"} and not session["pending"]
            source = session["url"] if "youtube.com/watch?" in session["url"] else "creator-live:" + session["id"]
            creator_queue.collect(connection, profile, source, session["url"], session["clips"], DATA,
                                  now if finished else now + creator_queue.LIVE_PICK_DELAY)
            if finished:
                connection.execute("UPDATE creator_candidates SET ready=MIN(ready,?) WHERE source=? AND creator=?",
                                   (now, source, profile["id"]))
        if account:
            creator_queue.select(connection, account["channel_id"], now)
    WAKE.set()


def live_worker(stop):
    with httpx.Client(timeout=10, trust_env=False) as client:
        while not stop.is_set():
            try:
                sync_live(client)
            except (ValueError, httpx.HTTPError, OSError):
                pass
            try:
                sync_creators(client)
            except (ValueError, httpx.HTTPError, OSError, sqlite3.Error):
                pass
            stop.wait(5)


def upload_worker(stop):
    global NOTE
    while not stop.is_set():
        try:
            upload_one()
        except Exception:
            NOTE = "Check the YouTube connection before uploading."
        WAKE.wait(2)
        WAKE.clear()


@asynccontextmanager
async def lifetime(_app):
    setup()
    stop = threading.Event()
    threading.Thread(target=worker, args=(stop,), daemon=True).start()
    threading.Thread(target=upload_worker, args=(stop,), daemon=True).start()
    threading.Thread(target=live_worker, args=(stop,), daemon=True).start()
    yield
    stop.set()
    WAKE.set()


app = FastAPI(lifespan=lifetime, docs_url=None, redoc_url=None, openapi_url=None)
montage.attach(app, HERE.parent, DATA, HERE)


@app.get("/montage/library")
def montage_library():
    try:
        with httpx.Client(timeout=12, trust_env=False) as client:
            clips = request(client, "GET", "/library")
    except (ValueError, httpx.HTTPError) as error:
        raise HTTPException(503, "Start the NeonClipping editor to load My Clips.") from error
    return [{"id": clip["id"], "title": clip["title"], "duration": clip["meta"]["duration"]}
            for clip in clips if HEX_ID.fullmatch(clip["id"]) and clip.get("meta", {}).get("duration")]


@app.middleware("http")
async def local_only(incoming: Request, proceed):
    if incoming.headers.get("host") != f"127.0.0.1:{PORT}":
        return HTMLResponse("Open the queue on this laptop at " + ORIGIN, status_code=403)
    origin = incoming.headers.get("origin")
    library_request = LIBRARY_ROUTE.fullmatch(incoming.url.path)
    local_request = incoming.method == "POST" and origin in (None, ORIGIN) and secrets.compare_digest(
        incoming.headers.get("x-workflow-token", ""), SECRET
    )
    if library_request and incoming.method in {"OPTIONS", "POST"} and not local_request:
        if origin not in LIBRARY_ORIGINS:
            return HTMLResponse("Open My Clips in the local NeonClipping studio.", status_code=403)
        if incoming.method == "OPTIONS":
            response = Response(status_code=204)
        elif incoming.headers.get("x-neon-library") == "1":
            response = await proceed(incoming)
        else:
            return HTMLResponse("Open My Clips to add a finished clip.", status_code=403)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST"
        response.headers["Access-Control-Allow-Headers"] = "X-Neon-Library"
        response.headers["Vary"] = "Origin"
    else:
        if incoming.method == "POST":
            if origin not in (None, ORIGIN) or not secrets.compare_digest(
                incoming.headers.get("x-workflow-token", ""), SECRET
            ):
                return HTMLResponse("Refresh the queue before making changes.", status_code=403)
        response = await proceed(incoming)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def home():
    html = (HERE / "queue.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__WORKFLOW_TOKEN__", SECRET))


@app.get("/review-insights.js")
def insights_script():
    return FileResponse(HERE / "review-insights.js", media_type="text/javascript")


def review_media_path(clip, take):
    if take:
        return COMMENTARY / f"{take}.mp4"
    return DATA / f"{clip}.mp4"


def packaging_media(file, original=False):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    with db() as connection:
        clip = connection.execute("SELECT commentary_take FROM clips WHERE id=? AND status='review'", (file,)).fetchone()
        if not clip:
            raise HTTPException(404, "Clip is no longer waiting for review.")
    media = review_media_path(file, None if original else clip["commentary_take"])
    if not media.is_file():
        raise HTTPException(404, "The finished clip is no longer on this laptop.")
    return media


@app.get("/review/{file}/packaging")
def packaging_metadata(file: str):
    media = packaging_media(file)
    try:
        _, _, duration = probe_video(media)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError):
        raise HTTPException(400, "The finished clip could not be opened.") from None
    return {"duration": duration}


@app.get("/review/{file}/cover")
def cover_frame(file: str, at: float = 0):
    media = packaging_media(file)
    if not math.isfinite(at) or not 0 <= at <= 86400:
        raise HTTPException(400, "Choose a valid frame from the clip.")
    bundled = HERE.parent / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ffmpeg = str(bundled) if bundled.is_file() else "ffmpeg"
    try:
        frame = subprocess.run([
            ffmpeg, "-v", "error", "-nostdin", "-ss", str(at), "-i", str(media),
            "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=360:640:force_original_aspect_ratio=decrease",
            "-threads", "1", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
        ], capture_output=True, check=True, timeout=15).stdout
        if not frame:
            raise ValueError()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise HTTPException(400, "This frame could not be previewed. Try an earlier frame.") from None
    return Response(content=frame, media_type="image/jpeg")


@app.post("/review/{file}/packaging")
def save_packaging(file: str, body: dict):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    title = body.get("title")
    cover_text = body.get("cover_text", "")
    if not isinstance(title, str) or not isinstance(cover_text, str):
        raise HTTPException(400, "Enter a valid title and cover text.")
    title, cover_text = title.strip(), cover_text.strip()
    if not 1 <= len(title) <= 100 or re.search(r"[\x00-\x1f\x7f<>]", title):
        raise HTTPException(400, "Use a title of 1–100 characters without angle brackets or line breaks.")
    if len(cover_text) > 160:
        raise HTTPException(400, "Keep cover text at 160 characters or fewer.")
    try:
        seconds = float(body["cover_seconds"])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(400, "Choose a valid frame from the clip.") from None
    media = packaging_media(file)
    try:
        _, _, duration = probe_video(media)
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError):
        raise HTTPException(400, "The finished clip could not be opened.") from None
    if not 0 <= seconds < duration:
        raise HTTPException(400, "Choose a frame within the clip.")
    with LOCK, db() as connection:
        changed = connection.execute("UPDATE clips SET title=? WHERE id=? AND status='review'", (title, file))
        if not changed.rowcount:
            raise HTTPException(409, "This clip is no longer waiting for review.")
        connection.execute("""
            INSERT INTO clip_packaging(clip,title,cover_seconds,cover_text) VALUES(?,?,?,?)
            ON CONFLICT(clip) DO UPDATE SET title=excluded.title, cover_seconds=excluded.cover_seconds,
                                         cover_text=excluded.cover_text
        """, (file, title, seconds, cover_text))
    return {"title": title, "cover_seconds": seconds}


@app.post("/retention/analyze")
def analyze_retention(file: UploadFile = File(...), duration: float | None = Form(None)):
    limit = 1024 * 1024
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(400, "Choose a retention CSV under 1 MB.")
    try:
        return review_insights.retention_summary(content, duration)
    except UnicodeError as error:
        raise HTTPException(400, "The CSV must use UTF-8 or UTF-16 encoding.") from error
    except (ValueError, review_insights.csv.Error) as error:
        raise HTTPException(400, str(error)) from error


@app.get("/state")
def state():
    account = saved_account()
    with db() as connection:
        sources = [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY created DESC")]
        clips = [dict(row) for row in connection.execute("""
            SELECT v.*, p.name AS creator_name, k.cover_seconds, k.cover_text,
                   k.title=v.title AS packaging_checked,
                   (SELECT id FROM commentary_takes WHERE clip=v.id ORDER BY created DESC LIMIT 1) AS commentary_latest
            FROM clips v LEFT JOIN clip_packaging k ON k.clip=v.id
            LEFT JOIN creator_candidates c ON c.clip=v.id LEFT JOIN creator_profiles p ON p.id=c.creator
            WHERE v.status<>'removed'
            ORDER BY v.rowid DESC
        """)]
        removed_clips = [dict(row) for row in connection.execute("""
            SELECT id,title,removed_status,removed_at FROM clips
            WHERE status='removed' ORDER BY removed_at DESC
        """)]
        creators = [{**dict(row), "inbox": str(CREATOR_DROP / row["id"])}
                    for row in connection.execute("SELECT * FROM creator_profiles ORDER BY rowid")]
        live = connection.execute("SELECT * FROM live_uploads ORDER BY rowid DESC LIMIT 1").fetchone()
    return {
        "live_upload": dict(live) if live else None,
        "live_upload_supported": True, "creators": creators,
        "channel": {"id": account["channel_id"], "title": account["channel_title"]} if account else None,
        "configured": (SETTINGS / "client_secret.json").is_file(), "sources": sources,
        "clips": clips, "removed_clips": removed_clips, "note": NOTE, "drop": str(DROP),
        "schedule_min_minutes": int(SCHEDULE_LEAD.total_seconds() / 60),
    }


def remove_queue_clips(ids):
    with LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = []
        for key in ids:
            current = connection.execute("SELECT status FROM clips WHERE id=?", (key,)).fetchone()
            if not current:
                raise HTTPException(404, "A selected clip could not be found. Refresh the queue.")
            if current["status"] == "removed":
                continue
            if current["status"] not in REMOVABLE_CLIPS:
                raise HTTPException(409, "A selected clip is now queued for upload, uploading, or needs a Studio check. Review your selection.")
            pending.append(key)
        removed_at = time.time()
        connection.executemany("""
            UPDATE clips SET removed_status=status, removed_at=?,status='removed' WHERE id=?
        """, ((removed_at, key) for key in pending))
    return len(pending)


@app.post("/review/remove-selected")
def remove_selected_clips(body: dict):
    ids = body.get("clips")
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not HEX_ID.fullmatch(key) for key in ids):
        raise HTTPException(400, "Select clips to remove.")
    return {"removed": remove_queue_clips(dict.fromkeys(ids))}


@app.post("/review/{file}/remove")
def remove_review_clip(file: str):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    remove_queue_clips([file])
    return {"status": "removed"}


def restore_queue_clips(ids):
    statuses = {}
    restored = 0
    with LOCK, db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for key in ids:
            current = connection.execute("SELECT status,removed_status FROM clips WHERE id=?", (key,)).fetchone()
            if not current:
                raise HTTPException(404, "A selected clip could not be found. Refresh the queue.")
            status = current["status"]
            if status == "removed":
                previous = current["removed_status"]
                status = previous if previous in ("uploaded", "scheduled") else "review"
                connection.execute("""
                    UPDATE clips SET status=?,removed_status=NULL,removed_at=NULL,
                    detail=CASE WHEN ?='review' THEN 'Restored for manual review.' ELSE detail END
                    WHERE id=?
                """, (status, status, key))
                restored += 1
            statuses[key] = status
    return restored, statuses


@app.post("/review/restore-selected")
def restore_selected_clips(body: dict):
    ids = body.get("clips")
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not HEX_ID.fullmatch(key) for key in ids):
        raise HTTPException(400, "Select clips to restore.")
    restored, _ = restore_queue_clips(dict.fromkeys(ids))
    return {"restored": restored}


@app.post("/review/{file}/restore")
def restore_review_clip(file: str):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    _, statuses = restore_queue_clips([file])
    return {"status": statuses[file]}


@app.post("/review/library/{file}")
def review_library_clip(file: str):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    media = DATA / f"{file}.mp4"
    metadata = DATA / f"{file}.json"
    if media.is_symlink() or metadata.is_symlink() or not media.is_file():
        raise HTTPException(404, "This export is no longer in My Clips.")
    try:
        record = json.loads(metadata.read_text(encoding="utf-8-sig"))
        if record.get("result", {}).get("status") != "done" or record.get("superseded_by"):
            raise ValueError("Unavailable export")
        settings = record.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError("Invalid export settings")
    except (AttributeError, OSError, ValueError):
        raise HTTPException(404, "This export is no longer in My Clips. Refresh the library.") from None
    title = str(settings.get("title") or "NeonClipping clip").strip()[:100] or "NeonClipping clip"
    hook = str(settings.get("hook") or "").strip()
    source_url = str(settings.get("source_url") or "").strip()
    caption = str(settings.get("post_caption") or "").strip()
    voice = str(settings.get("voice_line") or "").strip()[:100]
    replay = str(settings.get("replay_line") or "").strip()[:160]
    picker = str(settings.get("picker_label") or "").strip()[:60]
    detail = ("Picker: " + picker) if picker else "Sent from My Clips."
    reason = str(settings.get("pick_reason") or "").strip()[:220]
    if picker and reason:
        detail += " · " + reason
    take = "Opening take (2–3 s): " + voice if voice else hook
    if voice and replay:
        take += " · Optional payoff line: " + replay
    parts = [caption or title]
    if hook and not voice and hook != title and hook not in parts[0]:
        parts.append(hook)
    if source_url.startswith(("https://", "http://")) and source_url not in parts[0]:
        parts.append("Source: " + source_url)
    with LOCK, db() as connection:
        existing = connection.execute("SELECT status FROM clips WHERE id=?", (file,)).fetchone()
        if existing and existing["status"] not in {"live_candidate", "creator_candidate"}:
            return {"id": file, "status": existing["status"], "added": False}
        creator = settings.get("creator")
        if not isinstance(creator, str):
            creator = ""
        disclosure = connection.execute("""
            SELECT 1 FROM creator_profiles WHERE disclosure=1
            AND (id=? OR (url<>'' AND (url=? OR instr(?,url)>0)))
        """, (creator, source_url, caption)).fetchone()
        if disclosure or re.search(r"(?im)^#ad(?:\s|$)", caption):
            if not re.match(r"(?i)^#ad\s", parts[0]):
                parts.insert(0, "#Ad")
            else:
                parts[0] = "#Ad" + parts[0][3:]
        description = "\n\n".join(parts).encode("utf-8")[:5000].decode("utf-8", errors="ignore")
        if existing:
            connection.execute("""
                UPDATE clips SET status='review', channel=NULL, publish_at=NULL, hook=?, detail=?, library_description=?
                WHERE id=?
            """, (take, detail, description, file))
        else:
            connection.execute("""
                INSERT INTO clips(id, source, title, hook, status, detail, library_description)
                VALUES(?, ?, ?, ?, 'review', ?, ?)
            """, (file, "library:" + file, title, take, detail, description))
    return {"id": file, "status": "review", "added": True}


@app.get("/commentary.js")
def commentary_script():
    return FileResponse(HERE / "commentary.js", media_type="text/javascript")


@app.get("/review/{file}/commentary/original")
def commentary_original(file: str):
    return FileResponse(packaging_media(file, original=True), media_type="video/mp4")


@app.post("/review/{file}/commentary/draft")
def commentary_draft(file: str, body: dict):
    media = packaging_media(file, original=True)
    angle = body.get("angle", "")
    if not isinstance(angle, str) or len(angle) > 350:
        raise HTTPException(400, "Keep your observation under 350 characters.")
    try:
        offset = float(body.get("offset", 0))
        _, _, duration = probe_video(media)
        if not math.isfinite(offset) or not 0 <= offset < duration:
            raise ValueError()
    except (ValueError, TypeError, KeyError, StopIteration, OSError, subprocess.SubprocessError):
        raise HTTPException(400, "Choose a start time within the clip.") from None
    with db() as connection:
        clip = connection.execute("SELECT title,hook FROM clips WHERE id=?", (file,)).fetchone()
    transcript = ""
    subtitles = DATA / f"{file}.srt"
    try:
        with subtitles.open(encoding="utf-8-sig") as stream:
            transcript = " ".join(line.strip() for line in stream.read(4500).splitlines()
                                  if line.strip() and not line.strip().isdigit() and "-->" not in line)[:1200]
    except (OSError, UnicodeError):
        pass
    draft, source = commentary_take.draft_script(clip["title"], clip["hook"], transcript, angle.strip(), duration - offset)
    return {"draft": draft, "source": source}


def clip_moment(file):
    """What NeonClipping's picker saved about this clip (voice line, reason, what happens on screen)."""
    try:
        settings = json.loads((DATA / f"{file}.json").read_text(encoding="utf-8")).get("settings") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(settings, dict):
        return {}
    review = settings.get("visual_review") if isinstance(settings.get("visual_review"), dict) else {}
    return {"voice_line": str(settings.get("voice_line") or "")[:200],
            "pick_reason": str(settings.get("pick_reason") or "")[:300],
            "replay_line": str(settings.get("replay_line") or "")[:200],
            "payoff_at": settings.get("payoff_at"),
            "outcome": str(review.get("outcome") or "")[:300]}


@app.get("/review/{file}/commentary/lines")
def commentary_lines(file: str):
    packaging_media(file, original=True)
    moment = clip_moment(file)
    payoff = moment.get("replay_line", "")
    # Daily batches already store the timestamp as part of the payoff line.
    timed = re.match(r"^(\d+(?:\.\d+)?)\s*s\s*·\s*(.+)$", payoff)
    if timed:
        payoff = timed[2]
    try:
        at = float(moment.get("payoff_at") if moment.get("payoff_at") is not None else timed[1])
        if not math.isfinite(at) or at < .25:
            at = None
    except (TypeError, ValueError):
        at = None
    return {"opening": moment.get("voice_line", ""), "payoff": payoff, "payoff_at": at}


@app.post("/review/{file}/commentary/script")
def commentary_full_script(file: str, body: dict):
    media = packaging_media(file, original=True)
    style, angle = body.get("style", "funny"), body.get("angle", "")
    if style not in commentary_take.STYLES or not isinstance(angle, str) or len(angle) > 350:
        raise HTTPException(400, "Choose a style and keep your observation under 350 characters.")
    try:
        offset = float(body.get("offset", 0))
        _, _, duration = probe_video(media)
        if not math.isfinite(offset) or not 0 <= offset < duration:
            raise ValueError()
    except (ValueError, TypeError, KeyError, StopIteration, OSError, subprocess.SubprocessError):
        raise HTTPException(400, "Choose a start time within the clip.") from None
    with db() as connection:
        clip = connection.execute("SELECT title FROM clips WHERE id=?", (file,)).fetchone()
    if not clip:
        raise HTTPException(404, "This clip is no longer in the queue.")
    transcript = ""
    try:
        with (DATA / f"{file}.srt").open(encoding="utf-8-sig") as stream:
            transcript = " ".join(line.strip() for line in stream.read(6000).splitlines()
                                  if line.strip() and not line.strip().isdigit() and "-->" not in line)[:1800]
    except (OSError, UnicodeError):
        pass
    try:
        voice = (SETTINGS / "voice.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        voice = ""
    lines, source = commentary_take.write_script(clip["title"], transcript, angle.strip(), duration - offset,
                                                 style, voice, clip_moment(file))
    return {"lines": lines, "source": source}


@app.post("/review/{file}/commentary/render")
def commentary_render(file: str, recording: UploadFile | None = File(None),
                      offset: float = Form(0), language: str = Form("auto"),
                      opening: UploadFile | None = File(None), payoff: UploadFile | None = File(None),
                      payoff_offset: float = Form(0)):
    if (recording and (opening or payoff)) or not (recording or opening or payoff):
        raise HTTPException(400, "Choose a full take or at least one separate line.")
    if payoff and (not math.isfinite(payoff_offset) or payoff_offset < .25):
        raise HTTPException(400, "Choose a payoff time inside the clip.")
    original = packaging_media(file, original=True)
    if not math.isfinite(offset) or offset < 0 or language not in commentary_take.LANGUAGES:
        raise HTTPException(400, "Choose a valid start time and caption language.")
    if not commentary_take.RENDER_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A commentary take is already processing. Try again when it finishes.")
    key = uuid.uuid4().hex
    try:
        with TemporaryDirectory(dir=COMMENTARY, prefix="take-") as temporary:
            work = Path(temporary)
            takes = ((recording, offset),) if recording else ((opening, 0), (payoff, payoff_offset))
            parts = []
            for index, (upload, start) in enumerate(takes):
                if upload is None:
                    continue
                content = upload.file.read(commentary_take.MAX_TAKE_BYTES + 1)
                if not content or len(content) > commentary_take.MAX_TAKE_BYTES:
                    raise HTTPException(400, "Choose a recording under 20 MB for each line.")
                raw = work / f"recording-{index}"
                raw.write_bytes(content)
                parts.append((raw, start))
            try:
                result, captions = commentary_take.render_parts(HERE.parent, SPEECH_MODELS, original,
                                                                parts, language, work)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                raise HTTPException(400, str(error)) from error
            with LOCK, db() as connection:
                if not connection.execute("SELECT 1 FROM clips WHERE id=? AND status='review'", (file,)).fetchone():
                    raise HTTPException(409, "This clip changed while the take was rendering.")
                result.replace(review_media_path(file, key))
                connection.execute("INSERT INTO commentary_takes VALUES(?,?,?,?)", (key, file, time.time(), captions))
    finally:
        commentary_take.RENDER_LOCK.release()
    return {"take": key, "caption_count": captions, "preview": f"/review/{file}/commentary/takes/{key}"}


@app.get("/review/{file}/commentary/takes/{take}")
def commentary_preview(file: str, take: str):
    if not HEX_ID.fullmatch(file) or not HEX_ID.fullmatch(take):
        raise HTTPException(404, "Take not found.")
    with db() as connection:
        if not connection.execute("SELECT 1 FROM commentary_takes WHERE clip=? AND id=?", (file, take)).fetchone():
            raise HTTPException(404, "Take not found.")
    media = review_media_path(file, take)
    if not media.is_file():
        raise HTTPException(404, "The commentary preview is missing.")
    return FileResponse(media, media_type="video/mp4")


@app.post("/review/{file}/commentary/use")
def commentary_use(file: str, body: dict):
    take = body.get("take")
    if 'take' not in body or not HEX_ID.fullmatch(file) or (take is not None and (
        not isinstance(take, str) or not HEX_ID.fullmatch(take)
    )):
        raise HTTPException(400, "Choose a valid commentary take.")
    with LOCK, db() as connection:
        clip = connection.execute("SELECT commentary_take FROM clips WHERE id=? AND status='review'", (file,)).fetchone()
        if not clip:
            raise HTTPException(409, "This clip is no longer waiting for review.")
        if take and (
            not connection.execute("SELECT 1 FROM commentary_takes WHERE clip=? AND id=?", (file, take)).fetchone()
            or not review_media_path(file, take).is_file()
        ):
            raise HTTPException(404, "Commentary take not found.")
        if take != clip["commentary_take"]:
            connection.execute("UPDATE clips SET commentary_take=?, detail=? WHERE id=?", (
                take, "Commentary ready for approval." if take else "Original clip restored.", file,
            ))
            connection.execute("DELETE FROM clip_packaging WHERE clip=?", (file,))
    return {"take": take}


@app.get("/preview/{file}")
def preview(file: str):
    if not HEX_ID.fullmatch(file):
        raise HTTPException(404, "Clip not found.")
    with db() as connection:
        row = connection.execute("SELECT commentary_take FROM clips WHERE id=?", (file,)).fetchone()
        if not row:
            raise HTTPException(404, "Clip not found.")
    return FileResponse(review_media_path(file, row["commentary_take"]), media_type="video/mp4")


@app.post("/connect")
def connect():
    config = client_config()
    flow = Flow.from_client_config(
        config, scopes=SCOPES, redirect_uri=CALLBACK, autogenerate_code_verifier=True
    )
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
    with LOCK:
        PENDING_SIGNINS.clear()
        PENDING_SIGNINS[state] = (time.monotonic() + 600, flow)
    return {"url": url}


@app.get("/oauth/callback")
def oauth_callback(state: str = "", code: str = ""):
    with LOCK:
        item = PENDING_SIGNINS.pop(state, None)
        if not item or time.monotonic() > item[0] or not code:
            return HTMLResponse("YouTube was not connected. Return to the queue and start sign-in again.", status_code=400)
        try:
            flow = item[1]
            flow.fetch_token(code=code, timeout=30)
            service = build("youtube", "v3", credentials=flow.credentials, cache_discovery=False)
            channels = service.channels().list(part="id,snippet", mine=True).execute().get("items", [])
            if len(channels) != 1 or not flow.credentials.refresh_token:
                raise ValueError("Select one YouTube channel and grant offline access.")
            item = channels[0]
            keyring.set_password(STORE, "account", json.dumps({
                "channel_id": item["id"], "channel_title": item["snippet"]["title"],
                "token": json.loads(flow.credentials.to_json()),
            }))
        except Exception:
            return HTMLResponse("YouTube sign-in could not be completed. Check your OAuth setup and try again.", status_code=400)
    return RedirectResponse("/", status_code=303)


@app.post("/approve")
def approve(body: dict):
    publish_at = approve_publish_time(body.get("publish_at"))
    with LOCK:
        account = saved_account()
        if not account or body.get("channel") != account["channel_id"]:
            raise HTTPException(409, "Connect YouTube and confirm the destination channel.")
        ids = body.get("clips")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 30 or any(
            not isinstance(key, str) or not HEX_ID.fullmatch(key) for key in ids
        ):
            raise HTTPException(400, "Select between 1 and 30 clips.")
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in dict.fromkeys(ids):
                checked = connection.execute("""
                    SELECT 1 FROM clips v JOIN clip_packaging k ON k.clip=v.id AND k.title=v.title
                    WHERE v.id=? AND v.status='review'
                """, (key,)).fetchone()
                if not checked:
                    raise HTTPException(409, "Complete Title + Thumbnail Check for each selected clip before approving.")
                if publish_at and connection.execute("""
                    SELECT 1 FROM creator_candidates c JOIN creator_profiles p ON p.id=c.creator
                    WHERE c.clip=? AND p.disclosure=1
                    UNION ALL SELECT 1 FROM clips WHERE id=? AND substr(library_description,1,4) = '#Ad' || char(10)
                """, (key, key)).fetchone():
                    raise HTTPException(409, "Upload this campaign clip privately and verify the disclosure in YouTube Studio first.")
                updated = connection.execute(
                    "UPDATE clips SET status='approved',channel=?,publish_at=?,detail='' WHERE id=? AND status='review'",
                    (account["channel_id"], publish_at, key),
                ).rowcount
                if not updated:
                    raise HTTPException(409, "A selected clip has already been queued. Refresh and review your selection.")
                connection.execute("UPDATE live_candidates SET approved=NULL WHERE file=?", (key,))
                connection.execute("UPDATE creator_candidates SET selected=NULL WHERE clip=?", (key,))
    WAKE.set()
    result = {"approved": len(set(ids)), "privacy": "private"}
    if publish_at:
        result["publish_at"] = publish_at
    return result


def creator_settings(body):
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    campaign = str(body.get("campaign") or "").strip()
    hashtags = " ".join(str(body.get("hashtags") or "").split())
    disclosure = body.get("disclosure") is True
    mode = body.get("mode")
    if not name or not url or len(name) > 80 or len(url) > 250 or len(campaign) > 500 or len(hashtags) > 200:
        raise HTTPException(400, "Enter a creator name and source URL. Keep tags under 200 characters.")
    if campaign and (urlsplit(campaign).scheme != "https" or not urlsplit(campaign).hostname):
        raise HTTPException(400, "Use an HTTPS campaign link.")
    if mode not in ("review", "private", "schedule") or (disclosure and mode == "schedule"):
        raise HTTPException(400, "Use Private or Review for clips that require Studio disclosures.")
    if mode != "review" and (body.get("certified") is not True or body.get("consent") != mode):
        raise HTTPException(400, "Confirm your rights and the requested YouTube visibility.")
    return name, url, campaign, hashtags, disclosure, mode


def creator_destination(body, mode):
    account = saved_account()
    destination = account["channel_id"] if account else None
    if mode != "review" and (not destination or body.get("channel") != destination):
        raise HTTPException(409, "Confirm the connected YouTube channel.")
    return destination


@app.post("/creator-queue/add")
def add_creator(body: dict):
    name, url, campaign, hashtags, disclosure, mode = creator_settings(body)
    with LOCK:
        destination = creator_destination(body, mode)
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                existing = request(client, "GET", "/creators")["creators"]
                native = next((creator for creator in existing if creator["url"] == url), None)
                if native is None:
                    key = request(client, "POST", "/creators", json={
                        "url": url, "name": name, "auto": False, "language": "en",
                        "caption_style": "clean", "layout": "blur", "hook_background": False,
                    })["id"]
                    native = next(creator for creator in request(client, "GET", "/creators")["creators"] if creator["id"] == key)
                key = native["id"]
                if not re.fullmatch(r"[0-9a-f]{12}", key):
                    raise ValueError("Invalid creator ID.")
                with db() as connection:
                    if connection.execute("SELECT 1 FROM creator_profiles WHERE id=?", (key,)).fetchone():
                        raise HTTPException(409, "This source is already in the creator queue.")
                    (CREATOR_DROP / key).mkdir(exist_ok=True)
                    connection.execute("""
                        INSERT INTO creator_profiles(id,name,url,campaign,hashtags,disclosure,mode,channel,updated)
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (key, name, native["url"], campaign, hashtags, disclosure, mode, destination, time.time()))
                request(client, "POST", f"/creators/{key}/auto", json={"auto": True})
        except (ValueError, httpx.HTTPError) as error:
            raise HTTPException(409, str(error)) from error
    return {"id": key, "mode": mode}


@app.post("/creator-queue/{key}/edit")
def edit_creator(key: str, body: dict):
    name, url, campaign, hashtags, disclosure, mode = creator_settings(body)
    with LOCK, db() as connection:
        destination = creator_destination(body, mode)
        current = connection.execute("SELECT * FROM creator_profiles WHERE id=?", (key,)).fetchone()
        if not current:
            raise HTTPException(404, "Creator not found.")
        if current["updated"] != body.get("version"):
            raise HTTPException(409, "This creator changed while you were editing. Close and reopen the editor.")
        changes_upload = (url, campaign, hashtags, disclosure, mode, destination) != (
            current["url"], current["campaign"], current["hashtags"], bool(current["disclosure"]),
            current["mode"], current["channel"]
        )
        if changes_upload and connection.execute("""
            SELECT 1 FROM clips JOIN creator_candidates c ON c.clip=clips.id
            WHERE c.creator=? AND clips.status='uploading'
        """, (key,)).fetchone():
            raise HTTPException(409, "Wait for this creator's current upload to finish.")
        if url != current["url"] or name != current["name"]:
            try:
                with httpx.Client(timeout=10, trust_env=False) as client:
                    native = request(client, "POST", f"/creators/{key}/edit", json={"url": url, "name": name})
                url = native["url"]
            except (ValueError, httpx.HTTPError) as error:
                raise HTTPException(409, str(error)) from error
        if changes_upload:
            connection.execute("""
                UPDATE clips SET status='review',channel=NULL,publish_at=NULL,detail='Creator settings changed. Review this clip.'
                WHERE id IN (SELECT clip FROM creator_candidates WHERE creator=? AND
                             (selected IS NOT NULL OR clips.status='creator_candidate'))
                  AND status IN ('creator_candidate','approved')
            """, (key,))
        if current["disclosure"] and not disclosure:
            connection.execute("""
                UPDATE clips SET library_description=COALESCE(library_description,'#Ad' || char(10) || title)
                WHERE id IN (SELECT clip FROM creator_candidates WHERE creator=?)
                  AND status='review'
            """, (key,))
        connection.execute("""
            UPDATE creator_profiles SET name=?,url=?,campaign=?,hashtags=?,disclosure=?,mode=?,channel=?,updated=?
            WHERE id=?
        """, (name, url, campaign, hashtags, disclosure, mode, destination, time.time(), key))
    return {"id": key, "mode": mode}


@app.post("/creator-queue/{key}/pause")
def pause_creator(key: str):
    with LOCK, db() as connection:
        if not connection.execute("""
            UPDATE creator_profiles SET mode='review',updated=? WHERE id=?
        """, (time.time(), key)).rowcount:
            raise HTTPException(404, "Creator not found.")
        connection.execute("""
            UPDATE clips SET status='review',publish_at=NULL,detail='Creator automation paused.'
            WHERE id IN (SELECT clip FROM creator_candidates WHERE creator=?)
              AND status IN ('creator_candidate','approved')
        """, (key,))
    return {"ok": True}


@app.post("/live-upload/start")
def start_live_upload(body: dict):
    if body.get("consent") != "upload-this-live-stream-as-private" or body.get("certified") is not True:
        raise HTTPException(400, "Confirm private uploads for this stream and certify your rights to the clips.")
    try:
        url = live_upload.canonical_url(body.get("url"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    cap = body.get("cap")
    hours = body.get("hours")
    if type(cap) is not int or not 1 <= cap <= 100 or type(hours) is not int or not 1 <= hours <= 24:
        raise HTTPException(400, "Choose 1 to 100 uploads per 24 hours and a session of 1 to 24 hours.")
    with LOCK:
        account = saved_account()
        if not account or account["channel_id"] != body.get("channel"):
            raise HTTPException(409, "Confirm the connected YouTube channel.")
        with db() as connection:
            if connection.execute("SELECT 1 FROM live_uploads WHERE status='active'").fetchone():
                raise HTTPException(409, "Stop the current live upload session first.")
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                run_id = request(client, "POST", "/live/start", json={
                    "url": url, "layout": "blur", "resolution": 1080, "caption_style": "clean",
                    "hook_background": False, "before": 24, "after": 14,
                })["live"]
        except (ValueError, httpx.HTTPError) as error:
            raise HTTPException(409, str(error)) from error
        now = time.time()
        with db() as connection:
            connection.execute(
                "INSERT INTO live_uploads VALUES(?,?,?,?,?,'active',?,?)",
                (run_id, url, account["channel_id"], cap, now + hours * 3600,
                 "Starting the live recorder", now + live_upload.WINDOW),
            )
    return {"live": run_id, "privacy": "private"}


@app.post("/live-upload/stop")
def stop_live_upload():
    with LOCK, db() as connection:
        rules = connection.execute("SELECT * FROM live_uploads WHERE status IN ('active','ended')").fetchall()
        if not rules:
            raise HTTPException(409, "There is no automatic live upload session to stop.")
        for rule in rules:
            live_upload.stop(connection, rule["id"], "Automatic uploads stopped. Review this clip manually.")
    with httpx.Client(timeout=10, trust_env=False) as client:
        for rule in rules:
            if rule["status"] == "active":
                try:
                    request(client, "POST", f"/live/{rule['id']}/stop", json={})
                except (ValueError, httpx.HTTPError):
                    return {"stopped": True, "recorder": "check"}
    return {"stopped": True}


@app.post("/retry-source/{key}")
def retry_source(key: str):
    with db() as connection:
        row = connection.execute("SELECT * FROM sources WHERE id=? AND status='error'", (key,)).fetchone()
    if not row:
        raise HTTPException(409, "This source is not waiting for a retry.")
    if row["job"]:
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                request(client, "POST", f"/queue/{row['job']}/retry", json={})
        except (ValueError, httpx.HTTPError):
            raise HTTPException(409, "NeonClipping could not retry the job. Check its local processing queue.")
    with db() as connection:
        connection.execute("UPDATE sources SET status=?,detail='' WHERE id=?",
                           ("processing" if row["job"] else "waiting", key))
    WAKE.set()
    return {"retrying": True}


if __name__ == "__main__":
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", PORT))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, access_log=False))
        server.run(sockets=[listener])
