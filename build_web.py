#!/usr/bin/env python3
"""
build_web.py — regenerate docs/ for GitHub Pages deployment.
Reads templates/index.html and static/ files, inlines everything into a single
self-contained docs/index.html that uses Pyodide instead of a Flask backend.
Run this after adding/changing car or track configs, or after updating
analyzer.py, ibt_parser.py, or any frontend files.
"""
import json, os, re, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, 'docs')
os.makedirs(DOCS, exist_ok=True)


def _read(relpath):
    with open(os.path.join(BASE, relpath), encoding='utf-8') as f:
        return f.read()


def load_dir(subdir):
    out = {}
    folder = os.path.join(BASE, subdir)
    for fname in sorted(os.listdir(folder)):
        if fname.endswith('.json'):
            stem = fname[:-5]
            with open(os.path.join(folder, fname), encoding='utf-8') as f:
                out[stem] = json.load(f)
    return out


# ── Generate data JS files ────────────────────────────────────────────────────
cars = load_dir('cars')
with open(os.path.join(DOCS, 'cars_data.js'), 'w', encoding='utf-8') as f:
    f.write('window.CARS_DATA = ')
    json.dump(cars, f, ensure_ascii=False, indent=2)
    f.write(';\n')
print(f'  cars_data.js   — {len(cars)} cars')

tracks = load_dir('tracks')
with open(os.path.join(DOCS, 'tracks_data.js'), 'w', encoding='utf-8') as f:
    f.write('window.TRACKS_DATA = ')
    json.dump(tracks, f, ensure_ascii=False, indent=2)
    f.write(';\n')
print(f'  tracks_data.js — {len(tracks)} tracks')

# ── Copy Python modules ──────────────────────────────────────────────────────
for src_name in ('ibt_parser.py', 'analyzer.py'):
    shutil.copy2(os.path.join(BASE, src_name), os.path.join(DOCS, src_name))
    print(f'  {src_name} copied')

# ── Read version ──────────────────────────────────────────────────────────────
try:
    ver = _read('version.txt').strip()
except OSError:
    ver = '?.?.?'

# ── Read source files ─────────────────────────────────────────────────────────
html      = _read('templates/index.html')
css       = _read('static/css/style.css')
viz_js    = _read('static/js/visualizations.js')
setup_js  = _read('static/js/setup-analysis.js')
app_js    = _read('static/js/app.js')
# heartbeat.js is NOT included — only needed in desktop (Flask) mode


# ── Pyodide-specific JS overrides ────────────────────────────────────────────
# These replace the Flask fetch()-based functions with Pyodide in-browser equivalents.
PYODIDE_OVERRIDES = r"""
// ── Pyodide overrides (replaces Flask fetch-based functions) ─────────────────
function loadOptions() {
  var carSel   = document.getElementById('car-select');
  var trackSel = document.getElementById('track-select');

  var cars = Object.entries(window.CARS_DATA)
    .sort(function(a, b) { return (a[1].name || a[0]).localeCompare(b[1].name || b[0]); });
  cars.forEach(function(entry) {
    var id = entry[0], c = entry[1];
    var o = document.createElement('option');
    o.value = id;
    o.textContent = c.name + (c['class'] ? ' (' + c['class'] + ')' : '');
    carSel.appendChild(o);
    carData[id] = {name: c.name, 'class': c['class'] || '', image_url: c.image_url || ''};
  });

  var trackEntries = Object.entries(window.TRACKS_DATA)
    .sort(function(a, b) { return (a[1].name || a[0]).localeCompare(b[1].name || b[0]); });
  trackEntries.forEach(function(entry) {
    var id = entry[0], t = entry[1];
    var o = document.createElement('option');
    o.value = id;
    o.textContent = t.name + (t.country ? ' \u2014 ' + t.country : '');
    trackSel.appendChild(o);
    trackData[id] = {name: t.name, country: t.country || '', map_url: t.map_url || ''};
  });

  var savedCar   = localStorage.getItem('iracing-car');
  var savedTrack = localStorage.getItem('iracing-track');
  if (savedCar   && carSel.querySelector('option[value="' + savedCar + '"]'))   carSel.value   = savedCar;
  if (savedTrack && trackSel.querySelector('option[value="' + savedTrack + '"]')) trackSel.value = savedTrack;
  updatePreview();
}

async function go() {
  var file = window._ibtFile;
  if (!file) { alert('Drop an .ibt file first.'); return; }

  var carId    = document.getElementById('car-select').value;
  var trackId  = document.getElementById('track-select').value;
  var airTempRaw = document.getElementById('air-temp') ? document.getElementById('air-temp').value : '';

  var carCfg   = window.CARS_DATA[carId]   || null;
  var trackCfg = window.TRACKS_DATA[trackId] || null;

  setStatus('Analyzing\u2026', 'info');
  document.getElementById('results').innerHTML = '';

  try {
    var bytes = new Uint8Array(await file.arrayBuffer());
    var py = window._pyodide;

    py.globals.set('_ibt_bytes',       bytes);
    py.globals.set('_car_cfg_json',    JSON.stringify(carCfg));
    py.globals.set('_track_cfg_json',  JSON.stringify(trackCfg));
    py.globals.set('_tracks_dict_json', JSON.stringify(window.TRACKS_DATA));
    py.globals.set('_cars_dict_json',   JSON.stringify(window.CARS_DATA));
    py.globals.set('_air_temp_raw',    airTempRaw);
    py.globals.set('_excluded_laps_json', JSON.stringify(Array.from(window._excludedLaps)));

    var resultJson = await py.runPythonAsync(
      'import json\n' +
      '_car   = json.loads(_car_cfg_json)\n' +
      '_trk   = json.loads(_track_cfg_json)\n' +
      '_cars  = json.loads(_cars_dict_json)\n' +
      '_trks  = json.loads(_tracks_dict_json)\n' +
      '_amb   = float(_air_temp_raw) if _air_temp_raw else None\n' +
      '_excl  = json.loads(_excluded_laps_json)\n' +
      'json.dumps(analyzer.analyze_ibt(bytes(_ibt_bytes), _car, _trk, _amb, _cars, _trks, _excl))'
    );

    var data = JSON.parse(resultJson);

    if (data.detected) {
      var d = data.detected;
      if (d.auto_detected_car   && d.car_id)   document.getElementById('car-select').value   = d.car_id;
      if (d.auto_detected_track && d.track_id) document.getElementById('track-select').value = d.track_id;
    }

    render(data);
    setStatus('Analysis complete', 'ok');
  } catch (e) {
    setStatus('Error: ' + e.message, 'error');
    console.error(e);
  }
}

async function runCompare() {
  var fa = window._compareFileA;
  var fb = window._compareFileB;
  if (!fa || !fb) { alert('Drop both files first.'); return; }

  setStatus('Comparing\u2026', 'info');

  try {
    var py   = window._pyodide;
    var bA   = new Uint8Array(await fa.arrayBuffer());
    var bB   = new Uint8Array(await fb.arrayBuffer());

    var carId   = document.getElementById('car-select').value;
    var trackId = document.getElementById('track-select').value;
    var carCfg   = window.CARS_DATA[carId]   || null;
    var trackCfg = window.TRACKS_DATA[trackId] || null;

    py.globals.set('_cmp_bytes_a',    bA);
    py.globals.set('_cmp_bytes_b',    bB);
    py.globals.set('_cmp_car_json',   JSON.stringify(carCfg));
    py.globals.set('_cmp_track_json', JSON.stringify(trackCfg));
    py.globals.set('_cars_dict_json',  JSON.stringify(window.CARS_DATA));
    py.globals.set('_tracks_dict_json', JSON.stringify(window.TRACKS_DATA));

    var resultJson = await py.runPythonAsync(
      'import json\n' +
      '_car  = json.loads(_cmp_car_json)\n' +
      '_trk  = json.loads(_cmp_track_json)\n' +
      '_cars = json.loads(_cars_dict_json)\n' +
      '_trks = json.loads(_tracks_dict_json)\n' +
      '_a = analyzer.analyze_ibt(bytes(_cmp_bytes_a), _car, _trk, None, _cars, _trks)\n' +
      '_b = analyzer.analyze_ibt(bytes(_cmp_bytes_b), _car, _trk, None, _cars, _trks)\n' +
      'json.dumps({"a": _a, "b": _b})'
    );

    var data = JSON.parse(resultJson);
    renderComparison(data.a, data.b);
    setStatus('Comparison complete', 'ok');
  } catch (e) {
    setStatus('Error: ' + e.message, 'error');
    console.error(e);
  }
}
"""

PYODIDE_INIT = r"""
// ── Pyodide initialisation ────────────────────────────────────────────────────
async function initPyodide() {
  var overlay = document.getElementById('py-loading');
  var prog    = document.getElementById('py-progress');
  var bar     = document.getElementById('py-bar');

  function step(msg, pct) {
    prog.textContent = msg;
    bar.style.width  = pct + '%';
  }

  try {
    step('Loading Pyodide runtime\u2026', 10);
    window._pyodide = await loadPyodide({
      indexURL: 'https://cdn.jsdelivr.net/pyodide/v0.27.0/full/',
    });

    step('Loading numpy\u2026', 40);
    await window._pyodide.loadPackage('numpy');

    step('Loading analysis modules\u2026', 70);
    var results = await Promise.all([
      fetch('ibt_parser.py').then(function(r) { return r.text(); }),
      fetch('analyzer.py').then(function(r) { return r.text(); }),
    ]);

    window._pyodide.FS.writeFile('/ibt_parser.py', results[0]);
    window._pyodide.FS.writeFile('/analyzer.py',   results[1]);

    step('Initialising modules\u2026', 90);
    await window._pyodide.runPythonAsync(
      "import sys; sys.path.insert(0, '/'); import ibt_parser, analyzer"
    );

    step('Ready', 100);
    overlay.style.display = 'none';
    loadOptions();

  } catch (e) {
    prog.textContent      = 'Failed to load: ' + e.message;
    prog.style.color      = '#f87171';
    bar.style.background  = '#ef4444';
    bar.style.width       = '100%';
    console.error(e);
  }
}

initPyodide();
"""

LOADING_OVERLAY = """<div id="py-loading" style="position:fixed;inset:0;background:#0f172a;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;gap:20px">
  <div style="color:#e2e8f0;font-family:sans-serif;font-size:1.2rem;font-weight:600">iRacing Setup Advisor</div>
  <div style="color:#94a3b8;font-family:sans-serif;font-size:.95rem">Loading Python runtime\u2026</div>
  <div id="py-progress" style="color:#64748b;font-family:monospace;font-size:.8rem;max-width:400px;text-align:center"></div>
  <div style="width:300px;height:4px;background:#1e293b;border-radius:2px;overflow:hidden">
    <div id="py-bar" style="height:100%;width:0%;background:#3b82f6;transition:width .3s ease;border-radius:2px"></div>
  </div>
</div>
"""


# ── Build docs/index.html ────────────────────────────────────────────────────

# 1. Replace Jinja2 version variable
html = html.replace('{{ version }}', ver)

# 2. Replace CSS <link> with inline <style>
css_link = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/style.css\') }}">'
html = html.replace(css_link, '<style>\n' + css + '\n</style>')

# 3. Add Pyodide + data scripts in <head> (before </head>)
head_scripts = """  <script src="https://cdn.jsdelivr.net/pyodide/v0.27.0/full/pyodide.js"></script>
  <script src="cars_data.js"></script>
  <script src="tracks_data.js"></script>"""
html = html.replace('</head>', head_scripts + '\n</head>')

# 4. Add loading overlay right after <body>
html = html.replace('<body>\n', '<body>\n' + LOADING_OVERLAY + '\n')

# 5. Remove library banner (needs Flask backend)
html = re.sub(
    r'<div id="library-banner".*?</div>\n*',
    '',
    html,
    flags=re.DOTALL
)

# 6. Remove STO panel (needs Flask backend / setupdelta API)
html = re.sub(
    r'<div id="sto-wrap".*?</div>\s*</div>\s*</div>\n*',
    '',
    html,
    flags=re.DOTALL
)

# 7. Replace <script src="..."> tags with inline scripts
#    Use plain string replace to avoid re.sub backslash issues in JS content.
_script_tag = lambda name: '{{ url_for(\'static\', filename=\'js/' + name + '\') }}'

def _replace_script(html, name, content):
    tag = '<script src="' + _script_tag(name) + '"></script>'
    return html.replace(tag, '<script>\n' + content + '\n</script>')

# Remove heartbeat.js (not needed in web mode)
hb_tag = '<script src="' + _script_tag('heartbeat.js') + '"></script>'
html = html.replace(hb_tag + '\n', '').replace(hb_tag, '')

# Inline the remaining JS files
html = _replace_script(html, 'visualizations.js', viz_js)
html = _replace_script(html, 'setup-analysis.js', setup_js)
html = _replace_script(html, 'app.js', app_js + '\n' + PYODIDE_OVERRIDES + '\n' + PYODIDE_INIT)

# ── Write output ──────────────────────────────────────────────────────────────
with open(os.path.join(DOCS, 'index.html'), 'w', encoding='utf-8') as f:
    f.write(html)
print(f'  index.html     — generated from templates + static')

print(f'\ndocs/ ready — v{ver}')
