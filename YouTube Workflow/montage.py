"""Assemble trimmed local clips into a single vertical montage."""
import json
import math
import re
import subprocess
import threading
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from commentary_take import run, tool

CLIP_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_SECONDS = 180
MAX_PARTS = 12
MAX_VIDEO = 200 * 1024 * 1024
MAX_AUDIO = 25 * 1024 * 1024


def probe(root, path):
    result = run([tool(root, "ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
                  "-show_streams", "-show_format", "-of", "json", str(path)], path.parent, timeout=20)
    meta = json.loads(result)
    try:
        duration = float(meta["format"]["duration"])
        streams = {stream["codec_type"] for stream in meta["streams"]}
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except (TypeError, KeyError, ValueError) as error:
        raise ValueError("Could not read the length of this media file.") from error
    return duration, streams


def montage_args(body, source, root, assets):
    parts = body.get("parts")
    title = body.get("title")
    caption = body.get("caption", "")
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARTS:
        raise ValueError("Add between 2 and 12 clips.")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or re.search(r"[\x00-\x1f<>]", title):
        raise ValueError("Enter a title of 1–100 characters.")
    if not isinstance(caption, str) or len(caption.encode("utf-8")) > 5000:
        raise ValueError("Keep the description at 5,000 bytes or fewer.")
    prepared = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Check each clip and its trim settings.")
        path = source(part.get("id"))
        duration, streams = probe(root, path)
        try:
            start, end = float(part["start"]), float(part["end"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Enter a start and end in seconds for each clip.") from error
        if not ("video" in streams and math.isfinite(start) and math.isfinite(end) and
                0 <= start < end <= duration + .05 and min(end, duration) - start >= .25):
            raise ValueError("Each trim must be at least a quarter second and within its source clip.")
        layout = part.get("layout", "fit")
        if layout not in ("fit", "fill"):
            raise ValueError("Choose Fit or Fill for each clip.")
        prepared.append((path, start, min(end, duration) - start, "audio" in streams, layout))
    if sum(part[2] for part in prepared) > MAX_SECONDS:
        raise ValueError("Keep the combined Short at three minutes or less.")
    music = None
    volume = 0
    if body.get("music"):
        key = body["music"]
        if not isinstance(key, str) or not CLIP_ID.fullmatch(key):
            raise ValueError("Choose a valid music file.")
        music = assets / f"{key}.audio"
        if not music.is_file():
            raise ValueError("The music file is missing.")
        try:
            volume = float(body.get("music_volume", .14))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Choose a valid music volume.") from error
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Choose a music volume between 0 and 100%.")
    return prepared, title.strip(), caption.strip(), music, volume


def render(root, data, work, job, parts, title, caption, music, volume, progress):
    ffmpeg = tool(root, "ffmpeg")
    dimensions = "1080:1920"
    for index, (path, start, duration, has_audio, layout) in enumerate(parts):
        progress(f"Rendering clip {index + 1} of {len(parts)}")
        if layout == "fill":
            framing = f"scale={dimensions}:force_original_aspect_ratio=increase,crop={dimensions}"
        else:
            framing = f"scale={dimensions}:force_original_aspect_ratio=decrease,pad={dimensions}:(ow-iw)/2:(oh-ih)/2"
        command = [ffmpeg, "-v", "error", "-nostdin", "-ss", str(start),
                   "-protocol_whitelist", "file,pipe", "-i", str(path)]
        if not has_audio:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        command += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", framing + ",setsar=1,fps=30",
                    "-af", "aformat=sample_rates=48000:channel_layouts=stereo,apad",
                    "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", f"part{index:02d}.mp4"]
        run(command, work, timeout=480)
    (work / "parts.txt").write_text("".join(f"file part{i:02d}.mp4\n" for i in range(len(parts))), encoding="ascii")
    progress("Joining clips and mixing audio")
    command = [ffmpeg, "-v", "error", "-nostdin", "-f", "concat", "-safe", "1", "-i", "parts.txt"]
    if music and volume > 0:
        command += ["-stream_loop", "-1", "-protocol_whitelist", "file,pipe", "-i", str(music),
                    "-filter_complex",
                    f"[1:a]volume={volume:.3f}[music];[0:a][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=.95:level=0:latency=1[a]",
                    "-map", "0:v:0", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", "0:v:0", "-map", "0:a:0", "-c:a", "copy"]
    command += ["-c:v", "copy", "-movflags", "+faststart", "montage.mp4"]
    run(command, work, timeout=240)
    duration, streams = probe(root, work / "montage.mp4")
    if streams != {"video", "audio"}:
        raise ValueError("The combined clip did not render with video and audio.")
    result = {"status": "done", "file": job, "meta": {"w": 1080, "h": 1920, "duration": duration}}
    record = {"source": job, "settings": {
        "id": job, "start": 0, "end": duration, "title": title, "post_caption": caption,
        "layout": "fill", "top": .32, "caption_style": "clean", "captions": False,
        "aspect": "vertical", "resolution": 1080, "fps": 30,
        "cam": {"x": 0, "y": 0, "w": .25, "h": .25}, "game": {"x": 0, "y": 0, "w": 1, "h": 1},
    }, "result": result}
    (work / "record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    (work / "montage.mp4").replace(data / f"{job}.mp4")
    (work / "record.json").replace(data / f"{job}.json")
    return result


def attach(app, root, data, here):
    assets = data / "Montage sources"
    jobs = {}
    active = threading.Lock()

    def source(key):
        if not isinstance(key, str) or not CLIP_ID.fullmatch(key):
            raise ValueError("Choose a valid clip.")
        uploaded = assets / f"{key}.video"
        if uploaded.is_file():
            return uploaded
        clip = data / f"{key}.mp4"
        if (data / f"{key}.json").is_file() and clip.is_file():
            return clip
        raise ValueError("One of the source clips is missing.")

    @app.get("/montage.js")
    def script():
        return FileResponse(here / "montage.js", media_type="text/javascript")

    @app.get("/montage/source/{key}")
    def preview_source(key: str):
        try:
            path = source(key)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return FileResponse(path, media_type="video/mp4")

    @app.post("/montage/asset/{kind}")
    def upload_asset(kind: str, file: UploadFile = File(...)):
        if kind not in ("video", "audio"):
            raise HTTPException(400, "Choose a video or audio file.")
        assets.mkdir(parents=True, exist_ok=True)
        key = uuid.uuid4().hex
        limit = MAX_VIDEO if kind == "video" else MAX_AUDIO
        with TemporaryDirectory(dir=assets, prefix="upload-") as temp:
            scratch = Path(temp) / "media"
            size = 0
            with scratch.open("wb") as stream:
                while block := file.file.read(1024 * 1024):
                    size += len(block)
                    if size > limit:
                        raise HTTPException(400, f"Choose a {kind} file under {limit // 1024 // 1024} MB.")
                    stream.write(block)
            try:
                duration, streams = probe(root, scratch)
                if kind not in streams:
                    raise ValueError()
            except (ValueError, OSError, subprocess.SubprocessError):
                raise HTTPException(400, f"Choose a readable {kind} file.") from None
            scratch.replace(assets / f"{key}.{kind}")
        return {"id": key, "title": Path(file.filename or kind).stem[:120], "duration": duration}

    @app.post("/montage/render")
    def start(body: dict):
        if not active.acquire(blocking=False):
            raise HTTPException(409, "A combined video is already rendering.")
        try:
            prepared, title, caption, music, volume = montage_args(body, source, root, assets)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            active.release()
            raise HTTPException(400, str(error)) from error
        job = uuid.uuid4().hex
        jobs[job] = {"status": "rendering", "detail": "Starting export"}

        def work():
            try:
                with TemporaryDirectory(dir=data, prefix="montage-") as temp:
                    result = render(root, data, Path(temp), job, prepared, title, caption, music, volume,
                                    lambda detail: jobs[job].update(detail=detail))
                    jobs[job] = result
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                jobs[job] = {"status": "error", "detail": str(error)}
            finally:
                active.release()

        threading.Thread(target=work, daemon=True).start()
        return {"job": job}

    @app.get("/montage/render/{job}")
    def status(job: str):
        if job not in jobs:
            raise HTTPException(404, "Montage export not found.")
        return jobs[job]
