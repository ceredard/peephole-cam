# Peephole camera

Motion-triggered recorder for a Raspberry Pi 3B + CSI camera (UC-439) mounted
in a door peephole. Watches continuously at low resolution; when it sees
motion it records a 30-second clip at 1280x720 to `/home/pi/peephole_clips`,
then goes back to watching.

## Hardware setup

1. Flash **Raspberry Pi OS Bookworm (Lite recommended** — the Pi 3B only has
   1GB RAM, and you don't need a desktop for this).
2. Connect the UC-439 camera to the CSI port with the ribbon cable, blue side
   facing the correct direction per the camera's instructions.
3. Boot the Pi, then check the camera is detected:
   ```
   libcamera-hello --list-cameras
   ```
   "UC-439" is a seller's product code, not a sensor name — these generic CSI
   modules are usually OV5647 or IMX219 based clones. If `libcamera-hello`
   doesn't find it, you likely need to tell the kernel which sensor driver to
   load by adding a line like `dtoverlay=ov5647` (or `imx219`, whichever
   matches your module) to `/boot/firmware/config.txt`, then reboot. Check the
   camera's listing/manual for the actual sensor chip if unsure.
4. Mount the camera at the peephole. Check the lens' focus distance matches
   your door's thickness before you commit to a permanent mount — many of
   these modules ship with a fixed-focus wide-angle lens meant for a specific
   distance range.

## Software setup

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-numpy ffmpeg --no-install-recommends
```

`picamera2` depends on system `libcamera` bindings, so install it via `apt`
(as above), not `pip`. If you want a virtualenv for anything else in this
project, create it with `--system-site-packages` so it can still see
picamera2.

Copy this folder to the Pi, e.g. `/home/pi/peephole-cam`.

### Test it manually first

```bash
cd /home/pi/peephole-cam
python3 motion_record.py
```

Wave a hand in front of the camera from a few feet away and confirm a clip
shows up in `/home/pi/peephole_clips`. Watch the console log while you do
this — it logs every time motion crosses the threshold.

Tune `MOTION_THRESHOLD` in `motion_record.py`:
- Clips triggering on hallway lighting changes / nothing happening → raise it.
- Real motion (someone approaching the door) not triggering a clip → lower it.

### Run it as a service

```bash
sudo cp peephole-cam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peephole-cam
```

Check it's running and watch logs:
```bash
sudo systemctl status peephole-cam
journalctl -u peephole-cam -f
```

## Viewing clips in a browser

`web_server.py` is a small Flask site that lists the clips by day with
thumbnails, plays them in the browser (seeking works), and lets you download
or delete them.

```bash
sudo apt install -y python3-flask
cd /home/pi/peephole-cam
printf 'PEEPHOLE_PASSWORD=choose-a-long-password\n' > web.env   # optional: PEEPHOLE_USER=name (default "pi")
chmod 600 web.env
sudo cp peephole-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peephole-web
```

Then open `http://<pi-hostname-or-ip>:8080/` (e.g. `http://raspberrypi.local:8080/`)
from any device on your network and log in. The server refuses to start
without a password.

- It is plain HTTP, so only use it on your home network. Don't port-forward
  it to the internet; for remote access use a VPN such as Tailscale or WireGuard.
- A clip that is still being recorded is hidden until it's been idle for 15s.
- Deleting in the UI removes the file from the Pi. Thumbnails are cached in
  `/home/pi/peephole_clips/.thumbs` and cleaned up automatically.

## Storage

Clips are H264-encoded 720p MP4s (small — a 30s clip is typically a few MB).
`RETENTION_GB` in `motion_record.py` (default 8GB) automatically deletes the
oldest clips once the folder passes that size, so the SD card won't fill up
unattended. Adjust it to whatever headroom your card has.

## A note on recording a shared hallway

If your peephole looks out onto a shared hallway/landing rather than only
your own doorway, check your building's rules and local law around recording
common areas before leaving this running long-term — requirements vary a lot
by location.
