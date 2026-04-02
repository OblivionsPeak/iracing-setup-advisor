#!/usr/bin/env python3
"""
iRacing Setup Advisor
Upload an .ibt telemetry file, choose your car and track, get setup recommendations.
Runs entirely on your local machine — no data leaves your PC.
"""

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
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
    Returns (car_id, track_id, raw_car_path, raw_track_name) — any may be None.
    """
    car_id   = None
    track_id = None
    raw_track_name = None
    raw_car_path   = None

    # Track name — try TrackName first, fall back to TrackDisplayName
    for pattern in (r'TrackName:\s*(.+)', r'TrackDisplayName:\s*(.+)'):
        m = re.search(pattern, session_info)
        if m:
            raw_track_name = m.group(1).strip()
            tn = raw_track_name.lower()
            track_id = TRACK_NAME_INDEX.get(tn)
            # Fuzzy: substring match in both directions
            if track_id is None:
                for k in TRACK_NAME_INDEX:
                    if k in tn or tn in k:
                        track_id = TRACK_NAME_INDEX[k]
                        break
            if track_id:
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
        raw_car_path = block_m.group(1).strip()
        cp = raw_car_path.lower()
        car_id = CAR_PATH_INDEX.get(cp)
        if car_id is None:
            # Fuzzy: substring in either direction
            for k in CAR_PATH_INDEX:
                if k in cp or cp in k:
                    car_id = CAR_PATH_INDEX[k]
                    break

    return car_id, track_id, raw_car_path, raw_track_name


def _detect_session_type(session_info):
    """Extract EventType (Race / Practice / Qualify / Time Trial) from session YAML."""
    for pattern in (r'EventType:\s*(.+)', r'SessionType:\s*(.+)'):
        m = re.search(pattern, session_info)
        if m:
            return m.group(1).strip()
    return None


def _car_from_filename(filename):
    """
    iRacing names IBT files: {carpath}_{track} {date}.ibt
    The car path is the underscore-connected prefix before any spaces.
    Try progressively shorter prefixes against the car index.
    """
    stem  = os.path.splitext(os.path.basename(filename))[0].lower()
    # Everything before the first space is "{carpath}_{partialtrack}"
    no_space = stem.split(' ')[0]          # e.g. "porsche992rgt3_roadatlanta"
    parts    = no_space.split('_')
    # Try longest-to-shortest prefix
    for i in range(len(parts), 0, -1):
        candidate = '_'.join(parts[:i])    # e.g. "porsche992rgt3"
        if candidate in CAR_PATH_INDEX:
            return CAR_PATH_INDEX[candidate], candidate
        for k in CAR_PATH_INDEX:
            if len(candidate) > 5 and (k == candidate or k in candidate or candidate in k):
                return CAR_PATH_INDEX[k], candidate
    return None, None


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
        detected_car, detected_track, raw_cp, raw_tn = _detect_from_session(session_info)
        return jsonify({
            'raw_track_name':   raw_tn,
            'raw_car_path':     raw_cp,
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
    air_temp_raw = request.form.get('air_temp_f', '').strip()
    ambient_temp_f = float(air_temp_raw) if air_temp_raw else None
    excluded_laps_raw = request.form.get('excluded_laps', '').strip()
    excluded_laps = json.loads(excluded_laps_raw) if excluded_laps_raw else None
    car_cfg   = CARS.get(car_id)
    track_cfg = TRACKS.get(track_id)

    with tempfile.NamedTemporaryFile(suffix='.ibt', delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        channels, session_info, tick_rate, record_count = parse_ibt(tmp_path)

        # Auto ambient + track temp from session YAML
        track_temp_f = None
        _temp_debug = None
        if session_info:
            _at_m = re.search(r'AirTemp:\s*([\d.]+)', session_info)
            if _at_m and ambient_temp_f is None:
                ambient_temp_f = round(float(_at_m.group(1)) * 9/5 + 32, 1)
            # Try TrackTemp first, then TrackSurfaceTemp as fallback
            _tt_m = re.search(r'TrackTemp:\s*([\d.]+)', session_info) or \
                    re.search(r'TrackSurfaceTemp:\s*([\d.]+)', session_info)
            if _tt_m:
                track_temp_f = round(float(_tt_m.group(1)) * 9/5 + 32, 1)
            # Grab raw temp lines for debug
            _temp_lines = re.findall(r'.{0,4}[Tt]emp.{0,40}', session_info)
            _temp_debug = _temp_lines[:8] if _temp_lines else ['(no temp lines found in YAML)']

        # Auto-detect car/track from session YAML if not manually specified
        detected_car_id, detected_track_id, raw_car_path, raw_track_name = \
            _detect_from_session(session_info)
        auto_detected_car   = False
        auto_detected_track = False

        # Fallback: filename-based car detection if YAML gave no car or matched pace car
        if not detected_car_id or (raw_car_path and raw_car_path.lower() == 'safety'):
            fname_car_id, fname_raw = _car_from_filename(f.filename)
            if fname_car_id:
                detected_car_id = fname_car_id
                raw_car_path    = fname_raw

        if not car_cfg and detected_car_id:
            car_id   = detected_car_id
            car_cfg  = CARS.get(car_id)
            auto_detected_car = True

        if not track_cfg and detected_track_id:
            track_id  = detected_track_id
            track_cfg = TRACKS.get(track_id)
            auto_detected_track = True

        session_type = _detect_session_type(session_info)
        result = analyze(channels, tick_rate, car_cfg=car_cfg, track_cfg=track_cfg,
                         ambient_temp_f=ambient_temp_f, excluded_laps=excluded_laps)
        result['meta'] = {
            'filename':     f.filename,
            'tick_rate':    tick_rate,
            'record_count': record_count,
            'session_type': session_type,
            'ambient_temp_f': ambient_temp_f,
            'track_temp_f':  track_temp_f,
            'temp_debug':    _temp_debug,
        }
        result['detected'] = {
            'car_id':            car_id   if auto_detected_car   else None,
            'track_id':          track_id if auto_detected_track else None,
            'auto_detected_car': auto_detected_car,
            'auto_detected_track': auto_detected_track,
            'raw_car_path':      raw_car_path,
            'raw_track_name':    raw_track_name,
        }
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.route('/api/compare', methods=['POST'])
def compare_route():
    for key in ('file_a', 'file_b'):
        if key not in request.files:
            return jsonify({'error': f'Missing {key}'}), 400
    results = {}
    tmp_paths = []
    try:
        for key, car_key, track_key in [('file_a', 'car_a', 'track_a'), ('file_b', 'car_b', 'track_b')]:
            f = request.files[key]
            if not f.filename.lower().endswith('.ibt'):
                return jsonify({'error': f'{key} must be .ibt'}), 400
            car_id    = request.form.get(car_key, '').strip()
            track_id  = request.form.get(track_key, '').strip()
            car_cfg   = CARS.get(car_id)
            track_cfg = TRACKS.get(track_id)
            with tempfile.NamedTemporaryFile(suffix='.ibt', delete=False) as tmp:
                tmp_path = tmp.name
                f.save(tmp_path)
                tmp_paths.append(tmp_path)
            channels, session_info, tick_rate, _ = parse_ibt(tmp_path)
            det_car, det_track, _, _ = _detect_from_session(session_info)
            if not car_cfg and det_car:
                car_cfg = CARS.get(det_car)
            if not track_cfg and det_track:
                track_cfg = TRACKS.get(det_track)
            result = analyze(channels, tick_rate, car_cfg=car_cfg, track_cfg=track_cfg)
            result['meta'] = {'filename': f.filename, 'tick_rate': tick_rate}
            results[key[5:]] = result   # 'a' or 'b'
        return jsonify(results)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        for _p in tmp_paths:
            try: os.unlink(_p)
            except Exception: pass


@app.route('/api/sto-notes', methods=['POST'])
def sto_notes_route():
    """Extract human-readable notes from an iRacing .sto setup file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    raw = f.read()
    notes = ''
    try:
        decoded = raw.decode('utf-16-le', errors='ignore')
        import re as _re_sto
        blocks = _re_sto.findall(r'[ -~\n\r\t]{20,}', decoded)
        notes = '\n\n'.join(b.strip() for b in blocks if b.strip())
    except Exception:
        pass
    if not notes:
        return jsonify({'error': 'No readable notes found in this .sto file.'}), 200
    return jsonify({'notes': notes, 'filename': f.filename})


@app.route('/api/sto-decode', methods=['POST'])
def sto_decode_route():
    """
    Decode an iRacing .sto file via setupdelta API and return structured parameters
    grouped by tab/section, plus embedded notes.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f    = request.files['file']
    raw  = f.read()
    name = f.filename

    # ── Decode via setupdelta ──────────────────────────────────────────────────
    try:
        import requests as _req
        resp = _req.post(
            'https://www.setupdelta.com/api/setup/decode',
            files={'file': (name, raw, 'application/octet-stream')},
            headers={'Origin': 'https://www.setupdelta.com',
                     'Referer': 'https://www.setupdelta.com/'},
            timeout=20,
        )
        if resp.status_code == 422:
            return jsonify({'error': 'unsupported_car'}), 400
        if resp.status_code != 200:
            return jsonify({'error': f'decode_api_error_{resp.status_code}'}), 400
        decoded = resp.json()
    except Exception as e:
        return jsonify({'error': f'network_error: {e}'}), 500

    rows = decoded.get('rows', [])
    car_name = decoded.get('carName', '')

    # ── Build grouped parameter dict (mapped rows only) ───────────────────────
    tabs = {}
    for row in rows:
        if not row.get('is_mapped'):
            continue
        tab  = row.get('tab')  or 'Other'
        sect = row.get('section') or 'General'
        tabs.setdefault(tab, {}).setdefault(sect, [])
        tabs[tab][sect].append({
            'label':       row.get('label', ''),
            'value':       row.get('metric_value', ''),
            'range_min':   (row.get('range_metric') or {}).get('min'),
            'range_max':   (row.get('range_metric') or {}).get('max'),
        })

    # ── Extract embedded notes ────────────────────────────────────────────────
    notes = ''
    try:
        text = raw.decode('utf-16-le', errors='ignore')
        blocks = re.findall(r'[ -~\n\r\t]{20,}', text)
        notes  = '\n\n'.join(b.strip() for b in blocks if b.strip())
    except Exception:
        pass

    # ── Look up car config for recommendation engine ─────────────────────────
    car_cfg = None
    if car_name:
        cfg_key = CAR_PATH_INDEX.get(car_name.lower())
        if cfg_key and cfg_key in CARS:
            car_cfg = CARS[cfg_key]

    return jsonify({
        'filename':  name,
        'car_name':  car_name,
        'tabs':      tabs,
        'notes':     notes,
        'row_count': len(rows),
        'mapped_count': sum(1 for r in rows if r.get('is_mapped')),
        'car_config': car_cfg,
    })


# ── Heartbeat / auto-shutdown (desktop mode only) ────────────────────────────
_last_heartbeat = time.monotonic()
_HEARTBEAT_TIMEOUT = 15          # seconds with no heartbeat before shutdown
_desktop_mode = False             # toggled True only in __main__


@app.route('/api/heartbeat')
def heartbeat():
    global _last_heartbeat
    _last_heartbeat = time.monotonic()
    return '', 200


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    # Only allow from localhost
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return '', 403
    if _desktop_mode:
        threading.Thread(target=_do_shutdown, daemon=True).start()
    return '', 200


def _do_shutdown():
    """Give Flask a moment to send the 200 response, then exit."""
    time.sleep(0.5)
    os._exit(0)


def _watchdog():
    """Background thread: exit if no heartbeat for _HEARTBEAT_TIMEOUT seconds."""
    while True:
        time.sleep(3)
        if time.monotonic() - _last_heartbeat > _HEARTBEAT_TIMEOUT:
            print("\n[watchdog] No heartbeat received — shutting down.")
            os._exit(0)


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

/* ── STO analysis panel ── */
.sto-analysis{background:#141414;border:1px solid #2a2a2a;border-radius:12px;overflow:hidden;margin-top:14px}
.sto-analysis-header{background:#181818;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #222}
.sto-analysis-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#777}
.sto-car-badge{font-size:11px;color:#2196F3;background:#0d2a40;padding:2px 10px;border-radius:8px;font-weight:600}
.sto-tabs{display:flex;gap:0;border-bottom:1px solid #222;overflow-x:auto}
.sto-tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:#555;padding:8px 16px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;transition:color .15s}
.sto-tab-btn:hover{color:#aaa}
.sto-tab-btn.active{color:#2196F3;border-bottom-color:#2196F3}
.sto-tab-content{padding:16px 20px;max-height:400px;overflow-y:auto}
.sto-section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#444;margin:12px 0 8px}
.sto-section-title:first-child{margin-top:0}
.sto-param-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #1a1a1a;font-size:12px}
.sto-param-row:last-child{border-bottom:none}
.sto-param-label{color:#666;flex:1}
.sto-param-value{font-weight:700;color:#ccc;margin-left:12px;font-variant-numeric:tabular-nums}
.sto-param-range{font-size:10px;color:#444;margin-left:8px}
.sto-insight{display:flex;align-items:flex-start;gap:8px;padding:7px 10px;border-radius:6px;margin-top:4px;font-size:12px}
.sto-insight.warn{background:#1a0f00;border-left:3px solid #ff9800;color:#ffb74d}
.sto-insight.good{background:#0a1a0a;border-left:3px solid #4caf50;color:#81c784}
.sto-insight.info{background:#0a1520;border-left:3px solid #2196F3;color:#64b5f6}
.sto-notes-block{padding:14px 20px;border-top:1px solid #1a1a1a}
.sto-notes-pre{color:#94a3b8;font-size:.75rem;white-space:pre-wrap;max-height:200px;overflow-y:auto;background:#0f172a;border-radius:6px;padding:12px;margin:0;line-height:1.5}

/* ── Setup analysis visualizations ── */
.setup-viz-section{padding:16px 20px;border-bottom:1px solid #1a1a1a}
.setup-viz-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#555;margin-bottom:14px}
.setup-viz-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.setup-viz-card{background:#181818;border-radius:10px;padding:16px 18px}
.setup-viz-card h4{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#666;margin-bottom:12px}
.car-outline-wrap{display:flex;justify-content:center;padding:8px 0}
.corner-data{font-size:11px;color:#aaa}
.corner-data .val{font-size:14px;font-weight:700;color:#ccc}
.corner-data .unit{font-size:10px;color:#555}
.bar-chart-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.bar-chart-row .bar-label{font-size:11px;color:#666;width:80px;text-align:right;flex-shrink:0}
.bar-chart-row .bar-wrap{flex:1;height:16px;background:#111;border-radius:3px;overflow:hidden;position:relative}
.bar-chart-row .bar-fill{height:100%;border-radius:3px;transition:width .3s}
.bar-chart-row .bar-val{font-size:11px;color:#aaa;width:60px;flex-shrink:0;font-variant-numeric:tabular-nums}
.bar-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:6px}
.bar-pair-label{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.3px;margin-bottom:4px;text-align:center}
.bar-pair-value{font-size:13px;font-weight:700;text-align:center;margin-top:4px}
.ratio-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;margin-left:8px}
.ratio-badge.front-bias{background:#0d2a40;color:#64b5f6}
.ratio-badge.rear-bias{background:#2d0a0a;color:#f44336}
.ratio-badge.balanced{background:#0a1a0a;color:#4caf50}
.setup-rec-list{display:flex;flex-direction:column;gap:8px}
.setup-rec{background:#181818;border-radius:8px;padding:12px 16px;border-left:4px solid #333}
.setup-rec.high{border-left-color:#f44336}
.setup-rec.medium{border-left-color:#ff9800}
.setup-rec.low{border-left-color:#4caf50}
.setup-rec-head{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.setup-rec-cat{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#666}
.setup-rec-priority{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;padding:2px 6px;border-radius:6px}
.setup-rec-priority.high{background:#2d0a0a;color:#f44336}
.setup-rec-priority.medium{background:#1a1000;color:#ff9800}
.setup-rec-priority.low{background:#0a1a0a;color:#4caf50}
.setup-rec-text{font-size:12px;color:#ccc;line-height:1.5}
.setup-rec-action{font-size:12px;color:#66bb6a;font-style:italic;margin-top:3px}
.aero-balance-indicator{display:flex;align-items:center;gap:2px;margin:12px 0}
.aero-balance-bar{height:8px;border-radius:4px;transition:width .3s}
.brake-bias-bar{display:flex;height:24px;border-radius:6px;overflow:hidden;margin:8px 0}
.brake-bias-front{background:#2196F3;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.brake-bias-rear{background:#f44336;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}

/* ── Setup card ── */
.setup-card{background:#141414;border:1px solid #222;border-radius:12px;overflow:hidden;margin-bottom:8px}
.sc-header{background:#181818;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #222}
.sc-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#777}
.btn-print{background:#1e1e1e;border:1px solid #333;color:#888;padding:5px 14px;border-radius:6px;
           font-size:11px;cursor:pointer;font-weight:600;letter-spacing:.4px}
.btn-print:hover{background:#252525;color:#ccc}
.sc-section{padding:14px 20px;border-bottom:1px solid #1a1a1a}
.sc-section:last-child{border-bottom:none}
.sc-section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#444;margin-bottom:10px}
/* Tyre pressure grid */
.sc-psi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.sc-psi-cell{background:#181818;border-radius:8px;padding:10px 12px;text-align:center}
.sc-psi-corner{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#555;margin-bottom:5px}
.sc-psi-hot{font-size:16px;font-weight:700;line-height:1}
.sc-psi-hot.psi-over{color:#f44336}.sc-psi-hot.psi-under{color:#2196F3}.sc-psi-hot.psi-ok{color:#4caf50}
.sc-psi-target{font-size:11px;color:#444;margin-top:3px}
.sc-psi-adj{font-size:12px;font-weight:700;margin-top:6px;padding:3px 0;border-radius:4px}
.sc-psi-adj.adj-over{color:#ff6b6b;background:#1a0505}
.sc-psi-adj.adj-under{color:#64b5f6;background:#0a1520}
.sc-psi-adj.adj-ok{color:#4caf50;background:#0a1a0a}
/* Camber */
.sc-camber-row{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid #1a1a1a}
.sc-camber-row:last-child{border-bottom:none}
.sc-camber-corner{font-size:11px;font-weight:700;color:#888;width:32px}
.sc-camber-action{font-size:13px;color:#ccc;flex:1}
.sc-camber-detail{font-size:11px;color:#555}
/* Suspension items */
.sc-susp-item{padding:10px 0;border-bottom:1px solid #1a1a1a}
.sc-susp-item:last-child{border-bottom:none}
.sc-susp-label{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.sc-susp-name{font-size:13px;font-weight:600;color:#ccc}
.sc-priority-badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;
                   padding:2px 8px;border-radius:8px}
.sc-priority-badge.high{background:#2d0a0a;color:#f44336}
.sc-priority-badge.medium{background:#1a1000;color:#ff9800}
.sc-options{display:flex;flex-direction:column;gap:4px}
.sc-option{display:flex;align-items:flex-start;gap:8px;font-size:12px;color:#888}
.sc-opt-num{color:#c084fc;font-weight:700;flex-shrink:0;width:16px}
.sc-opt-text{flex:1}

/* ── Track map ── */
.tm-controls{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.tm-btn{background:#1e1e1e;border:1px solid #333;color:#666;padding:5px 14px;
        border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;
        text-transform:uppercase;letter-spacing:.3px;transition:background .15s,color .15s}
.tm-btn:hover{background:#252525;color:#aaa}
.tm-btn.active{background:#0d2a40;border-color:#2196F3;color:#64b5f6}

/* ── Track map tooltip ── */
#tm-tooltip{position:fixed;background:#1a1a1a;border:1px solid #2a2a2a;color:#e0e0e0;
            padding:8px 12px;border-radius:6px;font-size:11px;pointer-events:none;
            display:none;z-index:1000;line-height:1.7;min-width:100px;
            box-shadow:0 4px 12px rgba(0,0,0,.5)}

/* ── Input trace (3-panel) ── */
.input-trace-wrap{background:#181818;border-radius:10px;padding:16px 18px}
.it-panel-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
                color:#555;margin:8px 0 2px}
.it-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px}
.it-legend-item{display:flex;align-items:center;gap:5px}
.it-legend-dot{width:18px;height:3px;border-radius:2px}

/* ── Sector splits ── */
.sector-splits-table{background:#181818;border-radius:10px;overflow:hidden}
.ss-row{display:grid;align-items:center;padding:9px 18px;border-bottom:1px solid #222;
        font-size:12px;color:#777}
.ss-row:last-child{border-bottom:none}
.ss-header{background:#111;position:sticky;top:0}
.ss-header div{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#555}
.ss-best{color:#c084fc;font-weight:700}
.ss-delta{font-size:11px}
.ss-delta.fast{color:#4caf50}.ss-delta.med{color:#ff9800}.ss-delta.slow{color:#f44336}

/* ── Tyre trend ── */
.tyre-trend-wrap{background:#181818;border-radius:10px;padding:16px 18px}
.tt-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px}
.tt-legend-item{display:flex;align-items:center;gap:5px}
.tt-legend-dot{width:14px;height:3px;border-radius:2px}

/* ── Speed trace ── */
.speed-trace-wrap{background:#181818;border-radius:10px;padding:16px 18px}
.st-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px}
.st-legend-item{display:flex;align-items:center;gap:5px}
.st-legend-dot{width:18px;height:3px;border-radius:2px}

/* ── Stints ── */
.stint-table{background:#181818;border-radius:10px;overflow:hidden}
.stint-row{display:grid;grid-template-columns:52px 80px 50px 90px 90px 80px;
           align-items:center;padding:9px 18px;border-bottom:1px solid #222;
           font-size:12px;color:#777}
.stint-row:last-child{border-bottom:none}
.stint-header{background:#111;position:sticky;top:0}
.stint-header div{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#555}

/* ── Compare ── */
.compare-wrap{background:#181818;border:2px dashed #2a2a2a;border-radius:14px;padding:28px;margin-top:16px;display:none}
.compare-wrap.visible{display:block}
.compare-inputs{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:16px}
.compare-file-group{flex:1;min-width:200px;display:flex;flex-direction:column;gap:6px}
.compare-file-group label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#555}
.compare-file-btn{background:#1e1e1e;border:1px solid #333;color:#777;padding:8px 14px;
                  border-radius:6px;font-size:12px;cursor:pointer;text-align:left}
.compare-file-btn.has-file{border-color:#2196F3;color:#64b5f6}
.btn-compare-go{background:#0d2a40;border:1px solid #2196F3;color:#64b5f6;padding:8px 20px;
                border-radius:6px;font-size:13px;font-weight:700;cursor:pointer}
.btn-compare-go:disabled{opacity:.4;cursor:default}
.compare-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.compare-col{background:#141414;border-radius:8px;padding:14px 18px}
.compare-col-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
                   color:#2196F3;margin-bottom:10px}
.compare-stat-row{display:flex;justify-content:space-between;align-items:center;
                  padding:5px 0;border-bottom:1px solid #1e1e1e;font-size:12px;color:#888}
.compare-stat-row:last-child{border-bottom:none}
.compare-stat-label{color:#555}
.compare-stat-val{font-weight:600;color:#ccc}
.compare-better{color:#4caf50}
.compare-worse{color:#f44336}

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
.lap-excluded { opacity: 0.4; text-decoration: line-through; }
.lap-excluded td { color: #555 !important; }
.btn{display:inline-flex;align-items:center;justify-content:center;padding:7px 16px;
     border-radius:6px;font-size:13px;font-weight:700;cursor:pointer;border:none}
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
  <div class="config-group" style="flex:0;min-width:120px">
    <label>Air Temp</label>
    <div style="display:flex;align-items:center;gap:6px">
      <input type="number" id="air-temp" placeholder="—" min="0" max="130" step="1"
             style="background:#1e1e1e;border:1px solid #333;color:#e0e0e0;padding:8px 10px;
                    border-radius:6px;font-size:13px;width:70px">
      <span style="font-size:13px;color:#555">°F</span>
    </div>
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

<div id="library-banner" style="display:none;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;background:#0c1a3d;border:1px solid #1f3a7a;border-radius:8px;padding:12px 18px;margin-bottom:16px;font-size:14px;color:#93c5fd;">
  <span>Opened from <strong>iRacing Setup Library</strong> — car &amp; track pre-selected. Upload a telemetry file to analyze this setup.</span>
  <a href="http://localhost:5057" target="_blank" style="color:#93c5fd;font-weight:600;white-space:nowrap;">← Back to Library</a>
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
<div style="text-align:center;padding:0 24px 8px">
  <button onclick="toggleCompare()" id="btn-compare-toggle"
          style="background:none;border:1px solid #333;color:#555;padding:5px 16px;
                 border-radius:6px;font-size:12px;cursor:pointer">
    ⚖ Compare two files
  </button>
</div>
<div class="compare-wrap" id="compare-wrap">
  <div style="font-size:13px;font-weight:700;color:#999;margin-bottom:14px">Compare two .ibt files</div>
  <div class="compare-inputs">
    <div class="compare-file-group">
      <label>File A</label>
      <button class="compare-file-btn" id="cmp-btn-a" onclick="document.getElementById('cmp-fi-a').click()">Choose .ibt file…</button>
      <input type="file" id="cmp-fi-a" accept=".ibt" style="display:none">
    </div>
    <div class="compare-file-group">
      <label>File B</label>
      <button class="compare-file-btn" id="cmp-btn-b" onclick="document.getElementById('cmp-fi-b').click()">Choose .ibt file…</button>
      <input type="file" id="cmp-fi-b" accept=".ibt" style="display:none">
    </div>
    <div style="display:flex;align-items:flex-end">
      <button class="btn-compare-go" id="btn-cmp-go" onclick="runCompare()" disabled>Compare →</button>
    </div>
  </div>
  <div id="compare-results"></div>
</div>
<div id="sto-wrap" style="margin-top:16px">
  <button onclick="toggleStoPanel()" class="btn" style="background:#374151;font-size:.8rem;padding:6px 14px">
    🔧 Analyze Setup (.sto)
  </button>
  <div id="sto-panel" style="display:none;margin-top:10px;background:#1e293b;border-radius:10px;padding:14px">
    <div id="sto-drop" style="border:2px dashed #374151;border-radius:8px;padding:20px;text-align:center;color:#64748b;font-size:.85rem;cursor:pointer"
         onclick="document.getElementById('sto-file-input').click()"
         ondragover="event.preventDefault()"
         ondrop="handleStoDrop(event)">
      Drop your .sto setup file here or click to browse
      <div style="font-size:.75rem;margin-top:6px;color:#4b5563">Parameters decoded and cross-referenced with your telemetry</div>
    </div>
    <input type="file" id="sto-file-input" accept=".sto" style="display:none" onchange="handleStoFile(this.files[0])">
    <div id="sto-loading" style="display:none;text-align:center;padding:16px;color:#555;font-size:13px">Decoding setup…</div>
    <div id="sto-analysis-output" style="display:none"></div>
  </div>
</div>
<div id="tm-tooltip"></div>
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
  // URL params from Setup Library take priority, then fall back to localStorage
  const urlParams  = new URLSearchParams(window.location.search);
  const urlCar     = urlParams.get('car');
  const urlTrack   = urlParams.get('track');
  const savedCar   = urlCar   || localStorage.getItem('iracing-car');
  const savedTrack = urlTrack || localStorage.getItem('iracing-track');
  if (savedCar)   document.getElementById('car-select').value   = savedCar;
  if (savedTrack) document.getElementById('track-select').value = savedTrack;
  if (urlCar || urlTrack) {
    document.getElementById('library-banner').style.display = 'flex';
  }
  updatePreview();
}
loadOptions();

// ── Preview panel ─────────────────────────────────────────────────────────────
function updatePreview() {
  const carId   = document.getElementById('car-select').value;
  const trackId = document.getElementById('track-select').value;
  if (carId)   localStorage.setItem('iracing-car',   carId);
  if (trackId) localStorage.setItem('iracing-track', trackId);
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
  if (e.dataTransfer.files[0]) {
    window._excludedLaps = new Set();
    const reBtn2 = document.getElementById('reanalyze-btn');
    if (reBtn2) reBtn2.style.display = 'none';
    go(e.dataTransfer.files[0]);
  }
});
fi.addEventListener('change', () => {
  if (fi.files[0]) {
    window._excludedLaps = new Set();
    const reBtn2 = document.getElementById('reanalyze-btn');
    if (reBtn2) reBtn2.style.display = 'none';
    go(fi.files[0]);
  }
});

async function go(file) {
  if (!file) return;
  window._ibtFile = file;
  const carId   = document.getElementById('car-select').value;
  const trackId = document.getElementById('track-select').value;

  setStatus('Parsing ' + file.name + ' …');
  document.getElementById('results').style.display = 'none';

  const form = new FormData();
  form.append('file',     file);
  form.append('car',      carId);
  form.append('track',    trackId);
  form.append('air_temp_f', document.getElementById('air-temp').value || '');
  form.append('excluded_laps', JSON.stringify([...window._excludedLaps]));

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

window._selectedLap = null;
window._excludedLaps = new Set();

function toggleExcludeLap(lapNum) {
  if (window._excludedLaps.has(lapNum)) {
    window._excludedLaps.delete(lapNum);
  } else {
    window._excludedLaps.add(lapNum);
  }
  // Update visual state of all rows for this lap
  document.querySelectorAll(`[data-lap="${lapNum}"]`).forEach(el => {
    el.classList.toggle('lap-excluded', window._excludedLaps.has(lapNum));
  });
  // Show/hide re-analyze button
  const btn = document.getElementById('reanalyze-btn');
  if (btn) btn.style.display = window._excludedLaps.size > 0 ? 'inline-flex' : 'none';
}

function selectLap(lapNum) {
  window._selectedLap = (window._selectedLap === lapNum) ? null : lapNum;
  // Highlight lap row
  document.querySelectorAll('.lap-row[data-lap]').forEach(el => {
    el.style.outline = (parseInt(el.dataset.lap) === window._selectedLap) ? '2px solid #2196F3' : '';
    el.style.background = (parseInt(el.dataset.lap) === window._selectedLap) ? '#0d2a40' : '';
  });
  // Highlight tyre trend row
  document.querySelectorAll('.tt-row[data-lap]').forEach(el => {
    el.style.outline = (parseInt(el.dataset.lap) === window._selectedLap) ? '2px solid #2196F3' : '';
    el.style.background = (parseInt(el.dataset.lap) === window._selectedLap) ? '#0d2a40' : '';
  });
  // Highlight lap delta dot
  document.querySelectorAll('[data-lap-dot]').forEach(el => {
    const isSelected = parseInt(el.dataset.lapDot) === window._selectedLap;
    el.setAttribute('r', isSelected ? '7' : '4');
    el.setAttribute('stroke-width', isSelected ? '2.5' : '1');
  });
}

function reset() {
  document.getElementById('results').style.display     = 'none';
  document.getElementById('upload-wrap').style.display = '';
  document.getElementById('fi').value = '';
  setStatus('');
  window._trackMapData = null;
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

function renderTrackTempBadge(trackTempF, airTempF) {
  if (!trackTempF && !airTempF) return '';
  const parts = [];
  if (trackTempF != null) {
    const c = Math.round((trackTempF - 32) * 5 / 9);
    const col  = trackTempF < 59  ? '#60a5fa' :   // cold — blue
                 trackTempF < 77  ? '#86efac' :   // cool — green
                 trackTempF < 95  ? '#fbbf24' :   // warm — amber
                                    '#f87171';    // hot  — red
    const label = trackTempF < 59  ? 'Cold' :
                  trackTempF < 77  ? 'Cool' :
                  trackTempF < 95  ? 'Warm' : 'Hot';
    parts.push(`<span style="display:inline-flex;align-items:center;gap:6px;background:#181818;border:1px solid ${col};border-radius:8px;padding:5px 14px;font-size:13px">
      <span style="font-size:16px">🌡</span>
      <span style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.5px">Track</span>
      <span style="color:${col};font-weight:700">${trackTempF} °F</span>
      <span style="color:#666;font-size:11px">(${c} °C)</span>
      <span style="background:${col};color:#111;border-radius:4px;padding:1px 7px;font-size:11px;font-weight:700">${label}</span>
    </span>`);
  }
  if (airTempF != null) {
    const c = Math.round((airTempF - 32) * 5 / 9);
    parts.push(`<span style="display:inline-flex;align-items:center;gap:6px;background:#181818;border:1px solid #475569;border-radius:8px;padding:5px 14px;font-size:13px">
      <span style="font-size:16px">💨</span>
      <span style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.5px">Air</span>
      <span style="color:#cbd5e1;font-weight:700">${airTempF} °F</span>
      <span style="color:#666;font-size:11px">(${c} °C)</span>
    </span>`);
  }
  const note = trackTempF != null
    ? (trackTempF < 68 ? ' Cold track — start with pressures 0.5–1 psi higher than baseline.' :
       trackTempF > 90 ? ' Hot track — pressures will build quickly; check hot readings carefully.' : '')
    : '';
  return `<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
    ${parts.join('')}
    ${note ? `<span style="font-size:12px;color:#94a3b8;font-style:italic">${note}</span>` : ''}
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

function renderSetupCard(sc, car, track) {
  if (!sc) return '';
  const t = sc.tyres || {};
  const pressures = t.pressures || [];
  const cambers   = t.camber   || [];
  const susp      = sc.suspension || [];

  // ── Tyre pressure grid ───────────────────────────────────────────────────
  const psiCells = pressures.map(p => {
    if (p.status === 'no_data') return `
      <div class="sc-psi-cell">
        <div class="sc-psi-corner">${p.corner}</div>
        <div class="sc-psi-hot psi-ok" style="font-size:13px;color:#333">No data</div>
      </div>`;
    const cls   = p.status === 'over' ? 'psi-over' : p.status === 'under' ? 'psi-under' : 'psi-ok';
    const adjCls = p.status === 'over' ? 'adj-over' : p.status === 'under' ? 'adj-under' : 'adj-ok';
    const adjStr = p.status === 'ok'
      ? '✓ On target'
      : (p.cold_adj > 0 ? `+${p.cold_adj}` : `${p.cold_adj}`) + ' psi cold';
    return `
      <div class="sc-psi-cell">
        <div class="sc-psi-corner">${p.corner}</div>
        <div class="sc-psi-hot ${cls}">${p.hot_psi} <span style="font-size:11px;font-weight:400">psi</span></div>
        <div class="sc-psi-target">target ${p.target_hot_psi} psi hot</div>
        <div class="sc-psi-adj ${adjCls}">${adjStr}</div>
      </div>`;
  }).join('');

  // ── Camber ───────────────────────────────────────────────────────────────
  let camberHtml = '';
  if (cambers.length) {
    const rows = cambers.map(c => {
      const dir = c.direction === 'add' ? 'Add' : 'Reduce';
      const why = c.direction === 'add'
        ? `outer ${Math.abs(c.spread_f)}°F hotter — rolling onto outside edge`
        : `inner ${Math.abs(c.spread_f)}°F hotter — too much negative camber`;
      return `<div class="sc-camber-row">
        <div class="sc-camber-corner">${c.corner}</div>
        <div class="sc-camber-action">${dir} negative camber <b>${c.range}</b></div>
        <div class="sc-camber-detail">${why}</div>
      </div>`;
    }).join('');
    camberHtml = `<div class="sc-section">
      <div class="sc-section-title">Camber</div>${rows}</div>`;
  }

  // ── Suspension (grouped by sector) ───────────────────────────────────────
  let suspHtml = '';
  if (susp.length) {
    // Group items by sector, preserving priority sort order within each group
    const sectorOrder = [];
    const bySector = {};
    susp.forEach(s => {
      const sec = s.sector || 'ALL';
      if (!bySector[sec]) { bySector[sec] = []; sectorOrder.push(sec); }
      bySector[sec].push(s);
    });
    const groups = sectorOrder.map(sec => {
      const items = bySector[sec].map(s => {
        if (s.issue === 'neutral') {
          return `<div class="sc-susp-item" style="color:#4caf50;font-size:.82rem;padding:6px 0">✓ No suspension changes needed</div>`;
        }
        const optItems = s.options.map((o, i) =>
          `<div class="sc-option"><span class="sc-opt-num">${i+1}</span><span class="sc-opt-text">${o}</span></div>`
        ).join('');
        return `<div class="sc-susp-item">
          <div class="sc-susp-label">
            <span class="sc-susp-name">${s.label}</span>
            <span class="sc-priority-badge ${s.priority}">${s.priority}</span>
          </div>
          <div class="sc-options">${optItems}</div>
        </div>`;
      }).join('');
      const secHeader = sec !== 'ALL'
        ? `<div style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b;padding:6px 0 2px 0;margin-top:4px">${sec}</div>`
        : '';
      return secHeader + items;
    }).join('');
    suspHtml = `<div class="sc-section">
      <div class="sc-section-title">Suspension — by sector</div>${groups}</div>`;
  }

  if (!psiCells && !camberHtml && !suspHtml) return '';

  return `<div class="section-label">Setup card
    <button class="btn-print" onclick="printCard()" style="float:right;margin-top:-2px">&#128438; Print / Save PDF</button>
  </div>
  <div class="setup-card" id="setup-card-block">
    <div class="sc-header">
      <span class="sc-title">Garage adjustments — ${car || 'Car'} at ${track || 'Track'}</span>
    </div>
    <div class="sc-section">
      <div class="sc-section-title">Tyre pressures — adjust cold to hit hot targets</div>
      <div class="sc-psi-grid">${psiCells}</div>
    </div>
    ${camberHtml}
    ${suspHtml}
  </div>`;
}

function printCard() {
  const card = document.getElementById('setup-card-block');
  if (!card) return;
  const win = window.open('', '_blank');
  win.document.write(`<!DOCTYPE html><html><head><title>Setup Card</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fff;color:#111;padding:24px;font-size:13px}
    h2{font-size:16px;margin-bottom:16px}
    .sc-psi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
    .sc-psi-cell{border:1px solid #ddd;border-radius:6px;padding:10px;text-align:center}
    .sc-psi-corner{font-size:10px;font-weight:700;text-transform:uppercase;color:#888;margin-bottom:4px}
    .sc-psi-hot{font-size:18px;font-weight:700}
    .sc-psi-hot.psi-over{color:#c00}.sc-psi-hot.psi-under{color:#00c}.sc-psi-hot.psi-ok{color:#080}
    .sc-psi-target{font-size:11px;color:#888;margin-top:2px}
    .sc-psi-adj{font-size:12px;font-weight:700;margin-top:6px}
    .adj-over{color:#c00}.adj-under{color:#00c}.adj-ok{color:#080}
    .section{margin:16px 0}
    .sec-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#999;margin-bottom:8px}
    .camber-row{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid #eee}
    .susp-item{padding:8px 0;border-bottom:1px solid #eee}
    .susp-label{font-weight:600;margin-bottom:4px}
    .option{display:flex;gap:6px;font-size:12px;color:#444;margin:2px 0}
    .opt-num{color:#7c3aed;font-weight:700;width:16px}
    @media print{body{padding:0}}
  </style></head><body>` + card.innerHTML + `</body></html>`);
  win.document.close();
  win.print();
}

function renderTrackMap(tm) {
  if (!tm || !tm.points || tm.points.length < 20) return '';
  window._trackMapData = tm;
  return `<div class="section-label">Track map — Lap ${tm.lap || '?'} &nbsp;·&nbsp; ${tm.max_speed} mph top speed</div>
<div style="background:#181818;border-radius:10px;padding:16px 18px">
  <div class="tm-controls">
    <button class="tm-btn active" data-mode="speed"    onclick="drawTrackMap('speed')">Speed</button>
    <button class="tm-btn"        data-mode="throttle" onclick="drawTrackMap('throttle')">Throttle</button>
    <button class="tm-btn"        data-mode="brake"    onclick="drawTrackMap('brake')">Brake</button>
    <button class="tm-btn"        data-mode="gear"     onclick="drawTrackMap('gear')">Gear</button>
    <button class="tm-btn"        data-mode="balance"  onclick="drawTrackMap('balance')">Balance</button>
    <button class="tm-btn" onclick="saveTrackMapPNG()" style="margin-left:auto">&#8595; PNG</button>
  </div>
  <svg id="track-map-svg" style="width:100%;max-height:500px;display:block"></svg>
  <div style="display:flex;gap:18px;margin-top:10px;font-size:11px;color:#444;flex-wrap:wrap;align-items:center">
    <span id="tm-legend-bar"></span>
    <span>&#9679; Sector split</span>
    <span>&#9675; Start / Finish</span>
  </div>
</div>`;
}

function drawTrackMap(mode) {
  if (mode) localStorage.setItem('iracing-tm-mode', mode);
  const tm = window._trackMapData;
  if (!tm) return;
  const pts = tm.points;

  document.querySelectorAll('.tm-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));

  // Bounding box of Python-normalised coords (0–1000 range)
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  pts.forEach(p => {
    if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
  });
  const PAD = 36;
  const VW  = (maxX - minX) + 2 * PAD;
  const VH  = (maxY - minY) + 2 * PAD;
  const ox  = -minX + PAD;
  const oy  = -minY + PAD;

  function valColor(v, m) {
    const c = x => Math.max(0, Math.min(1, x));
    const L = (a, b, t) => Math.round(a + (b - a) * c(t));
    const lRGB = (c1, c2, t) => [L(c1[0],c2[0],t), L(c1[1],c2[1],t), L(c1[2],c2[2],t)];
    if (m === 'speed') {
      const stops = [[26,79,216],[0,188,212],[76,175,80],[255,235,59],[244,67,54]];
      const t4 = c(v) * 4, i = Math.min(3, Math.floor(t4));
      const [r,g,b] = lRGB(stops[i], stops[i+1], t4 - i);
      return `rgb(${r},${g},${b})`;
    }
    if (m === 'throttle') {
      const [r,g,b] = lRGB([26,26,26], [0,230,118], c(v));
      return `rgb(${r},${g},${b})`;
    }
    if (m === 'gear') {
      const gc = [[60,60,60],[244,67,54],[255,152,0],[255,235,59],[76,175,80],[0,188,212],[33,150,243],[124,77,255],[224,64,251]];
      const [r,g,b] = gc[Math.min(8, Math.max(0, Math.round(v)))];
      return `rgb(${r},${g},${b})`;
    }
    if (m === 'balance') {
      // v is us_idx: <0.85 oversteer (red), ~1.0 neutral (gray), >1.15 understeer (blue)
      const t = Math.max(0, Math.min(1, (v - 0.7) / 0.6));   // 0=OS, 0.5=neutral, 1=US
      if (t < 0.5) { const [r,g,b] = lRGB([244,67,54],[80,80,80],t*2); return `rgb(${r},${g},${b})`; }
      else          { const [r,g,b] = lRGB([80,80,80],[33,150,243],(t-0.5)*2); return `rgb(${r},${g},${b})`; }
    }
    const [r,g,b] = lRGB([26,26,26], [244,67,54], c(v));
    return `rgb(${r},${g},${b})`;
  }

  const mx = tm.max_speed;
  let svg = '';

  // Coloured track path
  for (let i = 1; i < pts.length; i++) {
    const p0 = pts[i-1], p1 = pts[i];
    const v = mode === 'speed'    ? ((p0.spd + p1.spd) / 2) / mx
            : mode === 'throttle' ? (p0.thr + p1.thr) / 2
            : mode === 'gear'     ? ((p0.gear || 0) + (p1.gear || 0)) / 2
            : mode === 'balance'  ? ((p0.us  || 1) + (p1.us  || 1)) / 2
            :                       (p0.brk + p1.brk) / 2;
    const col = valColor(v, mode);
    svg += `<line x1="${(p0.x+ox).toFixed(1)}" y1="${(p0.y+oy).toFixed(1)}" `
         + `x2="${(p1.x+ox).toFixed(1)}" y2="${(p1.y+oy).toFixed(1)}" `
         + `stroke="${col}" stroke-width="3.5" stroke-linecap="round"/>`;
  }

  // Sector boundary markers
  (tm.sectors || []).forEach((s, si) => {
    if (s.start <= 0.005) return;
    let closest = 0, minD = Infinity;
    pts.forEach((p, i) => { const d = Math.abs(p.pct - s.start); if (d < minD) { minD = d; closest = i; } });
    const p = pts[closest];
    svg += `<circle cx="${(p.x+ox).toFixed(1)}" cy="${(p.y+oy).toFixed(1)}" r="5" fill="#f59e0b" stroke="#111" stroke-width="1.5"/>`;
    svg += `<text x="${(p.x+ox+8).toFixed(0)}" y="${(p.y+oy+4).toFixed(0)}" fill="#f59e0b" font-size="12" font-family="sans-serif" font-weight="700">S${si+1}</text>`;
  });

  // Start / finish ring
  if (pts.length > 0) {
    const sf = pts[0];
    svg += `<circle cx="${(sf.x+ox).toFixed(1)}" cy="${(sf.y+oy).toFixed(1)}" r="6" fill="none" stroke="#fff" stroke-width="2.5"/>`;
    svg += `<circle cx="${(sf.x+ox).toFixed(1)}" cy="${(sf.y+oy).toFixed(1)}" r="2.5" fill="#fff"/>`;
  }

  // Throttle application points (orange dots)
  (tm.throttle_apps || []).forEach(ap => {
    svg += `<circle cx="${(ap.x+ox).toFixed(1)}" cy="${(ap.y+oy).toFixed(1)}" r="4" fill="#f97316" stroke="#111" stroke-width="1" opacity="0.85"/>`;
  });

  // Corner minimum speed dots (cyan) with speed label
  (tm.corner_mins || []).forEach(cm => {
    svg += `<circle cx="${(cm.x+ox).toFixed(1)}" cy="${(cm.y+oy).toFixed(1)}" r="5" fill="#06b6d4" stroke="#111" stroke-width="1" opacity="0.9"/>`;
    svg += `<text x="${(cm.x+ox+7).toFixed(0)}" y="${(cm.y+oy+4).toFixed(0)}" fill="#06b6d4" font-size="9" font-family="sans-serif" font-weight="600">${cm.spd.toFixed(0)}</text>`;
  });

  // Coast zone overlay — grey segments where throttle < 5% and brake < 5%
  {
    const _coastSegs = [];
    let _cSeg = null;
    pts.forEach(p => {
      if (p.thr < 0.05 && p.brk < 0.05) {
        if (!_cSeg) _cSeg = [];
        _cSeg.push(p);
      } else {
        if (_cSeg && _cSeg.length > 1) _coastSegs.push(_cSeg);
        _cSeg = null;
      }
    });
    if (_cSeg && _cSeg.length > 1) _coastSegs.push(_cSeg);
    _coastSegs.forEach(seg => {
      const d = seg.map((p, i) => `${i===0?'M':'L'}${(p.x+ox).toFixed(1)},${(p.y+oy).toFixed(1)}`).join(' ');
      svg += `<path d="${d}" fill="none" stroke="rgba(200,200,200,0.45)" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>`;
    });
  }

  const svgEl = document.getElementById('track-map-svg');
  if (svgEl) {
    svgEl.setAttribute('viewBox', `0 0 ${VW.toFixed(0)} ${VH.toFixed(0)}`);
    svgEl.innerHTML = svg;
  }

  // Update colour legend
  const leg = document.getElementById('tm-legend-bar');
  if (leg) {
    const gradients = {
      speed:    'linear-gradient(to right,rgb(26,79,216),rgb(0,188,212),rgb(76,175,80),rgb(255,235,59),rgb(244,67,54))',
      throttle: 'linear-gradient(to right,rgb(26,26,26),rgb(0,230,118))',
      brake:    'linear-gradient(to right,rgb(26,26,26),rgb(244,67,54))',
      gear:     'linear-gradient(to right,rgb(244,67,54),rgb(255,152,0),rgb(255,235,59),rgb(76,175,80),rgb(0,188,212),rgb(33,150,243),rgb(124,77,255),rgb(224,64,251))',
      balance:  'linear-gradient(to right,rgb(244,67,54),rgb(80,80,80),rgb(33,150,243))',
    };
    const labels = { speed: `Slow → ${mx} mph`, throttle: '0 → 100% throttle', brake: '0 → 100% brake', gear: '1st → 8th gear', balance: 'Oversteer ← Neutral → Understeer' };
    leg.innerHTML = `<span style="display:inline-flex;align-items:center;gap:6px">
      <span style="display:inline-block;width:80px;height:6px;border-radius:3px;background:${gradients[mode]}"></span>
      <span>${labels[mode]}</span></span>
      <span style="display:inline-flex;align-items:center;gap:4px;margin-left:12px;opacity:0.7">
        <span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:rgba(200,200,200,0.6)"></span>
        <span style="font-size:11px">coast</span>
      </span>`;
  }

  if (svgEl) {
    if (svgEl._tmMousemove)  svgEl.removeEventListener('mousemove',  svgEl._tmMousemove);
    if (svgEl._tmMouseleave) svgEl.removeEventListener('mouseleave', svgEl._tmMouseleave);
    const tt = document.getElementById('tm-tooltip');
    const _pts = pts, _ox = ox, _oy = oy;
    svgEl._tmMousemove = function(e) {
      const rect = svgEl.getBoundingClientRect();
      const vb = svgEl.viewBox.baseVal;
      if (!vb.width) return;
      const mx = (e.clientX - rect.left) / rect.width * vb.width;
      const my = (e.clientY - rect.top)  / rect.height * vb.height;
      let bestIdx = 0, bestD = Infinity;
      _pts.forEach((p, i) => {
        const d = (p.x + _ox - mx) ** 2 + (p.y + _oy - my) ** 2;
        if (d < bestD) { bestD = d; bestIdx = i; }
      });
      const bp = _pts[bestIdx];
      const _thresh = (vb.width * 0.06) ** 2;
      if (tt && bestD < _thresh) {
        const gStr = bp.gear != null ? `<br>Gear: <b>${bp.gear || 'N'}</b>` : '';
        tt.innerHTML = `<b>${bp.spd.toFixed(0)} mph</b><br>Throttle: ${(bp.thr*100).toFixed(0)}%<br>Brake: ${(bp.brk*100).toFixed(0)}%${gStr}<br><span style="color:#555">${(bp.pct*100).toFixed(1)}% lap</span>`;
        tt.style.display = 'block';
        tt.style.left = (e.clientX + 16) + 'px';
        tt.style.top  = (e.clientY - 8)  + 'px';
      } else if (tt) { tt.style.display = 'none'; }
    };
    svgEl._tmMouseleave = function() { if (tt) tt.style.display = 'none'; };
    svgEl.addEventListener('mousemove',  svgEl._tmMousemove);
    svgEl.addEventListener('mouseleave', svgEl._tmMouseleave);
  }
}

function saveTrackMapPNG() {
  const svgEl = document.getElementById('track-map-svg');
  if (!svgEl) return;
  const vb = svgEl.viewBox.baseVal;
  if (!vb.width) return;
  const scale = 2, w = Math.round(vb.width * scale), h = Math.round(vb.height * scale);
  const svgData = new XMLSerializer().serializeToString(svgEl);
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#181818';
  ctx.fillRect(0, 0, w, h);
  const blob = new Blob([svgData], {type: 'image/svg+xml;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0, w, h);
    URL.revokeObjectURL(url);
    const a = document.createElement('a');
    const tm = window._trackMapData;
    a.download = tm ? `track-map-lap${tm.lap}.png` : 'track-map.png';
    a.href = canvas.toDataURL('image/png');
    a.click();
  };
  img.src = url;
}

// ── Input Trace (3-panel speed / throttle / brake) ───────────────────────────
function renderInputTrace(st) {
  if (!st || !st.laps || !st.laps.length) return '';
  const W = 600, PL = 44, PR = 12, PT = 6, PB = 18;
  const panels = [
    {key: 'spd', label: 'Speed (mph)', h: 100, color: '#c084fc', max: null},
    {key: 'thr', label: 'Throttle %',  h: 60,  color: '#4caf50', max: 1},
    {key: 'brk', label: 'Brake %',     h: 60,  color: '#f44336', max: 1},
  ];

  // Determine max speed across all laps
  let maxSpd = 0;
  st.laps.forEach(l => l.points.forEach(p => { if (p.spd > maxSpd) maxSpd = p.spd; }));
  panels[0].max = maxSpd || 1;

  const colors = ['#c084fc','#64b5f6','#81c784','#ffb74d','#f06292'];

  let totalH = panels.reduce((s, p) => s + p.h + PT + PB, 0) + 10;
  let svgContent = '';
  let yOffset = 0;

  panels.forEach((panel, pi) => {
    const IW = W - PL - PR, IH = panel.h;
    const yBase = yOffset + PT;

    // Background
    svgContent += `<rect x="${PL}" y="${yBase}" width="${IW}" height="${IH}" fill="#111" rx="3"/>`;

    // Grid lines (25%, 50%, 75%)
    for (let g = 25; g <= 75; g += 25) {
      const gy = (yBase + IH - (g / 100) * IH).toFixed(1);
      const val = panel.key === 'spd' ? Math.round(panel.max * g / 100) : g;
      svgContent += `<line x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}" stroke="#222" stroke-width="1"/>`;
      svgContent += `<text x="${PL - 4}" y="${parseFloat(gy) + 4}" text-anchor="end" fill="#444" font-size="9">${val}</text>`;
    }

    // Panel label
    svgContent += `<text x="${PL}" y="${yBase - 2}" fill="#555" font-size="9" font-weight="700" text-transform="uppercase">${panel.label}</text>`;

    // Draw non-best laps first (dimmed)
    st.laps.filter(l => !l.is_best).forEach((l, li) => {
      if (!l.points[0] || l.points[0][panel.key] == null) return;
      const col = colors[(li + 1) % colors.length];
      const pts = l.points.map(p => {
        const v = panel.key === 'spd' ? p.spd / panel.max : (p[panel.key] || 0);
        return `${(PL + p.pct * IW).toFixed(1)},${(yBase + IH - v * IH).toFixed(1)}`;
      }).join(' ');
      svgContent += `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1" opacity="0.35"/>`;
    });

    // Draw best lap on top
    st.laps.filter(l => l.is_best).forEach(l => {
      if (!l.points[0] || l.points[0][panel.key] == null) return;
      const pts = l.points.map(p => {
        const v = panel.key === 'spd' ? p.spd / panel.max : (p[panel.key] || 0);
        return `${(PL + p.pct * IW).toFixed(1)},${(yBase + IH - v * IH).toFixed(1)}`;
      }).join(' ');
      svgContent += `<polyline points="${pts}" fill="none" stroke="${panel.color}" stroke-width="1.8"/>`;
    });

    // X-axis labels on last panel only
    if (pi === panels.length - 1) {
      svgContent += `<text x="${PL}" y="${yBase + IH + 12}" fill="#444" font-size="9">0%</text>`;
      svgContent += `<text x="${W - PR}" y="${yBase + IH + 12}" text-anchor="end" fill="#444" font-size="9">100% lap</text>`;
    }

    yOffset += PT + IH + PB;
  });

  const legendItems = st.laps.map((l, i) => {
    const col = l.is_best ? '#c084fc' : colors[i % colors.length];
    const op  = l.is_best ? '1' : '0.5';
    return `<span class="it-legend-item" style="opacity:${op}">
      <span class="it-legend-dot" style="background:${col}"></span>
      Lap ${l.lap} — ${fmtLap(l.time_s)}${l.is_best ? ' ⬤' : ''}
    </span>`;
  }).join('');

  return `<div class="section-label">Driver inputs — Top ${st.laps.length} laps</div>
<div class="input-trace-wrap">
  <svg viewBox="0 0 ${W} ${totalH}" style="width:100%;display:block">${svgContent}</svg>
  <div class="it-legend">${legendItems}</div>
</div>`;
}

// ── Sector Splits ─────────────────────────────────────────────────────────────
function renderSectorSplits(st) {
  if (!st || !st.laps || !st.laps.length || !st.sectors) return '';
  const sectors  = st.sectors;
  const nSec     = sectors.length;
  const bestSpl  = st.best_splits || [];
  const colTpl   = `52px repeat(${nSec}, 1fr) 90px`;

  const headerCells = sectors.map((s, i) =>
    `<div>${s.name.replace(/ —.*/, '').trim()}</div>`
  ).join('');

  const rows = st.laps.map(l => {
    const splits = l.splits || [];
    const cells = splits.map((s, i) => {
      const best  = bestSpl[i];
      const isBst = best != null && Math.abs(s - best) < 0.001;
      const delta = best != null ? s - best : null;
      const dStr  = isBst ? '' : (delta != null ? `+${delta.toFixed(3)}` : '');
      const dCls  = delta == null ? '' : delta < 0.1 ? 'fast' : delta < 0.4 ? 'med' : 'slow';
      return `<div>
        <span class="${isBst ? 'ss-best' : ''}">${fmtLap(s)}</span>
        ${dStr ? `<span class="ss-delta ${dCls}"> ${dStr}</span>` : ''}
      </div>`;
    }).join('');
    return `<div class="ss-row" style="grid-template-columns:${colTpl}">
      <div style="color:#666">${l.lap}</div>
      ${cells}
      <div style="color:#aaa;font-weight:600">${fmtLap(l.total_s)}</div>
    </div>`;
  }).join('');

  const bestRow = `<div class="ss-row" style="grid-template-columns:${colTpl};background:#140a24">
    <div style="color:#a855f7;font-size:10px;font-weight:700">BEST</div>
    ${bestSpl.map(b => `<div class="ss-best">${b != null ? fmtLap(b) : '—'}</div>`).join('')}
    <div class="ss-best">${fmtLap(bestSpl.filter(b => b != null).reduce((a, b) => a + b, 0))}</div>
  </div>`;

  return `<div class="section-label">Sector split times</div>
<div class="sector-splits-table">
  <div class="ss-row ss-header" style="grid-template-columns:${colTpl}">
    <div>Lap</div>${headerCells}<div>Total</div>
  </div>
  ${bestRow}
  ${rows}
</div>`;
}

// ── Tyre Trend ────────────────────────────────────────────────────────────────
function renderTyreTrend(trend) {
  if (!trend || !trend.laps || trend.laps.length < 3) return '';
  const laps = trend.laps;
  const corners = ['LF','RF','LR','RR'];
  const cols    = {LF:'#64b5f6', RF:'#4caf50', LR:'#ff9800', RR:'#f44336'};
  const W = 600, PL = 44, PR = 12, PT = 10, PB = 24, H = 130;
  const IW = W - PL - PR, IH = H - PT - PB;

  // Find temp range across all corners and laps
  let minT = Infinity, maxT = -Infinity;
  laps.forEach(l => corners.forEach(c => {
    if (l[c] != null) { if (l[c] < minT) minT = l[c]; if (l[c] > maxT) maxT = l[c]; }
  }));
  if (minT === Infinity) return '';
  const pad = 5;
  minT = Math.floor(minT - pad);
  maxT = Math.ceil(maxT + pad);
  const tRange = maxT - minT || 1;

  const lapNums = laps.map(l => l.lap);
  const nLaps = lapNums.length;

  let svg = `<rect x="${PL}" y="${PT}" width="${IW}" height="${IH}" fill="#111" rx="3"/>`;

  // Gridlines
  for (let g = 0; g <= 1; g += 0.5) {
    const gy = (PT + IH - g * IH).toFixed(1);
    const tv = Math.round(minT + g * tRange);
    svg += `<line x1="${PL}" y1="${gy}" x2="${W-PR}" y2="${gy}" stroke="#222" stroke-width="1"/>`;
    svg += `<text x="${PL-4}" y="${parseFloat(gy)+4}" text-anchor="end" fill="#444" font-size="9">${tv}°F</text>`;
  }

  // X axis labels
  svg += `<text x="${PL}" y="${H-4}" fill="#444" font-size="9">Lap ${lapNums[0]}</text>`;
  svg += `<text x="${W-PR}" y="${H-4}" text-anchor="end" fill="#444" font-size="9">Lap ${lapNums[nLaps-1]}</text>`;

  // Lines per corner
  corners.forEach(c => {
    const pts = laps.map((l, i) => {
      if (l[c] == null) return null;
      const x = PL + (i / Math.max(nLaps - 1, 1)) * IW;
      const y = PT + IH - ((l[c] - minT) / tRange) * IH;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).filter(Boolean);
    if (pts.length < 2) return;
    svg += `<polyline points="${pts.join(' ')}" fill="none" stroke="${cols[c]}" stroke-width="1.8"/>`;
  });

  const legend = corners.map(c =>
    `<span class="tt-legend-item"><span class="tt-legend-dot" style="background:${cols[c]}"></span>${c}</span>`
  ).join('');

  return `<div class="section-label">Tyre temperature trend</div>
<div class="tyre-trend-wrap">
  <svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block">${svg}</svg>
  <div class="tt-legend">${legend}</div>
</div>`;
}

// ── Speed Trace ───────────────────────────────────────────────────────────────
function renderSpeedTrace(st) {
  if (!st || !st.laps || !st.laps.length) return '';
  const W = 600, H = 160, PL = 40, PR = 12, PT = 10, PB = 24;
  const IW = W - PL - PR, IH = H - PT - PB;
  let allSpds = [];
  st.laps.forEach(l => l.points.forEach(p => allSpds.push(p.spd)));
  const maxSpd = Math.max(...allSpds) || 1;
  const colors = ['#c084fc','#64b5f6','#81c784','#ffb74d','#f06292'];
  let svgLines = '';
  // Draw non-best laps first (dimmed)
  st.laps.filter(l => !l.is_best).forEach((l, li) => {
    const pts = l.points.map(p =>
      `${(PL + p.pct * IW).toFixed(1)},${(PT + IH - (p.spd / maxSpd) * IH).toFixed(1)}`
    ).join(' ');
    svgLines += `<polyline points="${pts}" fill="none" stroke="${colors[(li+1)%colors.length]}" stroke-width="1" opacity="0.4"/>`;
  });
  // Draw best lap on top
  st.laps.filter(l => l.is_best).forEach(l => {
    const pts = l.points.map(p =>
      `${(PL + p.pct * IW).toFixed(1)},${(PT + IH - (p.spd / maxSpd) * IH).toFixed(1)}`
    ).join(' ');
    svgLines += `<polyline points="${pts}" fill="none" stroke="#c084fc" stroke-width="2"/>`;
  });
  // Y-axis gridlines
  let gridLines = '';
  for (let g = 25; g <= 75; g += 25) {
    const y = (PT + IH - (g / 100) * IH).toFixed(1);
    const spd = Math.round(maxSpd * g / 100);
    gridLines += `<line x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}" stroke="#222" stroke-width="1"/>`;
    gridLines += `<text x="${PL - 4}" y="${parseFloat(y)+4}" text-anchor="end" fill="#444" font-size="9">${spd}</text>`;
  }
  // X-axis label
  gridLines += `<text x="${PL}" y="${H - 2}" fill="#444" font-size="9">0%</text>`;
  gridLines += `<text x="${W - PR}" y="${H - 2}" text-anchor="end" fill="#444" font-size="9">100% lap</text>`;
  // Legend
  const legendItems = st.laps.map((l, i) => {
    const col = l.is_best ? '#c084fc' : colors[(i) % colors.length];
    const opacity = l.is_best ? '1' : '0.5';
    return `<span class="st-legend-item" style="opacity:${opacity}">
      <span class="st-legend-dot" style="background:${col}"></span>
      Lap ${l.lap} — ${fmtLap(l.time_s)}${l.is_best ? ' ⬤' : ''}
    </span>`;
  }).join('');
  return `<div class="section-label">Speed trace — Top ${st.laps.length} laps</div>
<div class="speed-trace-wrap">
  <svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block">
    <rect x="${PL}" y="${PT}" width="${IW}" height="${IH}" fill="#111" rx="4"/>
    ${gridLines}
    ${svgLines}
  </svg>
  <div class="st-legend">${legendItems}</div>
</div>`;
}

// ── Stints ─────────────────────────────────────────────────────────────────────
function renderStints(stints) {
  if (!stints || stints.length < 2) return '';
  const rows = stints.map(s => `
    <div class="stint-row">
      <div style="font-weight:700;color:#aaa">Stint ${s.stint}</div>
      <div>L${s.start_lap}–${s.end_lap}</div>
      <div>${s.lap_count} laps</div>
      <div>${fmtLap(s.avg_lap_s) || '—'}</div>
      <div style="color:#c084fc">${fmtLap(s.best_lap_s) || '—'}</div>
      <div>${s.fuel_used_gal != null ? s.fuel_used_gal.toFixed(2) + ' gal' : '—'}</div>
    </div>`).join('');
  return `<div class="section-label">Stint analysis</div>
<div class="stint-table">
  <div class="stint-row stint-header">
    <div>Stint</div><div>Laps</div><div>Count</div><div>Avg time</div><div>Best time</div><div>Fuel used</div>
  </div>
  ${rows}
</div>`;
}

// ── Compare ───────────────────────────────────────────────────────────────────
function toggleCompare() {
  const wrap = document.getElementById('compare-wrap');
  wrap.classList.toggle('visible');
}

function toggleStoPanel() {
  const p = document.getElementById('sto-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}

async function handleStoDrop(e) {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) await handleStoFile(file);
}

// ── Telemetry cross-reference hints ──────────────────────────────────────────
// Maps setup parameter label keywords → how to interpret them against telemetry.
// Returns null if no relevant telemetry available, else {type, insight, priority}
function _crossRef(label, value, lastAnalysis, setupAnalysis) {
  const l   = label.toLowerCase();
  const val = parseFloat(value);

  // ── Setup-only insights (no telemetry needed) ──────────────────────────────
  if (setupAnalysis) {
    const sa = setupAnalysis;
    // Range warnings from setup analysis
    const rangeRec = (sa.recs || []).find(r => r.category === 'Range Limit' && r.text.toLowerCase().includes(label.toLowerCase().substring(0, 15)));
    if (rangeRec) {
      return {type: rangeRec.priority === 'high' ? 'warn' : 'info', text: `${rangeRec.text}. ${rangeRec.action}`};
    }
  }

  if (!lastAnalysis) return null;
  const recs = (lastAnalysis.recommendations || []);
  const handling = lastAnalysis.handling || {};

  // Determine overall balance tendency from handling sectors
  const tendencies = Object.values(handling).map(h => h.tendency).filter(Boolean);
  const usCount = tendencies.filter(t => t === 'understeer').length;
  const osCount = tendencies.filter(t => t === 'oversteer').length;
  const overallUS = usCount > osCount;
  const overallOS = osCount > usCount;

  // Enhance with setup analysis tendency
  const setupTendency = setupAnalysis ? setupAnalysis.tendencySummary : null;

  // Front anti-roll bar
  if (l.includes('front arb') || l.includes('front anti-roll') || (l.includes('arb') && l.includes('front'))) {
    if (overallUS) return {type:'warn', text:`Telemetry shows understeer — consider increasing front ARB to add front grip response, or decreasing rear ARB to free the rear.`};
    if (overallOS) return {type:'warn', text:`Telemetry shows oversteer — consider softening front ARB to reduce snap.`};
  }
  // Rear anti-roll bar
  if (l.includes('rear arb') || l.includes('rear anti-roll') || l.includes('rarb')) {
    if (overallUS) return {type:'info', text:`Understeer detected — softening rear ARB can free up rear rotation and reduce understeer.`};
    if (overallOS) return {type:'warn', text:`Oversteer detected — stiffening rear ARB may add stability.`};
  }
  // Springs
  if ((l.includes('spring') || l.includes('spring rate')) && (l.includes('front') || l.includes('left front') || l.includes('right front'))) {
    if (overallUS) return {type:'info', text:`Front understeer — softening front spring rate can increase front mechanical grip.`};
  }
  if ((l.includes('spring') || l.includes('spring rate')) && (l.includes('rear') || l.includes('left rear') || l.includes('right rear'))) {
    if (overallOS) return {type:'info', text:`Oversteer — stiffening rear spring rate can add rear stability.`};
  }
  // Tyre pressures — cross-ref with pressure recs
  const cornerMap = {'lf':'LF','rf':'RF','lr':'LR','rr':'RR','left front':'LF','right front':'RF','left rear':'LR','right rear':'RR'};
  for (const [kw, corner] of Object.entries(cornerMap)) {
    if (l.includes(kw) && (l.includes('pressure') || l.includes('psi'))) {
      const pRec = recs.find(r => r.category === 'Tyre Pressure' && r.corner === corner);
      if (pRec) return {type:'warn', text:`Telemetry: ${pRec.issue}. Suggested action: ${pRec.action}.`};
      const pressures = lastAnalysis.tyre_pressures || {};
      if (pressures[corner] != null) {
        return {type:'good', text:`Hot pressure measured at ${pressures[corner].toFixed(1)} psi at ${corner}.`};
      }
    }
  }
  // Camber
  for (const [kw, corner] of Object.entries(cornerMap)) {
    if (l.includes(kw) && l.includes('camber')) {
      const cRec = recs.find(r => r.category === 'Camber' && r.corner === corner);
      if (cRec) return {type:'warn', text:`Telemetry: ${cRec.issue}. ${cRec.action}.`};
    }
  }
  // Brake bias
  if (l.includes('brake bias') || l.includes('brake balance') || l.includes('brake pressure bias')) {
    const brk = lastAnalysis.balance;
    if (brk && brk.brake_balance_pct != null) {
      let extra = '';
      if (setupAnalysis && setupAnalysis.brakeBias != null) {
        const diff = setupAnalysis.brakeBias - brk.brake_balance_pct;
        if (Math.abs(diff) > 0.5) {
          extra = ` Setup bias ${setupAnalysis.brakeBias}% vs telemetry avg ${brk.brake_balance_pct.toFixed(1)}% — driver adjusted by ${diff > 0 ? '+' : ''}${diff.toFixed(1)}% during session.`;
        }
      }
      return {type:'info', text:`Session average brake balance: ${brk.brake_balance_pct.toFixed(1)}%.${extra}`};
    }
  }
  // Aero / downforce
  if (l.includes('wing') || l.includes('downforce') || l.includes('aero')) {
    if (overallUS) return {type:'info', text:`Understeer present — adding front aero (if available) can help balance.`};
  }
  // Combined insight: if telemetry and setup agree on tendency
  if (setupTendency && setupTendency !== 'balanced') {
    if (l.includes('spring') || l.includes('arb') || l.includes('damper')) {
      if (setupTendency === 'understeer' && overallUS) {
        return {type:'warn', text:`Both setup geometry and telemetry suggest understeer. This parameter contributes to the imbalance.`};
      }
      if (setupTendency === 'oversteer' && overallOS) {
        return {type:'warn', text:`Both setup geometry and telemetry suggest oversteer. This parameter contributes to the imbalance.`};
      }
    }
  }
  return null;
}

// ── Setup Recommendation Engine ──────────────────────────────────────────────
// Analyzes decoded .sto parameters and generates intelligent recommendations.
function analyzeSetup(tabs, carConfig) {
  const recs = [];
  const params = {};  // flat lookup by label-keyword

  // Flatten all parameters for easy lookup
  Object.values(tabs).forEach(sections => {
    Object.entries(sections).forEach(([sect, pList]) => {
      pList.forEach(p => {
        const key = (sect + ' ' + p.label).toLowerCase();
        params[key] = p;
        // Also store by just label
        params[p.label.toLowerCase()] = p;
      });
    });
  });

  // Helper: parse numeric value from metric_value string (e.g. "280 N/mm" → 280)
  function numVal(p) {
    if (!p || !p.value) return null;
    const m = p.value.match(/-?[\d.]+/);
    return m ? parseFloat(m[0]) : null;
  }

  // Helper: parse range bounds
  function rangeVal(str) {
    if (!str) return null;
    const m = str.match(/-?[\d.]+/);
    return m ? parseFloat(m[0]) : null;
  }

  // Helper: find parameter by partial label match
  function findParam(keywords) {
    const kws = keywords.map(k => k.toLowerCase());
    for (const [key, p] of Object.entries(params)) {
      if (kws.every(kw => key.includes(kw))) return p;
    }
    return null;
  }

  // Collect corner parameters
  const corners = {
    LF: {label:'Left Front'}, RF: {label:'Right Front'},
    LR: {label:'Left Rear'}, RR: {label:'Right Rear'}
  };
  const cornerKeys = {LF:'left front', RF:'right front', LR:'left rear', RR:'right rear'};

  Object.entries(cornerKeys).forEach(([corner, searchKey]) => {
    corners[corner].pressure = numVal(findParam([searchKey, 'pressure']));
    corners[corner].spring = numVal(findParam([searchKey, 'spring']));
    corners[corner].rideHeight = numVal(findParam([searchKey, 'ride height']));
    corners[corner].bumpRubber = numVal(findParam([searchKey, 'bump rubber']));
    corners[corner].camber = numVal(findParam([searchKey, 'camber']));
  });

  // Front/rear parameters
  const frontARB = numVal(findParam(['arb setting']));
  const rearARB = numVal(findParam(['rarb setting']));
  const wing = numVal(findParam(['wing setting']));
  const frontRHSpeed = numVal(findParam(['front rh at speed']));
  const rearRHSpeed = numVal(findParam(['rear rh at speed']));
  const brakeBias = numVal(findParam(['brake pressure bias'])) || numVal(findParam(['brake bias']));
  const frontMC = numVal(findParam(['front master']));
  const rearMC = numVal(findParam(['rear master']));
  const brakePads = (findParam(['brake pads']) || {}).value || '';

  // Damper parameters
  const dampers = {};
  ['Low Speed Compression', 'High Speed Compression', 'Low Speed Rebound', 'High Speed Rebound'].forEach(mode => {
    const frontP = findParam(['front dampers', mode.toLowerCase()]);
    const rearP = findParam(['rear dampers', mode.toLowerCase()]);
    dampers[mode] = {
      front: numVal(frontP),
      rear: numVal(rearP),
      frontRange: frontP ? {min: rangeVal(frontP.range_min), max: rangeVal(frontP.range_max)} : null,
      rearRange: rearP ? {min: rangeVal(rearP.range_min), max: rangeVal(rearP.range_max)} : null
    };
  });

  // ── 1. Range validation ────────────────────────────────────────────────────
  Object.values(tabs).forEach(sections => {
    Object.values(sections).forEach(pList => {
      pList.forEach(p => {
        if (!p.range_min || !p.range_max) return;
        const v = numVal(p);
        const mn = rangeVal(p.range_min);
        const mx = rangeVal(p.range_max);
        if (v == null || mn == null || mx == null) return;
        const range = mx - mn;
        if (range <= 0) return;
        if (v <= mn) {
          recs.push({category:'Range Limit', priority:'medium', text:`${p.label} is at minimum (${p.value})`, action:`Consider increasing — currently at the lowest possible setting.`});
        } else if (v >= mx) {
          recs.push({category:'Range Limit', priority:'medium', text:`${p.label} is at maximum (${p.value})`, action:`Consider decreasing — currently at the highest possible setting. You may be compensating for another issue.`});
        } else if ((v - mn) / range < 0.05) {
          recs.push({category:'Range Limit', priority:'low', text:`${p.label} is near minimum (${p.value}, range ${p.range_min}–${p.range_max})`, action:`Very close to the limit — check if this is intentional.`});
        } else if ((mx - v) / range < 0.05) {
          recs.push({category:'Range Limit', priority:'low', text:`${p.label} is near maximum (${p.value}, range ${p.range_min}–${p.range_max})`, action:`Very close to the limit — check if this is intentional.`});
        }
      });
    });
  });

  // ── 2. Cross-corner balance ────────────────────────────────────────────────
  // Springs: LF vs RF, LR vs RR
  if (corners.LF.spring != null && corners.RF.spring != null && corners.LF.spring !== corners.RF.spring) {
    const diff = Math.abs(corners.LF.spring - corners.RF.spring);
    recs.push({category:'Cross-Corner Balance', priority: diff > 20 ? 'high' : 'medium',
      text:`Front spring asymmetry: LF ${corners.LF.spring} vs RF ${corners.RF.spring} N/mm (Δ${diff})`,
      action:`Asymmetric front springs are unusual unless compensating for a specific track characteristic. Verify this is intentional.`});
  }
  if (corners.LR.spring != null && corners.RR.spring != null && corners.LR.spring !== corners.RR.spring) {
    const diff = Math.abs(corners.LR.spring - corners.RR.spring);
    recs.push({category:'Cross-Corner Balance', priority: diff > 20 ? 'high' : 'medium',
      text:`Rear spring asymmetry: LR ${corners.LR.spring} vs RR ${corners.RR.spring} N/mm (Δ${diff})`,
      action:`Asymmetric rear springs create an uneven platform — verify this is intentional.`});
  }

  // Ride heights: LF vs RF, LR vs RR
  if (corners.LF.rideHeight != null && corners.RF.rideHeight != null) {
    const diff = Math.abs(corners.LF.rideHeight - corners.RF.rideHeight);
    if (diff > 0.001) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Front ride height asymmetry: LF ${(corners.LF.rideHeight*1000).toFixed(1)} vs RF ${(corners.RF.rideHeight*1000).toFixed(1)} mm`,
        action:`Asymmetric ride heights affect aero balance. Usually indicates a track with more load on one side.`});
    }
  }
  if (corners.LR.rideHeight != null && corners.RR.rideHeight != null) {
    const diff = Math.abs(corners.LR.rideHeight - corners.RR.rideHeight);
    if (diff > 0.001) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Rear ride height asymmetry: LR ${(corners.LR.rideHeight*1000).toFixed(1)} vs RR ${(corners.RR.rideHeight*1000).toFixed(1)} mm`,
        action:`Asymmetric rear ride heights affect mechanical grip balance. May be intentional for oval-style setups.`});
    }
  }

  // Camber: should be mirrored (LF positive ≈ RF negative, etc.)
  if (corners.LF.camber != null && corners.RF.camber != null) {
    const diff = Math.abs(corners.LF.camber + corners.RF.camber);
    if (diff > 0.2) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Front camber asymmetry: LF ${corners.LF.camber}° vs RF ${corners.RF.camber}° (should mirror)`,
        action:`On road courses, front camber is typically symmetric (LF ≈ -RF). A difference suggests intentional oval compensation or a possible error.`});
    }
  }
  if (corners.LR.camber != null && corners.RR.camber != null) {
    const diff = Math.abs(corners.LR.camber + corners.RR.camber);
    if (diff > 0.2) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Rear camber asymmetry: LR ${corners.LR.camber}° vs RR ${corners.RR.camber}° (should mirror)`,
        action:`Symmetric rear camber is standard for road courses. Review if this asymmetry is intentional.`});
    }
  }

  // Pressures: cross-corner
  if (corners.LF.pressure != null && corners.RF.pressure != null) {
    const diff = Math.abs(corners.LF.pressure - corners.RF.pressure);
    if (diff > 3) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Front pressure asymmetry: LF ${corners.LF.pressure} vs RF ${corners.RF.pressure} kPa`,
        action:`Starting pressures are usually equal across an axle. Split pressures are uncommon unless compensating for asymmetric track load.`});
    }
  }
  if (corners.LR.pressure != null && corners.RR.pressure != null) {
    const diff = Math.abs(corners.LR.pressure - corners.RR.pressure);
    if (diff > 3) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:`Rear pressure asymmetry: LR ${corners.LR.pressure} vs RR ${corners.RR.pressure} kPa`,
        action:`Starting pressures are usually equal across an axle. Verify this split is intentional.`});
    }
  }

  // ── 3. Front-to-rear balance analysis ──────────────────────────────────────
  const frontSpring = (corners.LF.spring != null && corners.RF.spring != null) ? (corners.LF.spring + corners.RF.spring) / 2 : null;
  const rearSpring = (corners.LR.spring != null && corners.RR.spring != null) ? (corners.LR.spring + corners.RR.spring) / 2 : null;

  if (frontSpring != null && rearSpring != null) {
    const ratio = frontSpring / rearSpring;
    if (ratio > 1.15) {
      recs.push({category:'Front/Rear Balance', priority:'medium',
        text:`Front springs significantly stiffer than rear (${frontSpring} vs ${rearSpring} N/mm, ratio ${ratio.toFixed(2)})`,
        action:`A stiff front relative to rear tends toward understeer. Soften front springs or stiffen rear for more neutral balance.`});
    } else if (ratio < 0.85) {
      recs.push({category:'Front/Rear Balance', priority:'medium',
        text:`Rear springs significantly stiffer than front (${rearSpring} vs ${frontSpring} N/mm, ratio ${(1/ratio).toFixed(2)})`,
        action:`A stiff rear relative to front tends toward oversteer. Stiffen front or soften rear for more stability.`});
    }
  }

  // Damper ratios
  Object.entries(dampers).forEach(([mode, d]) => {
    if (d.front != null && d.rear != null && d.front !== d.rear) {
      const diff = d.front - d.rear;
      if (Math.abs(diff) > 4) {
        const stiffer = diff > 0 ? 'Front' : 'Rear';
        const tendency = mode.includes('Compression')
          ? (diff > 0 ? 'understeer on bump' : 'oversteer on bump')
          : (diff > 0 ? 'understeer on rebound' : 'oversteer on rebound');
        recs.push({category:'Front/Rear Balance', priority:'low',
          text:`${mode}: front ${d.front} vs rear ${d.rear} clicks — ${stiffer} is stiffer`,
          action:`Large damper split contributes to ${tendency}. ${mode.includes('Compression') ? 'Compression affects weight transfer rate into corners.' : 'Rebound affects weight transfer rate out of corners.'}`});
      }
    }
  });

  // ARB ratio
  if (frontARB != null && rearARB != null) {
    if (frontARB > rearARB + 2) {
      recs.push({category:'Front/Rear Balance', priority:'low',
        text:`Front ARB stiffer than rear (${frontARB} vs ${rearARB})`,
        action:`Stiffer front ARB reduces front grip in corners → tends toward understeer. Consider softening front or stiffening rear ARB.`});
    } else if (rearARB > frontARB + 2) {
      recs.push({category:'Front/Rear Balance', priority:'low',
        text:`Rear ARB stiffer than front (${rearARB} vs ${frontARB})`,
        action:`Stiffer rear ARB reduces rear grip in corners → tends toward oversteer. Consider softening rear or stiffening front ARB.`});
    }
  }

  // ── 4. Contextual recommendations (car config aware) ───────────────────────
  if (carConfig) {
    // Tire pressure vs target
    const targetPsi = carConfig.target_hot_psi;
    if (targetPsi) {
      Object.entries(cornerKeys).forEach(([corner, searchKey]) => {
        const pKpa = corners[corner].pressure;
        if (pKpa == null) return;
        const pPsi = pKpa * 0.14503773773;  // kPa → psi
        const target = targetPsi[corner];
        if (!target) return;
        const diff = pPsi - target;
        // Cold pressure is what we have; target is hot. Cold is typically 4-6 psi below hot.
        // So we can't directly compare, but we can note the starting pressure.
      });
    }
  }

  // Wing angle context
  if (wing != null) {
    const wingParam = findParam(['wing setting']);
    if (wingParam && wingParam.range_min && wingParam.range_max) {
      const wMin = rangeVal(wingParam.range_min);
      const wMax = rangeVal(wingParam.range_max);
      if (wMin != null && wMax != null) {
        const wRange = wMax - wMin;
        const wPct = (wing - wMin) / wRange;
        if (wPct > 0.8) {
          recs.push({category:'Aero', priority:'low',
            text:`Wing at ${wing}° — high downforce end (${(wPct*100).toFixed(0)}% of range)`,
            action:`Good for technical tracks with lots of slow-medium corners. May sacrifice top speed on long straights.`});
        } else if (wPct < 0.2) {
          recs.push({category:'Aero', priority:'low',
            text:`Wing at ${wing}° — low downforce end (${(wPct*100).toFixed(0)}% of range)`,
            action:`Good for high-speed tracks. May lack rear grip in slow corners — compensate with mechanical grip (springs, dampers).`});
        }
      }
    }
  }

  // Bump rubber gap
  Object.entries(cornerKeys).forEach(([corner, searchKey]) => {
    const gap = corners[corner].bumpRubber;
    if (gap != null && gap < 0.02) {
      recs.push({category:'Suspension', priority:'medium',
        text:`${corners[corner].label} bump rubber gap very small (${(gap*1000).toFixed(0)} mm)`,
        action:`A small gap means the car will frequently contact bump stops, creating a harsh, non-linear suspension response. Consider raising ride height or stiffening springs.`});
    }
  });

  // Handling tendency summary
  let tendencySummary = 'balanced';
  let usScore = 0, osScore = 0;
  if (frontSpring != null && rearSpring != null) {
    if (frontSpring > rearSpring * 1.05) usScore++;
    if (rearSpring > frontSpring * 1.05) osScore++;
  }
  if (frontARB != null && rearARB != null) {
    if (frontARB > rearARB) usScore++;
    if (rearARB > frontARB) osScore++;
  }
  if (brakeBias != null) {
    if (brakeBias > 58) usScore++;
    if (brakeBias < 54) osScore++;
  }
  if (usScore > osScore) tendencySummary = 'understeer';
  else if (osScore > usScore) tendencySummary = 'oversteer';

  return {
    recs,
    corners,
    dampers,
    frontARB, rearARB,
    wing, frontRHSpeed, rearRHSpeed,
    brakeBias, frontMC, rearMC, brakePads,
    frontSpring, rearSpring,
    tendencySummary
  };
}

// ── SVG: Car outline with tire pressures, camber, ride heights ──────────────
function renderCarOutlineSVG(analysis) {
  const c = analysis.corners;
  const W = 340, H = 440;
  // Car body shape (top-down view)
  const bodyX = 110, bodyY = 60, bodyW = 120, bodyH = 320;
  const wheelW = 28, wheelH = 56;

  function cornerColor(val, min, max) {
    if (val == null || min == null || max == null) return '#555';
    const range = max - min;
    const pct = range > 0 ? (val - min) / range : 0.5;
    if (pct < 0.15) return '#2196F3';
    if (pct > 0.85) return '#f44336';
    return '#4caf50';
  }

  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:340px;height:auto;display:block;margin:0 auto">`;
  // Background
  svg += `<rect width="${W}" height="${H}" fill="#111" rx="8"/>`;
  // Car body
  svg += `<rect x="${bodyX}" y="${bodyY}" width="${bodyW}" height="${bodyH}" rx="20" ry="20" fill="#1a1a1a" stroke="#333" stroke-width="1.5"/>`;
  // Front window
  svg += `<path d="M${bodyX+20} ${bodyY+40} L${bodyX+bodyW-20} ${bodyY+40} L${bodyX+bodyW-30} ${bodyY+80} L${bodyX+30} ${bodyY+80} Z" fill="#222" stroke="#333" stroke-width="1"/>`;
  // Rear window
  svg += `<path d="M${bodyX+25} ${bodyY+bodyH-80} L${bodyX+bodyW-25} ${bodyY+bodyH-80} L${bodyX+bodyW-20} ${bodyY+bodyH-45} L${bodyX+20} ${bodyY+bodyH-45} Z" fill="#222" stroke="#333" stroke-width="1"/>`;
  // Center line
  svg += `<line x1="${W/2}" y1="${bodyY+10}" x2="${W/2}" y2="${bodyY+bodyH-10}" stroke="#2a2a2a" stroke-width="1" stroke-dasharray="4,4"/>`;

  // Wheels + corner data
  const positions = {
    LF: {wx: bodyX - wheelW - 4, wy: bodyY + 30, tx: 6, ty: bodyY + 20},
    RF: {wx: bodyX + bodyW + 4, wy: bodyY + 30, tx: bodyX + bodyW + wheelW + 12, ty: bodyY + 20},
    LR: {wx: bodyX - wheelW - 4, wy: bodyY + bodyH - 30 - wheelH, tx: 6, ty: bodyY + bodyH - 96},
    RR: {wx: bodyX + bodyW + 4, wy: bodyY + bodyH - 30 - wheelH, tx: bodyX + bodyW + wheelW + 12, ty: bodyY + bodyH - 96}
  };

  Object.entries(positions).forEach(([corner, pos]) => {
    const cd = c[corner];
    const pKpa = cd.pressure;
    const pPsi = pKpa != null ? (pKpa * 0.14503773773).toFixed(1) : '—';
    const camber = cd.camber != null ? cd.camber.toFixed(1) + '°' : '—';
    const rh = cd.rideHeight != null ? (cd.rideHeight * 1000).toFixed(1) : '—';

    // Wheel rectangle
    const wheelColor = pKpa != null ? '#2a2a2a' : '#1e1e1e';
    svg += `<rect x="${pos.wx}" y="${pos.wy}" width="${wheelW}" height="${wheelH}" rx="4" fill="${wheelColor}" stroke="#444" stroke-width="1.5"/>`;
    // Tire tread lines
    for (let i = 0; i < 4; i++) {
      const ly = pos.wy + 10 + i * 12;
      svg += `<line x1="${pos.wx+4}" y1="${ly}" x2="${pos.wx+wheelW-4}" y2="${ly}" stroke="#555" stroke-width="1" opacity="0.5"/>`;
    }

    // Data labels
    const isLeft = corner.startsWith('L');
    const anchor = isLeft ? 'end' : 'start';
    const labelX = isLeft ? pos.tx + 90 : pos.tx;

    svg += `<text x="${labelX}" y="${pos.ty}" fill="#666" font-size="10" font-weight="700" text-anchor="${anchor}" letter-spacing=".5">${corner}</text>`;
    svg += `<text x="${labelX}" y="${pos.ty + 16}" fill="#ccc" font-size="14" font-weight="700" text-anchor="${anchor}">${pPsi}<tspan fill="#555" font-size="10"> psi</tspan></text>`;
    svg += `<text x="${labelX}" y="${pos.ty + 32}" fill="#aaa" font-size="11" text-anchor="${anchor}">⟨ ${camber}</text>`;
    svg += `<text x="${labelX}" y="${pos.ty + 46}" fill="#888" font-size="11" text-anchor="${anchor}">↕ ${rh}<tspan fill="#555" font-size="9"> mm</tspan></text>`;
  });

  // Front/Rear labels
  svg += `<text x="${W/2}" y="${bodyY - 8}" fill="#444" font-size="10" text-anchor="middle" font-weight="700">FRONT</text>`;
  svg += `<text x="${W/2}" y="${bodyY + bodyH + 16}" fill="#444" font-size="10" text-anchor="middle" font-weight="700">REAR</text>`;

  // Heading arrow
  svg += `<path d="M${W/2} ${bodyY+12} L${W/2-6} ${bodyY+22} L${W/2+6} ${bodyY+22} Z" fill="#2196F3" opacity="0.6"/>`;

  svg += `</svg>`;
  return svg;
}

// ── SVG: Suspension & damper bar charts ──────────────────────────────────────
function renderSuspensionBars(analysis) {
  const {corners, dampers, frontARB, rearARB, frontSpring, rearSpring} = analysis;
  let html = '';

  // Springs bar chart
  if (frontSpring != null || rearSpring != null) {
    const maxSpring = Math.max(frontSpring || 0, rearSpring || 0, 1);
    html += `<h4>Spring Rates</h4>`;
    html += `<div class="bar-pair">`;
    html += `<div><div class="bar-pair-label">Front avg</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${(frontSpring/maxSpring*100).toFixed(0)}%;background:#2196F3"></div></div></div>
      <div class="bar-pair-value" style="color:#64b5f6">${frontSpring != null ? frontSpring + ' N/mm' : '—'}</div></div>`;
    html += `<div><div class="bar-pair-label">Rear avg</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${(rearSpring/maxSpring*100).toFixed(0)}%;background:#f44336"></div></div></div>
      <div class="bar-pair-value" style="color:#ef9a9a">${rearSpring != null ? rearSpring + ' N/mm' : '—'}</div></div>`;
    html += `</div>`;
    if (frontSpring && rearSpring) {
      const ratio = (frontSpring / rearSpring).toFixed(2);
      const cls = ratio > 1.05 ? 'front-bias' : ratio < 0.95 ? 'rear-bias' : 'balanced';
      html += `<div style="text-align:center;margin-bottom:12px"><span style="font-size:11px;color:#555">F/R Ratio</span> <span class="ratio-badge ${cls}">${ratio}</span></div>`;
    }
  }

  // ARB
  if (frontARB != null || rearARB != null) {
    const maxARB = Math.max(frontARB || 0, rearARB || 0, 1);
    html += `<h4>Anti-Roll Bars</h4>`;
    html += `<div class="bar-pair">`;
    html += `<div><div class="bar-pair-label">Front</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((frontARB||0)/maxARB*100).toFixed(0)}%;background:#2196F3"></div></div></div>
      <div class="bar-pair-value" style="color:#64b5f6">${frontARB != null ? frontARB : '—'}</div></div>`;
    html += `<div><div class="bar-pair-label">Rear</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((rearARB||0)/maxARB*100).toFixed(0)}%;background:#f44336"></div></div></div>
      <div class="bar-pair-value" style="color:#ef9a9a">${rearARB != null ? rearARB : '—'}</div></div>`;
    html += `</div>`;
  }

  // Bump rubber gaps
  const frontBR = (corners.LF.bumpRubber != null && corners.RF.bumpRubber != null) ? (corners.LF.bumpRubber + corners.RF.bumpRubber) / 2 : null;
  const rearBR = (corners.LR.bumpRubber != null && corners.RR.bumpRubber != null) ? (corners.LR.bumpRubber + corners.RR.bumpRubber) / 2 : null;
  if (frontBR != null || rearBR != null) {
    const maxBR = Math.max(frontBR || 0, rearBR || 0, 0.001);
    html += `<h4>Bump Rubber Gap</h4>`;
    html += `<div class="bar-pair">`;
    html += `<div><div class="bar-pair-label">Front avg</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((frontBR||0)/maxBR*100).toFixed(0)}%;background:#2196F3"></div></div></div>
      <div class="bar-pair-value" style="color:#64b5f6">${frontBR != null ? (frontBR*1000).toFixed(0) + ' mm' : '—'}</div></div>`;
    html += `<div><div class="bar-pair-label">Rear avg</div>
      <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((rearBR||0)/maxBR*100).toFixed(0)}%;background:#f44336"></div></div></div>
      <div class="bar-pair-value" style="color:#ef9a9a">${rearBR != null ? (rearBR*1000).toFixed(0) + ' mm' : '—'}</div></div>`;
    html += `</div>`;
  }

  // Dampers
  const damperModes = Object.entries(dampers);
  const hasDampers = damperModes.some(([_, d]) => d.front != null || d.rear != null);
  if (hasDampers) {
    html += `<h4>Damper Settings</h4>`;
    damperModes.forEach(([mode, d]) => {
      if (d.front == null && d.rear == null) return;
      const max = Math.max(d.front || 0, d.rear || 0, 1);
      const short = mode.replace('Speed ', '').replace('damping', '').trim();
      html += `<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin:6px 0 4px">${short}</div>`;
      html += `<div class="bar-pair">`;
      html += `<div><div class="bar-pair-label">Front</div>
        <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((d.front||0)/max*100).toFixed(0)}%;background:#2196F3"></div></div></div>
        <div class="bar-pair-value" style="color:#64b5f6">${d.front != null ? d.front + ' cl' : '—'}</div></div>`;
      html += `<div><div class="bar-pair-label">Rear</div>
        <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${((d.rear||0)/max*100).toFixed(0)}%;background:#f44336"></div></div></div>
        <div class="bar-pair-value" style="color:#ef9a9a">${d.rear != null ? d.rear + ' cl' : '—'}</div></div>`;
      html += `</div>`;
    });
  }

  return html;
}

// ── SVG: Aero balance diagram ────────────────────────────────────────────────
function renderAeroDiagram(analysis) {
  const {wing, frontRHSpeed, rearRHSpeed} = analysis;
  if (wing == null && frontRHSpeed == null && rearRHSpeed == null) return '';

  let html = '';
  const W = 300, H = 160;
  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:300px;height:auto;display:block;margin:0 auto">`;
  svg += `<rect width="${W}" height="${H}" fill="#111" rx="6"/>`;

  // Wing angle indicator
  if (wing != null) {
    const wingP = analysis.wing;
    const cx = W/2, cy = 36;
    // Wing angle arc
    const angleRad = wing * Math.PI / 180;
    const wingLen = 50;
    svg += `<text x="${cx}" y="16" fill="#555" font-size="9" text-anchor="middle" font-weight="700">WING ANGLE</text>`;
    // Wing line
    const x2 = cx + wingLen * Math.cos(-angleRad);
    const y2 = cy - wingLen * Math.sin(-angleRad);
    svg += `<line x1="${cx - wingLen}" y1="${cy}" x2="${cx + wingLen}" y2="${cy}" stroke="#2a2a2a" stroke-width="1"/>`;
    svg += `<line x1="${cx - 30}" y1="${cy}" x2="${cx + 30}" y2="${cy - Math.tan(angleRad)*30}" stroke="#2196F3" stroke-width="3" stroke-linecap="round"/>`;
    svg += `<text x="${cx}" y="${cy + 16}" fill="#64b5f6" font-size="14" font-weight="700" text-anchor="middle">${wing}°</text>`;
  }

  // Ride heights at speed
  if (frontRHSpeed != null && rearRHSpeed != null) {
    const baseY = H - 20;
    const groundY = baseY - 8;
    const maxRH = Math.max(frontRHSpeed, rearRHSpeed, 1);
    const scale = 40 / maxRH;
    const fH = frontRHSpeed * scale;
    const rH = rearRHSpeed * scale;

    svg += `<line x1="40" y1="${groundY}" x2="${W-40}" y2="${groundY}" stroke="#333" stroke-width="1"/>`;
    svg += `<text x="40" y="${groundY + 12}" fill="#444" font-size="8" text-anchor="start">GROUND</text>`;

    // Front RH
    svg += `<rect x="60" y="${groundY - fH}" width="30" height="${fH}" fill="#2196F3" opacity="0.4" rx="2"/>`;
    svg += `<text x="75" y="${groundY - fH - 6}" fill="#64b5f6" font-size="11" font-weight="700" text-anchor="middle">${frontRHSpeed} mm</text>`;
    svg += `<text x="75" y="${groundY - fH - 18}" fill="#555" font-size="9" text-anchor="middle">FRONT</text>`;

    // Rear RH
    svg += `<rect x="${W-90}" y="${groundY - rH}" width="30" height="${rH}" fill="#f44336" opacity="0.4" rx="2"/>`;
    svg += `<text x="${W-75}" y="${groundY - rH - 6}" fill="#ef9a9a" font-size="11" font-weight="700" text-anchor="middle">${rearRHSpeed} mm</text>`;
    svg += `<text x="${W-75}" y="${groundY - rH - 18}" fill="#555" font-size="9" text-anchor="middle">REAR</text>`;

    // Rake line
    svg += `<line x1="75" y1="${groundY - fH}" x2="${W-75}" y2="${groundY - rH}" stroke="#ff9800" stroke-width="1.5" stroke-dasharray="4,3"/>`;
    const rake = rearRHSpeed - frontRHSpeed;
    svg += `<text x="${W/2}" y="${Math.min(groundY - fH, groundY - rH) - 6}" fill="#ff9800" font-size="10" font-weight="700" text-anchor="middle">Rake: ${rake > 0 ? '+' : ''}${rake} mm</text>`;
  }

  svg += `</svg>`;
  html += svg;

  // Aero balance estimate
  if (frontRHSpeed != null && rearRHSpeed != null) {
    const totalRH = frontRHSpeed + rearRHSpeed;
    const frontPct = totalRH > 0 ? (rearRHSpeed / totalRH * 100) : 50; // Lower front RH = more front DF
    html += `<div style="margin-top:10px;text-align:center">`;
    html += `<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px">Aero Balance Estimate</div>`;
    html += `<div class="aero-balance-indicator" style="justify-content:center">
      <span style="font-size:10px;color:#64b5f6;width:50px;text-align:right">Front</span>
      <div style="flex:1;max-width:200px;height:8px;background:#1e1e1e;border-radius:4px;overflow:hidden;display:flex">
        <div style="width:${frontPct.toFixed(0)}%;background:#2196F3;border-radius:4px 0 0 4px"></div>
        <div style="width:${(100-frontPct).toFixed(0)}%;background:#f44336;border-radius:0 4px 4px 0"></div>
      </div>
      <span style="font-size:10px;color:#ef9a9a;width:50px">Rear</span>
    </div>`;
    html += `</div>`;
  }

  return html;
}

// ── SVG: Brake system overview ───────────────────────────────────────────────
function renderBrakeDiagram(analysis) {
  const {brakeBias, frontMC, rearMC, brakePads} = analysis;
  if (brakeBias == null && frontMC == null && rearMC == null) return '';

  let html = '';

  // Brake bias bar
  if (brakeBias != null) {
    const rearBias = 100 - brakeBias;
    html += `<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px">Brake Bias</div>`;
    html += `<div class="brake-bias-bar">
      <div class="brake-bias-front" style="width:${brakeBias}%">F ${brakeBias}%</div>
      <div class="brake-bias-rear" style="width:${rearBias}%">R ${rearBias.toFixed(1)}%</div>
    </div>`;
    // Context
    const biasNote = brakeBias > 58 ? 'Forward bias — stable under braking, may understeer on entry'
                   : brakeBias < 54 ? 'Rearward bias — aggressive, risk of rear lockup under heavy braking'
                   : 'Moderate bias — good balance for most conditions';
    html += `<div style="font-size:11px;color:#666;margin-top:4px;margin-bottom:12px">${biasNote}</div>`;
  }

  // Master cylinders
  if (frontMC != null || rearMC != null) {
    html += `<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px;margin-top:8px">Master Cylinders</div>`;
    const maxMC = Math.max(frontMC || 0, rearMC || 0, 1);
    html += `<div class="bar-pair">`;
    if (frontMC != null) {
      html += `<div><div class="bar-pair-label">Front</div>
        <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${(frontMC/maxMC*100).toFixed(0)}%;background:#2196F3"></div></div></div>
        <div class="bar-pair-value" style="color:#64b5f6">${frontMC} mm</div></div>`;
    }
    if (rearMC != null) {
      html += `<div><div class="bar-pair-label">Rear</div>
        <div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:${(rearMC/maxMC*100).toFixed(0)}%;background:#f44336"></div></div></div>
        <div class="bar-pair-value" style="color:#ef9a9a">${rearMC} mm</div></div>`;
    }
    html += `</div>`;
    if (frontMC != null && rearMC != null) {
      const note = frontMC > rearMC ? 'Larger front MC = more front braking force & firmer pedal feel'
                 : frontMC < rearMC ? 'Larger rear MC = more rear braking force'
                 : 'Equal MC sizes — neutral pedal response';
      html += `<div style="font-size:11px;color:#555;margin-top:4px">${note}</div>`;
    }
  }

  // Brake pads
  if (brakePads) {
    html += `<div style="margin-top:8px;font-size:12px;color:#888">Brake pads: <span style="color:#ccc;font-weight:600">${brakePads}</span></div>`;
  }

  return html;
}

// ── Render setup recommendations list ────────────────────────────────────────
function renderSetupRecs(recs) {
  if (!recs || !recs.length) return `<p style="color:#444;font-size:12px">No issues detected — setup parameters look well-balanced.</p>`;
  // Sort by priority
  const order = {high: 0, medium: 1, low: 2};
  const sorted = [...recs].sort((a, b) => (order[a.priority] || 3) - (order[b.priority] || 3));
  return sorted.map(r => `
    <div class="setup-rec ${r.priority}">
      <div class="setup-rec-head">
        <span class="setup-rec-cat">${r.category}</span>
        <span class="setup-rec-priority ${r.priority}">${r.priority}</span>
      </div>
      <div class="setup-rec-text">${r.text}</div>
      ${r.action ? `<div class="setup-rec-action">→ ${r.action}</div>` : ''}
    </div>
  `).join('');
}

// ── Render handling tendency badge ───────────────────────────────────────────
function renderTendencyBadge(tendency) {
  const colors = {
    understeer: {bg: '#0d47a1', text: '#64b5f6', icon: '↰'},
    oversteer:  {bg: '#b71c1c', text: '#ef9a9a', icon: '↱'},
    balanced:   {bg: '#1b5e20', text: '#81c784', icon: '↔'}
  };
  const c = colors[tendency] || colors.balanced;
  return `<span style="display:inline-flex;align-items:center;gap:6px;background:${c.bg};color:${c.text};padding:4px 12px;border-radius:8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.3px">${c.icon} ${tendency}</span>`;
}

async function handleStoFile(file) {
  if (!file || !file.name.endsWith('.sto')) {
    alert('Please drop a .sto iRacing setup file.'); return;
  }
  document.getElementById('sto-drop').style.display    = 'none';
  document.getElementById('sto-loading').style.display = 'block';
  document.getElementById('sto-analysis-output').style.display = 'none';

  const fd = new FormData();
  fd.append('file', file);
  let data;
  try {
    const res = await fetch('/api/sto-decode', {method: 'POST', body: fd});
    data = await res.json();
  } catch (e) {
    document.getElementById('sto-loading').style.display = 'none';
    document.getElementById('sto-drop').style.display = '';
    alert('Error decoding setup file: ' + e.message);
    return;
  }
  document.getElementById('sto-loading').style.display = 'none';

  if (data.error) {
    document.getElementById('sto-drop').style.display = '';
    const msg = data.error === 'unsupported_car'
      ? 'This car is not supported by the decoder (setupdelta does not have a mapping for it).'
      : 'Could not decode this setup file: ' + data.error;
    alert(msg);
    return;
  }

  // Get last telemetry analysis result if available
  const lastResult = window._lastAnalysisResult || null;

  const tabs   = data.tabs || {};
  const tabNames = Object.keys(tabs);

  // Run setup analysis
  const setupAnalysis = analyzeSetup(tabs, data.car_config || null);
  window._lastSetupAnalysis = setupAnalysis;

  // Build HTML
  let html = `<div class="sto-analysis">`;
  html += `<div class="sto-analysis-header">
    <span class="sto-analysis-title">🔧 ${file.name}</span>
    <div style="display:flex;align-items:center;gap:8px">
      ${renderTendencyBadge(setupAnalysis.tendencySummary)}
      ${data.car_name ? `<span class="sto-car-badge">${data.car_name}</span>` : ''}
    </div>
  </div>`;

  if (!lastResult) {
    html += `<div style="padding:10px 20px;font-size:11px;color:#555;border-bottom:1px solid #1a1a1a">
      💡 Analyze a telemetry file first to get cross-referenced setup insights.
    </div>`;
  } else {
    html += `<div style="padding:10px 20px;font-size:11px;color:#4caf50;border-bottom:1px solid #1a1a1a">
      ✓ Cross-referencing with current telemetry session
    </div>`;
  }

  // Master tab nav: Setup Analysis + original decode tabs
  const allTabNames = ['Setup Analysis', ...tabNames];
  html += `<div class="sto-tabs" id="sto-tab-nav">`;
  allTabNames.forEach((t, i) => {
    html += `<button class="sto-tab-btn${i===0?' active':''}" onclick="switchStoTab('${t}')" data-tab="${t}">${t}</button>`;
  });
  html += `</div>`;

  // ── Setup Analysis tab (new visualizations) ──────────────────────────────
  html += `<div class="sto-tab-content" id="sto-tab-Setup_Analysis" style="display:block;max-height:none">`;

  // Car outline with tire pressures
  html += `<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">`;
  html += `<div class="setup-viz-title">Car Overview — Pressures, Camber & Ride Heights</div>`;
  html += `<div class="car-outline-wrap">${renderCarOutlineSVG(setupAnalysis)}</div>`;
  html += `</div>`;

  // Suspension & Damper charts
  html += `<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">`;
  html += `<div class="setup-viz-title">Suspension & Damper Balance</div>`;
  html += renderSuspensionBars(setupAnalysis);
  html += `</div>`;

  // Aero + Brakes side by side
  const aeroHtml = renderAeroDiagram(setupAnalysis);
  const brakeHtml = renderBrakeDiagram(setupAnalysis);
  if (aeroHtml || brakeHtml) {
    html += `<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">`;
    html += `<div class="setup-viz-grid">`;
    if (aeroHtml) {
      html += `<div class="setup-viz-card"><h4>Aero Balance</h4>${aeroHtml}</div>`;
    }
    if (brakeHtml) {
      html += `<div class="setup-viz-card"><h4>Brake System</h4>${brakeHtml}</div>`;
    }
    html += `</div></div>`;
  }

  // Setup Recommendations
  html += `<div class="setup-viz-section">`;
  html += `<div class="setup-viz-title">Setup Recommendations</div>`;
  html += `<div class="setup-rec-list">${renderSetupRecs(setupAnalysis.recs)}</div>`;
  html += `</div>`;

  html += `</div>`; // end Setup Analysis tab

  // ── Original parameter tabs ──────────────────────────────────────────────
  if (tabNames.length) {
    tabNames.forEach((tabName, i) => {
      html += `<div class="sto-tab-content" id="sto-tab-${tabName.replace(/\s+/g,'_')}" style="display:none">`;
      const sections = tabs[tabName];
      Object.entries(sections).forEach(([sectName, params]) => {
        html += `<div class="sto-section-title">${sectName}</div>`;
        params.forEach(p => {
          const insight = _crossRef(p.label, p.value, lastResult, setupAnalysis);
          const rangeHtml = (p.range_min != null && p.range_max != null)
            ? `<span class="sto-param-range">${p.range_min}–${p.range_max}</span>` : '';
          html += `<div class="sto-param-row">
            <span class="sto-param-label">${p.label}</span>
            <span>${rangeHtml}<span class="sto-param-value">${p.value}</span></span>
          </div>`;
          if (insight) {
            html += `<div class="sto-insight ${insight.type}">⚡ ${insight.text}</div>`;
          }
        });
      });
      html += `</div>`;
    });
  }

  // Notes
  if (data.notes) {
    html += `<div class="sto-notes-block">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#444;margin-bottom:8px">Setup Notes</div>
      <pre class="sto-notes-pre">${data.notes.replace(/</g,'&lt;')}</pre>
    </div>`;
  }

  html += `</div>`;

  const out = document.getElementById('sto-analysis-output');
  out.innerHTML = html;
  out.style.display = 'block';
}

function switchStoTab(name) {
  document.querySelectorAll('.sto-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.sto-tab-content').forEach(p => {
    p.style.display = p.id === 'sto-tab-' + name.replace(/\s+/g,'_') ? 'block' : 'none';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  ['a','b'].forEach(id => {
    const fi = document.getElementById(`cmp-fi-${id}`);
    const btn = document.getElementById(`cmp-btn-${id}`);
    if (!fi || !btn) return;
    fi.addEventListener('change', () => {
      if (fi.files[0]) {
        btn.textContent = fi.files[0].name;
        btn.classList.add('has-file');
      }
      const fa = document.getElementById('cmp-fi-a').files[0];
      const fb = document.getElementById('cmp-fi-b').files[0];
      document.getElementById('btn-cmp-go').disabled = !(fa && fb);
    });
  });
});

async function runCompare() {
  const fa = document.getElementById('cmp-fi-a').files[0];
  const fb = document.getElementById('cmp-fi-b').files[0];
  if (!fa || !fb) return;
  const btn = document.getElementById('btn-cmp-go');
  btn.disabled = true; btn.textContent = 'Comparing…';
  const form = new FormData();
  form.append('file_a', fa);
  form.append('file_b', fb);
  try {
    const res  = await fetch('/api/compare', {method: 'POST', body: form});
    const data = await res.json();
    if (data.error) { document.getElementById('compare-results').innerHTML = `<p style="color:#f44336">${data.error}</p>`; return; }
    document.getElementById('compare-results').innerHTML = renderComparison(data.a, data.b);
  } catch(e) {
    document.getElementById('compare-results').innerHTML = `<p style="color:#f44336">${e.message}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Compare →';
  }
}

function renderComparison(a, b) {
  function statRow(label, aVal, bVal, lowerIsBetter = false) {
    if (aVal == null && bVal == null) return '';
    const aStr = aVal != null ? aVal : '—';
    const bStr = bVal != null ? bVal : '—';
    let aCls = '', bCls = '';
    if (aVal != null && bVal != null && aVal !== bVal) {
      const aWins = lowerIsBetter ? aVal < bVal : aVal > bVal;
      aCls = aWins ? 'compare-better' : 'compare-worse';
      bCls = aWins ? 'compare-worse'  : 'compare-better';
    }
    return `<div class="compare-stat-row">
      <span class="compare-stat-label">${label}</span>
      <span class="compare-stat-val ${aCls}">${aStr}</span>
      <span class="compare-stat-val ${bCls}">${bStr}</span>
    </div>`;
  }
  const sa = a.summary || {}, sb = b.summary || {};
  const ma = a.meta    || {}, mb = b.meta    || {};
  const statsHtml = `
    <div class="compare-stat-row" style="background:#111;border-radius:4px;padding:4px 0">
      <span class="compare-stat-label" style="font-size:10px;color:#444;text-transform:uppercase">Metric</span>
      <span style="font-size:10px;color:#2196F3;font-weight:700">A: ${ma.filename || '?'}</span>
      <span style="font-size:10px;color:#ff9800;font-weight:700">B: ${mb.filename || '?'}</span>
    </div>
    ${statRow('Best lap', sa.best_lap_s ? fmtLap(sa.best_lap_s) : null, sb.best_lap_s ? fmtLap(sb.best_lap_s) : null, true)}
    ${statRow('Avg lap',  sa.avg_lap_s  ? fmtLap(sa.avg_lap_s)  : null, sb.avg_lap_s  ? fmtLap(sb.avg_lap_s)  : null, true)}
    ${statRow('Consistency', sa.lap_consistency_s ? '±'+sa.lap_consistency_s+'s' : null, sb.lap_consistency_s ? '±'+sb.lap_consistency_s+'s' : null, true)}
    ${statRow('Top speed', sa.max_speed_mph ? sa.max_speed_mph+' mph' : null, sb.max_speed_mph ? sb.max_speed_mph+' mph' : null)}
    ${statRow('Fuel/lap',  sa.fuel_per_lap_gal ? sa.fuel_per_lap_gal+' gal' : null, sb.fuel_per_lap_gal ? sb.fuel_per_lap_gal+' gal' : null, true)}
    ${statRow('Peak lat G', sa.peak_lat_g ? sa.peak_lat_g+'g' : null, sb.peak_lat_g ? sb.peak_lat_g+'g' : null)}
  `;
  // Sector handling comparison
  const ha = a.handling || {}, hb = b.handling || {};
  const sectorNames = [...new Set([...Object.keys(ha), ...Object.keys(hb)])];
  const sectorRows = sectorNames.map(name => {
    const da = ha[name] || {}, db = hb[name] || {};
    const ta = da.tendency || '—', tb = db.tendency || '—';
    const clsA = ta === 'understeer' ? '#2196F3' : ta === 'oversteer' ? '#f44336' : '#4caf50';
    const clsB = tb === 'understeer' ? '#2196F3' : tb === 'oversteer' ? '#f44336' : '#4caf50';
    return `<div class="compare-stat-row">
      <span class="compare-stat-label" style="font-size:11px">${name.replace(/ \(.*\)/,'')}</span>
      <span style="color:${clsA};font-size:11px;font-weight:700">${ta}</span>
      <span style="color:${clsB};font-size:11px;font-weight:700">${tb}</span>
    </div>`;
  }).join('');
  return `<div style="margin-top:16px">
  <div class="section-label" style="margin-top:0">Summary comparison</div>
  <div style="background:#181818;border-radius:10px;padding:14px 18px">${statsHtml}</div>
  ${sectorRows.length ? `<div class="section-label">Handling by sector</div>
  <div style="background:#181818;border-radius:10px;padding:14px 18px">${sectorRows}</div>` : ''}
</div>`;
}

function renderDownforceRec(summary) {
  if (!summary || !summary.downforce_rec) return '';
  const dr  = summary.downforce_rec;
  const col = dr.trim === 'High' ? '#3b82f6'
            : dr.trim === 'Medium' ? '#8b5cf6'
            : '#f59e0b';
  const icon = dr.trim === 'High' ? '▲▲' : dr.trim === 'Medium' ? '▲' : '▼';
  return `<div style="display:flex;align-items:center;gap:12px;background:#1e293b;border-left:3px solid ${col};border-radius:0 8px 8px 0;padding:10px 14px;margin-bottom:12px">
    <span style="color:${col};font-size:1.1rem;font-weight:700">${icon}</span>
    <div>
      <span style="color:${col};font-weight:700;font-size:.9rem">${dr.trim} Downforce</span>
      <span style="color:#64748b;font-size:.8rem;margin-left:8px">recommended</span>
      <div style="color:#94a3b8;font-size:.82rem;margin-top:2px">${dr.note}</div>
    </div>
  </div>`;
}

function renderTechStatus(ts) {
  if (!ts || !ts.corners || !Object.keys(ts.corners).length) return '';
  const overall = ts.pass;
  const badge   = overall
    ? '<span style="background:#16a34a;color:#fff;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:700">PASS</span>'
    : '<span style="background:#dc2626;color:#fff;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:700">FAIL</span>';

  const rows = Object.entries(ts.corners).map(([corner, c]) => {
    const col = c.status === 'fail'    ? '#ef4444'
              : c.status === 'warning' ? '#f59e0b'
              :                          '#22c55e';
    const icon = c.status === 'fail' ? '✖' : c.status === 'warning' ? '⚠' : '✔';
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #1e293b">
      <span style="color:#94a3b8;font-size:.85rem;width:36px">${corner}</span>
      <span style="color:#e2e8f0;font-size:.85rem">${c.measured_mm} mm measured</span>
      <span style="color:#64748b;font-size:.8rem">min ${c.min_mm} mm</span>
      <span style="color:#64748b;font-size:.8rem">+${c.margin_mm} mm margin</span>
      <span style="color:${col};font-size:.9rem;font-weight:700;width:20px;text-align:right">${icon}</span>
    </div>`;
  }).join('');

  const note = ts.series_note
    ? `<div style="color:#64748b;font-size:.75rem;margin-top:8px;line-height:1.4">${ts.series_note}</div>`
    : '';

  return `<div class="section-block">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <h3 style="margin:0;color:#e2e8f0;font-size:.95rem">Tech Inspection — Ride Heights</h3>
      ${badge}
    </div>
    ${rows}
    ${note}
  </div>`;
}

function renderRideHeights(rh) {
  if (!rh) return '';
  const corners = ['LF','RF','LR','RR'];
  const vals = corners.map(c => rh[c]);
  if (vals.every(v => v == null)) return '';
  const cells = corners.map(c => {
    const v = rh[c];
    if (v == null) return `<div style="text-align:center"><div style="font-size:11px;color:#555">${c}</div><div style="color:#444">—</div></div>`;
    const col = v < 15 ? '#f44336' : v < 25 ? '#ff9800' : '#86efac';
    return `<div style="text-align:center"><div style="font-size:11px;color:#777">${c}</div><div style="font-size:18px;font-weight:700;color:${col}">${v.toFixed(1)}</div><div style="font-size:10px;color:#555">mm</div></div>`;
  }).join('');
  return `<div class="section-label">Ride heights</div>
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;background:#181818;border-radius:10px;padding:14px 18px;margin-bottom:12px">${cells}</div>`;
}

function renderConfidence(conf, sigWarnings) {
  const warns = (sigWarnings || []).concat((conf && conf.issues) || []);
  if (!conf && !warns.length) return '';
  const dq = conf ? conf.data_quality : 1;
  const laps = conf ? conf.flying_laps : '?';
  const barCol = dq >= 0.8 ? '#4caf50' : dq >= 0.6 ? '#ff9800' : '#f44336';
  const labelCol = dq >= 0.8 ? '#86efac' : dq >= 0.6 ? '#fbbf24' : '#fca5a5';
  const label = dq >= 0.8 ? 'Good' : dq >= 0.6 ? 'Fair' : 'Low';
  const pct = Math.round(dq * 100);
  const warnHtml = warns.length
    ? `<ul style="margin:6px 0 0 0;padding-left:18px;color:#aaa;font-size:12px">${warns.map(w=>`<li>${w}</li>`).join('')}</ul>`
    : '';
  return `<div style="background:#181818;border-radius:10px;padding:12px 18px;margin-bottom:12px;border-left:3px solid ${barCol}">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <span style="font-size:12px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.5px">Data confidence</span>
    <span style="color:${labelCol};font-weight:700">${label} (${pct}%)</span>
    <span style="flex:1;height:4px;background:#222;border-radius:2px;min-width:60px">
      <span style="display:block;height:4px;background:${barCol};border-radius:2px;width:${pct}%"></span>
    </span>
    <span style="color:#555;font-size:12px">${laps} flying lap${laps===1?'':'s'}</span>
  </div>
  ${warnHtml}
</div>`;
}

function renderLapTimes(lapTimes) {
  if (!lapTimes || !lapTimes.length) return '';
  const best = Math.min(...lapTimes.map(l => l.time_s));
  const rows = lapTimes.map(l => {
    const isBest   = l.time_s === best;
    const delta    = l.time_s - best;
    const deltaStr = isBest ? '⬤ Fastest' : '+' + delta.toFixed(3) + 's';
    const isExcluded = window._excludedLaps && window._excludedLaps.has(l.lap);
    return `<div class="lap-row${isBest ? ' lap-fastest' : ''}${isExcluded ? ' lap-excluded' : ''}" data-lap="${l.lap}" onclick="selectLap(${l.lap})" style="cursor:pointer;grid-template-columns:56px 1fr 1fr 40px">
      <div class="lap-num">${l.lap}</div>
      <div class="lap-time">${fmtLap(l.time_s)}</div>
      <div class="lap-delta">${deltaStr}</div>
      <div style="padding:2px 6px;text-align:center">
        <button onclick="event.stopPropagation();toggleExcludeLap(${l.lap})"
          title="Exclude lap from analysis"
          style="background:none;border:1px solid #333;color:#666;border-radius:3px;padding:1px 5px;cursor:pointer;font-size:.7rem;line-height:1.2">✕</button>
      </div>
    </div>`;
  }).join('');
  const _mn = best, _mx2 = Math.max(...lapTimes.map(l => l.time_s)), _rng = _mx2 - _mn || 1;
  const _SW = 400, _SH = 28, _SP = 3;
  const _sparkSegs = lapTimes.map((l, i) => {
    if (i === 0) return '';
    const x1 = _SP + ((i-1)/(lapTimes.length-1))*(_SW-2*_SP);
    const y1 = _SH - _SP - ((lapTimes[i-1].time_s - _mn)/_rng)*(_SH-2*_SP);
    const x2 = _SP + (i/(lapTimes.length-1))*(_SW-2*_SP);
    const y2 = _SH - _SP - ((l.time_s - _mn)/_rng)*(_SH-2*_SP);
    const d = l.time_s - _mn;
    const col = d < 0.3 ? '#4caf50' : d < 1.5 ? '#ff9800' : '#f44336';
    return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${col}" stroke-width="2" stroke-linecap="round"/>`;
  }).join('');
  return `<div class="section-label">Lap times</div>
<svg viewBox="0 0 400 28" style="width:100%;height:28px;display:block;margin-bottom:6px;background:#111;border-radius:6px">${_sparkSegs}</svg>
<div class="lap-table">
  <div class="lap-row lap-header" style="grid-template-columns:56px 1fr 1fr 40px">
    <div>Lap</div><div>Time</div><div>Δ Best</div><div></div>
  </div>
  ${rows}
</div>`;
}

function renderLapDelta(lapTimes) {
  if (!lapTimes || lapTimes.length < 2) return '';
  const best = Math.min(...lapTimes.map(l => l.time_s));
  const deltas = lapTimes.map(l => l.time_s - best);
  const maxD = Math.max(...deltas, 0.001);
  const W = 500, H = 80, PL = 36, PR = 8, PT = 8, PB = 18;
  const iW = W - PL - PR, iH = H - PT - PB;
  const n = lapTimes.length;
  const xScale = i => PL + (i / Math.max(n - 1, 1)) * iW;
  const yScale = d => PT + iH - (d / maxD) * iH;
  let svg = `<line x1="${PL}" y1="${PT+iH}" x2="${PL+iW}" y2="${PT+iH}" stroke="#333" stroke-width="1"/>`;
  svg += `<text x="${PL-2}" y="${PT+4}" fill="#555" font-size="9" text-anchor="end">+${maxD.toFixed(1)}s</text>`;
  svg += `<text x="${PL-2}" y="${PT+iH+4}" fill="#555" font-size="9" text-anchor="end">±0</text>`;
  for (let i = 1; i < n; i++) {
    const x1 = xScale(i-1), y1 = yScale(deltas[i-1]);
    const x2 = xScale(i),   y2 = yScale(deltas[i]);
    svg += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="#444" stroke-width="1.5"/>`;
  }
  lapTimes.forEach((l, i) => {
    const d = deltas[i];
    const col = d < 0.001 ? '#4caf50' : d < 0.3 ? '#4caf50' : d < 1.5 ? '#ff9800' : '#f44336';
    const cx = xScale(i).toFixed(1), cy = yScale(d).toFixed(1);
    svg += `<circle cx="${cx}" cy="${cy}" r="4" fill="${col}" stroke="#111" stroke-width="1" data-lap-dot="${l.lap}"/>`;
    svg += `<text x="${cx}" y="${(PT+iH+12).toFixed(0)}" fill="#555" font-size="9" text-anchor="middle">${l.lap}</text>`;
  });
  return `<div class="section-label">Lap delta vs best</div>
<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block;background:#111;border-radius:6px;margin-bottom:12px">${svg}</svg>`;
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
  ${r.toe_verify ? '<div style="color:#f59e0b;font-size:.75rem;margin-top:4px">⚠ Toe adjustment suggested — iRacing telemetry does not report current toe angle. Verify the change is within your series\' legal adjustment range before applying.</div>' : ''}
</div>`).join('');
}

function exportJSON() {
  if (!window._analysisData) return;
  const blob = new Blob([JSON.stringify(window._analysisData, null, 2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = (window._analysisData.meta?.filename || 'telemetry').replace(/\.ibt$/i,'') + '_analysis.json';
  a.click();
}
function exportCSV() {
  if (!window._analysisData) return;
  const laps = (window._analysisData.lap_times || []);
  if (!laps.length) return;
  const best = Math.min(...laps.map(l => l.time_s));
  const rows = [['lap','time_s','delta_s'], ...laps.map(l => [l.lap, l.time_s.toFixed(3), (l.time_s - best).toFixed(3)])];
  const csv = rows.map(r => r.join(',')).join('\n');
  const blob = new Blob([csv], {type:'text/csv'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = (window._analysisData.meta?.filename || 'telemetry').replace(/\.ibt$/i,'') + '_laps.csv';
  a.click();
}
function render(data) {
  // Show re-analyze button if laps are excluded
  let reBtn = document.getElementById('reanalyze-btn');
  if (!reBtn) {
    reBtn = document.createElement('button');
    reBtn.id = 'reanalyze-btn';
    reBtn.className = 'btn';
    reBtn.style.cssText = 'display:none;margin:8px 0;background:#b45309;color:#fff;';
    reBtn.textContent = 'Re-analyze (excluding selected laps)';
    reBtn.onclick = () => go(window._ibtFile);
    const status = document.getElementById('status');
    if (status) status.parentNode.insertBefore(reBtn, status.nextSibling);
  }
  reBtn.style.display = window._excludedLaps.size > 0 ? 'inline-flex' : 'none';

  window._analysisData = data;
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

  const carId_selected   = document.getElementById('car-select').value;
  const trackId_selected = document.getElementById('track-select').value;
  const missingBanner = (!det.auto_detected_car && !carId_selected) || (!det.auto_detected_track && !trackId_selected) ? `
  <div style="background:#1a1000;border-left:3px solid #ff9800;padding:8px 28px;font-size:12px;color:#ffb74d">
    ⚠ Could not auto-detect from telemetry —
    ${det.raw_car_path   ? 'CarPath: <b>' + det.raw_car_path   + '</b>' : 'CarPath: <b>not found</b>'}
    &nbsp;·&nbsp;
    ${det.raw_track_name ? 'TrackName: <b>' + det.raw_track_name + '</b>' : 'TrackName: <b>not found</b>'}
    &nbsp;— select manually above or report this so the config can be updated.
  </div>` : '';

  const sessionType = m.session_type || null;
  const sessionBadge = sessionType
    ? `<span style="background:${sessionType.toLowerCase().includes('race') ? '#7c2d12' : '#1a2a1a'};
                    color:${sessionType.toLowerCase().includes('race') ? '#fca5a5' : '#86efac'};
                    border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;
                    text-transform:uppercase;letter-spacing:.5px">${sessionType}</span>`
    : '';
  const raceWarning = sessionType && sessionType.toLowerCase().includes('race')
    ? `<div style="background:#1a0a00;border-left:3px solid #f59e0b;padding:6px 28px;font-size:12px;color:#fbbf24">
         ⚠ Race session detected — tyre data may include SC laps or early-stint anomalies. Recommendations are less reliable than practice/qual data.
       </div>` : '';

  let html = `${autoDetectBanner}${missingBanner}${raceWarning}<div class="meta-bar">
    <div style="display:flex;align-items:center;gap:8px"><b>${m.filename || ''}</b> ${sessionBadge}</div>
    <div class="meta-car-track">${carLabel} &nbsp;·&nbsp; ${trackLabel}</div>
    ${m.ambient_temp_f ? `<div>Air temp: <b>${m.ambient_temp_f} °F</b></div>` : ''}
    ${m.track_temp_f   ? `<div>Track temp: <b>${m.track_temp_f} °F</b></div>` : ''}
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
    <button class="btn-reset" onclick="reset()">&#8592; Analyse another file</button>
    <button class="btn-reset" onclick="exportJSON()" style="background:#1a2a3a;border-color:#2196F3;color:#64b5f6">&#8595; JSON</button>
    <button class="btn-reset" onclick="exportCSV()" style="background:#1a2a1a;border-color:#4caf50;color:#86efac">&#8595; CSV</button>
  </div>
  <div class="page">
    ${renderDownforceRec(data.summary)}
    ${renderConfidence(data.confidence, data.signal_warnings)}
    <div class="section-label">Tyre temperatures &amp; hot pressures</div>
    ${renderTrackTempBadge(m.track_temp_f, m.ambient_temp_f)}
    ${(!m.track_temp_f && m.temp_debug) ? `<div style="background:#1a0a00;border-left:3px solid #555;padding:6px 12px;font-size:11px;color:#888;margin-bottom:10px;border-radius:4px"><b style="color:#aaa">Debug — temp lines from YAML:</b><br>${(m.temp_debug||[]).map(l=>`<code>${l}</code>`).join('<br>')}</div>` : ''}
    <div class="tyre-grid">
      ${tyreCard('LF — Left Front',  t.LF, p.LF)}
      ${tyreCard('RF — Right Front', t.RF, p.RF)}
      ${tyreCard('LR — Left Rear',   t.LR, p.LR)}
      ${tyreCard('RR — Right Rear',  t.RR, p.RR)}
    </div>
    ${renderTechStatus(data.tech_status)}
    ${renderRideHeights(data.ride_heights)}
    ${renderBalance(data.balance)}
    ${renderTyreTrend(data.tyre_trend)}
    ${renderTrackMap(data.track_map)}
    ${renderInputTrace(data.speed_trace)}
    ${renderSpeedTrace(data.speed_trace)}
    ${renderHandling(data.handling)}
    ${renderBrake(data.brake)}
    ${renderOverlap(data.throttle_overlap)}
    ${renderStints(data.stints)}
    ${renderSectorSplits(data.sector_times)}
    ${renderLapDelta(data.lap_times)}
    ${renderLapTimes(data.lap_times)}
    ${renderSetupCard(data.setup_card, data.car, data.track)}
    <div class="section-label">Full recommendations</div>
    <div class="rec-list">${renderRecs(data.recommendations)}</div>
    ${renderLibrarySave(data)}
  </div>`;

  const resultsEl = document.getElementById('results');
  resultsEl.innerHTML = html;
  resultsEl.style.display = 'block';
  document.getElementById('upload-wrap').style.display = 'none';
  if (window._trackMapData) setTimeout(() => drawTrackMap(localStorage.getItem('iracing-tm-mode') || 'speed'), 20);
  window._lastAnalysisResult = data;
}

function renderLibrarySave(data) {
  const carId   = document.getElementById('car-select').value;
  const trackId = document.getElementById('track-select').value;
  if (!carId || !trackId) return '';
  const carName   = data.car   ? data.car.name   : carId;
  const trackName = data.track ? data.track.name : trackId;
  return `
  <div id="library-save-block" style="background:#0c1a3d;border:1px solid #1f3a7a;border-radius:8px;padding:20px;margin-top:16px;">
    <div style="font-size:15px;font-weight:600;color:#93c5fd;margin-bottom:6px;">Save to Setup Library</div>
    <div style="font-size:13px;color:#6b90c4;margin-bottom:14px;">
      Append these recommendations to your <strong>${carName}</strong> setup at <strong>${trackName}</strong> in the library.
    </div>
    <button onclick="saveToLibrary('${carId}','${trackId}')"
            style="background:#1d4ed8;color:#fff;border:none;padding:8px 18px;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;">
      Save to Library
    </button>
    <span id="library-save-status" style="margin-left:12px;font-size:13px;"></span>
  </div>`;
}

async function saveToLibrary(carKey, trackKey) {
  const statusEl = document.getElementById('library-save-status');
  statusEl.textContent = 'Saving…';
  statusEl.style.color = '#6b90c4';

  // Build a plain-text summary of recommendations
  const recs  = (window._analysisData.recommendations || []);
  const sc    = window._analysisData.setup_card || {};
  let lines   = [];

  if (sc.tyres && sc.tyres.pressures) {
    lines.push('TYRE PRESSURES:');
    sc.tyres.pressures.forEach(t => {
      if (t.cold_adj) lines.push(`  ${t.corner}: ${t.cold_adj > 0 ? '+' : ''}${t.cold_adj} PSI cold (hot target: ${t.target_hot_psi} PSI)`);
    });
  }
  if (sc.tyres && sc.tyres.camber && sc.tyres.camber.length) {
    lines.push('CAMBER:');
    sc.tyres.camber.forEach(c => lines.push(`  ${c.corner}: ${c.direction}`));
  }
  if (sc.suspension && sc.suspension.length) {
    lines.push('SUSPENSION:');
    sc.suspension.forEach(s => lines.push(`  [${s.priority}] ${s.sector} — ${s.label}: ${(s.options||[]).join(', ')}`));
  }
  if (recs.length) {
    lines.push('RECOMMENDATIONS:');
    recs.forEach(r => lines.push(`  [${r.priority}] ${r.category}${r.corner ? ' ' + r.corner : ''}: ${r.issue} → ${r.action}`));
  }

  const notes = lines.join('\n');

  try {
    const resp = await fetch('http://localhost:5057/api/advisor-notes', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ car_key: carKey, track_key: trackKey, notes }),
    });
    const result = await resp.json();
    if (resp.ok) {
      statusEl.textContent = `Saved to "${result.setup_filename}"`;
      statusEl.style.color = '#3fb950';
    } else {
      statusEl.textContent = result.error || 'Save failed';
      statusEl.style.color = '#fca5a5';
    }
  } catch (e) {
    statusEl.textContent = 'Could not reach Setup Library (is it running?)';
    statusEl.style.color = '#fca5a5';
  }
}

// ── Heartbeat: keep desktop server alive while tab is open ──────────────
(function() {
  var hbTimer = setInterval(function() {
    fetch('/api/heartbeat').catch(function(){});
  }, 4000);

  function sendShutdown() {
    navigator.sendBeacon('/api/shutdown');
  }
  window.addEventListener('beforeunload', sendShutdown);
  window.addEventListener('pagehide',     sendShutdown);
})();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    try:
        _desktop_mode = True
        _last_heartbeat = time.monotonic()

        # Start watchdog thread — auto-shuts down when browser tab closes
        _wd = threading.Thread(target=_watchdog, daemon=True)
        _wd.start()

        port = _free_port(start=7701)
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
