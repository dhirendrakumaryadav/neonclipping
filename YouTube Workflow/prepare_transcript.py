"""Prepare one local transcript with a cached Whisper model in a separate process."""
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

source = Path(sys.argv[1])
output = Path(sys.argv[2])
models = Path(sys.argv[3])
app = Path(sys.argv[4])
bundled_probe = Path(__file__).resolve().parents[1] / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"
ffprobe = str(bundled_probe) if bundled_probe.is_file() else "ffprobe"
if bundled_probe.is_file():
    os.environ["PATH"] = str(bundled_probe.parent) + os.pathsep + os.environ["PATH"]
os.environ["NEONCLIPPING_CPU"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
sys.path.insert(0, str(app))

from captions import transcribe

for size in ("small", "base"):
    snapshots = models / f"models--Systran--faster-whisper-{size}" / "snapshots"
    model = next((p for p in snapshots.glob("*") if all(
        (p / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
    )), None)
    if model is not None:
        break
if model is None:
    raise SystemExit("No complete local speech model is available.")
duration = float(subprocess.check_output(
    [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(source)],
    text=True, timeout=30,
).strip())
with TemporaryDirectory(dir=output.parent) as tmp:
    words = transcribe(source, 0, duration, Path(tmp), model=str(model))["words"]
output.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
