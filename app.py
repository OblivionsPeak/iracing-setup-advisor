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

from flask import Flask, jsonify, render_template, request

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
app = Flask(__name__,
            template_folder=os.path.join(BASE, 'templates'),
            static_folder=os.path.join(BASE, 'static'))
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024   # 512 MB


@app.route('/')
def index():
    return render_template('index.html', version=VERSION)


@app.route('/api/cars')
def list_cars():
    return jsonify([
        {'id': k, 'name': v.get('name', k), 'class': v.get('class', ''),
         'image_url': v.get('image_url', '')}
        for k, v in sorted(CARS.items(), key=lambda x: x[1].get('name', ''))
    ])


@app.route('/api/car-config/<car_id>')
def car_config(car_id):
    cfg = CARS.get(car_id)
    if not cfg:
        return jsonify({'error': 'Car not found'}), 404
    return jsonify({
        'id': car_id,
        'name': cfg.get('name', car_id),
        'class': cfg.get('class', ''),
        'target_hot_psi': cfg.get('target_hot_psi'),
        'notes': cfg.get('notes', ''),
    })


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
