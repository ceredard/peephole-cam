#!/usr/bin/env python3
"""Web UI for browsing and playing the clips recorded by motion_record.py.

Runs as its own long-lived service next to the recorder. Reads the same
OUTPUT_DIR; never talks to the camera. Protected by HTTP basic auth because the
clips show who comes to your door.
"""

import hmac
import os
import re
import subprocess
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template_string, request,
                   send_from_directory, url_for)

import camera_settings

# --- Configuration ----------------------------------------------------------

CLIPS_DIR = Path("/home/cred/peephole_clips")
THUMBS_DIR = CLIPS_DIR / ".thumbs"
SETTINGS_FILE = CLIPS_DIR / "settings.json"   # read by motion_record.py
LIVE_FLAG = CLIPS_DIR / ".live"               # touched while a live view is open
PREVIEW_FILE = CLIPS_DIR / ".preview.jpg"     # written by motion_record.py
HOST = "0.0.0.0"
PORT = 8080

USERNAME = os.environ.get("PEEPHOLE_USER", "pi")
PASSWORD = os.environ.get("PEEPHOLE_PASSWORD", "")

IN_PROGRESS_SECONDS = 15         # a file modified this recently is still being recorded

CLIP_RE = re.compile(r"^door_\d{8}_\d{6}\.mp4$")

if not PASSWORD:
    raise SystemExit("Set PEEPHOLE_PASSWORD (see README) - refusing to serve clips without a password.")

THUMBS_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)


# --- Auth --------------------------------------------------------------------

def requires_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest(auth.username or "", USERNAME)
            and hmac.compare_digest(auth.password or "", PASSWORD)
        )
        if not ok:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Peephole cam"'})
        return view(*args, **kwargs)
    return wrapped


# --- Clip helpers ------------------------------------------------------------

def day_dir_for(name: str) -> Path:
    """Day folder (YYYY-MM-DD) a clip lives in, derived from its filename."""
    d = name[5:13]
    return CLIPS_DIR / f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def migrate_flat_clips() -> None:
    """Move clips recorded before per-day folders existed into their day folder."""
    now = datetime.now().timestamp()
    for p in CLIPS_DIR.glob("door_*.mp4"):
        if not CLIP_RE.match(p.name) or now - p.stat().st_mtime < IN_PROGRESS_SECONDS:
            continue
        target = day_dir_for(p.name)
        target.mkdir(exist_ok=True)
        p.rename(target / p.name)


def remove_empty_day_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def list_clips() -> list[dict]:
    now = datetime.now().timestamp()
    clips = []
    for p in CLIPS_DIR.glob("*/door_*.mp4"):
        if not CLIP_RE.match(p.name):
            continue
        st = p.stat()
        if now - st.st_mtime < IN_PROGRESS_SECONDS:
            continue
        try:
            when = datetime.strptime(p.name[5:-4], "%Y%m%d_%H%M%S")
        except ValueError:
            when = datetime.fromtimestamp(st.st_mtime)
        clips.append({"name": p.name, "when": when, "mb": st.st_size / 1024 ** 2})
    clips.sort(key=lambda c: c["when"], reverse=True)
    return clips


def clip_path(name: str) -> Path:
    """Resolve a user-supplied clip name, rejecting anything but our own filenames."""
    if not CLIP_RE.match(name):
        abort(404)
    path = day_dir_for(name) / name
    if not path.is_file():
        abort(404)
    return path


def prune_thumbs() -> None:
    """Drop thumbnails whose clip was removed by the recorder's retention policy."""
    for t in THUMBS_DIR.glob("*.jpg"):
        if not (day_dir_for(t.stem + ".mp4") / (t.stem + ".mp4")).exists():
            t.unlink(missing_ok=True)


# --- Views -------------------------------------------------------------------

STYLE = """
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --card:#f2f2f4; --muted:#777; --accent:#2a6df4; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#eee; --card:#1d1d20; --muted:#999; --accent:#6a9cff; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px system-ui, sans-serif; }
  header { padding:16px; display:flex; justify-content:space-between; align-items:baseline; gap:16px; flex-wrap:wrap; }
  h1 { margin:0; font-size:1.25rem; }
  nav.tabs { display:flex; gap:16px; flex:1; }
  nav.tabs a { color:var(--muted); padding-bottom:2px; }
  nav.tabs a.active { color:var(--fg); border-bottom:2px solid var(--accent); }
  .muted { color:var(--muted); font-size:.875rem; }
  main { padding:0 16px 32px; max-width:1100px; margin:0 auto; }
  h2 { font-size:1rem; margin:24px 0 8px; color:var(--muted); font-weight:600; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:12px; }
  .card { background:var(--card); border-radius:8px; overflow:hidden; }
  .card a.thumb { display:block; aspect-ratio:16/9; background:#000; }
  .card img { width:100%; height:100%; object-fit:cover; display:block; }
  .meta { padding:8px 10px; display:flex; justify-content:space-between; align-items:center; gap:8px; }
  a { color:var(--accent); text-decoration:none; }
  button { font:inherit; background:none; border:0; color:#d33; cursor:pointer; padding:4px; }
  video { width:100%; max-height:70vh; background:#000; border-radius:8px; }
  .empty { padding:48px 0; text-align:center; }
  .live { display:grid; grid-template-columns:minmax(0, 2fr) minmax(260px, 1fr); gap:16px; align-items:start; }
  @media (max-width: 800px) { .live { grid-template-columns:1fr; } }
  .live img.view { width:100%; aspect-ratio:16/9; background:#000; border-radius:8px; display:block; }
  #settings { background:var(--card); border-radius:8px; padding:4px 12px 12px; }
  #settings fieldset { border:0; padding:0; margin:12px 0 0; }
  #settings legend { color:var(--muted); font-weight:600; font-size:.875rem; padding:0; margin-bottom:4px; }
  #settings label { display:block; margin:8px 0; font-size:.875rem; }
  #settings .row { display:flex; align-items:center; gap:8px; }
  #settings input[type=range] { flex:1; min-width:0; }
  #settings output { min-width:3.5em; text-align:right; font-variant-numeric:tabular-nums; }
  #settings select { font:inherit; width:100%; }
  .tools { display:flex; justify-content:space-between; align-items:center; padding-top:12px; }
  .tools button { color:var(--accent); }
"""

NAV = """<nav class="tabs">
  <a href="{{ url_for('index') }}" class="{{ 'active' if tab == 'clips' }}">Clips</a>
  <a href="{{ url_for('live') }}" class="{{ 'active' if tab == 'live' }}">Live &amp; settings</a>
</nav>"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Peephole cam</title>
<style>{{ style|safe }}</style></head><body>
<header><h1><a href="{{ url_for('index') }}" style="color:inherit">Peephole cam</a></h1>
""" + NAV + """
<span class="muted">{{ clips|length }} clip{{ '' if clips|length == 1 else 's' }}
{% if clips %}
  <form method="post" action="{{ url_for('delete_all') }}" style="display:inline;margin-left:12px"
        onsubmit="return confirm('Delete ALL {{ clips|length }} recordings? This cannot be undone.')"><button title="Delete all recordings">Delete all</button></form>
{% endif %}
</span></header>
<main>
{% if current %}
  <video src="{{ url_for('media', name=current.name) }}" controls autoplay playsinline></video>
  <div class="meta">
    {% if older %}<a id="prev" href="{{ url_for('index', clip=older.name) }}" title="Older (left arrow)">&#8249; Older</a>{% else %}<span class="muted">&#8249; Older</span>{% endif %}
    <strong>{{ current.when.strftime('%A %d %B %Y, %H:%M:%S') }}</strong>
    {% if newer %}<a id="next" href="{{ url_for('index', clip=newer.name) }}" title="Newer (right arrow)">Newer &#8250;</a>{% else %}<span class="muted">Newer &#8250;</span>{% endif %}
  </div>
  <div class="meta"><span></span>
    <a href="{{ url_for('media', name=current.name) }}" download>Download</a>
  </div>
  <script>
    document.addEventListener('keydown', e => {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const link = document.getElementById(e.key === 'ArrowLeft' ? 'prev' : e.key === 'ArrowRight' ? 'next' : '');
      if (link) { e.preventDefault(); location.href = link.href; }
    }, true);
    let sx = null;
    const v = document.querySelector('video');
    v.addEventListener('touchstart', e => { sx = e.touches[0].clientX; }, {passive: true});
    v.addEventListener('touchend', e => {
      if (sx === null) return;
      const dx = e.changedTouches[0].clientX - sx; sx = null;
      const link = document.getElementById(dx > 80 ? 'prev' : dx < -80 ? 'next' : '');
      if (link) location.href = link.href;
    }, {passive: true});
  </script>
{% endif %}
{% if not clips %}
  <p class="empty muted">No clips yet.</p>
{% endif %}
{% for day, items in days %}
  <h2>{{ day }}</h2>
  <div class="grid">
  {% for c in items %}
    <div class="card">
      <a class="thumb" href="{{ url_for('index', clip=c.name) }}">
        <img loading="lazy" src="{{ url_for('thumb', name=c.name) }}" alt="">
      </a>
      <div class="meta">
        <span>{{ c.when.strftime('%H:%M:%S') }} <span class="muted">{{ '%.1f'|format(c.mb) }} MB</span></span>
        <form method="post" action="{{ url_for('delete', name=c.name) }}"
              onsubmit="return confirm('Delete this clip?')"><button title="Delete">&#128465;</button></form>
      </div>
    </div>
  {% endfor %}
  </div>
{% endfor %}
</main></body></html>
"""


@app.get("/")
@requires_auth
def index():
    clips = list_clips()
    days: list[tuple[str, list[dict]]] = []
    for c in clips:
        label = c["when"].strftime("%A %d %B %Y")
        if not days or days[-1][0] != label:
            days.append((label, []))
        days[-1][1].append(c)

    current = older = newer = None
    selected = request.args.get("clip", "")
    if selected:
        # clips are sorted newest first, so the next index is the older clip
        i = next((n for n, c in enumerate(clips) if c["name"] == selected), None)
        if i is not None:
            current = clips[i]
            newer = clips[i - 1] if i > 0 else None
            older = clips[i + 1] if i + 1 < len(clips) else None
    return render_template_string(PAGE, clips=clips, days=days, current=current, older=older,
                                  newer=newer, style=STYLE, tab="clips")


LIVE_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Peephole cam - live</title>
<style>{{ style|safe }}</style></head><body>
<header><h1><a href="{{ url_for('index') }}" style="color:inherit">Peephole cam</a></h1>
""" + NAV + """
<span class="muted" id="status"></span></header>
<main><div class="live">
  <div>
    <img class="view" src="{{ url_for('live_stream') }}" alt="Live view">
    <p class="muted">Live view is a few frames per second and lower resolution than the recorded clips.
    Settings marked &#8635; apply after the current clip finishes.</p>
  </div>
  <form id="settings" onsubmit="return false">
  {% for group, fields in groups %}
    <fieldset><legend>{{ group }}</legend>
    {% for f in fields %}
      <label>{{ f.label }}{{ ' ↻' if f.apply == 'restart' }}
      {% if f.type == 'bool' %}
        <input type="checkbox" name="{{ f.key }}" {{ 'checked' if values[f.key] }}>
      {% elif f.type == 'enum' %}
        <select name="{{ f.key }}">
        {% for o in f.options %}<option {{ 'selected' if values[f.key] == o }}>{{ o }}</option>{% endfor %}
        </select>
      {% else %}
        <span class="row">
          <input type="range" name="{{ f.key }}" min="{{ f.min }}" max="{{ f.max }}" step="{{ f.step }}" value="{{ values[f.key] }}">
          <output>{{ values[f.key] }}</output>
        </span>
      {% endif %}
      </label>
    {% endfor %}
    </fieldset>
  {% endfor %}
    <div class="tools"><button type="button" id="reset">Reset to defaults</button></div>
  </form>
</div></main>
<script>
  const form = document.getElementById('settings');
  const status = document.getElementById('status');
  let timer;

  function collect() {
    const out = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      out[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'range' ? Number(el.value) : el.value;
    }
    return out;
  }

  function fill(values) {
    for (const el of form.elements) {
      if (!el.name || !(el.name in values)) continue;
      if (el.type === 'checkbox') el.checked = values[el.name];
      else el.value = values[el.name];
      if (el.type === 'range') el.nextElementSibling.value = el.value;
    }
  }

  async function post(url, body) {
    status.textContent = 'Saving…';
    try {
      const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                   body: JSON.stringify(body || {})});
      if (!r.ok) throw new Error(r.status);
      status.textContent = 'Saved';
      return await r.json();
    } catch (e) {
      status.textContent = 'Save failed';
    }
  }

  form.addEventListener('input', (e) => {
    if (e.target.type === 'range') e.target.nextElementSibling.value = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => post("{{ url_for('save_settings') }}", collect()), 150);
  });

  document.getElementById('reset').addEventListener('click', async () => {
    if (!confirm('Reset all camera settings to defaults?')) return;
    const values = await post("{{ url_for('reset_settings') }}");
    if (values) fill(values);
  });
</script>
</body></html>
"""


@app.get("/live")
@requires_auth
def live():
    groups: dict[str, list[dict]] = {}
    for f in camera_settings.SCHEMA:
        groups.setdefault(f["group"], []).append(f)
    return render_template_string(
        LIVE_PAGE, style=STYLE, tab="live", groups=list(groups.items()),
        values=camera_settings.load(SETTINGS_FILE),
    )


@app.get("/live/stream")
@requires_auth
def live_stream():
    """MJPEG stream built from preview frames the recorder publishes while LIVE_FLAG is fresh."""
    def frames():
        try:
            last = PREVIEW_FILE.stat().st_mtime_ns   # skip any stale frame left from a previous viewer
        except OSError:
            last = None
        while True:
            LIVE_FLAG.touch()
            try:
                mtime = PREVIEW_FILE.stat().st_mtime_ns
                if mtime != last:
                    data = PREVIEW_FILE.read_bytes()
                    last = mtime
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
            except OSError:
                pass
            time.sleep(0.1)

    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/live/settings")
@requires_auth
def save_settings():
    changes = request.get_json(silent=True)
    if not isinstance(changes, dict):
        abort(400)
    # clean() drops unknown keys and clamps values, so the form can't push anything invalid.
    saved = camera_settings.save(SETTINGS_FILE, {**camera_settings.load(SETTINGS_FILE), **changes})
    return jsonify(saved)


@app.post("/live/settings/reset")
@requires_auth
def reset_settings():
    return jsonify(camera_settings.save(SETTINGS_FILE, camera_settings.defaults()))


@app.get("/media/<name>")
@requires_auth
def media(name: str):
    path = clip_path(name)
    # send_from_directory supports HTTP Range requests, which browsers need to seek.
    return send_from_directory(path.parent, name, mimetype="video/mp4", conditional=True)


@app.get("/thumb/<name>")
@requires_auth
def thumb(name: str):
    src = clip_path(name)
    out = THUMBS_DIR / (src.stem + ".jpg")
    if not out.exists():
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=480:-1", str(out)],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or not out.exists():
            # Clips shorter than 2s, or not yet finalized: fall back to the first frame.
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                 "-frames:v", "1", "-vf", "scale=480:-1", str(out)],
                capture_output=True, timeout=30,
            )
        if not out.exists():
            abort(404)
        prune_thumbs()
    return send_from_directory(THUMBS_DIR, out.name, mimetype="image/jpeg", max_age=3600)


@app.post("/delete/<name>")
@requires_auth
def delete(name: str):
    path = clip_path(name)
    path.unlink(missing_ok=True)
    (THUMBS_DIR / (path.stem + ".jpg")).unlink(missing_ok=True)
    remove_empty_day_dir(path.parent)
    return redirect(url_for("index"))


@app.post("/delete-all")
@requires_auth
def delete_all():
    # list_clips() skips files still being recorded, so an active clip is left alone.
    for c in list_clips():
        path = day_dir_for(c["name"]) / c["name"]
        path.unlink(missing_ok=True)
        (THUMBS_DIR / (path.stem + ".jpg")).unlink(missing_ok=True)
        remove_empty_day_dir(path.parent)
    return redirect(url_for("index"))


migrate_flat_clips()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
