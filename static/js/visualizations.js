// ── Track Map ─────────────────────────────────────────────────────────────────
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
  var tm = window._trackMapData;
  if (!tm) return;
  var pts = tm.points;

  document.querySelectorAll('.tm-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.mode === mode);
  });

  // Bounding box of Python-normalised coords (0–1000 range)
  var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  pts.forEach(function(p) {
    if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
  });
  var PAD = 36;
  var VW  = (maxX - minX) + 2 * PAD;
  var VH  = (maxY - minY) + 2 * PAD;
  var ox  = -minX + PAD;
  var oy  = -minY + PAD;

  function valColor(v, m) {
    var c = function(x) { return Math.max(0, Math.min(1, x)); };
    var L = function(a, b, t) { return Math.round(a + (b - a) * c(t)); };
    var lRGB = function(c1, c2, t) { return [L(c1[0],c2[0],t), L(c1[1],c2[1],t), L(c1[2],c2[2],t)]; };
    if (m === 'speed') {
      var stops = [[26,79,216],[0,188,212],[76,175,80],[255,235,59],[244,67,54]];
      var t4 = c(v) * 4, i = Math.min(3, Math.floor(t4));
      var rgb = lRGB(stops[i], stops[i+1], t4 - i);
      return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')';
    }
    if (m === 'throttle') {
      var rgb = lRGB([26,26,26], [0,230,118], c(v));
      return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')';
    }
    if (m === 'gear') {
      var gc = [[60,60,60],[244,67,54],[255,152,0],[255,235,59],[76,175,80],[0,188,212],[33,150,243],[124,77,255],[224,64,251]];
      var rgb = gc[Math.min(8, Math.max(0, Math.round(v)))];
      return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')';
    }
    if (m === 'balance') {
      var t = Math.max(0, Math.min(1, (v - 0.7) / 0.6));
      if (t < 0.5) { var rgb = lRGB([244,67,54],[80,80,80],t*2); return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')'; }
      else          { var rgb = lRGB([80,80,80],[33,150,243],(t-0.5)*2); return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')'; }
    }
    var rgb = lRGB([26,26,26], [244,67,54], c(v));
    return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')';
  }

  var mx = tm.max_speed;
  var svg = '';

  // Coloured track path
  for (var i = 1; i < pts.length; i++) {
    var p0 = pts[i-1], p1 = pts[i];
    var v = mode === 'speed'    ? ((p0.spd + p1.spd) / 2) / mx
            : mode === 'throttle' ? (p0.thr + p1.thr) / 2
            : mode === 'gear'     ? ((p0.gear || 0) + (p1.gear || 0)) / 2
            : mode === 'balance'  ? ((p0.us  || 1) + (p1.us  || 1)) / 2
            :                       (p0.brk + p1.brk) / 2;
    var col = valColor(v, mode);
    svg += '<line x1="'+(p0.x+ox).toFixed(1)+'" y1="'+(p0.y+oy).toFixed(1)+'" '
         + 'x2="'+(p1.x+ox).toFixed(1)+'" y2="'+(p1.y+oy).toFixed(1)+'" '
         + 'stroke="'+col+'" stroke-width="3.5" stroke-linecap="round"/>';
  }

  // Sector boundary markers
  (tm.sectors || []).forEach(function(s, si) {
    if (s.start <= 0.005) return;
    var closest = 0, minD = Infinity;
    pts.forEach(function(p, i) { var d = Math.abs(p.pct - s.start); if (d < minD) { minD = d; closest = i; } });
    var p = pts[closest];
    svg += '<circle cx="'+(p.x+ox).toFixed(1)+'" cy="'+(p.y+oy).toFixed(1)+'" r="5" fill="#f59e0b" stroke="#111" stroke-width="1.5"/>';
    svg += '<text x="'+(p.x+ox+8).toFixed(0)+'" y="'+(p.y+oy+4).toFixed(0)+'" fill="#f59e0b" font-size="12" font-family="sans-serif" font-weight="700">S'+(si+1)+'</text>';
  });

  // Start / finish ring
  if (pts.length > 0) {
    var sf = pts[0];
    svg += '<circle cx="'+(sf.x+ox).toFixed(1)+'" cy="'+(sf.y+oy).toFixed(1)+'" r="6" fill="none" stroke="#fff" stroke-width="2.5"/>';
    svg += '<circle cx="'+(sf.x+ox).toFixed(1)+'" cy="'+(sf.y+oy).toFixed(1)+'" r="2.5" fill="#fff"/>';
  }

  // Throttle application points (orange dots)
  (tm.throttle_apps || []).forEach(function(ap) {
    svg += '<circle cx="'+(ap.x+ox).toFixed(1)+'" cy="'+(ap.y+oy).toFixed(1)+'" r="4" fill="#f97316" stroke="#111" stroke-width="1" opacity="0.85"/>';
  });

  // Corner minimum speed dots (cyan) with speed label
  (tm.corner_mins || []).forEach(function(cm) {
    svg += '<circle cx="'+(cm.x+ox).toFixed(1)+'" cy="'+(cm.y+oy).toFixed(1)+'" r="5" fill="#06b6d4" stroke="#111" stroke-width="1" opacity="0.9"/>';
    svg += '<text x="'+(cm.x+ox+7).toFixed(0)+'" y="'+(cm.y+oy+4).toFixed(0)+'" fill="#06b6d4" font-size="9" font-family="sans-serif" font-weight="600">'+cm.spd.toFixed(0)+'</text>';
  });

  // Coast zone overlay — grey segments where throttle < 5% and brake < 5%
  (function() {
    var _coastSegs = [];
    var _cSeg = null;
    pts.forEach(function(p) {
      if (p.thr < 0.05 && p.brk < 0.05) {
        if (!_cSeg) _cSeg = [];
        _cSeg.push(p);
      } else {
        if (_cSeg && _cSeg.length > 1) _coastSegs.push(_cSeg);
        _cSeg = null;
      }
    });
    if (_cSeg && _cSeg.length > 1) _coastSegs.push(_cSeg);
    _coastSegs.forEach(function(seg) {
      var d = seg.map(function(p, i) { return (i===0?'M':'L')+(p.x+ox).toFixed(1)+','+(p.y+oy).toFixed(1); }).join(' ');
      svg += '<path d="'+d+'" fill="none" stroke="rgba(200,200,200,0.45)" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>';
    });
  })();

  var svgEl = document.getElementById('track-map-svg');
  if (svgEl) {
    svgEl.setAttribute('viewBox', '0 0 '+VW.toFixed(0)+' '+VH.toFixed(0));
    svgEl.innerHTML = svg;
  }

  // Update colour legend
  var leg = document.getElementById('tm-legend-bar');
  if (leg) {
    var gradients = {
      speed:    'linear-gradient(to right,rgb(26,79,216),rgb(0,188,212),rgb(76,175,80),rgb(255,235,59),rgb(244,67,54))',
      throttle: 'linear-gradient(to right,rgb(26,26,26),rgb(0,230,118))',
      brake:    'linear-gradient(to right,rgb(26,26,26),rgb(244,67,54))',
      gear:     'linear-gradient(to right,rgb(244,67,54),rgb(255,152,0),rgb(255,235,59),rgb(76,175,80),rgb(0,188,212),rgb(33,150,243),rgb(124,77,255),rgb(224,64,251))',
      balance:  'linear-gradient(to right,rgb(244,67,54),rgb(80,80,80),rgb(33,150,243))',
    };
    var labels = { speed: 'Slow \u2192 '+mx+' mph', throttle: '0 \u2192 100% throttle', brake: '0 \u2192 100% brake', gear: '1st \u2192 8th gear', balance: 'Oversteer \u2190 Neutral \u2192 Understeer' };
    leg.innerHTML = '<span style="display:inline-flex;align-items:center;gap:6px">'
      + '<span style="display:inline-block;width:80px;height:6px;border-radius:3px;background:'+gradients[mode]+'"></span>'
      + '<span>'+labels[mode]+'</span></span>'
      + '<span style="display:inline-flex;align-items:center;gap:4px;margin-left:12px;opacity:0.7">'
      + '<span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:rgba(200,200,200,0.6)"></span>'
      + '<span style="font-size:11px">coast</span>'
      + '</span>';
  }

  if (svgEl) {
    if (svgEl._tmMousemove)  svgEl.removeEventListener('mousemove',  svgEl._tmMousemove);
    if (svgEl._tmMouseleave) svgEl.removeEventListener('mouseleave', svgEl._tmMouseleave);
    var tt = document.getElementById('tm-tooltip');
    var _pts = pts, _ox = ox, _oy = oy;
    svgEl._tmMousemove = function(e) {
      var rect = svgEl.getBoundingClientRect();
      var vb = svgEl.viewBox.baseVal;
      if (!vb.width) return;
      var mx = (e.clientX - rect.left) / rect.width * vb.width;
      var my = (e.clientY - rect.top)  / rect.height * vb.height;
      var bestIdx = 0, bestD = Infinity;
      _pts.forEach(function(p, i) {
        var d = Math.pow(p.x + _ox - mx, 2) + Math.pow(p.y + _oy - my, 2);
        if (d < bestD) { bestD = d; bestIdx = i; }
      });
      var bp = _pts[bestIdx];
      var _thresh = Math.pow(vb.width * 0.06, 2);
      if (tt && bestD < _thresh) {
        var gStr = bp.gear != null ? '<br>Gear: <b>'+(bp.gear || 'N')+'</b>' : '';
        tt.innerHTML = '<b>'+bp.spd.toFixed(0)+' mph</b><br>Throttle: '+(bp.thr*100).toFixed(0)+'%<br>Brake: '+(bp.brk*100).toFixed(0)+'%'+gStr+'<br><span style="color:#555">'+(bp.pct*100).toFixed(1)+'% lap</span>';
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
  var svgEl = document.getElementById('track-map-svg');
  if (!svgEl) return;
  var vb = svgEl.viewBox.baseVal;
  if (!vb.width) return;
  var scale = 2, w = Math.round(vb.width * scale), h = Math.round(vb.height * scale);
  var svgData = new XMLSerializer().serializeToString(svgEl);
  var canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  var ctx = canvas.getContext('2d');
  ctx.fillStyle = '#181818';
  ctx.fillRect(0, 0, w, h);
  var blob = new Blob([svgData], {type: 'image/svg+xml;charset=utf-8'});
  var url = URL.createObjectURL(blob);
  var img = new Image();
  img.onload = function() {
    ctx.drawImage(img, 0, 0, w, h);
    URL.revokeObjectURL(url);
    var a = document.createElement('a');
    var tm = window._trackMapData;
    a.download = tm ? 'track-map-lap'+tm.lap+'.png' : 'track-map.png';
    a.href = canvas.toDataURL('image/png');
    a.click();
  };
  img.src = url;
}

// ── Input Trace (3-panel speed / throttle / brake) ───────────────────────────
function renderInputTrace(st) {
  if (!st || !st.laps || !st.laps.length) return '';
  var W = 600, PL = 44, PR = 12, PT = 6, PB = 18;
  var panels = [
    {key: 'spd', label: 'Speed (mph)', h: 100, color: '#c084fc', max: null},
    {key: 'thr', label: 'Throttle %',  h: 60,  color: '#4caf50', max: 1},
    {key: 'brk', label: 'Brake %',     h: 60,  color: '#f44336', max: 1},
  ];

  var maxSpd = 0;
  st.laps.forEach(function(l) { l.points.forEach(function(p) { if (p.spd > maxSpd) maxSpd = p.spd; }); });
  panels[0].max = maxSpd || 1;

  var colors = ['#c084fc','#64b5f6','#81c784','#ffb74d','#f06292'];

  var totalH = panels.reduce(function(s, p) { return s + p.h + PT + PB; }, 0) + 10;
  var svgContent = '';
  var yOffset = 0;

  panels.forEach(function(panel, pi) {
    var IW = W - PL - PR, IH = panel.h;
    var yBase = yOffset + PT;

    svgContent += '<rect x="'+PL+'" y="'+yBase+'" width="'+IW+'" height="'+IH+'" fill="#111" rx="3"/>';

    for (var g = 25; g <= 75; g += 25) {
      var gy = (yBase + IH - (g / 100) * IH).toFixed(1);
      var val = panel.key === 'spd' ? Math.round(panel.max * g / 100) : g;
      svgContent += '<line x1="'+PL+'" y1="'+gy+'" x2="'+(W - PR)+'" y2="'+gy+'" stroke="#222" stroke-width="1"/>';
      svgContent += '<text x="'+(PL - 4)+'" y="'+(parseFloat(gy) + 4)+'" text-anchor="end" fill="#444" font-size="9">'+val+'</text>';
    }

    svgContent += '<text x="'+PL+'" y="'+(yBase - 2)+'" fill="#555" font-size="9" font-weight="700" text-transform="uppercase">'+panel.label+'</text>';

    st.laps.filter(function(l) { return !l.is_best; }).forEach(function(l, li) {
      if (!l.points[0] || l.points[0][panel.key] == null) return;
      var col = colors[(li + 1) % colors.length];
      var pts = l.points.map(function(p) {
        var v = panel.key === 'spd' ? p.spd / panel.max : (p[panel.key] || 0);
        return (PL + p.pct * IW).toFixed(1)+','+(yBase + IH - v * IH).toFixed(1);
      }).join(' ');
      svgContent += '<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1" opacity="0.35"/>';
    });

    st.laps.filter(function(l) { return l.is_best; }).forEach(function(l) {
      if (!l.points[0] || l.points[0][panel.key] == null) return;
      var pts = l.points.map(function(p) {
        var v = panel.key === 'spd' ? p.spd / panel.max : (p[panel.key] || 0);
        return (PL + p.pct * IW).toFixed(1)+','+(yBase + IH - v * IH).toFixed(1);
      }).join(' ');
      svgContent += '<polyline points="'+pts+'" fill="none" stroke="'+panel.color+'" stroke-width="1.8"/>';
    });

    if (pi === panels.length - 1) {
      svgContent += '<text x="'+PL+'" y="'+(yBase + IH + 12)+'" fill="#444" font-size="9">0%</text>';
      svgContent += '<text x="'+(W - PR)+'" y="'+(yBase + IH + 12)+'" text-anchor="end" fill="#444" font-size="9">100% lap</text>';
    }

    yOffset += PT + IH + PB;
  });

  var legendItems = st.laps.map(function(l, i) {
    var col = l.is_best ? '#c084fc' : colors[i % colors.length];
    var op  = l.is_best ? '1' : '0.5';
    return '<span class="it-legend-item" style="opacity:'+op+'">'
      + '<span class="it-legend-dot" style="background:'+col+'"></span>'
      + 'Lap '+l.lap+' \u2014 '+fmtLap(l.time_s)+(l.is_best ? ' \u2b24' : '')
      + '</span>';
  }).join('');

  return '<div class="section-label">Driver inputs \u2014 Top '+st.laps.length+' laps</div>'
    + '<div class="input-trace-wrap">'
    + '<svg viewBox="0 0 '+W+' '+totalH+'" style="width:100%;display:block">'+svgContent+'</svg>'
    + '<div class="it-legend">'+legendItems+'</div>'
    + '</div>';
}

// ── Sector Splits ─────────────────────────────────────────────────────────────
function renderSectorSplits(st) {
  if (!st || !st.laps || !st.laps.length || !st.sectors) return '';
  var sectors  = st.sectors;
  var nSec     = sectors.length;
  var bestSpl  = st.best_splits || [];
  var colTpl   = '52px repeat('+nSec+', 1fr) 90px';

  var headerCells = sectors.map(function(s) {
    return '<div>'+s.name.replace(/ —.*/, '').trim()+'</div>';
  }).join('');

  var rows = st.laps.map(function(l) {
    var splits = l.splits || [];
    var cells = splits.map(function(s, i) {
      var best  = bestSpl[i];
      var isBst = best != null && Math.abs(s - best) < 0.001;
      var delta = best != null ? s - best : null;
      var dStr  = isBst ? '' : (delta != null ? '+'+delta.toFixed(3) : '');
      var dCls  = delta == null ? '' : delta < 0.1 ? 'fast' : delta < 0.4 ? 'med' : 'slow';
      return '<div>'
        + '<span class="'+(isBst ? 'ss-best' : '')+'">'+fmtLap(s)+'</span>'
        + (dStr ? '<span class="ss-delta '+dCls+'"> '+dStr+'</span>' : '')
        + '</div>';
    }).join('');
    return '<div class="ss-row" style="grid-template-columns:'+colTpl+'">'
      + '<div style="color:#666">'+l.lap+'</div>'
      + cells
      + '<div style="color:#aaa;font-weight:600">'+fmtLap(l.total_s)+'</div>'
      + '</div>';
  }).join('');

  var bestRow = '<div class="ss-row" style="grid-template-columns:'+colTpl+';background:#140a24">'
    + '<div style="color:#a855f7;font-size:10px;font-weight:700">BEST</div>'
    + bestSpl.map(function(b) { return '<div class="ss-best">'+(b != null ? fmtLap(b) : '\u2014')+'</div>'; }).join('')
    + '<div class="ss-best">'+fmtLap(bestSpl.filter(function(b) { return b != null; }).reduce(function(a, b) { return a + b; }, 0))+'</div>'
    + '</div>';

  return '<div class="section-label">Sector split times</div>'
    + '<div class="sector-splits-table">'
    + '<div class="ss-row ss-header" style="grid-template-columns:'+colTpl+'">'
    + '<div>Lap</div>'+headerCells+'<div>Total</div>'
    + '</div>'
    + bestRow
    + rows
    + '</div>';
}

// ── Tyre Trend ────────────────────────────────────────────────────────────────
function renderTyreTrend(trend) {
  if (!trend || !trend.laps || trend.laps.length < 3) return '';
  var laps = trend.laps;
  var corners = ['LF','RF','LR','RR'];
  var cols    = {LF:'#64b5f6', RF:'#4caf50', LR:'#ff9800', RR:'#f44336'};
  var W = 600, PL = 44, PR = 12, PT = 10, PB = 24, H = 130;
  var IW = W - PL - PR, IH = H - PT - PB;

  var minT = Infinity, maxT = -Infinity;
  laps.forEach(function(l) { corners.forEach(function(c) {
    if (l[c] != null) { if (l[c] < minT) minT = l[c]; if (l[c] > maxT) maxT = l[c]; }
  }); });
  if (minT === Infinity) return '';
  var pad = 5;
  minT = Math.floor(minT - pad);
  maxT = Math.ceil(maxT + pad);
  var tRange = maxT - minT || 1;

  var lapNums = laps.map(function(l) { return l.lap; });
  var nLaps = lapNums.length;

  var svg = '<rect x="'+PL+'" y="'+PT+'" width="'+IW+'" height="'+IH+'" fill="#111" rx="3"/>';

  for (var g = 0; g <= 1; g += 0.5) {
    var gy = (PT + IH - g * IH).toFixed(1);
    var tv = Math.round(minT + g * tRange);
    svg += '<line x1="'+PL+'" y1="'+gy+'" x2="'+(W-PR)+'" y2="'+gy+'" stroke="#222" stroke-width="1"/>';
    svg += '<text x="'+(PL-4)+'" y="'+(parseFloat(gy)+4)+'" text-anchor="end" fill="#444" font-size="9">'+tv+'\u00b0F</text>';
  }

  svg += '<text x="'+PL+'" y="'+(H-4)+'" fill="#444" font-size="9">Lap '+lapNums[0]+'</text>';
  svg += '<text x="'+(W-PR)+'" y="'+(H-4)+'" text-anchor="end" fill="#444" font-size="9">Lap '+lapNums[nLaps-1]+'</text>';

  corners.forEach(function(c) {
    var pts = laps.map(function(l, i) {
      if (l[c] == null) return null;
      var x = PL + (i / Math.max(nLaps - 1, 1)) * IW;
      var y = PT + IH - ((l[c] - minT) / tRange) * IH;
      return x.toFixed(1)+','+y.toFixed(1);
    }).filter(Boolean);
    if (pts.length < 2) return;
    svg += '<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+cols[c]+'" stroke-width="1.8"/>';
  });

  var legend = corners.map(function(c) {
    return '<span class="tt-legend-item"><span class="tt-legend-dot" style="background:'+cols[c]+'"></span>'+c+'</span>';
  }).join('');

  return '<div class="section-label">Tyre temperature trend</div>'
    + '<div class="tyre-trend-wrap">'
    + '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;display:block">'+svg+'</svg>'
    + '<div class="tt-legend">'+legend+'</div>'
    + '</div>';
}

// ── Speed Trace ───────────────────────────────────────────────────────────────
function renderSpeedTrace(st) {
  if (!st || !st.laps || !st.laps.length) return '';
  var W = 600, H = 160, PL = 40, PR = 12, PT = 10, PB = 24;
  var IW = W - PL - PR, IH = H - PT - PB;
  var allSpds = [];
  st.laps.forEach(function(l) { l.points.forEach(function(p) { allSpds.push(p.spd); }); });
  var maxSpd = Math.max.apply(null, allSpds) || 1;
  var colors = ['#c084fc','#64b5f6','#81c784','#ffb74d','#f06292'];
  var svgLines = '';
  st.laps.filter(function(l) { return !l.is_best; }).forEach(function(l, li) {
    var pts = l.points.map(function(p) {
      return (PL + p.pct * IW).toFixed(1)+','+(PT + IH - (p.spd / maxSpd) * IH).toFixed(1);
    }).join(' ');
    svgLines += '<polyline points="'+pts+'" fill="none" stroke="'+colors[(li+1)%colors.length]+'" stroke-width="1" opacity="0.4"/>';
  });
  st.laps.filter(function(l) { return l.is_best; }).forEach(function(l) {
    var pts = l.points.map(function(p) {
      return (PL + p.pct * IW).toFixed(1)+','+(PT + IH - (p.spd / maxSpd) * IH).toFixed(1);
    }).join(' ');
    svgLines += '<polyline points="'+pts+'" fill="none" stroke="#c084fc" stroke-width="2"/>';
  });
  var gridLines = '';
  for (var g = 25; g <= 75; g += 25) {
    var y = (PT + IH - (g / 100) * IH).toFixed(1);
    var spd = Math.round(maxSpd * g / 100);
    gridLines += '<line x1="'+PL+'" y1="'+y+'" x2="'+(W - PR)+'" y2="'+y+'" stroke="#222" stroke-width="1"/>';
    gridLines += '<text x="'+(PL - 4)+'" y="'+(parseFloat(y)+4)+'" text-anchor="end" fill="#444" font-size="9">'+spd+'</text>';
  }
  gridLines += '<text x="'+PL+'" y="'+(H - 2)+'" fill="#444" font-size="9">0%</text>';
  gridLines += '<text x="'+(W - PR)+'" y="'+(H - 2)+'" text-anchor="end" fill="#444" font-size="9">100% lap</text>';
  var legendItems = st.laps.map(function(l, i) {
    var col = l.is_best ? '#c084fc' : colors[(i) % colors.length];
    var opacity = l.is_best ? '1' : '0.5';
    return '<span class="st-legend-item" style="opacity:'+opacity+'">'
      + '<span class="st-legend-dot" style="background:'+col+'"></span>'
      + 'Lap '+l.lap+' \u2014 '+fmtLap(l.time_s)+(l.is_best ? ' \u2b24' : '')
      + '</span>';
  }).join('');
  return '<div class="section-label">Speed trace \u2014 Top '+st.laps.length+' laps</div>'
    + '<div class="speed-trace-wrap">'
    + '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;display:block">'
    + '<rect x="'+PL+'" y="'+PT+'" width="'+IW+'" height="'+IH+'" fill="#111" rx="4"/>'
    + gridLines
    + svgLines
    + '</svg>'
    + '<div class="st-legend">'+legendItems+'</div>'
    + '</div>';
}

// ── Lap Delta Chart ──────────────────────────────────────────────────────────
function renderLapDelta(lapTimes) {
  if (!lapTimes || lapTimes.length < 2) return '';
  var best = Math.min.apply(null, lapTimes.map(function(l) { return l.time_s; }));
  var deltas = lapTimes.map(function(l) { return l.time_s - best; });
  var maxD = Math.max.apply(null, deltas.concat([0.001]));
  var W = 500, H = 80, PL = 36, PR = 8, PT = 8, PB = 18;
  var iW = W - PL - PR, iH = H - PT - PB;
  var n = lapTimes.length;
  var xScale = function(i) { return PL + (i / Math.max(n - 1, 1)) * iW; };
  var yScale = function(d) { return PT + iH - (d / maxD) * iH; };
  var svg = '<line x1="'+PL+'" y1="'+(PT+iH)+'" x2="'+(PL+iW)+'" y2="'+(PT+iH)+'" stroke="#333" stroke-width="1"/>';
  svg += '<text x="'+(PL-2)+'" y="'+(PT+4)+'" fill="#555" font-size="9" text-anchor="end">+'+maxD.toFixed(1)+'s</text>';
  svg += '<text x="'+(PL-2)+'" y="'+(PT+iH+4)+'" fill="#555" font-size="9" text-anchor="end">\u00b10</text>';
  for (var i = 1; i < n; i++) {
    var x1 = xScale(i-1), y1 = yScale(deltas[i-1]);
    var x2 = xScale(i),   y2 = yScale(deltas[i]);
    svg += '<line x1="'+x1.toFixed(1)+'" y1="'+y1.toFixed(1)+'" x2="'+x2.toFixed(1)+'" y2="'+y2.toFixed(1)+'" stroke="#444" stroke-width="1.5"/>';
  }
  lapTimes.forEach(function(l, i) {
    var d = deltas[i];
    var col = d < 0.001 ? '#4caf50' : d < 0.3 ? '#4caf50' : d < 1.5 ? '#ff9800' : '#f44336';
    var cx = xScale(i).toFixed(1), cy = yScale(d).toFixed(1);
    svg += '<circle cx="'+cx+'" cy="'+cy+'" r="4" fill="'+col+'" stroke="#111" stroke-width="1" data-lap-dot="'+l.lap+'"/>';
    svg += '<text x="'+cx+'" y="'+(PT+iH+12).toFixed(0)+'" fill="#555" font-size="9" text-anchor="middle">'+l.lap+'</text>';
  });
  return '<div class="section-label">Lap delta vs best</div>'
    + '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:'+H+'px;display:block;background:#111;border-radius:6px;margin-bottom:12px">'+svg+'</svg>';
}
