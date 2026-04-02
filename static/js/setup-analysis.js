// ── Telemetry cross-reference hints ──────────────────────────────────────────
function _crossRef(label, value, lastAnalysis, setupAnalysis) {
  var l   = label.toLowerCase();
  var val = parseFloat(value);

  if (setupAnalysis) {
    var sa = setupAnalysis;
    var rangeRec = (sa.recs || []).find(function(r) { return r.category === 'Range Limit' && r.text.toLowerCase().includes(label.toLowerCase().substring(0, 15)); });
    if (rangeRec) {
      return {type: rangeRec.priority === 'high' ? 'warn' : 'info', text: rangeRec.text + '. ' + rangeRec.action};
    }
  }

  if (!lastAnalysis) return null;
  var recs = (lastAnalysis.recommendations || []);
  var handling = lastAnalysis.handling || {};

  var tendencies = Object.values(handling).map(function(h) { return h.tendency; }).filter(Boolean);
  var usCount = tendencies.filter(function(t) { return t === 'understeer'; }).length;
  var osCount = tendencies.filter(function(t) { return t === 'oversteer'; }).length;
  var overallUS = usCount > osCount;
  var overallOS = osCount > usCount;

  var setupTendency = setupAnalysis ? setupAnalysis.tendencySummary : null;

  if (l.includes('front arb') || l.includes('front anti-roll') || (l.includes('arb') && l.includes('front'))) {
    if (overallUS) return {type:'warn', text:'Telemetry shows understeer \u2014 consider increasing front ARB to add front grip response, or decreasing rear ARB to free the rear.'};
    if (overallOS) return {type:'warn', text:'Telemetry shows oversteer \u2014 consider softening front ARB to reduce snap.'};
  }
  if (l.includes('rear arb') || l.includes('rear anti-roll') || l.includes('rarb')) {
    if (overallUS) return {type:'info', text:'Understeer detected \u2014 softening rear ARB can free up rear rotation and reduce understeer.'};
    if (overallOS) return {type:'warn', text:'Oversteer detected \u2014 stiffening rear ARB may add stability.'};
  }
  if ((l.includes('spring') || l.includes('spring rate')) && (l.includes('front') || l.includes('left front') || l.includes('right front'))) {
    if (overallUS) return {type:'info', text:'Front understeer \u2014 softening front spring rate can increase front mechanical grip.'};
  }
  if ((l.includes('spring') || l.includes('spring rate')) && (l.includes('rear') || l.includes('left rear') || l.includes('right rear'))) {
    if (overallOS) return {type:'info', text:'Oversteer \u2014 stiffening rear spring rate can add rear stability.'};
  }
  var cornerMap = {'lf':'LF','rf':'RF','lr':'LR','rr':'RR','left front':'LF','right front':'RF','left rear':'LR','right rear':'RR'};
  for (var kw in cornerMap) {
    var corner = cornerMap[kw];
    if (l.includes(kw) && (l.includes('pressure') || l.includes('psi'))) {
      var pRec = recs.find(function(r) { return r.category === 'Tyre Pressure' && r.corner === corner; });
      if (pRec) return {type:'warn', text:'Telemetry: '+pRec.issue+'. Suggested action: '+pRec.action+'.'};
      var pressures = lastAnalysis.tyre_pressures || {};
      if (pressures[corner] != null) {
        return {type:'good', text:'Hot pressure measured at '+pressures[corner].toFixed(1)+' psi at '+corner+'.'};
      }
    }
  }
  for (var kw in cornerMap) {
    var corner = cornerMap[kw];
    if (l.includes(kw) && l.includes('camber')) {
      var cRec = recs.find(function(r) { return r.category === 'Camber' && r.corner === corner; });
      if (cRec) return {type:'warn', text:'Telemetry: '+cRec.issue+'. '+cRec.action+'.'};
    }
  }
  if (l.includes('brake bias') || l.includes('brake balance') || l.includes('brake pressure bias')) {
    var brk = lastAnalysis.balance;
    if (brk && brk.brake_balance_pct != null) {
      var extra = '';
      if (setupAnalysis && setupAnalysis.brakeBias != null) {
        var diff = setupAnalysis.brakeBias - brk.brake_balance_pct;
        if (Math.abs(diff) > 0.5) {
          extra = ' Setup bias '+setupAnalysis.brakeBias+'% vs telemetry avg '+brk.brake_balance_pct.toFixed(1)+'% \u2014 driver adjusted by '+(diff > 0 ? '+' : '')+diff.toFixed(1)+'% during session.';
        }
      }
      return {type:'info', text:'Session average brake balance: '+brk.brake_balance_pct.toFixed(1)+'%.'+extra};
    }
  }
  if (l.includes('wing') || l.includes('downforce') || l.includes('aero')) {
    if (overallUS) return {type:'info', text:'Understeer present \u2014 adding front aero (if available) can help balance.'};
  }
  if (setupTendency && setupTendency !== 'balanced') {
    if (l.includes('spring') || l.includes('arb') || l.includes('damper')) {
      if (setupTendency === 'understeer' && overallUS) {
        return {type:'warn', text:'Both setup geometry and telemetry suggest understeer. This parameter contributes to the imbalance.'};
      }
      if (setupTendency === 'oversteer' && overallOS) {
        return {type:'warn', text:'Both setup geometry and telemetry suggest oversteer. This parameter contributes to the imbalance.'};
      }
    }
  }
  return null;
}

// ── Setup Recommendation Engine ──────────────────────────────────────────────
function analyzeSetup(tabs, carConfig) {
  var recs = [];
  var params = {};

  Object.values(tabs).forEach(function(sections) {
    Object.entries(sections).forEach(function(entry) {
      var sect = entry[0], pList = entry[1];
      pList.forEach(function(p) {
        var key = (sect + ' ' + p.label).toLowerCase();
        params[key] = p;
        params[p.label.toLowerCase()] = p;
      });
    });
  });

  function numVal(p) {
    if (!p || !p.value) return null;
    var m = p.value.match(/-?[\d.]+/);
    return m ? parseFloat(m[0]) : null;
  }

  function rangeVal(str) {
    if (!str) return null;
    var m = str.match(/-?[\d.]+/);
    return m ? parseFloat(m[0]) : null;
  }

  function findParam(keywords) {
    var kws = keywords.map(function(k) { return k.toLowerCase(); });
    for (var key in params) {
      if (kws.every(function(kw) { return key.includes(kw); })) return params[key];
    }
    return null;
  }

  var corners = {
    LF: {label:'Left Front'}, RF: {label:'Right Front'},
    LR: {label:'Left Rear'}, RR: {label:'Right Rear'}
  };
  var cornerKeys = {LF:'left front', RF:'right front', LR:'left rear', RR:'right rear'};

  Object.entries(cornerKeys).forEach(function(entry) {
    var corner = entry[0], searchKey = entry[1];
    corners[corner].pressure = numVal(findParam([searchKey, 'pressure']));
    corners[corner].spring = numVal(findParam([searchKey, 'spring']));
    corners[corner].rideHeight = numVal(findParam([searchKey, 'ride height']));
    corners[corner].bumpRubber = numVal(findParam([searchKey, 'bump rubber']));
    corners[corner].camber = numVal(findParam([searchKey, 'camber']));
  });

  var frontARB = numVal(findParam(['arb setting']));
  var rearARB = numVal(findParam(['rarb setting']));
  var wing = numVal(findParam(['wing setting']));
  var frontRHSpeed = numVal(findParam(['front rh at speed']));
  var rearRHSpeed = numVal(findParam(['rear rh at speed']));
  var brakeBias = numVal(findParam(['brake pressure bias'])) || numVal(findParam(['brake bias']));
  var frontMC = numVal(findParam(['front master']));
  var rearMC = numVal(findParam(['rear master']));
  var brakePads = (findParam(['brake pads']) || {}).value || '';

  var dampers = {};
  ['Low Speed Compression', 'High Speed Compression', 'Low Speed Rebound', 'High Speed Rebound'].forEach(function(mode) {
    var frontP = findParam(['front dampers', mode.toLowerCase()]);
    var rearP = findParam(['rear dampers', mode.toLowerCase()]);
    dampers[mode] = {
      front: numVal(frontP),
      rear: numVal(rearP),
      frontRange: frontP ? {min: rangeVal(frontP.range_min), max: rangeVal(frontP.range_max)} : null,
      rearRange: rearP ? {min: rangeVal(rearP.range_min), max: rangeVal(rearP.range_max)} : null
    };
  });

  // ── 1. Range validation ────────────────────────────────────────────────────
  Object.values(tabs).forEach(function(sections) {
    Object.values(sections).forEach(function(pList) {
      pList.forEach(function(p) {
        if (!p.range_min || !p.range_max) return;
        var v = numVal(p);
        var mn = rangeVal(p.range_min);
        var mx = rangeVal(p.range_max);
        if (v == null || mn == null || mx == null) return;
        var range = mx - mn;
        if (range <= 0) return;
        if (v <= mn) {
          recs.push({category:'Range Limit', priority:'medium', text:p.label+' is at minimum ('+p.value+')', action:'Consider increasing \u2014 currently at the lowest possible setting.'});
        } else if (v >= mx) {
          recs.push({category:'Range Limit', priority:'medium', text:p.label+' is at maximum ('+p.value+')', action:'Consider decreasing \u2014 currently at the highest possible setting. You may be compensating for another issue.'});
        } else if ((v - mn) / range < 0.05) {
          recs.push({category:'Range Limit', priority:'low', text:p.label+' is near minimum ('+p.value+', range '+p.range_min+'\u2013'+p.range_max+')', action:'Very close to the limit \u2014 check if this is intentional.'});
        } else if ((mx - v) / range < 0.05) {
          recs.push({category:'Range Limit', priority:'low', text:p.label+' is near maximum ('+p.value+', range '+p.range_min+'\u2013'+p.range_max+')', action:'Very close to the limit \u2014 check if this is intentional.'});
        }
      });
    });
  });

  // ── 2. Cross-corner balance ────────────────────────────────────────────────
  if (corners.LF.spring != null && corners.RF.spring != null && corners.LF.spring !== corners.RF.spring) {
    var diff = Math.abs(corners.LF.spring - corners.RF.spring);
    recs.push({category:'Cross-Corner Balance', priority: diff > 20 ? 'high' : 'medium',
      text:'Front spring asymmetry: LF '+corners.LF.spring+' vs RF '+corners.RF.spring+' N/mm (\u0394'+diff+')',
      action:'Asymmetric front springs are unusual unless compensating for a specific track characteristic. Verify this is intentional.'});
  }
  if (corners.LR.spring != null && corners.RR.spring != null && corners.LR.spring !== corners.RR.spring) {
    var diff = Math.abs(corners.LR.spring - corners.RR.spring);
    recs.push({category:'Cross-Corner Balance', priority: diff > 20 ? 'high' : 'medium',
      text:'Rear spring asymmetry: LR '+corners.LR.spring+' vs RR '+corners.RR.spring+' N/mm (\u0394'+diff+')',
      action:'Asymmetric rear springs create an uneven platform \u2014 verify this is intentional.'});
  }

  if (corners.LF.rideHeight != null && corners.RF.rideHeight != null) {
    var diff = Math.abs(corners.LF.rideHeight - corners.RF.rideHeight);
    if (diff > 0.001) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Front ride height asymmetry: LF '+(corners.LF.rideHeight*1000).toFixed(1)+' vs RF '+(corners.RF.rideHeight*1000).toFixed(1)+' mm',
        action:'Asymmetric ride heights affect aero balance. Usually indicates a track with more load on one side.'});
    }
  }
  if (corners.LR.rideHeight != null && corners.RR.rideHeight != null) {
    var diff = Math.abs(corners.LR.rideHeight - corners.RR.rideHeight);
    if (diff > 0.001) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Rear ride height asymmetry: LR '+(corners.LR.rideHeight*1000).toFixed(1)+' vs RR '+(corners.RR.rideHeight*1000).toFixed(1)+' mm',
        action:'Asymmetric rear ride heights affect mechanical grip balance. May be intentional for oval-style setups.'});
    }
  }

  if (corners.LF.camber != null && corners.RF.camber != null) {
    var diff = Math.abs(corners.LF.camber + corners.RF.camber);
    if (diff > 0.2) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Front camber asymmetry: LF '+corners.LF.camber+'\u00b0 vs RF '+corners.RF.camber+'\u00b0 (should mirror)',
        action:'On road courses, front camber is typically symmetric (LF \u2248 -RF). A difference suggests intentional oval compensation or a possible error.'});
    }
  }
  if (corners.LR.camber != null && corners.RR.camber != null) {
    var diff = Math.abs(corners.LR.camber + corners.RR.camber);
    if (diff > 0.2) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Rear camber asymmetry: LR '+corners.LR.camber+'\u00b0 vs RR '+corners.RR.camber+'\u00b0 (should mirror)',
        action:'Symmetric rear camber is standard for road courses. Review if this asymmetry is intentional.'});
    }
  }

  if (corners.LF.pressure != null && corners.RF.pressure != null) {
    var diff = Math.abs(corners.LF.pressure - corners.RF.pressure);
    if (diff > 3) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Front pressure asymmetry: LF '+corners.LF.pressure+' vs RF '+corners.RF.pressure+' kPa',
        action:'Starting pressures are usually equal across an axle. Split pressures are uncommon unless compensating for asymmetric track load.'});
    }
  }
  if (corners.LR.pressure != null && corners.RR.pressure != null) {
    var diff = Math.abs(corners.LR.pressure - corners.RR.pressure);
    if (diff > 3) {
      recs.push({category:'Cross-Corner Balance', priority:'medium',
        text:'Rear pressure asymmetry: LR '+corners.LR.pressure+' vs RR '+corners.RR.pressure+' kPa',
        action:'Starting pressures are usually equal across an axle. Verify this split is intentional.'});
    }
  }

  // ── 3. Front-to-rear balance analysis ──────────────────────────────────────
  var frontSpring = (corners.LF.spring != null && corners.RF.spring != null) ? (corners.LF.spring + corners.RF.spring) / 2 : null;
  var rearSpring = (corners.LR.spring != null && corners.RR.spring != null) ? (corners.LR.spring + corners.RR.spring) / 2 : null;

  if (frontSpring != null && rearSpring != null) {
    var ratio = frontSpring / rearSpring;
    if (ratio > 1.15) {
      recs.push({category:'Front/Rear Balance', priority:'medium',
        text:'Front springs significantly stiffer than rear ('+frontSpring+' vs '+rearSpring+' N/mm, ratio '+ratio.toFixed(2)+')',
        action:'A stiff front relative to rear tends toward understeer. Soften front springs or stiffen rear for more neutral balance.'});
    } else if (ratio < 0.85) {
      recs.push({category:'Front/Rear Balance', priority:'medium',
        text:'Rear springs significantly stiffer than front ('+rearSpring+' vs '+frontSpring+' N/mm, ratio '+(1/ratio).toFixed(2)+')',
        action:'A stiff rear relative to front tends toward oversteer. Stiffen front or soften rear for more stability.'});
    }
  }

  Object.entries(dampers).forEach(function(entry) {
    var mode = entry[0], d = entry[1];
    if (d.front != null && d.rear != null && d.front !== d.rear) {
      var diff = d.front - d.rear;
      if (Math.abs(diff) > 4) {
        var stiffer = diff > 0 ? 'Front' : 'Rear';
        var tendency = mode.includes('Compression')
          ? (diff > 0 ? 'understeer on bump' : 'oversteer on bump')
          : (diff > 0 ? 'understeer on rebound' : 'oversteer on rebound');
        recs.push({category:'Front/Rear Balance', priority:'low',
          text:mode+': front '+d.front+' vs rear '+d.rear+' clicks \u2014 '+stiffer+' is stiffer',
          action:'Large damper split contributes to '+tendency+'. '+(mode.includes('Compression') ? 'Compression affects weight transfer rate into corners.' : 'Rebound affects weight transfer rate out of corners.')});
      }
    }
  });

  // ── Damper sweet spot analysis ──────────────────────────────────────────────
  if (carConfig && carConfig.damper_sweet_spot) {
    var sweetSpot = carConfig.damper_sweet_spot;
    var damperKeyMap = {
      'Low Speed Compression': 'ls_compression',
      'High Speed Compression': 'hs_compression',
      'Low Speed Rebound': 'ls_rebound',
      'High Speed Rebound': 'hs_rebound'
    };
    Object.entries(dampers).forEach(function(entry) {
      var mode = entry[0], d = entry[1];
      var ssKey = damperKeyMap[mode];
      if (!ssKey) return;
      ['front', 'rear'].forEach(function(end) {
        var val = d[end];
        if (val == null) return;
        var ss = sweetSpot[end] && sweetSpot[end][ssKey];
        if (!ss) return;
        var endLabel = end.charAt(0).toUpperCase() + end.slice(1);
        if (val >= ss.min && val <= ss.max) {
          if (val === ss.min || val === ss.max) {
            recs.push({category:'Damper Sweet Spot', priority:'low',
              text:endLabel+' '+mode+' ('+val+' clicks) is at the edge of the sweet spot ('+ss.min+'\u2013'+ss.max+')',
              action:'Currently at the boundary \u2014 consider moving 1\u20132 clicks toward the center of the range for more consistent platform control.'});
          }
        } else {
          var diff = val < ss.min ? ss.min - val : val - ss.max;
          var direction = val < ss.min ? 'low' : 'high';
          var priority = diff > 3 ? 'medium' : 'low';
          var actionText = direction === 'low'
            ? 'Consider increasing by '+diff+' click'+(diff>1?'s':'')+' for more damping force and better platform control.'
            : 'Consider decreasing by '+diff+' click'+(diff>1?'s':'')+' to avoid over-damping and improve mechanical grip.';
          recs.push({category:'Damper Sweet Spot', priority:priority,
            text:endLabel+' '+mode+' ('+val+' clicks) is '+diff+' click'+(diff>1?'s':'')+' '+direction+' of the sweet spot ('+ss.min+'\u2013'+ss.max+')',
            action:actionText});
        }
      });
    });
  }

  if (frontARB != null && rearARB != null) {
    if (frontARB > rearARB + 2) {
      recs.push({category:'Front/Rear Balance', priority:'low',
        text:'Front ARB stiffer than rear ('+frontARB+' vs '+rearARB+')',
        action:'Stiffer front ARB reduces front grip in corners \u2192 tends toward understeer. Consider softening front or stiffening rear ARB.'});
    } else if (rearARB > frontARB + 2) {
      recs.push({category:'Front/Rear Balance', priority:'low',
        text:'Rear ARB stiffer than front ('+rearARB+' vs '+frontARB+')',
        action:'Stiffer rear ARB reduces rear grip in corners \u2192 tends toward oversteer. Consider softening rear or stiffening front ARB.'});
    }
  }

  // ── 4. Contextual recommendations (car config aware) ───────────────────────
  if (carConfig) {
    var targetPsi = carConfig.target_hot_psi;
    if (targetPsi) {
      Object.entries(cornerKeys).forEach(function(entry) {
        var corner = entry[0], searchKey = entry[1];
        var pKpa = corners[corner].pressure;
        if (pKpa == null) return;
        var pPsi = pKpa * 0.14503773773;
        var target = targetPsi[corner];
        if (!target) return;
      });
    }
  }

  if (wing != null) {
    var wingParam = findParam(['wing setting']);
    if (wingParam && wingParam.range_min && wingParam.range_max) {
      var wMin = rangeVal(wingParam.range_min);
      var wMax = rangeVal(wingParam.range_max);
      if (wMin != null && wMax != null) {
        var wRange = wMax - wMin;
        var wPct = (wing - wMin) / wRange;
        if (wPct > 0.8) {
          recs.push({category:'Aero', priority:'low',
            text:'Wing at '+wing+'\u00b0 \u2014 high downforce end ('+(wPct*100).toFixed(0)+'% of range)',
            action:'Good for technical tracks with lots of slow-medium corners. May sacrifice top speed on long straights.'});
        } else if (wPct < 0.2) {
          recs.push({category:'Aero', priority:'low',
            text:'Wing at '+wing+'\u00b0 \u2014 low downforce end ('+(wPct*100).toFixed(0)+'% of range)',
            action:'Good for high-speed tracks. May lack rear grip in slow corners \u2014 compensate with mechanical grip (springs, dampers).'});
        }
      }
    }
  }

  Object.entries(cornerKeys).forEach(function(entry) {
    var corner = entry[0], searchKey = entry[1];
    var gap = corners[corner].bumpRubber;
    if (gap != null && gap < 0.02) {
      recs.push({category:'Suspension', priority:'medium',
        text:corners[corner].label+' bump rubber gap very small ('+(gap*1000).toFixed(0)+' mm)',
        action:'A small gap means the car will frequently contact bump stops, creating a harsh, non-linear suspension response. Consider raising ride height or stiffening springs.'});
    }
  });

  var tendencySummary = 'balanced';
  var usScore = 0, osScore = 0;
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
    recs: recs,
    corners: corners,
    dampers: dampers,
    frontARB: frontARB, rearARB: rearARB,
    wing: wing, frontRHSpeed: frontRHSpeed, rearRHSpeed: rearRHSpeed,
    brakeBias: brakeBias, frontMC: frontMC, rearMC: rearMC, brakePads: brakePads,
    frontSpring: frontSpring, rearSpring: rearSpring,
    tendencySummary: tendencySummary,
    carConfig: carConfig || null
  };
}

// ── SVG: Car outline with tire pressures, camber, ride heights ──────────────
function renderCarOutlineSVG(analysis) {
  var c = analysis.corners;
  var W = 340, H = 440;
  var bodyX = 110, bodyY = 60, bodyW = 120, bodyH = 320;
  var wheelW = 28, wheelH = 56;

  var svg = '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;max-width:340px;height:auto;display:block;margin:0 auto">';
  svg += '<rect width="'+W+'" height="'+H+'" fill="#111" rx="8"/>';
  svg += '<rect x="'+bodyX+'" y="'+bodyY+'" width="'+bodyW+'" height="'+bodyH+'" rx="20" ry="20" fill="#1a1a1a" stroke="#333" stroke-width="1.5"/>';
  svg += '<path d="M'+(bodyX+20)+' '+(bodyY+40)+' L'+(bodyX+bodyW-20)+' '+(bodyY+40)+' L'+(bodyX+bodyW-30)+' '+(bodyY+80)+' L'+(bodyX+30)+' '+(bodyY+80)+' Z" fill="#222" stroke="#333" stroke-width="1"/>';
  svg += '<path d="M'+(bodyX+25)+' '+(bodyY+bodyH-80)+' L'+(bodyX+bodyW-25)+' '+(bodyY+bodyH-80)+' L'+(bodyX+bodyW-20)+' '+(bodyY+bodyH-45)+' L'+(bodyX+20)+' '+(bodyY+bodyH-45)+' Z" fill="#222" stroke="#333" stroke-width="1"/>';
  svg += '<line x1="'+(W/2)+'" y1="'+(bodyY+10)+'" x2="'+(W/2)+'" y2="'+(bodyY+bodyH-10)+'" stroke="#2a2a2a" stroke-width="1" stroke-dasharray="4,4"/>';

  var positions = {
    LF: {wx: bodyX - wheelW - 4, wy: bodyY + 30, tx: 6, ty: bodyY + 20},
    RF: {wx: bodyX + bodyW + 4, wy: bodyY + 30, tx: bodyX + bodyW + wheelW + 12, ty: bodyY + 20},
    LR: {wx: bodyX - wheelW - 4, wy: bodyY + bodyH - 30 - wheelH, tx: 6, ty: bodyY + bodyH - 96},
    RR: {wx: bodyX + bodyW + 4, wy: bodyY + bodyH - 30 - wheelH, tx: bodyX + bodyW + wheelW + 12, ty: bodyY + bodyH - 96}
  };

  Object.entries(positions).forEach(function(entry) {
    var corner = entry[0], pos = entry[1];
    var cd = c[corner];
    var pKpa = cd.pressure;
    var pPsi = pKpa != null ? (pKpa * 0.14503773773).toFixed(1) : '\u2014';
    var camber = cd.camber != null ? cd.camber.toFixed(1) + '\u00b0' : '\u2014';
    var rh = cd.rideHeight != null ? (cd.rideHeight * 1000).toFixed(1) : '\u2014';

    var wheelColor = pKpa != null ? '#2a2a2a' : '#1e1e1e';
    svg += '<rect x="'+pos.wx+'" y="'+pos.wy+'" width="'+wheelW+'" height="'+wheelH+'" rx="4" fill="'+wheelColor+'" stroke="#444" stroke-width="1.5"/>';
    for (var i = 0; i < 4; i++) {
      var ly = pos.wy + 10 + i * 12;
      svg += '<line x1="'+(pos.wx+4)+'" y1="'+ly+'" x2="'+(pos.wx+wheelW-4)+'" y2="'+ly+'" stroke="#555" stroke-width="1" opacity="0.5"/>';
    }

    var isLeft = corner.startsWith('L');
    var anchor = isLeft ? 'end' : 'start';
    var labelX = isLeft ? pos.tx + 90 : pos.tx;

    svg += '<text x="'+labelX+'" y="'+pos.ty+'" fill="#666" font-size="10" font-weight="700" text-anchor="'+anchor+'" letter-spacing=".5">'+corner+'</text>';
    svg += '<text x="'+labelX+'" y="'+(pos.ty + 16)+'" fill="#ccc" font-size="14" font-weight="700" text-anchor="'+anchor+'">'+pPsi+'<tspan fill="#555" font-size="10"> psi</tspan></text>';
    svg += '<text x="'+labelX+'" y="'+(pos.ty + 32)+'" fill="#aaa" font-size="11" text-anchor="'+anchor+'">\u27e8 '+camber+'</text>';
    svg += '<text x="'+labelX+'" y="'+(pos.ty + 46)+'" fill="#888" font-size="11" text-anchor="'+anchor+'">\u2195 '+rh+'<tspan fill="#555" font-size="9"> mm</tspan></text>';
  });

  svg += '<text x="'+(W/2)+'" y="'+(bodyY - 8)+'" fill="#444" font-size="10" text-anchor="middle" font-weight="700">FRONT</text>';
  svg += '<text x="'+(W/2)+'" y="'+(bodyY + bodyH + 16)+'" fill="#444" font-size="10" text-anchor="middle" font-weight="700">REAR</text>';
  svg += '<path d="M'+(W/2)+' '+(bodyY+12)+' L'+(W/2-6)+' '+(bodyY+22)+' L'+(W/2+6)+' '+(bodyY+22)+' Z" fill="#2196F3" opacity="0.6"/>';

  svg += '</svg>';
  return svg;
}

// ── SVG: Suspension & damper bar charts ──────────────────────────────────────
function renderSuspensionBars(analysis) {
  var corners = analysis.corners, dampers = analysis.dampers;
  var frontARB = analysis.frontARB, rearARB = analysis.rearARB;
  var frontSpring = analysis.frontSpring, rearSpring = analysis.rearSpring;
  var html = '';

  if (frontSpring != null || rearSpring != null) {
    var maxSpring = Math.max(frontSpring || 0, rearSpring || 0, 1);
    html += '<h4>Spring Rates</h4>';
    html += '<div class="bar-pair">';
    html += '<div><div class="bar-pair-label">Front avg</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+((frontSpring/maxSpring*100).toFixed(0))+'%;background:#2196F3"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#64b5f6">'+(frontSpring != null ? frontSpring + ' N/mm' : '\u2014')+'</div></div>';
    html += '<div><div class="bar-pair-label">Rear avg</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+((rearSpring/maxSpring*100).toFixed(0))+'%;background:#f44336"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#ef9a9a">'+(rearSpring != null ? rearSpring + ' N/mm' : '\u2014')+'</div></div>';
    html += '</div>';
    if (frontSpring && rearSpring) {
      var ratio = (frontSpring / rearSpring).toFixed(2);
      var cls = ratio > 1.05 ? 'front-bias' : ratio < 0.95 ? 'rear-bias' : 'balanced';
      html += '<div style="text-align:center;margin-bottom:12px"><span style="font-size:11px;color:#555">F/R Ratio</span> <span class="ratio-badge '+cls+'">'+ratio+'</span></div>';
    }
  }

  if (frontARB != null || rearARB != null) {
    var maxARB = Math.max(frontARB || 0, rearARB || 0, 1);
    html += '<h4>Anti-Roll Bars</h4>';
    html += '<div class="bar-pair">';
    html += '<div><div class="bar-pair-label">Front</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(((frontARB||0)/maxARB*100).toFixed(0))+'%;background:#2196F3"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#64b5f6">'+(frontARB != null ? frontARB : '\u2014')+'</div></div>';
    html += '<div><div class="bar-pair-label">Rear</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(((rearARB||0)/maxARB*100).toFixed(0))+'%;background:#f44336"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#ef9a9a">'+(rearARB != null ? rearARB : '\u2014')+'</div></div>';
    html += '</div>';
  }

  var frontBR = (corners.LF.bumpRubber != null && corners.RF.bumpRubber != null) ? (corners.LF.bumpRubber + corners.RF.bumpRubber) / 2 : null;
  var rearBR = (corners.LR.bumpRubber != null && corners.RR.bumpRubber != null) ? (corners.LR.bumpRubber + corners.RR.bumpRubber) / 2 : null;
  if (frontBR != null || rearBR != null) {
    var maxBR = Math.max(frontBR || 0, rearBR || 0, 0.001);
    html += '<h4>Bump Rubber Gap</h4>';
    html += '<div class="bar-pair">';
    html += '<div><div class="bar-pair-label">Front avg</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(((frontBR||0)/maxBR*100).toFixed(0))+'%;background:#2196F3"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#64b5f6">'+(frontBR != null ? (frontBR*1000).toFixed(0) + ' mm' : '\u2014')+'</div></div>';
    html += '<div><div class="bar-pair-label">Rear avg</div>'
      + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(((rearBR||0)/maxBR*100).toFixed(0))+'%;background:#f44336"></div></div></div>'
      + '<div class="bar-pair-value" style="color:#ef9a9a">'+(rearBR != null ? (rearBR*1000).toFixed(0) + ' mm' : '\u2014')+'</div></div>';
    html += '</div>';
  }

  var damperModes = Object.entries(dampers);
  var hasDampers = damperModes.some(function(entry) { return entry[1].front != null || entry[1].rear != null; });
  if (hasDampers) {
    var sweetSpot = (analysis.carConfig && analysis.carConfig.damper_sweet_spot) || null;
    var damperKeyMap = {
      'Low Speed Compression': 'ls_compression',
      'High Speed Compression': 'hs_compression',
      'Low Speed Rebound': 'ls_rebound',
      'High Speed Rebound': 'hs_rebound'
    };

    html += '<h4>Damper Settings</h4>';
    if (sweetSpot) {
      html += '<div class="damper-legend">'
        + '<span class="damper-legend-item"><span class="damper-legend-swatch" style="background:#4caf50"></span> In sweet spot</span>'
        + '<span class="damper-legend-item"><span class="damper-legend-swatch" style="background:#ff9800"></span> Within 1 click</span>'
        + '<span class="damper-legend-item"><span class="damper-legend-swatch" style="background:#f44336"></span> Outside range</span>'
        + '<span class="damper-legend-item"><span class="damper-legend-swatch damper-legend-band"></span> Sweet spot range</span>'
        + '</div>';
    }
    damperModes.forEach(function(entry) {
      var mode = entry[0], d = entry[1];
      if (d.front == null && d.rear == null) return;
      var ssKey = damperKeyMap[mode];
      var frontSS = sweetSpot && ssKey && sweetSpot.front ? sweetSpot.front[ssKey] : null;
      var rearSS = sweetSpot && ssKey && sweetSpot.rear ? sweetSpot.rear[ssKey] : null;

      var rangeMax = 1;
      if (d.frontRange && d.frontRange.max != null) rangeMax = Math.max(rangeMax, d.frontRange.max);
      if (d.rearRange && d.rearRange.max != null) rangeMax = Math.max(rangeMax, d.rearRange.max);
      if (frontSS) rangeMax = Math.max(rangeMax, frontSS.max);
      if (rearSS) rangeMax = Math.max(rangeMax, rearSS.max);
      rangeMax = Math.max(rangeMax, d.front || 0, d.rear || 0);

      var short = mode.replace('Speed ', '').replace('damping', '').trim();
      html += '<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin:6px 0 4px">'+short+'</div>';
      html += '<div class="bar-pair">';

      ['front', 'rear'].forEach(function(end) {
        var val = d[end];
        var ss = end === 'front' ? frontSS : rearSS;
        var endLabel = end.charAt(0).toUpperCase() + end.slice(1);
        var baseColor = end === 'front' ? '#2196F3' : '#f44336';
        var baseTextColor = end === 'front' ? '#64b5f6' : '#ef9a9a';

        var barColor = baseColor;
        var statusText = '';
        if (val != null && ss) {
          if (val >= ss.min && val <= ss.max) {
            barColor = '#4caf50';
            statusText = '<span class="damper-status damper-status-good">In sweet spot</span>';
          } else if (val >= ss.min - 1 && val <= ss.max + 1) {
            barColor = '#ff9800';
            var offBy = val < ss.min ? 'low' : 'high';
            statusText = '<span class="damper-status damper-status-warn">1 click '+offBy+'</span>';
          } else {
            barColor = '#f44336';
            var diff = val < ss.min ? ss.min - val : val - ss.max;
            var offBy = val < ss.min ? 'low' : 'high';
            statusText = '<span class="damper-status damper-status-bad">'+diff+' clicks '+offBy+'</span>';
          }
        }

        var barPct = val != null ? ((val / rangeMax) * 100).toFixed(0) : '0';
        var ssBandHtml = '';
        if (ss) {
          var ssLeft = ((ss.min / rangeMax) * 100).toFixed(1);
          var ssWidth = (((ss.max - ss.min) / rangeMax) * 100).toFixed(1);
          ssBandHtml = '<div class="damper-sweet-band" style="left:'+ssLeft+'%;width:'+ssWidth+'%"></div>';
        }

        html += '<div><div class="bar-pair-label">'+endLabel+'</div>'
          + '<div class="bar-chart-row"><div class="bar-wrap">'
          + ssBandHtml
          + '<div class="bar-fill" style="width:'+barPct+'%;background:'+barColor+'"></div>'
          + '</div></div>'
          + '<div class="bar-pair-value" style="color:'+baseTextColor+'">'+(val != null ? val + ' cl' : '\u2014')+'</div>'
          + statusText
          + '</div>';
      });
      html += '</div>';
    });
  }

  return html;
}

// ── SVG: Aero balance diagram ────────────────────────────────────────────────
function renderAeroDiagram(analysis) {
  var wing = analysis.wing, frontRHSpeed = analysis.frontRHSpeed, rearRHSpeed = analysis.rearRHSpeed;
  if (wing == null && frontRHSpeed == null && rearRHSpeed == null) return '';

  var html = '';
  var W = 300, H = 160;
  var svg = '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;max-width:300px;height:auto;display:block;margin:0 auto">';
  svg += '<rect width="'+W+'" height="'+H+'" fill="#111" rx="6"/>';

  if (wing != null) {
    var cx = W/2, cy = 36;
    var angleRad = wing * Math.PI / 180;
    svg += '<text x="'+cx+'" y="16" fill="#555" font-size="9" text-anchor="middle" font-weight="700">WING ANGLE</text>';
    svg += '<line x1="'+(cx - 50)+'" y1="'+cy+'" x2="'+(cx + 50)+'" y2="'+cy+'" stroke="#2a2a2a" stroke-width="1"/>';
    svg += '<line x1="'+(cx - 30)+'" y1="'+cy+'" x2="'+(cx + 30)+'" y2="'+(cy - Math.tan(angleRad)*30)+'" stroke="#2196F3" stroke-width="3" stroke-linecap="round"/>';
    svg += '<text x="'+cx+'" y="'+(cy + 16)+'" fill="#64b5f6" font-size="14" font-weight="700" text-anchor="middle">'+wing+'\u00b0</text>';
  }

  if (frontRHSpeed != null && rearRHSpeed != null) {
    var baseY = H - 20;
    var groundY = baseY - 8;
    var maxRH = Math.max(frontRHSpeed, rearRHSpeed, 1);
    var scale = 40 / maxRH;
    var fH = frontRHSpeed * scale;
    var rH = rearRHSpeed * scale;

    svg += '<line x1="40" y1="'+groundY+'" x2="'+(W-40)+'" y2="'+groundY+'" stroke="#333" stroke-width="1"/>';
    svg += '<text x="40" y="'+(groundY + 12)+'" fill="#444" font-size="8" text-anchor="start">GROUND</text>';

    svg += '<rect x="60" y="'+(groundY - fH)+'" width="30" height="'+fH+'" fill="#2196F3" opacity="0.4" rx="2"/>';
    svg += '<text x="75" y="'+(groundY - fH - 6)+'" fill="#64b5f6" font-size="11" font-weight="700" text-anchor="middle">'+frontRHSpeed+' mm</text>';
    svg += '<text x="75" y="'+(groundY - fH - 18)+'" fill="#555" font-size="9" text-anchor="middle">FRONT</text>';

    svg += '<rect x="'+(W-90)+'" y="'+(groundY - rH)+'" width="30" height="'+rH+'" fill="#f44336" opacity="0.4" rx="2"/>';
    svg += '<text x="'+(W-75)+'" y="'+(groundY - rH - 6)+'" fill="#ef9a9a" font-size="11" font-weight="700" text-anchor="middle">'+rearRHSpeed+' mm</text>';
    svg += '<text x="'+(W-75)+'" y="'+(groundY - rH - 18)+'" fill="#555" font-size="9" text-anchor="middle">REAR</text>';

    svg += '<line x1="75" y1="'+(groundY - fH)+'" x2="'+(W-75)+'" y2="'+(groundY - rH)+'" stroke="#ff9800" stroke-width="1.5" stroke-dasharray="4,3"/>';
    var rake = rearRHSpeed - frontRHSpeed;
    svg += '<text x="'+(W/2)+'" y="'+(Math.min(groundY - fH, groundY - rH) - 6)+'" fill="#ff9800" font-size="10" font-weight="700" text-anchor="middle">Rake: '+(rake > 0 ? '+' : '')+rake+' mm</text>';
  }

  svg += '</svg>';
  html += svg;

  if (frontRHSpeed != null && rearRHSpeed != null) {
    var totalRH = frontRHSpeed + rearRHSpeed;
    var frontPct = totalRH > 0 ? (rearRHSpeed / totalRH * 100) : 50;
    html += '<div style="margin-top:10px;text-align:center">';
    html += '<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px">Aero Balance Estimate</div>';
    html += '<div class="aero-balance-indicator" style="justify-content:center">'
      + '<span style="font-size:10px;color:#64b5f6;width:50px;text-align:right">Front</span>'
      + '<div style="flex:1;max-width:200px;height:8px;background:#1e1e1e;border-radius:4px;overflow:hidden;display:flex">'
      + '<div style="width:'+frontPct.toFixed(0)+'%;background:#2196F3;border-radius:4px 0 0 4px"></div>'
      + '<div style="width:'+(100-frontPct).toFixed(0)+'%;background:#f44336;border-radius:0 4px 4px 0"></div>'
      + '</div>'
      + '<span style="font-size:10px;color:#ef9a9a;width:50px">Rear</span>'
      + '</div>';
    html += '</div>';
  }

  return html;
}

// ── SVG: Brake system overview ───────────────────────────────────────────────
function renderBrakeDiagram(analysis) {
  var brakeBias = analysis.brakeBias, frontMC = analysis.frontMC, rearMC = analysis.rearMC, brakePads = analysis.brakePads;
  if (brakeBias == null && frontMC == null && rearMC == null) return '';

  var html = '';

  if (brakeBias != null) {
    var rearBias = 100 - brakeBias;
    html += '<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px">Brake Bias</div>';
    html += '<div class="brake-bias-bar">'
      + '<div class="brake-bias-front" style="width:'+brakeBias+'%">F '+brakeBias+'%</div>'
      + '<div class="brake-bias-rear" style="width:'+rearBias+'%">R '+rearBias.toFixed(1)+'%</div>'
      + '</div>';
    var biasNote = brakeBias > 58 ? 'Forward bias \u2014 stable under braking, may understeer on entry'
                   : brakeBias < 54 ? 'Rearward bias \u2014 aggressive, risk of rear lockup under heavy braking'
                   : 'Moderate bias \u2014 good balance for most conditions';
    html += '<div style="font-size:11px;color:#666;margin-top:4px;margin-bottom:12px">'+biasNote+'</div>';
  }

  if (frontMC != null || rearMC != null) {
    html += '<div style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px;margin-top:8px">Master Cylinders</div>';
    var maxMC = Math.max(frontMC || 0, rearMC || 0, 1);
    html += '<div class="bar-pair">';
    if (frontMC != null) {
      html += '<div><div class="bar-pair-label">Front</div>'
        + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(frontMC/maxMC*100).toFixed(0)+'%;background:#2196F3"></div></div></div>'
        + '<div class="bar-pair-value" style="color:#64b5f6">'+frontMC+' mm</div></div>';
    }
    if (rearMC != null) {
      html += '<div><div class="bar-pair-label">Rear</div>'
        + '<div class="bar-chart-row"><div class="bar-wrap"><div class="bar-fill" style="width:'+(rearMC/maxMC*100).toFixed(0)+'%;background:#f44336"></div></div></div>'
        + '<div class="bar-pair-value" style="color:#ef9a9a">'+rearMC+' mm</div></div>';
    }
    html += '</div>';
    if (frontMC != null && rearMC != null) {
      var note = frontMC > rearMC ? 'Larger front MC = more front braking force & firmer pedal feel'
                 : frontMC < rearMC ? 'Larger rear MC = more rear braking force'
                 : 'Equal MC sizes \u2014 neutral pedal response';
      html += '<div style="font-size:11px;color:#555;margin-top:4px">'+note+'</div>';
    }
  }

  if (brakePads) {
    html += '<div style="margin-top:8px;font-size:12px;color:#888">Brake pads: <span style="color:#ccc;font-weight:600">'+brakePads+'</span></div>';
  }

  return html;
}

// ── Render setup recommendations list ────────────────────────────────────────
function renderSetupRecs(recs) {
  if (!recs || !recs.length) return '<p style="color:#444;font-size:12px">No issues detected \u2014 setup parameters look well-balanced.</p>';
  var order = {high: 0, medium: 1, low: 2};
  var sorted = recs.slice().sort(function(a, b) { return (order[a.priority] || 3) - (order[b.priority] || 3); });
  return sorted.map(function(r) {
    return '<div class="setup-rec '+r.priority+'">'
      + '<div class="setup-rec-head">'
      + '<span class="setup-rec-cat">'+r.category+'</span>'
      + '<span class="setup-rec-priority '+r.priority+'">'+r.priority+'</span>'
      + '</div>'
      + '<div class="setup-rec-text">'+r.text+'</div>'
      + (r.action ? '<div class="setup-rec-action">\u2192 '+r.action+'</div>' : '')
      + '</div>';
  }).join('');
}

// ── Render handling tendency badge ───────────────────────────────────────────
function renderTendencyBadge(tendency) {
  var colors = {
    understeer: {bg: '#0d47a1', text: '#64b5f6', icon: '\u21b0'},
    oversteer:  {bg: '#b71c1c', text: '#ef9a9a', icon: '\u21b1'},
    balanced:   {bg: '#1b5e20', text: '#81c784', icon: '\u2194'}
  };
  var c = colors[tendency] || colors.balanced;
  return '<span style="display:inline-flex;align-items:center;gap:6px;background:'+c.bg+';color:'+c.text+';padding:4px 12px;border-radius:8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.3px">'+c.icon+' '+tendency+'</span>';
}
