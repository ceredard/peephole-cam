"""Camera/recorder settings shared by motion_record.py and web_server.py.

The web server writes settings.json; the recorder (the only process that can
open the camera) reads it and applies it. This module is the single source of
truth for what settings exist, their ranges and defaults, so the web form and
the recorder can never disagree. It must not import picamera2/flask.

"apply" says when a change takes effect:
  live    - set on the running camera immediately, even mid-clip
  restart - needs a camera reconfigure, done between clips
  app     - recorder behaviour, read whenever it is needed
"""

import json
import os
from pathlib import Path

RESOLUTIONS = ["1280x720", "1536x864", "1920x1080"]   # all 16:9 so zoom/crop never distorts
AWB_MODES = ["Auto", "Tungsten", "Fluorescent", "Indoor", "Daylight", "Cloudy"]
NOISE_MODES = ["Off", "Fast", "HighQuality"]

SCHEMA = [
    # Image
    {"key": "brightness", "label": "Brightness", "group": "Image", "type": "float",
     "min": -1.0, "max": 1.0, "step": 0.05, "default": 0.0, "apply": "live"},
    {"key": "contrast", "label": "Contrast", "group": "Image", "type": "float",
     "min": 0.0, "max": 4.0, "step": 0.05, "default": 1.0, "apply": "live"},
    {"key": "saturation", "label": "Saturation", "group": "Image", "type": "float",
     "min": 0.0, "max": 4.0, "step": 0.05, "default": 1.0, "apply": "live"},
    {"key": "sharpness", "label": "Sharpness", "group": "Image", "type": "float",
     "min": 0.0, "max": 8.0, "step": 0.1, "default": 1.0, "apply": "live"},
    {"key": "noise_reduction", "label": "Noise reduction", "group": "Image", "type": "enum",
     "options": NOISE_MODES, "default": "Fast", "apply": "live"},
    # Exposure & colour
    {"key": "ae_enable", "label": "Auto exposure", "group": "Exposure & colour", "type": "bool",
     "default": True, "apply": "live"},
    {"key": "exposure_value", "label": "Exposure compensation (auto)", "group": "Exposure & colour",
     "type": "float", "min": -4.0, "max": 4.0, "step": 0.1, "default": 0.0, "apply": "live"},
    {"key": "exposure_time_us", "label": "Exposure time µs (manual)", "group": "Exposure & colour",
     "type": "int", "min": 100, "max": 100000, "step": 100, "default": 20000, "apply": "live"},
    {"key": "analogue_gain", "label": "Gain (manual)", "group": "Exposure & colour",
     "type": "float", "min": 1.0, "max": 16.0, "step": 0.1, "default": 1.0, "apply": "live"},
    {"key": "awb_mode", "label": "White balance", "group": "Exposure & colour", "type": "enum",
     "options": AWB_MODES, "default": "Auto", "apply": "live"},
    # Lens
    {"key": "lens_position", "label": "Focus (dioptres, higher = closer)", "group": "Lens & framing",
     "type": "float", "min": 0.0, "max": 10.0, "step": 0.1, "default": 3.0, "apply": "live"},
    {"key": "zoom", "label": "Zoom", "group": "Lens & framing", "type": "float",
     "min": 1.0, "max": 8.0, "step": 0.1, "default": 1.0, "apply": "live"},
    {"key": "pan_x", "label": "Pan left/right (when zoomed)", "group": "Lens & framing", "type": "float",
     "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.5, "apply": "live"},
    {"key": "pan_y", "label": "Pan up/down (when zoomed)", "group": "Lens & framing", "type": "float",
     "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.5, "apply": "live"},
    {"key": "hflip", "label": "Mirror horizontally", "group": "Lens & framing", "type": "bool",
     "default": False, "apply": "restart"},
    {"key": "vflip", "label": "Flip vertically", "group": "Lens & framing", "type": "bool",
     "default": False, "apply": "restart"},
    # Recording
    {"key": "resolution", "label": "Clip resolution", "group": "Recording", "type": "enum",
     "options": RESOLUTIONS, "default": "1280x720", "apply": "restart"},
    {"key": "clip_seconds", "label": "Clip length (s)", "group": "Recording", "type": "int",
     "min": 5, "max": 300, "step": 1, "default": 30, "apply": "app"},
    {"key": "motion_threshold", "label": "Motion threshold (raise if shadows/lights trigger it)",
     "group": "Recording", "type": "float", "min": 0.5, "max": 100.0, "step": 0.5, "default": 7.0,
     "apply": "app"},
    {"key": "cooldown_seconds", "label": "Cooldown after clip (s)", "group": "Recording", "type": "int",
     "min": 0, "max": 60, "step": 1, "default": 3, "apply": "app"},
    {"key": "retention_gb", "label": "Keep at most (GB)", "group": "Recording", "type": "float",
     "min": 0.5, "max": 500.0, "step": 0.5, "default": 8.0, "apply": "app"},
]

_BY_KEY = {f["key"]: f for f in SCHEMA}


def defaults() -> dict:
    return {f["key"]: f["default"] for f in SCHEMA}


def clean(raw) -> dict:
    """Return a complete, valid settings dict: unknown keys dropped, bad values defaulted, numbers clamped."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        f = _BY_KEY.get(key)
        if f is None:
            continue
        try:
            if f["type"] == "bool":
                if not isinstance(value, bool):
                    continue
                out[key] = value
            elif f["type"] == "enum":
                if value in f["options"]:
                    out[key] = value
            else:
                if isinstance(value, bool):
                    continue
                num = float(value)
                if num != num:  # NaN
                    continue
                num = min(max(num, f["min"]), f["max"])
                out[key] = int(round(num)) if f["type"] == "int" else num
        except (TypeError, ValueError):
            continue
    return out


def load(path: Path) -> dict:
    try:
        return clean(json.loads(path.read_text()))
    except (OSError, ValueError):
        return defaults()


def save(path: Path, settings: dict) -> dict:
    """Validate and atomically write settings; returns what was stored."""
    settings = clean(settings)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    os.replace(tmp, path)
    return settings
