"""Parse point-by-point audience retention exports without contacting YouTube."""
import csv
import io
import math
import re


def header_key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


AXES = {
    "elapsedvideotimeratio": "ratio",
    "elapsedvideotimepercentage": "percent",
    "videotimepercentage": "percent",
    "videopositionpercentage": "percent",
    "videoprogress": "percent",
    "elapsedtimepercentage": "percent",
    "percentageofvideo": "percent",
    "videopercentage": "percent",
    "percentage": "percent",
    "elapsedtime": "time",
    "time": "time",
    "times": "time",
    "timeseconds": "time",
    "elapsedtimeseconds": "time",
    "seconds": "time",
    "videotime": "time",
    "timestamp": "time",
    "position": "time",
}
METRICS = {
    "audiencewatchratio": "ratio",
    "audienceretentionpercentage": "percent",
    "retentionpercentage": "percent",
    "audienceretention": "percent",
    "retention": "percent",
    "absoluteaudienceretention": "percent",
    "viewersstillwatching": "percent",
    "percentageofviewers": "percent",
    "watchpercentage": "percent",
}


def read_number(value):
    token = value.strip().replace("\u00a0", "").replace("\u202f", "")
    if token.endswith("%"):
        token = token[:-1].strip()
    if "," in token and "." not in token:
        token = token.replace(",", ".")
    value = float(token)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite nonnegative number.")
    return value


def timestamp(value):
    if ":" not in value:
        return read_number(value)
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid timestamp.")
    values = [read_number(part) for part in parts]
    if any(part >= 60 for part in values[1:]):
        raise ValueError("Invalid timestamp.")
    return sum(part * 60 ** i for i, part in enumerate(reversed(values)))


def parse_retention(content):
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = content.decode("utf-16")
    else:
        text = content.decode("utf-8-sig")
    for delim in (",", "\t", ";"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        for index, row in enumerate(rows[:25]):
            names = [header_key(cell) for cell in row]
            axis = next((i for i, key in enumerate(names) if key in AXES), None)
            metric = next((i for i, key in enumerate(names) if key in METRICS), None)
            if axis is not None and metric is not None:
                break
        else:
            continue

        axis_kind = AXES[names[axis]]
        metric_kind = METRICS[names[metric]]
        data_rows = rows[index + 1:]
        if "%" in row[axis] or (axis_kind == "time" and any(
            len(r) > axis and r[axis].strip().endswith("%") for r in data_rows
        )):
            axis_kind = "percent"
        unit = "seconds" if axis_kind == "time" else "percent"
        timeline = {}
        for r in data_rows:
            if len(r) <= max(axis, metric):
                continue
            x_text, y_text = r[axis], r[metric]
            try:
                x = timestamp(x_text) if unit == "seconds" else read_number(x_text)
                y = read_number(y_text)
            except ValueError:
                continue
            if axis_kind == "ratio" and not x_text.strip().endswith("%"):
                x *= 100
            if metric_kind == "ratio" and not y_text.strip().endswith("%"):
                y *= 100
            if (unit == "percent" and x > 100) or y > 100000:
                raise ValueError("The CSV contains a position or retention value outside its expected range.")
            if x in timeline and timeline[x] != y:
                raise ValueError("This CSV has multiple values at the same position. Export a single video's retention curve.")
            timeline[x] = y
        if len(timeline) < 3:
            raise ValueError("At least three valid audience-retention points are needed.")
        return unit, sorted(timeline.items())
    raise ValueError("Use a video-level audience-retention CSV with a time or progress column and a retention column.")


def retention_summary(content, duration=None):
    unit, points = parse_retention(content)
    if duration is not None and (not math.isfinite(duration) or duration <= 0 or duration > 86400):
        raise ValueError("Enter a video length greater than 0 and up to 86,400 seconds.")
    if unit == "percent" and duration is not None:
        points = [(x * duration / 100, y) for x, y in points]
        unit = "seconds"
    # Use an opening segment that stays meaningful for short videos.
    opening_cutoff = min(30, (duration or points[-1][0]) * .20) if unit == "seconds" else 20
    opening_end = next((i for i, (x, _) in enumerate(points) if x >= opening_cutoff), None)
    opening = None
    if opening_end and points[opening_end - 1][0] <= opening_cutoff:
        a, b = points[opening_end - 1], points[opening_end]
        proportion = (opening_cutoff - a[0]) / (b[0] - a[0])
        y = a[1] + proportion * (b[1] - a[1])
        opening = {"at": opening_cutoff, "change": y - points[0][1]}
    drops = sorted(
        ({"from": points[i-1][0], "to": x, "loss": points[i-1][1] - y, "index": i}
         for i, (x, y) in enumerate(points[1:], start=1) if points[i-1][1] - y >= .1),
        key=lambda drop: drop["loss"], reverse=True,
    )
    return {"unit": unit, "points": points, "opening": opening, "drops": drops[:3]}
