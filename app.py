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


# ── Find a free port ──────────────────────────────────────────────────────────
def _free_port(start=5050):
    for port in range(start, start + 20):
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
        {'id': k, 'name': v.get('name', k), 'class': v.get('class', '')}
        for k, v in sorted(CARS.items(), key=lambda x: x[1].get('name', ''))
    ])


@app.route('/api/tracks')
def list_tracks():
    return jsonify([
        {'id': k, 'name': v.get('name', k), 'country': v.get('country', '')}
        for k, v in sorted(TRACKS.items(), key=lambda x: x[1].get('name', ''))
    ])


@app.route('/api/analyze', methods=['POST'])
def analyze_route():
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request.'}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.ibt'):
        return jsonify({'error': 'File must be a .ibt iRacing telemetry file.'}), 400

    car_id   = request.form.get('car', '')
    track_id = request.form.get('track', '')
    car_cfg   = CARS.get(car_id)
    track_cfg = TRACKS.get(track_id)

    with tempfile.NamedTemporaryFile(suffix='.ibt', delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        channels, session_info, tick_rate, record_count = parse_ibt(tmp_path)
        result = analyze(channels, tick_rate, car_cfg=car_cfg, track_cfg=track_cfg)
        result['meta'] = {
            'filename':     f.filename,
            'tick_rate':    tick_rate,
            'record_count': record_count,
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
// ── Populate dropdowns ────────────────────────────────────────────────────────
async function loadOptions() {
  const [cars, tracks] = await Promise.all([
    fetch('/api/cars').then(r => r.json()),
    fetch('/api/tracks').then(r => r.json()),
  ]);

  const carSel   = document.getElementById('car-select');
  const trackSel = document.getElementById('track-select');

  cars.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.name + (c.class ? ` (${c.class})` : '');
    carSel.appendChild(opt);
  });

  tracks.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.name + (t.country ? ` — ${t.country}` : '');
    trackSel.appendChild(opt);
  });
}
loadOptions();

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
  } catch(e) { setStatus('Error: ' + e.message); }
}

function setStatus(msg) {
  const el = document.getElementById('status');
  el.textContent = msg;
}

function reset() {
  document.getElementById('results').style.display    = 'none';
  document.getElementById('upload-wrap').style.display = '';
  document.getElementById('fi').value = '';
  setStatus('');
}

// ── Rendering ─────────────────────────────────────────────────────────────────
function tempClass(t) {
  return t < 75 ? 'tc-cold' : t < 95 ? 'tc-ok' : t < 110 ? 'tc-warm' : 'tc-hot';
}

function tyreCard(label, td, psi) {
  if (!td) return `<div class="tyre-card"><h3>${label}</h3>
    <p style="color:#444;font-size:12px">No data in telemetry</p></div>`;
  const sStr = (td.spread >= 0 ? '+' : '') + td.spread.toFixed(1) + ' °C';
  const pStr = psi != null ? psi.toFixed(1) + ' psi' : '—';
  return `
<div class="tyre-card"><h3>${label}</h3>
  <div class="temp-bars">
    ${['inner','mid','outer'].map(k => `
    <div class="temp-col">
      <div class="lbl">${k}</div>
      <div class="temp-box ${tempClass(td[k])}">${td[k].toFixed(0)}°</div>
    </div>`).join('')}
  </div>
  <div class="tyre-meta">
    <div>Avg <em>${td.avg.toFixed(0)} °C</em></div>
    <div>Spread <em>${sStr}</em></div>
    <div>Hot <em>${pStr}</em></div>
  </div>
</div>`;
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

  const dur  = s.duration_s;
  const topv = s.max_speed_kph;
  const laps = s.laps_analysed;

  let html = `<div class="meta-bar">
    <div><b>${m.filename || ''}</b></div>
    <div class="meta-car-track">${data.car || ''} &nbsp;·&nbsp; ${data.track || ''}</div>
    ${laps ? `<div>Laps: <b>${laps}</b></div>` : ''}
    ${dur  ? `<div>Duration: <b>${Math.floor(dur/60)}m ${dur%60}s</b></div>` : ''}
    ${topv ? `<div>Top speed: <b>${topv} km/h</b></div>` : ''}
    <div>Sample rate: <b>${m.tick_rate || '?'} Hz</b></div>
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
    ${renderHandling(data.handling)}
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
