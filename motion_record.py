#!/usr/bin/env python3
"""Motion-triggered clip recorder for a peephole camera on a Raspberry Pi.

Watches a low-res preview stream for motion; when movement crosses the
threshold it records a fixed-length clip from the full-res stream, then goes
back to watching. Designed to run as a long-lived systemd service.

This is the only process that opens the camera. The web server changes camera
settings by writing settings.json (see camera_settings.py), which is picked up
here, and shows a live view from preview frames this process publishes while
someone is watching.
"""

import logging
import logging.handlers
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
from libcamera import Transform, controls

import camera_settings

# --- Configuration ----------------------------------------------------------
# Tunable values (resolution, clip length, motion threshold, retention, image
# controls...) and their defaults live in camera_settings.py / settings.json.

OUTPUT_DIR = Path("/home/cred/peephole_clips")
LOG_FILE = OUTPUT_DIR / "peephole.log"
SETTINGS_FILE = OUTPUT_DIR / "settings.json"
LIVE_FLAG = OUTPUT_DIR / ".live"             # web server touches this while a viewer is watching
PREVIEW_FILE = OUTPUT_DIR / ".preview.jpg"

DEFAULT_SENSOR_SIZE = (4608, 2592)   # Camera Module 3 (imx708) full pixel array, used for zoom crops
LORES_SIZE = (320, 240)          # resolution used for motion detection only
MOTION_POLL_HZ = 5               # lores frames sampled per second while idle

LIVE_ACTIVE_SECONDS = 5          # preview is published while the flag is this fresh
PREVIEW_INTERVAL = 0.25          # seconds between preview frames
PREVIEW_WIDTH = 800

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
    clips = sorted(folder.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in clips)
    while total > cap_bytes and clips:
        oldest = clips.pop(0)
        total -= oldest.stat().st_size
        log.info("Retention: deleting %s to stay under %.1f GB", oldest.name, cap_gb)
        oldest.unlink(missing_ok=True)
        try:
            oldest.parent.rmdir()  # only succeeds if the day folder is now empty
        except OSError:
            pass


def build_controls(s: dict, sensor_size: tuple[int, int]) -> dict:
    """Translate settings into libcamera controls."""
    full_w, full_h = sensor_size
    crop_w, crop_h = int(full_w / s["zoom"]), int(full_h / s["zoom"])
    crop_x = int((full_w - crop_w) * s["pan_x"])
    crop_y = int((full_h - crop_h) * s["pan_y"])

    c = {
        "Brightness": s["brightness"],
        "Contrast": s["contrast"],
        "Saturation": s["saturation"],
        "Sharpness": s["sharpness"],
        "AeEnable": s["ae_enable"],
        "AwbMode": getattr(controls.AwbModeEnum, s["awb_mode"]),
        "AfMode": controls.AfModeEnum.Manual,
        "LensPosition": s["lens_position"],
        "ScalerCrop": (crop_x, crop_y, crop_w, crop_h),
        "NoiseReductionMode": getattr(controls.draft.NoiseReductionModeEnum, s["noise_reduction"]),
    }
    if s["ae_enable"]:
        c["ExposureValue"] = s["exposure_value"]
    else:
        c["ExposureTime"] = s["exposure_time_us"]
        c["AnalogueGain"] = s["analogue_gain"]
    return c


class Recorder:
    def __init__(self) -> None:
        self.picam2 = Picamera2()
        size = self.picam2.camera_properties.get("PixelArraySize")
        if size is None:  # libcamera lacks static properties for some sensors (e.g. imx708_wide)
            size = DEFAULT_SENSOR_SIZE
            log.warning("Sensor size not reported by libcamera; assuming %s", size)
        self.sensor_size = tuple(size)
        self.settings = camera_settings.load(SETTINGS_FILE)
        self.settings_mtime = self._file_mtime(SETTINGS_FILE)
        self.applied = dict(self.settings)     # what the hardware is currently set to
        self.encoder = H264Encoder()
        self.last_preview = 0.0

    @staticmethod
    def _file_mtime(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    # -- camera setup --

    def _configure(self) -> None:
        w, h = (int(v) for v in self.settings["resolution"].split("x"))
        config = self.picam2.create_video_configuration(
            main={"size": (w, h)},
            lores={"size": LORES_SIZE, "format": "YUV420"},
            transform=Transform(hflip=self.settings["hflip"], vflip=self.settings["vflip"]),
        )
        self.picam2.configure(config)

    def _apply_controls(self) -> None:
        wanted = build_controls(self.settings, self.sensor_size)
        try:
            self.picam2.set_controls(wanted)
        except Exception as exc:  # one control unsupported by this camera/libcamera build
            log.warning("Setting all controls failed (%s); applying one at a time", exc)
            for key, value in wanted.items():
                try:
                    self.picam2.set_controls({key: value})
                except Exception as exc:
                    log.warning("Ignoring control %s=%r: %s", key, value, exc)

    def start(self) -> None:
        self._configure()
        self.picam2.start()
        self._apply_controls()
        self.applied = dict(self.settings)
        log.info("Camera started with settings %s", self.settings)

    def _differs(self, kind: str) -> bool:
        return any(self.settings[f["key"]] != self.applied[f["key"]]
                   for f in camera_settings.SCHEMA if f["apply"] == kind)

    def _mark_applied(self, kind: str) -> None:
        for f in camera_settings.SCHEMA:
            if f["apply"] == kind:
                self.applied[f["key"]] = self.settings[f["key"]]

    # -- per-tick work: settings reload and live preview --

    def poll(self, can_reconfigure: bool) -> bool:
        """Pick up settings changes and publish a preview frame if someone is watching.

        Returns True if the picture changed, so the caller should reset its motion baseline.
        """
        changed = False

        mtime = self._file_mtime(SETTINGS_FILE)
        if mtime != self.settings_mtime:
            self.settings_mtime = mtime
            new = camera_settings.load(SETTINGS_FILE)
            if new != self.settings:
                log.info("Settings changed: %s", {k: v for k, v in new.items() if v != self.settings[k]})
                self.settings = new

        if can_reconfigure and self._differs("restart"):
            log.info("Reconfiguring camera (resolution/orientation changed)")
            self.picam2.stop()
            self._configure()
            self.encoder = H264Encoder()
            self.picam2.start()
            self._apply_controls()
            self._mark_applied("restart")
            self._mark_applied("live")
            changed = True
        elif self._differs("live"):
            self._apply_controls()
            self._mark_applied("live")
            changed = True

        self._publish_preview()
        return changed

    def _publish_preview(self) -> None:
        now = time.monotonic()
        if now - self.last_preview < PREVIEW_INTERVAL:
            return
        mtime = self._file_mtime(LIVE_FLAG)
        if mtime is None or time.time() - mtime > LIVE_ACTIVE_SECONDS:
            return
        self.last_preview = now
        try:
            img = self.picam2.capture_image("main").convert("RGB")
            img.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH))
            tmp = PREVIEW_FILE.with_name(PREVIEW_FILE.name + ".tmp")
            img.save(tmp, "JPEG", quality=70)
            os.replace(tmp, PREVIEW_FILE)
        except Exception as exc:
            log.warning("Preview frame failed: %s", exc)

    def wait(self, seconds: float, can_reconfigure: bool) -> None:
        """Sleep, but keep settings and the live preview serviced."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.poll(can_reconfigure)
            time.sleep(0.1)

    # -- recording --

    def record_clip(self) -> Path:
        duration = self.settings["clip_seconds"]
        now = datetime.now()
        day_dir = OUTPUT_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(exist_ok=True)
        clip_path = day_dir / f"door_{now.strftime('%Y%m%d_%H%M%S')}.mp4"
        self.encoder.output = FfmpegOutput(str(clip_path))

        log.info("Motion detected - recording %s (%ds)", clip_path.name, duration)
        self.picam2.start_encoder(self.encoder)
        try:
            # Live controls and preview keep working; resolution changes wait for the clip to end.
            self.wait(duration, can_reconfigure=False)
        finally:
            self.picam2.stop_encoder()
        log.info("Finished %s", clip_path.name)
        return clip_path


def main() -> None:
    rec = Recorder()
    rec.start()

    w, h = LORES_SIZE
    prev_frame = None
    poll_interval = 1.0 / MOTION_POLL_HZ

    try:
        while True:
            if rec.poll(can_reconfigure=True):
                prev_frame = None  # the picture changed on purpose; don't call that motion

            buf = rec.picam2.capture_buffer("lores")
            # YUV420: the first w*h bytes are the Y (luminance) plane, which is
            # all we need to detect movement cheaply.
            frame = buf[: w * h].reshape(h, w).astype(np.float32)

            if prev_frame is not None:
                mse = float(np.mean((frame - prev_frame) ** 2))
                if mse > rec.settings["motion_threshold"]:
                    rec.record_clip()
                    enforce_retention(OUTPUT_DIR, rec.settings["retention_gb"])
                    rec.wait(rec.settings["cooldown_seconds"], can_reconfigure=True)
                    prev_frame = None
                    continue

            prev_frame = frame
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("Stopping on keyboard interrupt")
    finally:
        rec.picam2.stop()


if __name__ == "__main__":
    main()
