#!/usr/bin/env python3
"""
iRacing Setup Advisor
Upload an .ibt telemetry file, choose your car and track, get setup recommendations.
Runs entirely on your local machine — no data leaves your PC.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import webbrowser

from flask import Flask, jsonify, request

from ibt_parser import parse_ibt
from analyzer import analyze

# ── Resolve base path (works both in dev and as a PyInstaller bundle) ─────────
BASE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def _read_version():
    try:
        with open(os.path.join(BASE, 'version.txt')) as f:
            return f.read().strip()
    except OSError:
        return '?.?.?'


VERSION = _read_version()


# ── Load car / track configs from JSON files ──────────────────────────────────
def _load_configs(subdir):
    """Return {stem: config_dict} for every .json in BASE/subdir."""
    folder = os.path.join(BASE, subdir)
    configs = {}
    if not os.path.isdir(folder):
        return configs
    for fname in sorted(os.listdir(folder)):
        if fname.endswith('.json'):
            stem = fname[:-5]
            try:
                with open(os.path.join(folder, fname), encoding='utf-8') as f:
                    configs[stem] = json.load(f)
            except Exception:
                pass
    return configs


CARS   = _load_configs('cars')
TRACKS = _load_configs('tracks')


def _as_list(v):
    """Wrap scalar in list, or return list as-is."""
    if isinstance(v, list): return v
    if v: return [v]
    return []

# ── Build fast lookup indexes ─────────────────────────────────────────────
# Maps iracing CarPath / TrackName (lower) → config key
CAR_PATH_INDEX   = {}
TRACK_NAME_INDEX = {}

for _key, _cfg in CARS.items():
    CAR_PATH_INDEX[_key.lower()] = _key
    for _p in _as_list(_cfg.get('iracing_car_path', [])):
        CAR_PATH_INDEX[_p.lower()] = _key

for _key, _cfg in TRACKS.items():
    TRACK_NAME_INDEX[_key.lower()] = _key
    for _n in _as_list(_cfg.get('iracing_track_name', [])):
        TRACK_NAME_INDEX[_n.lower()] = _key


def _detect_from_session(session_info):
    """
    Parse iRacing session YAML to detect car and track.
    Returns (car_id, track_id) — either may be None.
    """
    import re
    car_id   = None
    track_id = None

    # Track name
    m = re.search(r'TrackName:\s*(.+)', session_info)
    if m:
        tn = m.group(1).strip().lower()
        track_id = TRACK_NAME_INDEX.get(tn)
        # Fuzzy: try substring match if exact fails
        if track_id is None:
            for k in TRACK_NAME_INDEX:
                if k in tn or tn in k:
                    track_id = TRACK_NAME_INDEX[k]
                    break

    # Player car: find DriverCarIdx, then match CarPath for that index
    idx_m = re.search(r'DriverCarIdx:\s*(\d+)', session_info)
    car_idx = idx_m.group(1) if idx_m else '0'

    # Find the driver entry block for this CarIdx.
    # Use negative lookbehind (?<![A-Za-z]) to avoid matching "DriverCarIdx".
    block_m = re.search(
        r'(?<![A-Za-z])CarIdx:\s*' + re.escape(car_idx) + r'\b.*?CarPath:\s*(\S+)',
        session_info, re.DOTALL
    )
    if block_m:
        cp = block_m.group(1).strip().lower()
        car_id = CAR_PATH_INDEX.get(cp)
        if car_id is None:
            # Fuzzy: check if any known path is a substring
            for k in CAR_PATH_INDEX:
                if k in cp or cp in k:
                    car_id = CAR_PATH_INDEX[k]
                    break

    return car_id, track_id


# ── Find a free port ──────────────────────────────────────────────────────────
# Ports blocked by Chromium-based browsers (Opera, Edge, Chrome) and Firefox
_BROWSER_BLOCKED_PORTS = {
    5060, 5061,   # SIP / SIPS
    6000, 6001, 6002, 6003, 6004, 6005, 6006, 6007,  # X11
}

def _free_port(start=7700):
    for port in range(start, start + 50):
        if port in _BROWSER_BLOCKED_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    return start


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024   # 512 MB


@app.route('/')
def index():
    return HTML.replace('{VERSION}', VERSION)


@app.route('/api/cars')
def list_cars():
    return jsonify([
        {'id': k, 'name': v.get('name', k), 'class': v.get('class', ''),
         'image_url': v.get('image_url', '')}
        for k, v in sorted(CARS.items(), key=lambda x: x[1].get('name', ''))
    ])


@app.route('/api/tracks')
def list_tracks():
    return jsonify([
        {'id': k, 'name': v.get('name', k), 'country': v.get('country', ''),
         'map_url': v.get('map_url', '')}
        for k, v in sorted(TRACKS.items(), key=lambda x: x[1].get('name', ''))
    ])


@app.route('/api/detect-debug', methods=['POST'])
def detect_debug():
    """Upload an .ibt and get back what the parser sees for car/track detection."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    with tempfile.NamedTemporaryFile(suffix='.ibt', delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)
    try:
        import re
        _, session_info, _, _ = parse_ibt(tmp_path)
        tn_m   = re.search(r'TrackName:\s*(.+)', session_info)
        idx_m  = re.search(r'DriverCarIdx:\s*(\d+)', session_info)
        car_idx = idx_m.group(1) if idx_m else '0'
        blk_m  = re.search(
            r'(?<![A-Za-z])CarIdx:\s*' + re.escape(car_idx) + r'\b.*?CarPath:\s*(\S+)',
            session_info, re.DOTALL)
        detected_car, detected_track = _detect_from_session(session_info)
        return jsonify({
            'raw_track_name':   tn_m.group(1).strip() if tn_m else None,
            'raw_car_path':     blk_m.group(1).strip() if blk_m else None,
            'driver_car_idx':   car_idx,
            'detected_car_id':   detected_car,
            'detected_track_id': detected_track,
            'known_car_paths':  sorted(CAR_PATH_INDEX.keys()),
            'known_track_names': sorted(TRACK_NAME_INDEX.keys()),
        })
    finally:
        os.unlink(tmp_path)


@app.route('/api/analyze', methods=['POST'])
def analyze_route():
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request.'}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.ibt'):
        return jsonify({'error': 'File must be a .ibt iRacing telemetry file.'}), 400

    car_id   = request.form.get('car',   '').strip()
    track_id = request.form.get('track', '').strip()
    car_cfg   = CARS.get(car_id)
    track_cfg = TRACKS.get(track_id)

    with tempfile.NamedTemporaryFile(suffix='.ibt', delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        channels, session_info, tick_rate, record_count = parse_ibt(tmp_path)

        # Auto-detect car/track from session YAML if not manually specified
        detected_car_id, detected_track_id = _detect_from_session(session_info)
        auto_detected_car   = False
        auto_detected_track = False

        if not car_cfg and detected_car_id:
            car_id   = detected_car_id
            car_cfg  = CARS.get(car_id)
            auto_detected_car = True

        if not track_cfg and detected_track_id:
            track_id  = detected_track_id
            track_cfg = TRACKS.get(track_id)
            auto_detected_track = True

        result = analyze(channels, tick_rate, car_cfg=car_cfg, track_cfg=track_cfg)
        result['meta'] = {
            'filename':     f.filename,
            'tick_rate':    tick_rate,
            'record_count': record_count,
        }
        result['detected'] = {
            'car_id':            car_id   if auto_detected_car   else None,
            'track_id':          track_id if auto_detected_track else None,
            'auto_detected_car': auto_detected_car,
            'auto_detected_track': auto_detected_track,
        }
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iRacing Setup Advisor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f0f0f;color:#e0e0e0;min-height:100vh}

.header{background:#181818;border-bottom:1px solid #2a2a2a;
        padding:14px 28px;display:flex;align-items:center;gap:12px}
.header h1{font-size:17px;font-weight:700}
.header .sub{font-size:12px;color:#555;margin-left:6px}

/* ── Config selectors ── */
.config-bar{background:#141414;border-bottom:1px solid #222;
            padding:16px 28px;display:flex;gap:20px;flex-wrap:wrap;align-items:flex-end}
.config-group{display:flex;flex-direction:column;gap:6px;flex:1;min-width:220px}
.config-group label{font-size:11px;font-weight:700;text-transform:uppercase;
                     letter-spacing:.5px;color:#555}
.config-group select{background:#1e1e1e;border:1px solid #333;color:#e0e0e0;
                      padding:8px 12px;border-radius:6px;font-size:13px;cursor:pointer}
.config-group select:focus{outline:none;border-color:#2196F3}
.config-hint{font-size:12px;color:#444;padding-top:6px}

/* ── Preview panel ── */
.preview-bar{background:#111;border-bottom:1px solid #1e1e1e;
             padding:0 28px;display:none;gap:24px;align-items:stretch;min-height:0;
             transition:all .2s}
.preview-bar.visible{display:flex;padding:16px 28px}
.preview-card{background:#181818;border-radius:10px;overflow:hidden;
              display:flex;align-items:center;gap:0;flex:1;max-width:460px;min-height:110px}
.preview-card.hidden{display:none}
.preview-img{width:200px;height:120px;object-fit:cover;flex-shrink:0;
             background:#111;display:block}
.preview-img.map{object-fit:contain;background:#1a1a1a;padding:6px}
.preview-info{padding:12px 16px;flex:1}
.preview-label{font-size:10px;font-weight:700;text-transform:uppercase;
               letter-spacing:.5px;color:#555;margin-bottom:4px}
.preview-name{font-size:14px;font-weight:600;color:#e0e0e0;line-height:1.3}
.preview-sub{font-size:12px;color:#555;margin-top:3px}
.preview-no-img{width:200px;height:120px;flex-shrink:0;background:#151515;
                display:flex;align-items:center;justify-content:center;
                font-size:32px;color:#2a2a2a}

/* ── Drop zone ── */
.drop-wrap{display:flex;justify-content:center;padding:40px 24px}
.drop-zone{background:#181818;border:2px dashed #2a2a2a;border-radius:14px;
           padding:48px 40px;text-align:center;cursor:pointer;
           transition:border-color .2s,background .2s;max-width:520px;width:100%}
.drop-zone:hover,.drop-zone.over{border-color:#2196F3;background:#0d1f2e}
.drop-icon{font-size:48px;margin-bottom:14px}
.drop-zone h2{font-size:16px;margin-bottom:8px}
.drop-zone p{font-size:13px;color:#555}

/* ── Status ── */
#status{text-align:center;padding:14px;font-size:13px;color:#888}

/* ── Meta bar ── */
.meta-bar{background:#161616;border-top:1px solid #222;border-bottom:1px solid #222;
          padding:9px 28px;font-size:12px;color:#555;display:flex;gap:24px;flex-wrap:wrap;align-items:center}
.meta-bar b{color:#999}
.meta-car-track{font-size:12px;color:#2196F3;font-weight:600}
.btn-reset{margin-left:auto;background:#2a2a2a;border:1px solid #3a3a3a;color:#ccc;
           padding:5px 14px;border-radius:6px;font-size:12px;cursor:pointer}
.btn-reset:hover{background:#333;color:#fff}
.auto-badge{background:#0d2a40;color:#64b5f6;font-size:10px;font-weight:700;
            padding:2px 7px;border-radius:8px;letter-spacing:.3px;margin-left:5px;
            text-transform:uppercase}

/* ── Page layout ── */
.page{max-width:980px;margin:0 auto;padding:24px 28px}
.section-label{font-size:11px;font-weight:700;text-transform:uppercase;
               letter-spacing:.8px;color:#555;margin:28px 0 14px}

/* ── Tyre grid ── */
.tyre-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.tyre-card{background:#181818;border-radius:10px;padding:16px 18px}
.tyre-card h3{font-size:12px;font-weight:700;text-transform:uppercase;
              letter-spacing:.5px;color:#666;margin-bottom:14px}
.temp-bars{display:flex;gap:6px;margin-bottom:12px}
.temp-col{flex:1;text-align:center}
.temp-col .lbl{font-size:10px;color:#444;text-transform:uppercase;margin-bottom:4px}
.temp-box{border-radius:5px;padding:11px 2px;font-size:14px;font-weight:700;color:#fff}
.tc-cold{background:#0d47a1}.tc-ok{background:#1b5e20}
.tc-warm{background:#e65100}.tc-hot{background:#b71c1c}
.tyre-meta{display:flex;justify-content:space-between;font-size:12px;color:#555;margin-top:6px}
.tyre-meta em{color:#aaa;font-style:normal;font-weight:600}

/* ── Balance row ── */
.balance-row{display:flex;gap:20px;background:#181818;border-radius:8px;
             padding:12px 18px;font-size:12px;color:#777;flex-wrap:wrap}
.balance-item{display:flex;flex-direction:column;gap:3px}
.balance-item .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#444}
.balance-item .val{font-size:14px;font-weight:700;color:#ccc}
.balance-diff{font-size:11px;color:#888}

/* ── Sector handling ── */
.sector-table{background:#181818;border-radius:10px;overflow:hidden}
.sector-row{display:flex;align-items:center;gap:14px;padding:11px 18px;
            border-bottom:1px solid #222}
.sector-row:last-child{border-bottom:none}
.s-name{font-size:12px;color:#777;flex:1}
.badge{padding:3px 12px;border-radius:10px;font-size:11px;font-weight:700;
       text-transform:uppercase;letter-spacing:.3px}
.b-us{background:#0d47a1}.b-neu{background:#1b5e20}
.b-os{background:#b71c1c}.b-nd{background:#2a2a2a;color:#444}

/* ── Brake analysis ── */
.brake-row{display:flex;gap:16px;background:#181818;border-radius:8px;
           padding:12px 18px;font-size:12px;color:#777;flex-wrap:wrap}
.brake-item{display:flex;flex-direction:column;gap:3px;flex:1;min-width:130px}
.brake-item .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#444}
.brake-item .val{font-size:14px;font-weight:700;color:#ccc}
.brake-item .sub{font-size:11px;color:#555}
.consistency-bar{height:8px;border-radius:4px;background:#1e1e1e;overflow:hidden;margin-top:4px;width:100%}
.consistency-fill{height:100%;border-radius:4px;transition:width .3s}

/* ── Throttle overlap ── */
.overlap-summary{background:#181818;border-radius:8px;padding:14px 18px;
                 display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.overlap-pct-big{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                 font-size:2.2rem;font-weight:700;line-height:1}
.overlap-desc{font-size:12px;color:#666;max-width:320px;line-height:1.5}
.overlap-sectors{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.overlap-sector-row{display:flex;align-items:center;gap:12px}
.overlap-sec-name{font-size:11px;color:#666;width:120px;flex-shrink:0}
.overlap-bar-wrap{flex:1;background:#1e1e1e;border-radius:4px;height:10px;overflow:hidden}
.overlap-bar-fill{height:100%;border-radius:4px}
.overlap-sec-pct{font-size:11px;font-weight:700;color:#ccc;width:36px;text-align:right;flex-shrink:0}

/* ── Recommendations ── */
.rec-list{display:flex;flex-direction:column;gap:10px}
.rec{background:#181818;border-radius:8px;padding:14px 18px;border-left:4px solid #333}
.rec.high{border-left-color:#f44336}
.rec.medium{border-left-color:#ff9800}
.rec.low{border-left-color:#4caf50}
.rec-head{display:flex;align-items:center;gap:10px;margin-bottom:7px}
.rec-cat{font-size:10px;font-weight:700;text-transform:uppercase;
         letter-spacing:.5px;color:#666}
.rec-corner{font-size:11px;font-weight:700;color:#2196F3;
            background:#0d2a40;padding:2px 10px;border-radius:10px}
.rec-issue{font-size:13px;color:#ccc;margin-bottom:5px}
.rec-action{font-size:13px;color:#66bb6a;font-style:italic}

/* ── Lap times ── */
.lap-table{background:#181818;border-radius:10px;overflow:hidden;max-height:420px;overflow-y:auto}
.lap-row{display:grid;grid-template-columns:56px 1fr 1fr;align-items:center;
         padding:9px 18px;border-bottom:1px solid #222}
.lap-row:last-child{border-bottom:none}
.lap-header{background:#111;position:sticky;top:0}
.lap-header div{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#555}
.lap-num{font-size:12px;color:#666}
.lap-time{font-size:13px;font-weight:600;color:#ccc;font-variant-numeric:tabular-nums}
.lap-delta{font-size:12px;color:#555}
.lap-fastest{background:#1e0a3c}
.lap-fastest .lap-num{color:#c084fc}
.lap-fastest .lap-time{color:#c084fc;font-weight:700}
.lap-fastest .lap-delta{color:#a855f7;font-weight:700}
</style>
</head>
<body>

<div class="header">
  <h1>iRacing Setup Advisor</h1>
  <span class="sub">v{VERSION}</span>
</div>

<!-- Car & Track selection -->
<div class="config-bar">
  <div class="config-group">
    <label>Car</label>
    <select id="car-select"><option value="">— Any / Generic —</option></select>
  </div>
  <div class="config-group">
    <label>Track</label>
    <select id="track-select"><option value="">— Any / Generic —</option></select>
  </div>
  <div class="config-hint">Select your car and track, then drop your .ibt file below</div>
</div>

<!-- Preview panel -->
<div class="preview-bar" id="preview-bar">
  <div class="preview-card hidden" id="car-preview">
    <div class="preview-no-img" id="car-no-img">🚗</div>
    <img class="preview-img" id="car-img" src="" alt="" onerror="this.style.display='none';document.getElementById('car-no-img').style.display='flex'">
    <div class="preview-info">
      <div class="preview-label">Car</div>
      <div class="preview-name" id="car-preview-name"></div>
      <div class="preview-sub" id="car-preview-class"></div>
    </div>
  </div>
  <div class="preview-card hidden" id="track-preview">
    <div class="preview-no-img" id="track-no-img">🗺</div>
    <img class="preview-img map" id="track-img" src="" alt="" onerror="this.style.display='none';document.getElementById('track-no-img').style.display='flex'">
    <div class="preview-info">
      <div class="preview-label">Track</div>
      <div class="preview-name" id="track-preview-name"></div>
      <div class="preview-sub" id="track-preview-country"></div>
    </div>
  </div>
</div>

<div id="upload-wrap">
  <div class="drop-wrap">
    <div class="drop-zone" id="dz" onclick="document.getElementById('fi').click()">
      <div class="drop-icon">🏁</div>
      <h2>Drop your .ibt telemetry file here</h2>
      <p>Or click to browse &nbsp;·&nbsp; Processed locally — never sent to the internet</p>
      <input type="file" id="fi" accept=".ibt" style="display:none">
    </div>
  </div>
</div>

<div id="status"></div>
<div id="results" style="display:none"></div>

<script>
// ── Populate dropdowns & preview lookups ─────────────────────────────────────
const carData   = {};   // id → {name, class, image_url}
const trackData = {};   // id → {name, country, map_url}

async function loadOptions() {
  const [cars, tracks] = await Promise.all([
    fetch('/api/cars').then(r => r.json()),
    fetch('/api/tracks').then(r => r.json()),
  ]);

  const carSel   = document.getElementById('car-select');
  const trackSel = document.getElementById('track-select');

  cars.forEach(c => {
    carData[c.id] = c;
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.name + (c.class ? ` (${c.class})` : '');
    carSel.appendChild(opt);
  });

  tracks.forEach(t => {
    trackData[t.id] = t;
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.name + (t.country ? ` — ${t.country}` : '');
    trackSel.appendChild(opt);
  });
}
loadOptions();

// ── Preview panel ─────────────────────────────────────────────────────────────
function updatePreview() {
  const carId   = document.getElementById('car-select').value;
  const trackId = document.getElementById('track-select').value;
  const bar     = document.getElementById('preview-bar');

  const carCard   = document.getElementById('car-preview');
  const trackCard = document.getElementById('track-preview');

  if (carId && carData[carId]) {
    const c = carData[carId];
    document.getElementById('car-preview-name').textContent  = c.name;
    document.getElementById('car-preview-class').textContent = c.class || '';
    const img   = document.getElementById('car-img');
    const noImg = document.getElementById('car-no-img');
    if (c.image_url) {
      img.src = c.image_url;
      img.style.display    = 'block';
      noImg.style.display  = 'none';
    } else {
      img.style.display    = 'none';
      noImg.style.display  = 'flex';
    }
    carCard.classList.remove('hidden');
  } else {
    carCard.classList.add('hidden');
  }

  if (trackId && trackData[trackId]) {
    const t = trackData[trackId];
    document.getElementById('track-preview-name').textContent    = t.name;
    document.getElementById('track-preview-country').textContent = t.country || '';
    const img   = document.getElementById('track-img');
    const noImg = document.getElementById('track-no-img');
    if (t.map_url) {
      img.src = t.map_url;
      img.style.display    = 'block';
      noImg.style.display  = 'none';
    } else {
      img.style.display    = 'none';
      noImg.style.display  = 'flex';
    }
    trackCard.classList.remove('hidden');
  } else {
    trackCard.classList.add('hidden');
  }

  const anyVisible = (carId && carData[carId]) || (trackId && trackData[trackId]);
  bar.classList.toggle('visible', !!anyVisible);
}

document.getElementById('car-select').addEventListener('change', updatePreview);
document.getElementById('track-select').addEventListener('change', updatePreview);

// ── File handling ─────────────────────────────────────────────────────────────
const dz = document.getElementById('dz');
const fi = document.getElementById('fi');

dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('over');
  if (e.dataTransfer.files[0]) go(e.dataTransfer.files[0]);
});
fi.addEventListener('change', () => { if (fi.files[0]) go(fi.files[0]); });

async function go(file) {
  const carId   = document.getElementById('car-select').value;
  const trackId = document.getElementById('track-select').value;

  setStatus('Parsing ' + file.name + ' …');
  document.getElementById('results').style.display = 'none';

  const form = new FormData();
  form.append('file',  file);
  form.append('car',   carId);
  form.append('track', trackId);

  try {
    const res  = await fetch('/api/analyze', { method: 'POST', body: form });
    const data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error); return; }
    setStatus('');
    render(data);
    const det = data.detected || {};
    if (det.auto_detected_car && det.car_id) {
      document.getElementById('car-select').value = det.car_id;
    }
    if (det.auto_detected_track && det.track_id) {
      document.getElementById('track-select').value = det.track_id;
    }
    if (det.auto_detected_car || det.auto_detected_track) {
      updatePreview();
    }
  } catch(e) { setStatus('Error: ' + e.message); }
}

function setStatus(msg) {
  const el = document.getElementById('status');
  el.textContent = msg;
}

function reset() {
  document.getElementById('results').style.display     = 'none';
  document.getElementById('upload-wrap').style.display = '';
  document.getElementById('fi').value = '';
  setStatus('');
  updatePreview();
}

// ── Rendering ─────────────────────────────────────────────────────────────────
function tempClass(t) {
  return t < 167 ? 'tc-cold' : t < 203 ? 'tc-ok' : t < 230 ? 'tc-warm' : 'tc-hot';
}

function fmtLap(s) {
  if (!s) return null;
  const m = Math.floor(s / 60);
  const r = (s % 60).toFixed(3).padStart(6, '0');
  return m + ':' + r;
}

function tyreCard(label, td, psi) {
  if (!td) return `<div class="tyre-card"><h3>${label}</h3>
    <p style="color:#444;font-size:12px">No data in telemetry</p></div>`;
  const sStr = (td.spread >= 0 ? '+' : '') + td.spread.toFixed(1) + ' °F';
  const pStr = psi != null ? psi.toFixed(1) + ' psi' : '—';
  return `
<div class="tyre-card"><h3>${label}</h3>
  <div class="temp-bars">
    ${['inner','mid','outer'].map(k => `
    <div class="temp-col">
      <div class="lbl">${k}</div>
      <div class="temp-box ${tempClass(td[k])}">${td[k].toFixed(0)}°F</div>
    </div>`).join('')}
  </div>
  <div class="tyre-meta">
    <div>Avg <em>${td.avg.toFixed(0)} °F</em></div>
    <div>Spread <em>${sStr}</em></div>
    <div>Hot <em>${pStr}</em></div>
  </div>
</div>`;
}

function renderBalance(b) {
  if (!b || (!b.front_avg && !b.left_avg)) return '';
  const frd = b.front_rear_diff;
  const lrd = b.left_right_diff;
  const items = [];
  if (b.front_avg != null && b.rear_avg != null) {
    const diff = frd > 0 ? `+${frd.toFixed(1)} rear` : `${(-frd).toFixed(1)} front`;
    items.push(`<div class="balance-item">
        <div class="lbl">Front / Rear</div>
        <div class="val">${b.front_avg}°F / ${b.rear_avg}°F</div>
        <div class="balance-diff">${diff} hotter</div></div>`);
  }
  if (b.left_avg != null && b.right_avg != null) {
    const diff2 = lrd > 0 ? `+${lrd.toFixed(1)} right` : `${(-lrd).toFixed(1)} left`;
    items.push(`<div class="balance-item">
        <div class="lbl">Left / Right</div>
        <div class="val">${b.left_avg}°F / ${b.right_avg}°F</div>
        <div class="balance-diff">${diff2} hotter</div></div>`);
  }
  if (!items.length) return '';
  return `<div class="section-label">Tyre balance</div>
<div class="balance-row">${items.join('')}</div>`;
}

function renderHandling(h) {
  if (!h || !Object.keys(h).length) return '';
  const rows = Object.entries(h).map(([name, d]) => {
    const t   = d.tendency || 'no data';
    const cls = t === 'understeer' ? 'b-us' : t === 'neutral' ? 'b-neu'
              : t === 'oversteer'  ? 'b-os' : 'b-nd';
    return `<div class="sector-row">
      <div class="s-name">${name}</div>
      <span class="badge ${cls}">${t}</span>
    </div>`;
  }).join('');
  return `<div class="section-label">Handling balance by sector</div>
<div class="sector-table">${rows}</div>`;
}

function renderBrake(b) {
  if (!b || !Object.keys(b).length) return '';
  const items = [];

  if (b.avg_bias_front_pct != null) {
    const rear = (100 - b.avg_bias_front_pct).toFixed(1);
    items.push(`<div class="brake-item">
      <div class="lbl">Bias Setting</div>
      <div class="val">${b.avg_bias_front_pct.toFixed(1)}% Front</div>
      <div class="sub">${rear}% Rear</div>
    </div>`);
  }
  if (b.actual_front_bias_pct != null) {
    items.push(`<div class="brake-item">
      <div class="lbl">Actual Split</div>
      <div class="val">${b.actual_front_bias_pct.toFixed(1)}% Front</div>
      <div class="sub">from line pressures</div>
    </div>`);
  }
  if (b.peak_brake_press_psi != null) {
    items.push(`<div class="brake-item">
      <div class="lbl">Peak Pressure</div>
      <div class="val">${b.peak_brake_press_psi} psi</div>
    </div>`);
  }
  if (b.brake_events != null) {
    items.push(`<div class="brake-item">
      <div class="lbl">Brake Events</div>
      <div class="val">${b.brake_events}</div>
    </div>`);
  }
  if (b.avg_peak_brake_pct != null) {
    items.push(`<div class="brake-item">
      <div class="lbl">Avg Peak Input</div>
      <div class="val">${b.avg_peak_brake_pct}%</div>
    </div>`);
  }
  if (b.brake_consistency != null) {
    const pct   = Math.min(b.brake_consistency * 3, 100);
    const color = b.brake_consistency < 5 ? '#4caf50' : b.brake_consistency < 12 ? '#ff9800' : '#f44336';
    const label = b.brake_consistency < 5 ? 'Consistent' : b.brake_consistency < 12 ? 'Moderate' : 'Inconsistent';
    items.push(`<div class="brake-item">
      <div class="lbl">Consistency (σ)</div>
      <div class="val">${label} <span style="color:${color};font-size:12px">±${b.brake_consistency}%</span></div>
      <div class="consistency-bar"><div class="consistency-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`);
  }
  if (!items.length) return '';
  return `<div class="section-label">Brake analysis</div>
<div class="brake-row">${items.join('')}</div>`;
}

function renderOverlap(o) {
  if (!o) return '';
  const pct = o.overall_pct;
  const color = pct < 3 ? '#4caf50' : pct < 8 ? '#ff9800' : '#f44336';
  const desc  = pct < 3
    ? 'Minimal overlap — clean pedal separation.'
    : pct < 8
    ? 'Moderate overlap — likely intentional trail braking. Check sector breakdown.'
    : 'High overlap — review technique. May indicate unintentional input or a setup balance issue.';

  let sectorHtml = '';
  if (o.by_sector) {
    const rows = Object.entries(o.by_sector).map(([name, p]) => {
      const w     = Math.min(p * 5, 100);
      const c     = p < 3 ? '#4caf50' : p < 8 ? '#ff9800' : '#f44336';
      return `<div class="overlap-sector-row">
        <div class="overlap-sec-name">${name}</div>
        <div class="overlap-bar-wrap"><div class="overlap-bar-fill" style="width:${w}%;background:${c}"></div></div>
        <div class="overlap-sec-pct">${p}%</div>
      </div>`;
    }).join('');
    sectorHtml = `<div class="overlap-sectors">${rows}</div>`;
  }

  return `<div class="section-label">Throttle / brake overlap</div>
<div class="overlap-summary">
  <div class="overlap-pct-big" style="color:${color}">${pct}%</div>
  <div class="overlap-desc">${desc}</div>
</div>
${sectorHtml}`;
}

function renderLapTimes(lapTimes) {
  if (!lapTimes || !lapTimes.length) return '';
  const best = Math.min(...lapTimes.map(l => l.time_s));
  const rows = lapTimes.map(l => {
    const isBest   = l.time_s === best;
    const delta    = l.time_s - best;
    const deltaStr = isBest ? '⬤ Fastest' : '+' + delta.toFixed(3) + 's';
    return `<div class="lap-row${isBest ? ' lap-fastest' : ''}">
      <div class="lap-num">${l.lap}</div>
      <div class="lap-time">${fmtLap(l.time_s)}</div>
      <div class="lap-delta">${deltaStr}</div>
    </div>`;
  }).join('');
  return `<div class="section-label">Lap times</div>
<div class="lap-table">
  <div class="lap-row lap-header">
    <div>Lap</div><div>Time</div><div>Δ Best</div>
  </div>
  ${rows}
</div>`;
}

function renderRecs(recs) {
  if (!recs || !recs.length)
    return `<p style="color:#444;font-size:13px">No issues flagged — data looks good, or insufficient data to analyse.</p>`;
  return recs.map(r => `
<div class="rec ${r.priority}">
  <div class="rec-head">
    <span class="rec-cat">${r.category}</span>
    <span class="rec-corner">${r.corner}</span>
  </div>
  <div class="rec-issue">${r.issue}</div>
  <div class="rec-action">→ ${r.action}</div>
</div>`).join('');
}

function render(data) {
  const t = data.tyre_temps     || {};
  const p = data.tyre_pressures || {};
  const m = data.meta           || {};
  const s = data.summary        || {};
  const det = data.detected     || {};

  const dur  = s.duration_s;
  const topv = s.max_speed_mph;
  const laps = s.laps_analysed;

  const carLabel   = (data.car   || '') + (det.auto_detected_car   ? ' <span class="auto-badge">auto</span>' : '');
  const trackLabel = (data.track || '') + (det.auto_detected_track ? ' <span class="auto-badge">auto</span>' : '');

  const autoDetectBanner = (det.auto_detected_car || det.auto_detected_track) ? `
  <div style="background:#0d2a40;border-left:3px solid #2196F3;padding:8px 28px;font-size:12px;color:#64b5f6">
    ⚡ Auto-detected from telemetry${det.auto_detected_car ? ' · Car: <b>' + (data.car || det.car_id) + '</b>' : ''}${det.auto_detected_track ? ' · Track: <b>' + (data.track || det.track_id) + '</b>' : ''}
  </div>` : '';

  let html = `${autoDetectBanner}<div class="meta-bar">
    <div><b>${m.filename || ''}</b></div>
    <div class="meta-car-track">${carLabel} &nbsp;·&nbsp; ${trackLabel}</div>
    ${laps ? `<div>Laps: <b>${laps}</b></div>` : ''}
    ${dur  ? `<div>Duration: <b>${Math.floor(dur/60)}m ${dur%60}s</b></div>` : ''}
    ${topv ? `<div>Top speed: <b>${topv} mph</b></div>` : ''}
    ${s.best_lap_s ? `<div>Best: <b>${fmtLap(s.best_lap_s)}</b></div>` : ''}
    ${s.avg_lap_s  ? `<div>Avg lap: <b>${fmtLap(s.avg_lap_s)}</b></div>` : ''}
    ${s.lap_consistency_s ? `<div>Consistency: <b>±${s.lap_consistency_s}s</b></div>` : ''}
    ${s.fuel_per_lap_gal ? `<div>Fuel/lap: <b>${s.fuel_per_lap_gal} gal</b></div>` : ''}
    ${s.laps_to_empty ? `<div>Laps to empty: <b>${s.laps_to_empty}</b></div>` : ''}
    ${s.peak_lat_g ? `<div>Peak lat G: <b>${s.peak_lat_g}g</b></div>` : ''}
    ${s.peak_brake_g ? `<div>Peak brake G: <b>${s.peak_brake_g}g</b></div>` : ''}
    <div>Sample rate: <b>${m.tick_rate || '?'} Hz</b></div>
    ${data._lap_debug ? `<div style="color:#555;font-size:11px" title="${JSON.stringify(data._lap_debug)}">Laps extracted: <b style="color:${(data._lap_debug.laps_found||0)>0?'#4caf50':'#f44336'}">${data._lap_debug.laps_found ?? '?'}</b></div>` : ''}
    <button class="btn-reset" onclick="reset()">&#8592; Analyse another file</button>
  </div>
  <div class="page">
    <div class="section-label">Tyre temperatures &amp; hot pressures</div>
    <div class="tyre-grid">
      ${tyreCard('LF — Left Front',  t.LF, p.LF)}
      ${tyreCard('RF — Right Front', t.RF, p.RF)}
      ${tyreCard('LR — Left Rear',   t.LR, p.LR)}
      ${tyreCard('RR — Right Rear',  t.RR, p.RR)}
    </div>
    ${renderBalance(data.balance)}
    ${renderHandling(data.handling)}
    ${renderBrake(data.brake)}
    ${renderOverlap(data.throttle_overlap)}
    ${renderLapTimes(data.lap_times)}
    <div class="section-label">Setup recommendations</div>
    <div class="rec-list">${renderRecs(data.recommendations)}</div>
  </div>`;

  const resultsEl = document.getElementById('results');
  resultsEl.innerHTML = html;
  resultsEl.style.display = 'block';
  document.getElementById('upload-wrap').style.display = 'none';
}
</script>
</body>
</html>
"""

if __name__ == '__main__':
    try:
        port = _free_port()
        url  = f'http://localhost:{port}'
        print(f"\n{'='*52}")
        print(f"  iRacing Setup Advisor v{VERSION}")
        print(f"  {len(CARS)} cars · {len(TRACKS)} tracks loaded")
        print(f"  Open: {url}")
        print(f"{'='*52}\n")
        print("Press Ctrl+C to stop.\n")
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        app.run(host='127.0.0.1', port=port, debug=False)
    except Exception as e:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Setup Advisor — Startup Error", str(e))
        except Exception:
            print(f"FATAL: {e}")
        sys.exit(1)
