#!/usr/bin/env python3
"""Motion-triggered 30-second clip recorder for a peephole camera on a Raspberry Pi.

Watches a low-res preview stream for motion; when movement crosses the
threshold it records a fixed-length clip from the full-res stream, then goes
back to watching. Designed to run as a long-lived systemd service.
"""

import logging
import logging.handlers
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
from libcamera import controls

# --- Configuration ----------------------------------------------------------

OUTPUT_DIR = Path("/home/cred/peephole_clips")
LOG_FILE = OUTPUT_DIR / "peephole.log"

CLIP_SECONDS = 30
MAIN_SIZE = (1280, 720)          # resolution of the recorded clip
LORES_SIZE = (320, 240)          # resolution used for motion detection only

MOTION_THRESHOLD = 7.0           # mean squared pixel difference to count as motion;
                                  # raise if the hallway light/shadows trigger false
                                  # positives, lower if real motion is being missed
MOTION_POLL_HZ = 5               # lores frames sampled per second while idle
COOLDOWN_SECONDS = 3             # pause after a clip before motion can retrigger

RETENTION_GB = 8.0               # delete oldest clips once this folder exceeds this size
LENS_POSITION = 3.0

# --- Logging -----------------------------------------------------------------

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("peephole")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)
log.addHandler(_console)

_file = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_file.setFormatter(_fmt)
log.addHandler(_file)


def enforce_retention(folder: Path, cap_gb: float) -> None:
    """Delete the oldest clips until the folder is back under cap_gb."""
    cap_bytes = cap_gb * 1024 ** 3
    clips = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in clips)
    while total > cap_bytes and clips:
        oldest = clips.pop(0)
        total -= oldest.stat().st_size
        log.info("Retention: deleting %s to stay under %.1f GB", oldest.name, cap_gb)
        oldest.unlink(missing_ok=True)


def record_clip(picam2: Picamera2, encoder: H264Encoder, duration: int) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_path = OUTPUT_DIR / f"door_{timestamp}.mp4"
    encoder.output = FfmpegOutput(str(clip_path))

    log.info("Motion detected - recording %s (%ds)", clip_path.name, duration)
    picam2.start_encoder(encoder)
    time.sleep(duration)
    picam2.stop_encoder()
    log.info("Finished %s", clip_path.name)
    return clip_path


def main() -> None:
    picam2 = Picamera2()
    config = picam2.create_video_configuration(
        main={"size": MAIN_SIZE},
        lores={"size": LORES_SIZE, "format": "YUV420"},
    )
    picam2.configure(config)
    picam2.start()
    picam2.set_controls({"AfMode": controls.AfModeEnum.Manual,
                         "LensPosition": LENS_POSITION})
    log.info(
        "Camera started (main=%s, lores=%s). Watching for motion, threshold=%.1f",
        MAIN_SIZE, LORES_SIZE, MOTION_THRESHOLD,
    )

    encoder = H264Encoder()
    w, h = LORES_SIZE
    prev_frame = None
    poll_interval = 1.0 / MOTION_POLL_HZ

    try:
        while True:
            buf = picam2.capture_buffer("lores")
            # YUV420: the first w*h bytes are the Y (luminance) plane, which is
            # all we need to detect movement cheaply.
            frame = buf[: w * h].reshape(h, w).astype(np.float32)

            if prev_frame is not None:
                mse = float(np.mean((frame - prev_frame) ** 2))
                if mse > MOTION_THRESHOLD:
                    record_clip(picam2, encoder, CLIP_SECONDS)
                    enforce_retention(OUTPUT_DIR, RETENTION_GB)
                    time.sleep(COOLDOWN_SECONDS)
                    prev_frame = None
                    continue

            prev_frame = frame
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("Stopping on keyboard interrupt")
    finally:
        picam2.stop()


if __name__ == "__main__":
    main()
