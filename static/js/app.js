// ── Populate dropdowns & preview lookups ─────────────────────────────────────
var carData   = {};   // id → {name, class, image_url}
var trackData = {};   // id → {name, country, map_url}

async function loadOptions() {
  var resp = await Promise.all([
    fetch('/api/cars').then(function(r) { return r.json(); }),
    fetch('/api/tracks').then(function(r) { return r.json(); }),
  ]);
  var cars = resp[0], tracks = resp[1];

  var carSel   = document.getElementById('car-select');
  var trackSel = document.getElementById('track-select');

  cars.forEach(function(c) {
    carData[c.id] = c;
    var opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.name + (c.class ? ' ('+c.class+')' : '');
    carSel.appendChild(opt);
  });

  tracks.forEach(function(t) {
    trackData[t.id] = t;
    var opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.name + (t.country ? ' \u2014 '+t.country : '');
    trackSel.appendChild(opt);
  });
  // URL params from Setup Library take priority, then fall back to localStorage
  var urlParams  = new URLSearchParams(window.location.search);
  var urlCar     = urlParams.get('car');
  var urlTrack   = urlParams.get('track');
  var savedCar   = urlCar   || localStorage.getItem('iracing-car');
  var savedTrack = urlTrack || localStorage.getItem('iracing-track');
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
  var carId   = document.getElementById('car-select').value;
  var trackId = document.getElementById('track-select').value;
  if (carId)   localStorage.setItem('iracing-car',   carId);
  if (trackId) localStorage.setItem('iracing-track', trackId);
  var bar     = document.getElementById('preview-bar');

  var carCard   = document.getElementById('car-preview');
  var trackCard = document.getElementById('track-preview');

  if (carId && carData[carId]) {
    var c = carData[carId];
    document.getElementById('car-preview-name').textContent  = c.name;
    document.getElementById('car-preview-class').textContent = c.class || '';
    var img   = document.getElementById('car-img');
    var noImg = document.getElementById('car-no-img');
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
    var t = trackData[trackId];
    document.getElementById('track-preview-name').textContent    = t.name;
    document.getElementById('track-preview-country').textContent = t.country || '';
    var img   = document.getElementById('track-img');
    var noImg = document.getElementById('track-no-img');
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

  var anyVisible = (carId && carData[carId]) || (trackId && trackData[trackId]);
  bar.classList.toggle('visible', !!anyVisible);
}

document.getElementById('car-select').addEventListener('change', updatePreview);
document.getElementById('track-select').addEventListener('change', updatePreview);

// ── File handling ─────────────────────────────────────────────────────────────
var dz = document.getElementById('dz');
var fi = document.getElementById('fi');

dz.addEventListener('dragover',  function(e) { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', function() { dz.classList.remove('over'); });
dz.addEventListener('drop', function(e) {
  e.preventDefault(); dz.classList.remove('over');
  if (e.dataTransfer.files[0]) {
    window._excludedLaps = new Set();
    var reBtn2 = document.getElementById('reanalyze-btn');
    if (reBtn2) reBtn2.style.display = 'none';
    go(e.dataTransfer.files[0]);
  }
});
fi.addEventListener('change', function() {
  if (fi.files[0]) {
    window._excludedLaps = new Set();
    var reBtn2 = document.getElementById('reanalyze-btn');
    if (reBtn2) reBtn2.style.display = 'none';
    go(fi.files[0]);
  }
});

async function go(file) {
  if (!file) return;
  window._ibtFile = file;
  var carId   = document.getElementById('car-select').value;
  var trackId = document.getElementById('track-select').value;

  setStatus('Parsing ' + file.name + ' \u2026');
  document.getElementById('results').style.display = 'none';

  var form = new FormData();
  form.append('file',     file);
  form.append('car',      carId);
  form.append('track',    trackId);
  form.append('air_temp_f', document.getElementById('air-temp').value || '');
  form.append('excluded_laps', JSON.stringify(Array.from(window._excludedLaps)));

  try {
    var res  = await fetch('/api/analyze', { method: 'POST', body: form });
    var data = await res.json();
    if (data.error) { setStatus('Error: ' + data.error); return; }
    setStatus('');
    render(data);
    var det = data.detected || {};
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
  var el = document.getElementById('status');
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
  document.querySelectorAll('[data-lap="'+lapNum+'"]').forEach(function(el) {
    el.classList.toggle('lap-excluded', window._excludedLaps.has(lapNum));
  });
  var btn = document.getElementById('reanalyze-btn');
  if (btn) btn.style.display = window._excludedLaps.size > 0 ? 'inline-flex' : 'none';
}

function selectLap(lapNum) {
  window._selectedLap = (window._selectedLap === lapNum) ? null : lapNum;
  document.querySelectorAll('.lap-row[data-lap]').forEach(function(el) {
    el.style.outline = (parseInt(el.dataset.lap) === window._selectedLap) ? '2px solid #2196F3' : '';
    el.style.background = (parseInt(el.dataset.lap) === window._selectedLap) ? '#0d2a40' : '';
  });
  document.querySelectorAll('.tt-row[data-lap]').forEach(function(el) {
    el.style.outline = (parseInt(el.dataset.lap) === window._selectedLap) ? '2px solid #2196F3' : '';
    el.style.background = (parseInt(el.dataset.lap) === window._selectedLap) ? '#0d2a40' : '';
  });
  document.querySelectorAll('[data-lap-dot]').forEach(function(el) {
    var isSelected = parseInt(el.dataset.lapDot) === window._selectedLap;
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

// ── Rendering helpers ─────────────────────────────────────────────────────────
function tempClass(t) {
  return t < 167 ? 'tc-cold' : t < 203 ? 'tc-ok' : t < 230 ? 'tc-warm' : 'tc-hot';
}

function fmtLap(s) {
  if (!s) return null;
  var m = Math.floor(s / 60);
  var r = (s % 60).toFixed(3).padStart(6, '0');
  return m + ':' + r;
}

function tyreCard(label, td, psi) {
  if (!td) return '<div class="tyre-card"><h3>'+label+'</h3>'
    + '<p style="color:#444;font-size:12px">No data in telemetry</p></div>';
  var sStr = (td.spread >= 0 ? '+' : '') + td.spread.toFixed(1) + ' \u00b0F';
  var pStr = psi != null ? psi.toFixed(1) + ' psi' : '\u2014';
  return '<div class="tyre-card"><h3>'+label+'</h3>'
    + '<div class="temp-bars">'
    + ['inner','mid','outer'].map(function(k) {
        return '<div class="temp-col">'
          + '<div class="lbl">'+k+'</div>'
          + '<div class="temp-box '+tempClass(td[k])+'">'+td[k].toFixed(0)+'\u00b0F</div>'
          + '</div>';
      }).join('')
    + '</div>'
    + '<div class="tyre-meta">'
    + '<div>Avg <em>'+td.avg.toFixed(0)+' \u00b0F</em></div>'
    + '<div>Spread <em>'+sStr+'</em></div>'
    + '<div>Hot <em>'+pStr+'</em></div>'
    + '</div></div>';
}

function renderTrackTempBadge(trackTempF, airTempF) {
  if (!trackTempF && !airTempF) return '';
  var parts = [];
  if (trackTempF != null) {
    var c = Math.round((trackTempF - 32) * 5 / 9);
    var col  = trackTempF < 59  ? '#60a5fa' :
               trackTempF < 77  ? '#86efac' :
               trackTempF < 95  ? '#fbbf24' :
                                  '#f87171';
    var label = trackTempF < 59  ? 'Cold' :
                trackTempF < 77  ? 'Cool' :
                trackTempF < 95  ? 'Warm' : 'Hot';
    parts.push('<span style="display:inline-flex;align-items:center;gap:6px;background:#181818;border:1px solid '+col+';border-radius:8px;padding:5px 14px;font-size:13px">'
      + '<span style="font-size:16px">\ud83c\udf21</span>'
      + '<span style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.5px">Track</span>'
      + '<span style="color:'+col+';font-weight:700">'+trackTempF+' \u00b0F</span>'
      + '<span style="color:#666;font-size:11px">('+c+' \u00b0C)</span>'
      + '<span style="background:'+col+';color:#111;border-radius:4px;padding:1px 7px;font-size:11px;font-weight:700">'+label+'</span>'
      + '</span>');
  }
  if (airTempF != null) {
    var c = Math.round((airTempF - 32) * 5 / 9);
    parts.push('<span style="display:inline-flex;align-items:center;gap:6px;background:#181818;border:1px solid #475569;border-radius:8px;padding:5px 14px;font-size:13px">'
      + '<span style="font-size:16px">\ud83d\udca8</span>'
      + '<span style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.5px">Air</span>'
      + '<span style="color:#cbd5e1;font-weight:700">'+airTempF+' \u00b0F</span>'
      + '<span style="color:#666;font-size:11px">('+c+' \u00b0C)</span>'
      + '</span>');
  }
  var note = trackTempF != null
    ? (trackTempF < 68 ? ' Cold track \u2014 start with pressures 0.5\u20131 psi higher than baseline.' :
       trackTempF > 90 ? ' Hot track \u2014 pressures will build quickly; check hot readings carefully.' : '')
    : '';
  return '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">'
    + parts.join('')
    + (note ? '<span style="font-size:12px;color:#94a3b8;font-style:italic">'+note+'</span>' : '')
    + '</div>';
}

function renderBalance(b) {
  if (!b || (!b.front_avg && !b.left_avg)) return '';
  var frd = b.front_rear_diff;
  var lrd = b.left_right_diff;
  var items = [];
  if (b.front_avg != null && b.rear_avg != null) {
    var diff = frd > 0 ? '+'+frd.toFixed(1)+' rear' : (-frd).toFixed(1)+' front';
    items.push('<div class="balance-item">'
        + '<div class="lbl">Front / Rear</div>'
        + '<div class="val">'+b.front_avg+'\u00b0F / '+b.rear_avg+'\u00b0F</div>'
        + '<div class="balance-diff">'+diff+' hotter</div></div>');
  }
  if (b.left_avg != null && b.right_avg != null) {
    var diff2 = lrd > 0 ? '+'+lrd.toFixed(1)+' right' : (-lrd).toFixed(1)+' left';
    items.push('<div class="balance-item">'
        + '<div class="lbl">Left / Right</div>'
        + '<div class="val">'+b.left_avg+'\u00b0F / '+b.right_avg+'\u00b0F</div>'
        + '<div class="balance-diff">'+diff2+' hotter</div></div>');
  }
  if (!items.length) return '';
  return '<div class="section-label">Tyre balance</div>'
    + '<div class="balance-row">'+items.join('')+'</div>';
}

function renderHandling(h) {
  if (!h || !Object.keys(h).length) return '';
  var rows = Object.entries(h).map(function(entry) {
    var name = entry[0], d = entry[1];
    var t   = d.tendency || 'no data';
    var cls = t === 'understeer' ? 'b-us' : t === 'neutral' ? 'b-neu'
              : t === 'oversteer'  ? 'b-os' : 'b-nd';
    return '<div class="sector-row">'
      + '<div class="s-name">'+name+'</div>'
      + '<span class="badge '+cls+'">'+t+'</span>'
      + '</div>';
  }).join('');
  return '<div class="section-label">Handling balance by sector</div>'
    + '<div class="sector-table">'+rows+'</div>';
}

function renderBrake(b) {
  if (!b || !Object.keys(b).length) return '';
  var items = [];

  if (b.avg_bias_front_pct != null) {
    var rear = (100 - b.avg_bias_front_pct).toFixed(1);
    items.push('<div class="brake-item"><div class="lbl">Bias Setting</div><div class="val">'+b.avg_bias_front_pct.toFixed(1)+'% Front</div><div class="sub">'+rear+'% Rear</div></div>');
  }
  if (b.actual_front_bias_pct != null) {
    items.push('<div class="brake-item"><div class="lbl">Actual Split</div><div class="val">'+b.actual_front_bias_pct.toFixed(1)+'% Front</div><div class="sub">from line pressures</div></div>');
  }
  if (b.peak_brake_press_psi != null) {
    items.push('<div class="brake-item"><div class="lbl">Peak Pressure</div><div class="val">'+b.peak_brake_press_psi+' psi</div></div>');
  }
  if (b.brake_events != null) {
    items.push('<div class="brake-item"><div class="lbl">Brake Events</div><div class="val">'+b.brake_events+'</div></div>');
  }
  if (b.avg_peak_brake_pct != null) {
    items.push('<div class="brake-item"><div class="lbl">Avg Peak Input</div><div class="val">'+b.avg_peak_brake_pct+'%</div></div>');
  }
  if (b.brake_consistency != null) {
    var pct   = Math.min(b.brake_consistency * 3, 100);
    var color = b.brake_consistency < 5 ? '#4caf50' : b.brake_consistency < 12 ? '#ff9800' : '#f44336';
    var label = b.brake_consistency < 5 ? 'Consistent' : b.brake_consistency < 12 ? 'Moderate' : 'Inconsistent';
    items.push('<div class="brake-item"><div class="lbl">Consistency (\u03c3)</div><div class="val">'+label+' <span style="color:'+color+';font-size:12px">\u00b1'+b.brake_consistency+'%</span></div><div class="consistency-bar"><div class="consistency-fill" style="width:'+pct+'%;background:'+color+'"></div></div></div>');
  }
  if (!items.length) return '';
  return '<div class="section-label">Brake analysis</div><div class="brake-row">'+items.join('')+'</div>';
}

function renderOverlap(o) {
  if (!o) return '';
  var pct = o.overall_pct;
  var color = pct < 3 ? '#4caf50' : pct < 8 ? '#ff9800' : '#f44336';
  var desc  = pct < 3
    ? 'Minimal overlap \u2014 clean pedal separation.'
    : pct < 8
    ? 'Moderate overlap \u2014 likely intentional trail braking. Check sector breakdown.'
    : 'High overlap \u2014 review technique. May indicate unintentional input or a setup balance issue.';

  var sectorHtml = '';
  if (o.by_sector) {
    var rows = Object.entries(o.by_sector).map(function(entry) {
      var name = entry[0], p = entry[1];
      var w     = Math.min(p * 5, 100);
      var c     = p < 3 ? '#4caf50' : p < 8 ? '#ff9800' : '#f44336';
      return '<div class="overlap-sector-row">'
        + '<div class="overlap-sec-name">'+name+'</div>'
        + '<div class="overlap-bar-wrap"><div class="overlap-bar-fill" style="width:'+w+'%;background:'+c+'"></div></div>'
        + '<div class="overlap-sec-pct">'+p+'%</div>'
        + '</div>';
    }).join('');
    sectorHtml = '<div class="overlap-sectors">'+rows+'</div>';
  }

  return '<div class="section-label">Throttle / brake overlap</div>'
    + '<div class="overlap-summary">'
    + '<div class="overlap-pct-big" style="color:'+color+'">'+pct+'%</div>'
    + '<div class="overlap-desc">'+desc+'</div>'
    + '</div>'
    + sectorHtml;
}

function renderSetupCard(sc, car, track) {
  if (!sc) return '';
  var t = sc.tyres || {};
  var pressures = t.pressures || [];
  var cambers   = t.camber   || [];
  var susp      = sc.suspension || [];

  var psiCells = pressures.map(function(p) {
    if (p.status === 'no_data') return '<div class="sc-psi-cell"><div class="sc-psi-corner">'+p.corner+'</div><div class="sc-psi-hot psi-ok" style="font-size:13px;color:#333">No data</div></div>';
    var cls   = p.status === 'over' ? 'psi-over' : p.status === 'under' ? 'psi-under' : 'psi-ok';
    var adjCls = p.status === 'over' ? 'adj-over' : p.status === 'under' ? 'adj-under' : 'adj-ok';
    var adjStr = p.status === 'ok'
      ? '\u2713 On target'
      : (p.cold_adj > 0 ? '+'+p.cold_adj : ''+p.cold_adj) + ' psi cold';
    return '<div class="sc-psi-cell"><div class="sc-psi-corner">'+p.corner+'</div><div class="sc-psi-hot '+cls+'">'+p.hot_psi+' <span style="font-size:11px;font-weight:400">psi</span></div><div class="sc-psi-target">target '+p.target_hot_psi+' psi hot</div><div class="sc-psi-adj '+adjCls+'">'+adjStr+'</div></div>';
  }).join('');

  var camberHtml = '';
  if (cambers.length) {
    var rows = cambers.map(function(c) {
      var dir = c.direction === 'add' ? 'Add' : 'Reduce';
      var why = c.direction === 'add'
        ? 'outer '+Math.abs(c.spread_f)+'\u00b0F hotter \u2014 rolling onto outside edge'
        : 'inner '+Math.abs(c.spread_f)+'\u00b0F hotter \u2014 too much negative camber';
      return '<div class="sc-camber-row"><div class="sc-camber-corner">'+c.corner+'</div><div class="sc-camber-action">'+dir+' negative camber <b>'+c.range+'</b></div><div class="sc-camber-detail">'+why+'</div></div>';
    }).join('');
    camberHtml = '<div class="sc-section"><div class="sc-section-title">Camber</div>'+rows+'</div>';
  }

  var suspHtml = '';
  if (susp.length) {
    var sectorOrder = [];
    var bySector = {};
    susp.forEach(function(s) {
      var sec = s.sector || 'ALL';
      if (!bySector[sec]) { bySector[sec] = []; sectorOrder.push(sec); }
      bySector[sec].push(s);
    });
    var groups = sectorOrder.map(function(sec) {
      var items = bySector[sec].map(function(s) {
        if (s.issue === 'neutral') {
          return '<div class="sc-susp-item" style="color:#4caf50;font-size:.82rem;padding:6px 0">\u2713 No suspension changes needed</div>';
        }
        var optItems = s.options.map(function(o, i) {
          return '<div class="sc-option"><span class="sc-opt-num">'+(i+1)+'</span><span class="sc-opt-text">'+o+'</span></div>';
        }).join('');
        return '<div class="sc-susp-item"><div class="sc-susp-label"><span class="sc-susp-name">'+s.label+'</span><span class="sc-priority-badge '+s.priority+'">'+s.priority+'</span></div><div class="sc-options">'+optItems+'</div></div>';
      }).join('');
      var secHeader = sec !== 'ALL'
        ? '<div style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b;padding:6px 0 2px 0;margin-top:4px">'+sec+'</div>'
        : '';
      return secHeader + items;
    }).join('');
    suspHtml = '<div class="sc-section"><div class="sc-section-title">Suspension \u2014 by sector</div>'+groups+'</div>';
  }

  if (!psiCells && !camberHtml && !suspHtml) return '';

  return '<div class="section-label">Setup card'
    + '<button class="btn-print" onclick="printCard()" style="float:right;margin-top:-2px">&#128438; Print / Save PDF</button>'
    + '</div>'
    + '<div class="setup-card" id="setup-card-block">'
    + '<div class="sc-header"><span class="sc-title">Garage adjustments \u2014 '+(car || 'Car')+' at '+(track || 'Track')+'</span></div>'
    + '<div class="sc-section"><div class="sc-section-title">Tyre pressures \u2014 adjust cold to hit hot targets</div><div class="sc-psi-grid">'+psiCells+'</div></div>'
    + camberHtml
    + suspHtml
    + '</div>';
}

function printCard() {
  var card = document.getElementById('setup-card-block');
  if (!card) return;
  var win = window.open('', '_blank');
  win.document.write('<!DOCTYPE html><html><head><title>Setup Card</title>'
    + '<style>'
    + 'body{font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;background:#fff;color:#111;padding:24px;font-size:13px}'
    + 'h2{font-size:16px;margin-bottom:16px}'
    + '.sc-psi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}'
    + '.sc-psi-cell{border:1px solid #ddd;border-radius:6px;padding:10px;text-align:center}'
    + '.sc-psi-corner{font-size:10px;font-weight:700;text-transform:uppercase;color:#888;margin-bottom:4px}'
    + '.sc-psi-hot{font-size:18px;font-weight:700}'
    + '.sc-psi-hot.psi-over{color:#c00}.sc-psi-hot.psi-under{color:#00c}.sc-psi-hot.psi-ok{color:#080}'
    + '.sc-psi-target{font-size:11px;color:#888;margin-top:2px}'
    + '.sc-psi-adj{font-size:12px;font-weight:700;margin-top:6px}'
    + '.adj-over{color:#c00}.adj-under{color:#00c}.adj-ok{color:#080}'
    + '.section{margin:16px 0}'
    + '.sec-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#999;margin-bottom:8px}'
    + '.camber-row{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid #eee}'
    + '.susp-item{padding:8px 0;border-bottom:1px solid #eee}'
    + '.susp-label{font-weight:600;margin-bottom:4px}'
    + '.option{display:flex;gap:6px;font-size:12px;color:#444;margin:2px 0}'
    + '.opt-num{color:#7c3aed;font-weight:700;width:16px}'
    + '@media print{body{padding:0}}'
    + '</style></head><body>' + card.innerHTML + '</body></html>');
  win.document.close();
  win.print();
}

// ── Stints ─────────────────────────────────────────────────────────────────────
function renderStints(stints) {
  if (!stints || stints.length < 2) return '';
  var rows = stints.map(function(s) {
    return '<div class="stint-row">'
      + '<div style="font-weight:700;color:#aaa">Stint '+s.stint+'</div>'
      + '<div>L'+s.start_lap+'\u2013'+s.end_lap+'</div>'
      + '<div>'+s.lap_count+' laps</div>'
      + '<div>'+(fmtLap(s.avg_lap_s) || '\u2014')+'</div>'
      + '<div style="color:#c084fc">'+(fmtLap(s.best_lap_s) || '\u2014')+'</div>'
      + '<div>'+(s.fuel_used_gal != null ? s.fuel_used_gal.toFixed(2) + ' gal' : '\u2014')+'</div>'
      + '</div>';
  }).join('');
  return '<div class="section-label">Stint analysis</div>'
    + '<div class="stint-table">'
    + '<div class="stint-row stint-header"><div>Stint</div><div>Laps</div><div>Count</div><div>Avg time</div><div>Best time</div><div>Fuel used</div></div>'
    + rows
    + '</div>';
}

// ── Compare ───────────────────────────────────────────────────────────────────
function toggleCompare() {
  var wrap = document.getElementById('compare-wrap');
  wrap.classList.toggle('visible');
}

function toggleStoPanel() {
  var p = document.getElementById('sto-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}

async function handleStoDrop(e) {
  e.preventDefault();
  var file = e.dataTransfer.files[0];
  if (file) await handleStoFile(file);
}

async function handleStoFile(file) {
  if (!file || !file.name.endsWith('.sto')) {
    alert('Please drop a .sto iRacing setup file.'); return;
  }
  document.getElementById('sto-drop').style.display    = 'none';
  document.getElementById('sto-loading').style.display = 'block';
  document.getElementById('sto-analysis-output').style.display = 'none';

  var fd = new FormData();
  fd.append('file', file);
  var data;
  try {
    var res = await fetch('/api/sto-decode', {method: 'POST', body: fd});
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
    var msg = data.error === 'unsupported_car'
      ? 'This car is not supported by the decoder (setupdelta does not have a mapping for it).'
      : 'Could not decode this setup file: ' + data.error;
    alert(msg);
    return;
  }

  var lastResult = window._lastAnalysisResult || null;

  var tabs   = data.tabs || {};
  var tabNames = Object.keys(tabs);

  var setupAnalysis = analyzeSetup(tabs, data.car_config || null);
  window._lastSetupAnalysis = setupAnalysis;

  var html = '<div class="sto-analysis">';
  html += '<div class="sto-analysis-header">'
    + '<span class="sto-analysis-title">\ud83d\udd27 '+file.name+'</span>'
    + '<div style="display:flex;align-items:center;gap:8px">'
    + renderTendencyBadge(setupAnalysis.tendencySummary)
    + (data.car_name ? '<span class="sto-car-badge">'+data.car_name+'</span>' : '')
    + '</div></div>';

  if (!lastResult) {
    html += '<div style="padding:10px 20px;font-size:11px;color:#555;border-bottom:1px solid #1a1a1a">'
      + '\ud83d\udca1 Analyze a telemetry file first to get cross-referenced setup insights.'
      + '</div>';
  } else {
    html += '<div style="padding:10px 20px;font-size:11px;color:#4caf50;border-bottom:1px solid #1a1a1a">'
      + '\u2713 Cross-referencing with current telemetry session'
      + '</div>';
  }

  var allTabNames = ['Setup Analysis'].concat(tabNames);
  html += '<div class="sto-tabs" id="sto-tab-nav">';
  allTabNames.forEach(function(t, i) {
    html += '<button class="sto-tab-btn'+(i===0?' active':'')+'" onclick="switchStoTab(\''+t+'\')" data-tab="'+t+'">'+t+'</button>';
  });
  html += '</div>';

  html += '<div class="sto-tab-content" id="sto-tab-Setup_Analysis" style="display:block;max-height:none">';

  html += '<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">';
  html += '<div class="setup-viz-title">Car Overview \u2014 Pressures, Camber & Ride Heights</div>';
  html += '<div class="car-outline-wrap">'+renderCarOutlineSVG(setupAnalysis)+'</div>';
  html += '</div>';

  html += '<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">';
  html += '<div class="setup-viz-title">Suspension & Damper Balance</div>';
  html += renderSuspensionBars(setupAnalysis);
  html += '</div>';

  var aeroHtml = renderAeroDiagram(setupAnalysis);
  var brakeHtml = renderBrakeDiagram(setupAnalysis);
  if (aeroHtml || brakeHtml) {
    html += '<div class="setup-viz-section" style="border-bottom:1px solid #1a1a1a">';
    html += '<div class="setup-viz-grid">';
    if (aeroHtml) {
      html += '<div class="setup-viz-card"><h4>Aero Balance</h4>'+aeroHtml+'</div>';
    }
    if (brakeHtml) {
      html += '<div class="setup-viz-card"><h4>Brake System</h4>'+brakeHtml+'</div>';
    }
    html += '</div></div>';
  }

  html += '<div class="setup-viz-section">';
  html += '<div class="setup-viz-title">Setup Recommendations</div>';
  html += '<div class="setup-rec-list">'+renderSetupRecs(setupAnalysis.recs)+'</div>';
  html += '</div>';

  html += '</div>';

  if (tabNames.length) {
    tabNames.forEach(function(tabName) {
      html += '<div class="sto-tab-content" id="sto-tab-'+tabName.replace(/\s+/g,'_')+'" style="display:none">';
      var sections = tabs[tabName];
      Object.entries(sections).forEach(function(entry) {
        var sectName = entry[0], params = entry[1];
        html += '<div class="sto-section-title">'+sectName+'</div>';
        params.forEach(function(p) {
          var insight = _crossRef(p.label, p.value, lastResult, setupAnalysis);
          var rangeHtml = (p.range_min != null && p.range_max != null)
            ? '<span class="sto-param-range">'+p.range_min+'\u2013'+p.range_max+'</span>' : '';
          html += '<div class="sto-param-row"><span class="sto-param-label">'+p.label+'</span><span>'+rangeHtml+'<span class="sto-param-value">'+p.value+'</span></span></div>';
          if (insight) {
            html += '<div class="sto-insight '+insight.type+'">\u26a1 '+insight.text+'</div>';
          }
        });
      });
      html += '</div>';
    });
  }

  if (data.notes) {
    html += '<div class="sto-notes-block">'
      + '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#444;margin-bottom:8px">Setup Notes</div>'
      + '<pre class="sto-notes-pre">'+data.notes.replace(/</g,'&lt;')+'</pre>'
      + '</div>';
  }

  html += '</div>';

  var out = document.getElementById('sto-analysis-output');
  out.innerHTML = html;
  out.style.display = 'block';

  // Auto-populate tire pressure calculator from decoded setup
  populateTireCalcFromSto(tabs, data.car_config || null);
}

function switchStoTab(name) {
  document.querySelectorAll('.sto-tab-btn').forEach(function(b) { b.classList.toggle('active', b.dataset.tab === name); });
  document.querySelectorAll('.sto-tab-content').forEach(function(p) {
    p.style.display = p.id === 'sto-tab-' + name.replace(/\s+/g,'_') ? 'block' : 'none';
  });
}

document.addEventListener('DOMContentLoaded', function() {
  ['a','b'].forEach(function(id) {
    var fi = document.getElementById('cmp-fi-'+id);
    var btn = document.getElementById('cmp-btn-'+id);
    if (!fi || !btn) return;
    fi.addEventListener('change', function() {
      if (fi.files[0]) {
        btn.textContent = fi.files[0].name;
        btn.classList.add('has-file');
      }
      var fa = document.getElementById('cmp-fi-a').files[0];
      var fb = document.getElementById('cmp-fi-b').files[0];
      document.getElementById('btn-cmp-go').disabled = !(fa && fb);
    });
  });
});

async function runCompare() {
  var fa = document.getElementById('cmp-fi-a').files[0];
  var fb = document.getElementById('cmp-fi-b').files[0];
  if (!fa || !fb) return;
  var btn = document.getElementById('btn-cmp-go');
  btn.disabled = true; btn.textContent = 'Comparing\u2026';
  var form = new FormData();
  form.append('file_a', fa);
  form.append('file_b', fb);
  try {
    var res  = await fetch('/api/compare', {method: 'POST', body: form});
    var data = await res.json();
    if (data.error) { document.getElementById('compare-results').innerHTML = '<p style="color:#f44336">'+data.error+'</p>'; return; }
    document.getElementById('compare-results').innerHTML = renderComparison(data.a, data.b);
  } catch(e) {
    document.getElementById('compare-results').innerHTML = '<p style="color:#f44336">'+e.message+'</p>';
  } finally {
    btn.disabled = false; btn.textContent = 'Compare \u2192';
  }
}

function renderComparison(a, b) {
  function statRow(label, aVal, bVal, lowerIsBetter) {
    if (aVal == null && bVal == null) return '';
    var aStr = aVal != null ? aVal : '\u2014';
    var bStr = bVal != null ? bVal : '\u2014';
    var aCls = '', bCls = '';
    if (aVal != null && bVal != null && aVal !== bVal) {
      var aWins = lowerIsBetter ? aVal < bVal : aVal > bVal;
      aCls = aWins ? 'compare-better' : 'compare-worse';
      bCls = aWins ? 'compare-worse'  : 'compare-better';
    }
    return '<div class="compare-stat-row"><span class="compare-stat-label">'+label+'</span><span class="compare-stat-val '+aCls+'">'+aStr+'</span><span class="compare-stat-val '+bCls+'">'+bStr+'</span></div>';
  }
  var sa = a.summary || {}, sb = b.summary || {};
  var ma = a.meta    || {}, mb = b.meta    || {};
  var statsHtml = '<div class="compare-stat-row" style="background:#111;border-radius:4px;padding:4px 0">'
    + '<span class="compare-stat-label" style="font-size:10px;color:#444;text-transform:uppercase">Metric</span>'
    + '<span style="font-size:10px;color:#2196F3;font-weight:700">A: '+(ma.filename || '?')+'</span>'
    + '<span style="font-size:10px;color:#ff9800;font-weight:700">B: '+(mb.filename || '?')+'</span>'
    + '</div>'
    + statRow('Best lap', sa.best_lap_s ? fmtLap(sa.best_lap_s) : null, sb.best_lap_s ? fmtLap(sb.best_lap_s) : null, true)
    + statRow('Avg lap',  sa.avg_lap_s  ? fmtLap(sa.avg_lap_s)  : null, sb.avg_lap_s  ? fmtLap(sb.avg_lap_s)  : null, true)
    + statRow('Consistency', sa.lap_consistency_s ? '\u00b1'+sa.lap_consistency_s+'s' : null, sb.lap_consistency_s ? '\u00b1'+sb.lap_consistency_s+'s' : null, true)
    + statRow('Top speed', sa.max_speed_mph ? sa.max_speed_mph+' mph' : null, sb.max_speed_mph ? sb.max_speed_mph+' mph' : null)
    + statRow('Fuel/lap',  sa.fuel_per_lap_gal ? sa.fuel_per_lap_gal+' gal' : null, sb.fuel_per_lap_gal ? sb.fuel_per_lap_gal+' gal' : null, true)
    + statRow('Peak lat G', sa.peak_lat_g ? sa.peak_lat_g+'g' : null, sb.peak_lat_g ? sb.peak_lat_g+'g' : null);
  var ha = a.handling || {}, hb = b.handling || {};
  var sectorNames = Object.keys(ha).concat(Object.keys(hb)).filter(function(v, i, a) { return a.indexOf(v) === i; });
  var sectorRows = sectorNames.map(function(name) {
    var da = ha[name] || {}, db = hb[name] || {};
    var ta = da.tendency || '\u2014', tb = db.tendency || '\u2014';
    var clsA = ta === 'understeer' ? '#2196F3' : ta === 'oversteer' ? '#f44336' : '#4caf50';
    var clsB = tb === 'understeer' ? '#2196F3' : tb === 'oversteer' ? '#f44336' : '#4caf50';
    return '<div class="compare-stat-row"><span class="compare-stat-label" style="font-size:11px">'+name.replace(/ \(.*\)/,'')+'</span><span style="color:'+clsA+';font-size:11px;font-weight:700">'+ta+'</span><span style="color:'+clsB+';font-size:11px;font-weight:700">'+tb+'</span></div>';
  }).join('');
  return '<div style="margin-top:16px">'
    + '<div class="section-label" style="margin-top:0">Summary comparison</div>'
    + '<div style="background:#181818;border-radius:10px;padding:14px 18px">'+statsHtml+'</div>'
    + (sectorRows.length ? '<div class="section-label">Handling by sector</div><div style="background:#181818;border-radius:10px;padding:14px 18px">'+sectorRows+'</div>' : '')
    + '</div>';
}

function renderDownforceRec(summary) {
  if (!summary || !summary.downforce_rec) return '';
  var dr  = summary.downforce_rec;
  var col = dr.trim === 'High' ? '#3b82f6'
            : dr.trim === 'Medium' ? '#8b5cf6'
            : '#f59e0b';
  var icon = dr.trim === 'High' ? '\u25b2\u25b2' : dr.trim === 'Medium' ? '\u25b2' : '\u25bc';
  return '<div style="display:flex;align-items:center;gap:12px;background:#1e293b;border-left:3px solid '+col+';border-radius:0 8px 8px 0;padding:10px 14px;margin-bottom:12px">'
    + '<span style="color:'+col+';font-size:1.1rem;font-weight:700">'+icon+'</span>'
    + '<div>'
    + '<span style="color:'+col+';font-weight:700;font-size:.9rem">'+dr.trim+' Downforce</span>'
    + '<span style="color:#64748b;font-size:.8rem;margin-left:8px">recommended</span>'
    + '<div style="color:#94a3b8;font-size:.82rem;margin-top:2px">'+dr.note+'</div>'
    + '</div></div>';
}

function renderTechStatus(ts) {
  if (!ts || !ts.corners || !Object.keys(ts.corners).length) return '';
  var overall = ts.pass;
  var badge   = overall
    ? '<span style="background:#16a34a;color:#fff;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:700">PASS</span>'
    : '<span style="background:#dc2626;color:#fff;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:700">FAIL</span>';

  var rows = Object.entries(ts.corners).map(function(entry) {
    var corner = entry[0], c = entry[1];
    var col = c.status === 'fail'    ? '#ef4444'
              : c.status === 'warning' ? '#f59e0b'
              :                          '#22c55e';
    var icon = c.status === 'fail' ? '\u2716' : c.status === 'warning' ? '\u26a0' : '\u2714';
    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #1e293b">'
      + '<span style="color:#94a3b8;font-size:.85rem;width:36px">'+corner+'</span>'
      + '<span style="color:#e2e8f0;font-size:.85rem">'+c.measured_mm+' mm measured</span>'
      + '<span style="color:#64748b;font-size:.8rem">min '+c.min_mm+' mm</span>'
      + '<span style="color:#64748b;font-size:.8rem">+'+c.margin_mm+' mm margin</span>'
      + '<span style="color:'+col+';font-size:.9rem;font-weight:700;width:20px;text-align:right">'+icon+'</span>'
      + '</div>';
  }).join('');

  var note = ts.series_note
    ? '<div style="color:#64748b;font-size:.75rem;margin-top:8px;line-height:1.4">'+ts.series_note+'</div>'
    : '';

  return '<div class="section-block">'
    + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
    + '<h3 style="margin:0;color:#e2e8f0;font-size:.95rem">Tech Inspection \u2014 Ride Heights</h3>'
    + badge
    + '</div>'
    + rows
    + note
    + '</div>';
}

function renderRideHeights(rh) {
  if (!rh) return '';
  var corners = ['LF','RF','LR','RR'];
  var vals = corners.map(function(c) { return rh[c]; });
  if (vals.every(function(v) { return v == null; })) return '';
  var cells = corners.map(function(c) {
    var v = rh[c];
    if (v == null) return '<div style="text-align:center"><div style="font-size:11px;color:#555">'+c+'</div><div style="color:#444">\u2014</div></div>';
    var col = v < 15 ? '#f44336' : v < 25 ? '#ff9800' : '#86efac';
    return '<div style="text-align:center"><div style="font-size:11px;color:#777">'+c+'</div><div style="font-size:18px;font-weight:700;color:'+col+'">'+v.toFixed(1)+'</div><div style="font-size:10px;color:#555">mm</div></div>';
  }).join('');
  return '<div class="section-label">Ride heights</div>'
    + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;background:#181818;border-radius:10px;padding:14px 18px;margin-bottom:12px">'+cells+'</div>';
}

function renderConfidence(conf, sigWarnings) {
  var warns = (sigWarnings || []).concat((conf && conf.issues) || []);
  if (!conf && !warns.length) return '';
  var dq = conf ? conf.data_quality : 1;
  var laps = conf ? conf.flying_laps : '?';
  var barCol = dq >= 0.8 ? '#4caf50' : dq >= 0.6 ? '#ff9800' : '#f44336';
  var labelCol = dq >= 0.8 ? '#86efac' : dq >= 0.6 ? '#fbbf24' : '#fca5a5';
  var label = dq >= 0.8 ? 'Good' : dq >= 0.6 ? 'Fair' : 'Low';
  var pct = Math.round(dq * 100);
  var warnHtml = warns.length
    ? '<ul style="margin:6px 0 0 0;padding-left:18px;color:#aaa;font-size:12px">'+warns.map(function(w) { return '<li>'+w+'</li>'; }).join('')+'</ul>'
    : '';
  return '<div style="background:#181818;border-radius:10px;padding:12px 18px;margin-bottom:12px;border-left:3px solid '+barCol+'">'
    + '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
    + '<span style="font-size:12px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.5px">Data confidence</span>'
    + '<span style="color:'+labelCol+';font-weight:700">'+label+' ('+pct+'%)</span>'
    + '<span style="flex:1;height:4px;background:#222;border-radius:2px;min-width:60px"><span style="display:block;height:4px;background:'+barCol+';border-radius:2px;width:'+pct+'%"></span></span>'
    + '<span style="color:#555;font-size:12px">'+laps+' flying lap'+(laps===1?'':'s')+'</span>'
    + '</div>'
    + warnHtml
    + '</div>';
}

function renderLapTimes(lapTimes) {
  if (!lapTimes || !lapTimes.length) return '';
  var best = Math.min.apply(null, lapTimes.map(function(l) { return l.time_s; }));
  var rows = lapTimes.map(function(l) {
    var isBest   = l.time_s === best;
    var delta    = l.time_s - best;
    var deltaStr = isBest ? '\u2b24 Fastest' : '+' + delta.toFixed(3) + 's';
    var isExcluded = window._excludedLaps && window._excludedLaps.has(l.lap);
    return '<div class="lap-row'+(isBest ? ' lap-fastest' : '')+(isExcluded ? ' lap-excluded' : '')+'" data-lap="'+l.lap+'" onclick="selectLap('+l.lap+')" style="cursor:pointer;grid-template-columns:56px 1fr 1fr 40px">'
      + '<div class="lap-num">'+l.lap+'</div>'
      + '<div class="lap-time">'+fmtLap(l.time_s)+'</div>'
      + '<div class="lap-delta">'+deltaStr+'</div>'
      + '<div style="padding:2px 6px;text-align:center">'
      + '<button onclick="event.stopPropagation();toggleExcludeLap('+l.lap+')" title="Exclude lap from analysis" style="background:none;border:1px solid #333;color:#666;border-radius:3px;padding:1px 5px;cursor:pointer;font-size:.7rem;line-height:1.2">\u2715</button>'
      + '</div></div>';
  }).join('');
  var _mn = best, _mx2 = Math.max.apply(null, lapTimes.map(function(l) { return l.time_s; })), _rng = _mx2 - _mn || 1;
  var _SW = 400, _SH = 28, _SP = 3;
  var _sparkSegs = lapTimes.map(function(l, i) {
    if (i === 0) return '';
    var x1 = _SP + ((i-1)/(lapTimes.length-1))*(_SW-2*_SP);
    var y1 = _SH - _SP - ((lapTimes[i-1].time_s - _mn)/_rng)*(_SH-2*_SP);
    var x2 = _SP + (i/(lapTimes.length-1))*(_SW-2*_SP);
    var y2 = _SH - _SP - ((l.time_s - _mn)/_rng)*(_SH-2*_SP);
    var d = l.time_s - _mn;
    var col = d < 0.3 ? '#4caf50' : d < 1.5 ? '#ff9800' : '#f44336';
    return '<line x1="'+x1.toFixed(1)+'" y1="'+y1.toFixed(1)+'" x2="'+x2.toFixed(1)+'" y2="'+y2.toFixed(1)+'" stroke="'+col+'" stroke-width="2" stroke-linecap="round"/>';
  }).join('');
  return '<div class="section-label">Lap times</div>'
    + '<svg viewBox="0 0 400 28" style="width:100%;height:28px;display:block;margin-bottom:6px;background:#111;border-radius:6px">'+_sparkSegs+'</svg>'
    + '<div class="lap-table">'
    + '<div class="lap-row lap-header" style="grid-template-columns:56px 1fr 1fr 40px"><div>Lap</div><div>Time</div><div>\u0394 Best</div><div></div></div>'
    + rows
    + '</div>';
}

function renderRecs(recs) {
  if (!recs || !recs.length)
    return '<p style="color:#444;font-size:13px">No issues flagged \u2014 data looks good, or insufficient data to analyse.</p>';
  return recs.map(function(r) {
    return '<div class="rec '+r.priority+'">'
      + '<div class="rec-head">'
      + '<span class="rec-cat">'+r.category+'</span>'
      + '<span class="rec-corner">'+r.corner+'</span>'
      + '</div>'
      + '<div class="rec-issue">'+r.issue+'</div>'
      + '<div class="rec-action">\u2192 '+r.action+'</div>'
      + (r.toe_verify ? '<div style="color:#f59e0b;font-size:.75rem;margin-top:4px">\u26a0 Toe adjustment suggested \u2014 iRacing telemetry does not report current toe angle. Verify the change is within your series\' legal adjustment range before applying.</div>' : '')
      + '</div>';
  }).join('');
}

function exportJSON() {
  if (!window._analysisData) return;
  var blob = new Blob([JSON.stringify(window._analysisData, null, 2)], {type:'application/json'});
  var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  var fname = (window._analysisData.meta && window._analysisData.meta.filename) || 'telemetry';
  a.download = fname.replace(/\.ibt$/i,'') + '_analysis.json';
  a.click();
}
function exportCSV() {
  if (!window._analysisData) return;
  var laps = (window._analysisData.lap_times || []);
  if (!laps.length) return;
  var best = Math.min.apply(null, laps.map(function(l) { return l.time_s; }));
  var rows = [['lap','time_s','delta_s']].concat(laps.map(function(l) { return [l.lap, l.time_s.toFixed(3), (l.time_s - best).toFixed(3)]; }));
  var csv = rows.map(function(r) { return r.join(','); }).join('\n');
  var blob = new Blob([csv], {type:'text/csv'});
  var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  var fname = (window._analysisData.meta && window._analysisData.meta.filename) || 'telemetry';
  a.download = fname.replace(/\.ibt$/i,'') + '_laps.csv';
  a.click();
}

function render(data) {
  var reBtn = document.getElementById('reanalyze-btn');
  if (!reBtn) {
    reBtn = document.createElement('button');
    reBtn.id = 'reanalyze-btn';
    reBtn.className = 'btn';
    reBtn.style.cssText = 'display:none;margin:8px 0;background:#b45309;color:#fff;';
    reBtn.textContent = 'Re-analyze (excluding selected laps)';
    reBtn.onclick = function() { go(window._ibtFile); };
    var status = document.getElementById('status');
    if (status) status.parentNode.insertBefore(reBtn, status.nextSibling);
  }
  reBtn.style.display = window._excludedLaps.size > 0 ? 'inline-flex' : 'none';

  window._analysisData = data;
  var t = data.tyre_temps     || {};
  var p = data.tyre_pressures || {};
  var m = data.meta           || {};
  var s = data.summary        || {};
  var det = data.detected     || {};

  var dur  = s.duration_s;
  var topv = s.max_speed_mph;
  var laps = s.laps_analysed;

  var carLabel   = (data.car   || '') + (det.auto_detected_car   ? ' <span class="auto-badge">auto</span>' : '');
  var trackLabel = (data.track || '') + (det.auto_detected_track ? ' <span class="auto-badge">auto</span>' : '');

  var autoDetectBanner = (det.auto_detected_car || det.auto_detected_track) ? '<div style="background:#0d2a40;border-left:3px solid #2196F3;padding:8px 28px;font-size:12px;color:#64b5f6">\u26a1 Auto-detected from telemetry'+(det.auto_detected_car ? ' \u00b7 Car: <b>'+(data.car || det.car_id)+'</b>' : '')+(det.auto_detected_track ? ' \u00b7 Track: <b>'+(data.track || det.track_id)+'</b>' : '')+'</div>' : '';

  var carId_selected   = document.getElementById('car-select').value;
  var trackId_selected = document.getElementById('track-select').value;
  var missingBanner = (!det.auto_detected_car && !carId_selected) || (!det.auto_detected_track && !trackId_selected) ? '<div style="background:#1a1000;border-left:3px solid #ff9800;padding:8px 28px;font-size:12px;color:#ffb74d">\u26a0 Could not auto-detect from telemetry \u2014 '+(det.raw_car_path ? 'CarPath: <b>'+det.raw_car_path+'</b>' : 'CarPath: <b>not found</b>')+' &nbsp;\u00b7&nbsp; '+(det.raw_track_name ? 'TrackName: <b>'+det.raw_track_name+'</b>' : 'TrackName: <b>not found</b>')+' &nbsp;\u2014 select manually above or report this so the config can be updated.</div>' : '';

  var sessionType = m.session_type || null;
  var sessionBadge = sessionType
    ? '<span style="background:'+(sessionType.toLowerCase().includes('race') ? '#7c2d12' : '#1a2a1a')+';color:'+(sessionType.toLowerCase().includes('race') ? '#fca5a5' : '#86efac')+';border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px">'+sessionType+'</span>'
    : '';
  var raceWarning = sessionType && sessionType.toLowerCase().includes('race')
    ? '<div style="background:#1a0a00;border-left:3px solid #f59e0b;padding:6px 28px;font-size:12px;color:#fbbf24">\u26a0 Race session detected \u2014 tyre data may include SC laps or early-stint anomalies. Recommendations are less reliable than practice/qual data.</div>' : '';

  var html = autoDetectBanner + missingBanner + raceWarning + '<div class="meta-bar">'
    + '<div style="display:flex;align-items:center;gap:8px"><b>'+(m.filename || '')+'</b> '+sessionBadge+'</div>'
    + '<div class="meta-car-track">'+carLabel+' &nbsp;\u00b7&nbsp; '+trackLabel+'</div>'
    + (m.ambient_temp_f ? '<div>Air temp: <b>'+m.ambient_temp_f+' \u00b0F</b></div>' : '')
    + (m.track_temp_f   ? '<div>Track temp: <b>'+m.track_temp_f+' \u00b0F</b></div>' : '')
    + (laps ? '<div>Laps: <b>'+laps+'</b></div>' : '')
    + (dur  ? '<div>Duration: <b>'+Math.floor(dur/60)+'m '+dur%60+'s</b></div>' : '')
    + (topv ? '<div>Top speed: <b>'+topv+' mph</b></div>' : '')
    + (s.best_lap_s ? '<div>Best: <b>'+fmtLap(s.best_lap_s)+'</b></div>' : '')
    + (s.avg_lap_s  ? '<div>Avg lap: <b>'+fmtLap(s.avg_lap_s)+'</b></div>' : '')
    + (s.lap_consistency_s ? '<div>Consistency: <b>\u00b1'+s.lap_consistency_s+'s</b></div>' : '')
    + (s.fuel_per_lap_gal ? '<div>Fuel/lap: <b>'+s.fuel_per_lap_gal+' gal</b></div>' : '')
    + (s.laps_to_empty ? '<div>Laps to empty: <b>'+s.laps_to_empty+'</b></div>' : '')
    + (s.peak_lat_g ? '<div>Peak lat G: <b>'+s.peak_lat_g+'g</b></div>' : '')
    + (s.peak_brake_g ? '<div>Peak brake G: <b>'+s.peak_brake_g+'g</b></div>' : '')
    + '<div>Sample rate: <b>'+(m.tick_rate || '?')+' Hz</b></div>'
    + '<button class="btn-reset" onclick="reset()">\u2190 Analyse another file</button>'
    + '<button class="btn-reset" onclick="exportJSON()" style="background:#1a2a3a;border-color:#2196F3;color:#64b5f6">\u2193 JSON</button>'
    + '<button class="btn-reset" onclick="exportCSV()" style="background:#1a2a1a;border-color:#4caf50;color:#86efac">\u2193 CSV</button>'
    + '</div>'
    + '<div class="page">'
    + renderDownforceRec(data.summary)
    + renderConfidence(data.confidence, data.signal_warnings)
    + '<div class="section-label">Tyre temperatures &amp; hot pressures</div>'
    + renderTrackTempBadge(m.track_temp_f, m.ambient_temp_f)
    + ((!m.track_temp_f && m.temp_debug) ? '<div style="background:#1a0a00;border-left:3px solid #555;padding:6px 12px;font-size:11px;color:#888;margin-bottom:10px;border-radius:4px"><b style="color:#aaa">Debug \u2014 temp lines from YAML:</b><br>'+(m.temp_debug||[]).map(function(l) { return '<code>'+l+'</code>'; }).join('<br>')+'</div>' : '')
    + '<div class="tyre-grid">'
    + tyreCard('LF \u2014 Left Front',  t.LF, p.LF)
    + tyreCard('RF \u2014 Right Front', t.RF, p.RF)
    + tyreCard('LR \u2014 Left Rear',   t.LR, p.LR)
    + tyreCard('RR \u2014 Right Rear',  t.RR, p.RR)
    + '</div>'
    + renderTechStatus(data.tech_status)
    + renderRideHeights(data.ride_heights)
    + renderBalance(data.balance)
    + renderTyreTrend(data.tyre_trend)
    + renderTrackMap(data.track_map)
    + renderInputTrace(data.speed_trace)
    + renderSpeedTrace(data.speed_trace)
    + renderHandling(data.handling)
    + renderBrake(data.brake)
    + renderOverlap(data.throttle_overlap)
    + renderStints(data.stints)
    + renderSectorSplits(data.sector_times)
    + renderLapDelta(data.lap_times)
    + renderLapTimes(data.lap_times)
    + renderSetupCard(data.setup_card, data.car, data.track)
    + '<div class="section-label">Full recommendations</div>'
    + '<div class="rec-list">'+renderRecs(data.recommendations)+'</div>'
    + renderLibrarySave(data)
    + '</div>';

  var resultsEl = document.getElementById('results');
  resultsEl.innerHTML = html;
  resultsEl.style.display = 'block';
  document.getElementById('upload-wrap').style.display = 'none';
  if (window._trackMapData) setTimeout(function() { drawTrackMap(localStorage.getItem('iracing-tm-mode') || 'speed'); }, 20);
  window._lastAnalysisResult = data;
}

function renderLibrarySave(data) {
  var carId   = document.getElementById('car-select').value;
  var trackId = document.getElementById('track-select').value;
  if (!carId || !trackId) return '';
  var carName   = data.car   ? data.car.name   : carId;
  var trackName = data.track ? data.track.name : trackId;
  return '<div id="library-save-block" style="background:#0c1a3d;border:1px solid #1f3a7a;border-radius:8px;padding:20px;margin-top:16px;">'
    + '<div style="font-size:15px;font-weight:600;color:#93c5fd;margin-bottom:6px;">Save to Setup Library</div>'
    + '<div style="font-size:13px;color:#6b90c4;margin-bottom:14px;">Append these recommendations to your <strong>'+carName+'</strong> setup at <strong>'+trackName+'</strong> in the library.</div>'
    + '<button onclick="saveToLibrary(\''+carId+'\',\''+trackId+'\')" style="background:#1d4ed8;color:#fff;border:none;padding:8px 18px;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;">Save to Library</button>'
    + '<span id="library-save-status" style="margin-left:12px;font-size:13px;"></span>'
    + '</div>';
}

// ── Tire Pressure Calculator ─────────────────────────────────────────────────
var _tireCalcUnit = 'F'; // 'F' or 'C'
var _tireCalcConfig = null; // loaded car config with target_hot_psi

function toggleTireCalcPanel() {
  var p = document.getElementById('tire-calc-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  if (p.style.display === 'block') initTireCalcDropdown();
}

function initTireCalcDropdown() {
  var sel = document.getElementById('tire-calc-car');
  if (sel.options.length > 1) return; // already populated
  Object.keys(carData).sort(function(a, b) {
    return (carData[a].name || a).localeCompare(carData[b].name || b);
  }).forEach(function(id) {
    var c = carData[id];
    var opt = document.createElement('option');
    opt.value = id;
    opt.textContent = c.name + (c.class ? ' (' + c.class + ')' : '');
    sel.appendChild(opt);
  });
  // Sync with main car selector
  var mainCar = document.getElementById('car-select').value;
  if (mainCar) sel.value = mainCar;
  // Sync ambient temp from main input
  var mainTemp = document.getElementById('air-temp').value;
  if (mainTemp) document.getElementById('tire-calc-temp').value = mainTemp;

  sel.addEventListener('change', loadTireCalcCarConfig);
  document.getElementById('tire-calc-temp').addEventListener('input', recalcTirePressures);
  document.getElementById('tire-calc-heat-gain').addEventListener('input', recalcTirePressures);
  document.querySelectorAll('.tire-calc-cold-input').forEach(function(inp) {
    inp.addEventListener('input', recalcTirePressures);
  });

  if (mainCar) loadTireCalcCarConfig();
}

async function loadTireCalcCarConfig() {
  var carId = document.getElementById('tire-calc-car').value;
  _tireCalcConfig = null;
  document.getElementById('tire-calc-targets').style.display = 'none';
  document.getElementById('tire-calc-cold-inputs').style.display = 'none';
  document.getElementById('tire-calc-results').style.display = 'none';

  if (!carId) return;

  try {
    var resp = await fetch('/api/car-config/' + encodeURIComponent(carId));
    var data = await resp.json();
    if (data.error || !data.target_hot_psi) {
      document.getElementById('tire-calc-target-grid').innerHTML =
        '<div style="grid-column:1/-1;color:#666;font-size:12px">No target hot pressures configured for this car.</div>';
      document.getElementById('tire-calc-targets').style.display = 'block';
      return;
    }
    _tireCalcConfig = data;
  } catch (e) {
    return;
  }

  var targets = _tireCalcConfig.target_hot_psi;
  var grid = document.getElementById('tire-calc-target-grid');
  grid.innerHTML = ['LF', 'RF', 'LR', 'RR'].map(function(c) {
    var val = targets[c] != null ? targets[c].toFixed(1) : '—';
    return '<div class="tire-calc-psi-cell">'
      + '<div class="tire-calc-corner-label">' + c + '</div>'
      + '<div class="tire-calc-target-val">' + val + ' <span class="tire-calc-unit">psi</span></div>'
      + '</div>';
  }).join('');

  document.getElementById('tire-calc-targets').style.display = 'block';
  document.getElementById('tire-calc-cold-inputs').style.display = 'block';
  recalcTirePressures();
}

function toggleTireCalcUnit() {
  var btn = document.getElementById('tire-calc-unit-btn');
  var inp = document.getElementById('tire-calc-temp');
  var curVal = parseFloat(inp.value);

  if (_tireCalcUnit === 'F') {
    _tireCalcUnit = 'C';
    btn.textContent = '\u00b0C';
    if (!isNaN(curVal)) inp.value = Math.round((curVal - 32) * 5 / 9);
    inp.placeholder = '24';
  } else {
    _tireCalcUnit = 'F';
    btn.textContent = '\u00b0F';
    if (!isNaN(curVal)) inp.value = Math.round(curVal * 9 / 5 + 32);
    inp.placeholder = '75';
  }
  recalcTirePressures();
}

function _getTireCalcAmbientF() {
  var val = parseFloat(document.getElementById('tire-calc-temp').value);
  if (isNaN(val)) return null;
  return _tireCalcUnit === 'C' ? val * 9 / 5 + 32 : val;
}

function recalcTirePressures() {
  var resultsDiv = document.getElementById('tire-calc-results');
  if (!_tireCalcConfig || !_tireCalcConfig.target_hot_psi) {
    resultsDiv.style.display = 'none';
    return;
  }

  var ambientF = _getTireCalcAmbientF();
  var heatGain = parseFloat(document.getElementById('tire-calc-heat-gain').value);
  if (isNaN(heatGain)) heatGain = 5.0;
  var targets = _tireCalcConfig.target_hot_psi;
  var baselineF = 75;
  var tempCorrectionPerF = 0.1;

  var corners = ['LF', 'RF', 'LR', 'RR'];
  var recommended = {};
  var tempCorrection = 0;
  if (ambientF != null) {
    tempCorrection = (baselineF - ambientF) * tempCorrectionPerF;
  }

  corners.forEach(function(c) {
    if (targets[c] == null) return;
    recommended[c] = targets[c] - heatGain + tempCorrection;
  });

  // Render recommended pressures
  var grid = document.getElementById('tire-calc-results-grid');
  grid.innerHTML = corners.map(function(c) {
    var val = recommended[c] != null ? recommended[c].toFixed(1) : '—';
    return '<div class="tire-calc-psi-cell tire-calc-result-cell">'
      + '<div class="tire-calc-corner-label">' + c + '</div>'
      + '<div class="tire-calc-rec-val">' + val + ' <span class="tire-calc-unit">psi</span></div>'
      + '</div>';
  }).join('');

  // Render deltas from current cold pressures (if entered)
  var deltasDiv = document.getElementById('tire-calc-deltas');
  var hasCold = false;
  var deltaHtml = '';
  var deltaItems = corners.map(function(c) {
    var coldInput = document.getElementById('tire-calc-cold-' + c.toLowerCase());
    var coldVal = coldInput ? parseFloat(coldInput.value) : NaN;
    if (isNaN(coldVal) || recommended[c] == null) return null;
    hasCold = true;
    var delta = recommended[c] - coldVal;
    var sign = delta >= 0 ? '+' : '';
    var cls = Math.abs(delta) < 0.3 ? 'delta-ok' : delta > 0 ? 'delta-increase' : 'delta-decrease';
    var label = Math.abs(delta) < 0.3 ? 'On target' : (delta > 0 ? 'Increase' : 'Decrease');
    return '<div class="tire-calc-delta-item ' + cls + '">'
      + '<span class="tire-calc-delta-corner">' + c + '</span>'
      + '<span class="tire-calc-delta-val">' + sign + delta.toFixed(1) + ' psi</span>'
      + '<span class="tire-calc-delta-label">' + label + '</span>'
      + '</div>';
  }).filter(Boolean);

  if (hasCold) {
    deltasDiv.innerHTML = '<div class="tire-calc-section-title">Adjustment from Current</div>'
      + '<div class="tire-calc-delta-grid">' + deltaItems.join('') + '</div>';
    deltasDiv.style.display = 'block';
  } else {
    deltasDiv.innerHTML = '';
  }

  // Render explanation
  var explDiv = document.getElementById('tire-calc-explanation');
  var tempStr = ambientF != null ? ambientF.toFixed(0) + '\u00b0F' : 'not set';
  var corrStr = ambientF != null
    ? (tempCorrection >= 0 ? '+' : '') + tempCorrection.toFixed(1) + ' psi'
    : 'N/A (no temp entered)';
  var corrDir = ambientF != null
    ? (ambientF > baselineF ? 'Above baseline \u2014 tires heat more, so start lower.'
       : ambientF < baselineF ? 'Below baseline \u2014 tires heat less, so start higher.'
       : 'At baseline \u2014 no correction needed.')
    : '';
  explDiv.innerHTML = '<div class="tire-calc-explain-title">How it works</div>'
    + '<div class="tire-calc-explain-formula">'
    + 'recommended_cold = target_hot - heat_gain + temp_correction'
    + '</div>'
    + '<div class="tire-calc-explain-items">'
    + '<div><span class="tire-calc-explain-label">Target hot:</span> From car config (varies per corner)</div>'
    + '<div><span class="tire-calc-explain-label">Expected heat gain:</span> ' + heatGain.toFixed(1) + ' psi (tires gain ~4\u20136 psi from cold to hot in iRacing)</div>'
    + '<div><span class="tire-calc-explain-label">Ambient temp:</span> ' + tempStr + '</div>'
    + '<div><span class="tire-calc-explain-label">Temp correction:</span> ' + corrStr + ' (0.1 psi per 1\u00b0F from 75\u00b0F baseline)</div>'
    + (corrDir ? '<div style="color:#888;font-style:italic;margin-top:2px">' + corrDir + '</div>' : '')
    + '</div>';

  resultsDiv.style.display = 'block';
}

// Auto-populate tire calculator from decoded .sto data
function populateTireCalcFromSto(tabs, carConfig) {
  if (!tabs) return;

  // Extract cold pressures from setup params
  var corners = {LF: 'left front', RF: 'right front', LR: 'left rear', RR: 'right rear'};
  var foundAny = false;

  Object.entries(corners).forEach(function(entry) {
    var corner = entry[0], searchKey = entry[1];
    var input = document.getElementById('tire-calc-cold-' + corner.toLowerCase());
    if (!input) return;

    // Search through tabs for pressure values
    Object.values(tabs).forEach(function(sections) {
      Object.values(sections).forEach(function(params) {
        params.forEach(function(p) {
          var label = p.label.toLowerCase();
          if (label.includes(searchKey) && (label.includes('pressure') || label.includes('psi'))) {
            var match = p.value.match(/([\d.]+)\s*(?:kPa|psi)/i);
            if (match) {
              var val = parseFloat(match[1]);
              // Convert kPa to PSI if needed
              if (p.value.toLowerCase().includes('kpa')) {
                val = val * 0.14503773773;
              }
              input.value = val.toFixed(1);
              foundAny = true;
            }
          }
        });
      });
    });
  });

  if (foundAny) {
    document.getElementById('tire-calc-sto-badge').style.display = 'inline';
    // Open the calculator panel if not already open
    var panel = document.getElementById('tire-calc-panel');
    if (panel.style.display === 'none') panel.style.display = 'block';
    // If car config was returned, sync the car dropdown
    if (carConfig && carConfig.name) {
      var sel = document.getElementById('tire-calc-car');
      if (sel.options.length <= 1) initTireCalcDropdown();
      // Try to match by name
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].textContent.includes(carConfig.name)) {
          sel.value = sel.options[i].value;
          loadTireCalcCarConfig();
          break;
        }
      }
    }
    recalcTirePressures();
  }
}

async function saveToLibrary(carKey, trackKey) {
  var statusEl = document.getElementById('library-save-status');
  statusEl.textContent = 'Saving\u2026';
  statusEl.style.color = '#6b90c4';

  var recs  = (window._analysisData.recommendations || []);
  var sc    = window._analysisData.setup_card || {};
  var lines   = [];

  if (sc.tyres && sc.tyres.pressures) {
    lines.push('TYRE PRESSURES:');
    sc.tyres.pressures.forEach(function(t) {
      if (t.cold_adj) lines.push('  '+t.corner+': '+(t.cold_adj > 0 ? '+' : '')+t.cold_adj+' PSI cold (hot target: '+t.target_hot_psi+' PSI)');
    });
  }
  if (sc.tyres && sc.tyres.camber && sc.tyres.camber.length) {
    lines.push('CAMBER:');
    sc.tyres.camber.forEach(function(c) { lines.push('  '+c.corner+': '+c.direction); });
  }
  if (sc.suspension && sc.suspension.length) {
    lines.push('SUSPENSION:');
    sc.suspension.forEach(function(s) { lines.push('  ['+s.priority+'] '+s.sector+' \u2014 '+s.label+': '+(s.options||[]).join(', ')); });
  }
  if (recs.length) {
    lines.push('RECOMMENDATIONS:');
    recs.forEach(function(r) { lines.push('  ['+r.priority+'] '+r.category+(r.corner ? ' '+r.corner : '')+': '+r.issue+' \u2192 '+r.action); });
  }

  var notes = lines.join('\n');

  try {
    var resp = await fetch('http://localhost:5057/api/advisor-notes', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ car_key: carKey, track_key: trackKey, notes: notes }),
    });
    var result = await resp.json();
    if (resp.ok) {
      statusEl.textContent = 'Saved to "'+result.setup_filename+'"';
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
