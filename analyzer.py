#!/usr/bin/env python3
"""
Setup analyzer for iRacing telemetry.
Car and track knowledge is supplied via JSON config dicts — no hardcoding.
All output is in imperial units (°F, mph, gal).
"""

import numpy as np

# ── Unit conversions ──────────────────────────────────────────────────────────
PA_TO_PSI   = 1.0 / 6894.757
MS_TO_MPH   = 2.23694
L_TO_GAL    = 0.264172
G           = 9.80665   # m/s²


def _c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def _diff_to_f(c):
    """Convert a Celsius temperature *difference* to Fahrenheit difference."""
    return c * 9.0 / 5.0


# ── iRacing channel names ─────────────────────────────────────────────────────
TEMP_VARS = {
    'LF': ('LFtempCL', 'LFtempCM', 'LFtempCR'),
    'RF': ('RFtempCL', 'RFtempCM', 'RFtempCR'),
    'LR': ('LRtempCL', 'LRtempCM', 'LRtempCR'),
    'RR': ('RRtempCL', 'RRtempCM', 'RRtempCR'),
}
PRESSURE_VARS = {
    'LF': 'LFpressure', 'RF': 'RFpressure',
    'LR': 'LRpressure', 'RR': 'RRpressure',
}

# ── Fallback config used when no car/track is selected ───────────────────────
DEFAULT_CAR = {
    'name': 'Generic GT Car',
    'target_hot_psi': {'LF': 29.0, 'RF': 29.0, 'LR': 29.0, 'RR': 29.0},
    'temp_min': 80, 'temp_max': 100, 'rear_weight_bias_c': 2.0,
}
DEFAULT_TRACK = {
    'name': 'Unknown Track',
    'sectors': [
        {'name': 'S1 (0–33 %)',  'start': 0.00, 'end': 0.33},
        {'name': 'S2 (33–67 %)', 'start': 0.33, 'end': 0.67},
        {'name': 'S3 (67–100 %)','start': 0.67, 'end': 1.00},
    ],
}


def _ch(channels, name):
    return channels.get(name)


def _flying_lap_mask(channels, n):
    """True for samples on representative flying laps (excludes in/out laps
    and laps more than 15 % slower/faster than the median lap time)."""
    lap = _ch(channels, 'Lap')
    if lap is None:
        return np.ones(n, dtype=bool)

    lap_i     = lap.astype(np.int32)
    changes   = np.where(np.diff(lap_i) > 0)[0] + 1
    starts    = np.concatenate([[0], changes])
    ends      = np.concatenate([changes, [n]])
    durations = ends - starts

    if len(durations) <= 2:
        return np.ones(n, dtype=bool)

    inner_durs = durations[1:-1]
    median_dur = float(np.median(inner_durs))

    mask = np.zeros(n, dtype=bool)
    for i, (s, e) in enumerate(zip(starts, ends)):
        if i == 0 or i == len(starts) - 1:
            continue
        if median_dur * 0.85 <= (e - s) <= median_dur * 1.15:
            mask[s:e] = True
    return mask


def analyze(channels, tick_rate, car_cfg=None, track_cfg=None, ambient_temp_f=None):
    """
    Analyse iRacing telemetry.

    Parameters
    ----------
    channels       : dict returned by ibt_parser.parse_ibt()
    tick_rate      : int — samples per second
    car_cfg        : dict loaded from cars/<name>.json  (falls back to DEFAULT_CAR)
    track_cfg      : dict loaded from tracks/<name>.json (falls back to DEFAULT_TRACK)
    ambient_temp_f : float | None — ambient air temperature in °F for cold pressure correction

    Returns
    -------
    dict with keys: summary, tyre_temps, tyre_pressures, balance,
                    handling, car, track, recommendations
    All temperatures in °F, speeds in mph, fuel in gal.
    """
    car   = car_cfg   or DEFAULT_CAR
    track = track_cfg or DEFAULT_TRACK

    target_hot_psi  = car['target_hot_psi']
    temp_min        = car['temp_min']      # Celsius — used for logic only
    temp_max        = car['temp_max']      # Celsius — used for logic only
    rear_bias_c     = car.get('rear_weight_bias_c', 0.0)
    sectors         = [(s['name'], s['start'], s['end']) for s in track['sectors']]

    n    = len(next(iter(channels.values())))
    mask = _flying_lap_mask(channels, n)

    # ── 0. Signal validation — flag implausible channel values ────────────────
    _signal_warnings = []
    _spd_v = _ch(channels, 'Speed')
    if _spd_v is not None and float(np.max(_spd_v)) * MS_TO_MPH > 280:
        _signal_warnings.append('Speed >280 mph detected — possible data corruption')
    _lat_v = _ch(channels, 'LatAccel')
    if _lat_v is not None and float(np.max(np.abs(_lat_v))) > 78.5:  # >8G
        _signal_warnings.append('LatAccel >8G detected — possible data corruption')
    _thr_v = _ch(channels, 'Throttle')
    if _thr_v is not None and float(np.max(_thr_v)) > 1.05:
        _signal_warnings.append('Throttle channel has values outside 0–1 range')
    _brk_v = _ch(channels, 'Brake')
    if _brk_v is not None and float(np.max(_brk_v)) > 1.05:
        _signal_warnings.append('Brake channel has values outside 0–1 range')

    # ── Ambient temperature correction for cold pressure targets ──────────────
    # Hot-to-cold ratio is ~0.6; ambient deviation from 70 °F shifts cold start
    # by roughly 0.35 psi per 10 °F (colder air = lower cold pressure needed).
    temp_correction = 0.0
    if ambient_temp_f is not None:
        try:
            _tf = float(ambient_temp_f)
            # Piecewise correction: tyre heat-up rate changes with ambient temp.
            # Cold air (<40°F): 0.45 psi per 10°F deviation — larger swing needed.
            # Moderate (40–70°F): 0.35 psi per 10°F — standard baseline.
            # Warm (>70°F): 0.25 psi per 10°F — tyre heats less aggressively.
            if _tf < 40.0:
                temp_correction = -(40.0 - 70.0) / 10.0 * 0.35 + -(_tf - 40.0) / 10.0 * 0.45
            elif _tf > 70.0:
                temp_correction = -(_tf - 70.0) / 10.0 * 0.25
            else:
                temp_correction = -(_tf - 70.0) / 10.0 * 0.35
        except (TypeError, ValueError):
            pass

    out = {
        'car':             car.get('name', 'Unknown'),
        'track':           track.get('name', 'Unknown'),
        'summary':         {},
        'tyre_temps':      {},
        'tyre_pressures':  {},
        'balance':         {},
        'handling':        {},
        'recommendations': [],
    }
    recs = out['recommendations']

    # ── 1. Tyre temperatures (stored in °C during analysis, converted at end) ──
    _temps_c = {}   # internal Celsius copy for rec logic
    for corner, (iv, mv, ov) in TEMP_VARS.items():
        inner = _ch(channels, iv)
        mid   = _ch(channels, mv)
        outer = _ch(channels, ov)
        if inner is None:
            out['tyre_temps'][corner] = None
            _temps_c[corner] = None
            continue

        im, mm, om = inner[mask], mid[mask], outer[mask]
        valid = (im > 20) & (mm > 20) & (om > 20)
        if valid.sum() < 100:
            out['tyre_temps'][corner] = None
            _temps_c[corner] = None
            continue

        td_c = {
            'inner':  float(np.mean(im[valid])),
            'mid':    float(np.mean(mm[valid])),
            'outer':  float(np.mean(om[valid])),
            'avg':    float(np.mean(np.concatenate([im[valid], mm[valid], om[valid]]))),
            'spread': float(np.mean(om[valid]) - np.mean(im[valid])),
        }
        _temps_c[corner] = td_c

    # ── 2. Tyre pressures ─────────────────────────────────────────────────────
    for corner, var in PRESSURE_VARS.items():
        pres = _ch(channels, var)
        if pres is None:
            out['tyre_pressures'][corner] = None
            continue
        pm    = pres[mask]
        valid = pm > 50_000
        if valid.sum() < 100:
            out['tyre_pressures'][corner] = None
            continue
        out['tyre_pressures'][corner] = float(np.mean(pm[valid])) * PA_TO_PSI

    # ── 3. Handling balance by sector ─────────────────────────────────────────
    lat      = _ch(channels, 'LatAccel')
    steer    = _ch(channels, 'SteeringWheelAngle')
    dist_pct = _ch(channels, 'LapDistPct')

    if lat is not None and steer is not None and dist_pct is not None:
        lat_m   = lat[mask]
        steer_m = steer[mask]
        dist_m  = dist_pct[mask]
        lat_g   = lat_m / G

        raw_indices = []
        for sector_name, s0, s1 in sectors:
            in_sector = (dist_m >= s0) & (dist_m < s1) & (np.abs(lat_g) > 0.5)
            if in_sector.sum() < 50:
                out['handling'][sector_name] = {'tendency': 'insufficient data', 'index': None, 'phases': {}}
                raw_indices.append(None)
                continue

            steer_abs = np.abs(steer_m[in_sector])
            lat_abs   = np.abs(lat_g[in_sector])
            us_idx    = float(np.mean(steer_abs / (lat_abs + 0.01)))

            # Phase detection: entry / apex / exit by steering-angle rate of change
            phases = {}
            if in_sector.sum() >= 90:
                steer_rate = np.gradient(steer_abs)
                std_rate   = float(np.std(steer_rate))
                if std_rate > 1e-4:
                    thresh = std_rate * 0.3
                    for phase_name, pmask in [
                        ('entry', steer_rate >  thresh),
                        ('apex',  np.abs(steer_rate) <= thresh),
                        ('exit',  steer_rate < -thresh),
                    ]:
                        if pmask.sum() >= 15:
                            phases[phase_name] = {
                                'index':    float(np.mean(steer_abs[pmask] / (lat_abs[pmask] + 0.01))),
                                'tendency': None,
                            }

            out['handling'][sector_name] = {'tendency': None, 'index': us_idx, 'phases': phases}
            raw_indices.append(us_idx)

        valid_idx = [x for x in raw_indices if x is not None]
        if valid_idx:
            session_mean = float(np.mean(valid_idx))
            for name in out['handling']:
                d = out['handling'][name]
                if d['index'] is None:
                    continue
                norm = d['index'] / session_mean
                d['normalised'] = round(norm, 3)
                d['tendency'] = (
                    'understeer' if norm > 1.15 else
                    'oversteer'  if norm < 0.85 else
                    'neutral'
                )
                # Classify each phase against the same session_mean baseline
                for phase_data in d['phases'].values():
                    pnorm = phase_data['index'] / session_mean
                    phase_data['normalised'] = round(pnorm, 3)
                    phase_data['tendency'] = (
                        'understeer' if pnorm > 1.15 else
                        'oversteer'  if pnorm < 0.85 else
                        'neutral'
                    )

    # ── 4. Recommendations (logic in °C, display strings in °F) ──────────────

    # 4a. Tyre pressure
    for corner, hot_psi in out['tyre_pressures'].items():
        if hot_psi is None:
            continue
        target = target_hot_psi[corner]
        diff   = hot_psi - target
        if abs(diff) > 0.5:
            cold_adj  = -diff * 0.6 + temp_correction
            direction = 'Reduce' if cold_adj < 0 else 'Increase'
            recs.append({
                'category': 'Tyre Pressure',
                'corner':   corner,
                'issue':    f'Hot pressure {hot_psi:.1f} psi (target {target:.1f} psi)',
                'action':   f'{direction} cold pressure by {abs(cold_adj):.1f} psi',
                'priority': 'high' if abs(diff) > 1.5 else 'medium',
            })

    # 4b. Camber (inner-outer spread)
    for corner, td_c in _temps_c.items():
        if td_c is None:
            continue
        spread   = td_c['spread']   # °C
        if abs(spread) < 8.0:
            continue
        spread_f = _diff_to_f(spread)
        adj      = abs(spread) / 10.0  # camber degrees — dimensionless ratio, stays the same
        if spread > 0:
            recs.append({
                'category': 'Camber',
                'corner':   corner,
                'issue':    f'Outer {spread_f:.1f} °F hotter than inner — tyre rolling onto outside edge',
                'action':   f'Add {adj:.1f}° negative camber to {corner}',
                'priority': 'high' if abs(spread) > 15 else 'medium',
            })
        else:
            recs.append({
                'category': 'Camber',
                'corner':   corner,
                'issue':    f'Inner {abs(spread_f):.1f} °F hotter than outer — too much negative camber',
                'action':   f'Reduce {corner} negative camber by {adj:.1f}°',
                'priority': 'high' if abs(spread) > 15 else 'medium',
            })

    # 4c. Temperature window
    temp_min_f = round(_c_to_f(temp_min))
    temp_max_f = round(_c_to_f(temp_max))
    for corner, td_c in _temps_c.items():
        if td_c is None:
            continue
        avg   = td_c['avg']   # °C
        avg_f = round(_c_to_f(avg))
        if avg < temp_min:
            recs.append({
                'category': 'Tyre Temperature',
                'corner':   corner,
                'issue':    f'Average {avg_f} °F below optimal window ({temp_min_f}–{temp_max_f} °F)',
                'action':   'Increase cold tyre pressure by 0.5 psi, or add a warm-up lap before pushing',
                'priority': 'medium',
            })
        elif avg > temp_max:
            recs.append({
                'category': 'Tyre Temperature',
                'corner':   corner,
                'issue':    f'Average {avg_f} °F above optimal window ({temp_min_f}–{temp_max_f} °F)',
                'action':   'Reduce cold tyre pressure by 0.5 psi, or soften the spring rate at this corner',
                'priority': 'medium',
            })

    # 4d. Front-rear balance
    f_vals = [_temps_c[c]['avg'] for c in ('LF', 'RF') if _temps_c.get(c)]
    r_vals = [_temps_c[c]['avg'] for c in ('LR', 'RR') if _temps_c.get(c)]
    if f_vals and r_vals:
        f_avg = float(np.mean(f_vals))
        r_avg = float(np.mean(r_vals))
        out['balance']['front_avg']       = round(_c_to_f(f_avg), 1)
        out['balance']['rear_avg']        = round(_c_to_f(r_avg), 1)
        out['balance']['front_rear_diff'] = round(_diff_to_f(r_avg - f_avg), 1)

        adjusted = (r_avg - f_avg) - rear_bias_c
        diff_f   = _diff_to_f(r_avg - f_avg)
        if adjusted > 12:
            recs.append({
                'category': 'Balance',
                'corner':   'REAR',
                'issue':    f'Rear tyres {diff_f:.1f} °F hotter than fronts (adjusted for {car.get("name","this car")} rear-bias offset)',
                'action':   '① Soften rear ARB 1–2 clicks  ② Raise rear ride height 1–2mm  ③ Increase rear wing 1 step (if available)  ④ Reduce rear toe-in 0.05°',
                'priority': 'high',
            })
        elif adjusted < -12:
            recs.append({
                'category': 'Balance',
                'corner':   'FRONT',
                'issue':    f'Front tyres {_diff_to_f(f_avg - r_avg):.1f} °F hotter than rears — push/understeer',
                'action':   '① Stiffen rear ARB 1 click  ② Lower front ride height 1mm  ③ Increase front wing 1 step (if available)  ④ Increase front toe-out 0.05°',
                'priority': 'high',
            })

    # Left/right balance
    l_vals_c = [_temps_c[c]['avg'] for c in ('LF', 'LR') if _temps_c.get(c)]
    r_vals_c = [_temps_c[c]['avg'] for c in ('RF', 'RR') if _temps_c.get(c)]
    if l_vals_c and r_vals_c:
        l_avg  = float(np.mean(l_vals_c))
        r_avg2 = float(np.mean(r_vals_c))
        out['balance']['left_avg']        = round(_c_to_f(l_avg), 1)
        out['balance']['right_avg']       = round(_c_to_f(r_avg2), 1)
        lr_diff_c = r_avg2 - l_avg
        out['balance']['left_right_diff'] = round(_diff_to_f(lr_diff_c), 1)
        if abs(lr_diff_c) > 18:
            hot  = 'RIGHT' if lr_diff_c > 0 else 'LEFT'
            cool = 'LEFT'  if lr_diff_c > 0 else 'RIGHT'
            recs.append({
                'category': 'Balance',
                'corner':   hot,
                'issue':    f'{"Right" if lr_diff_c > 0 else "Left"} tyres {_diff_to_f(abs(lr_diff_c)):.1f} °F hotter — extreme left/right imbalance',
                'action':   f'Add negative camber on {hot.lower()} side, or adjust {hot.lower()} ARB to transfer load to the {cool.lower()} side',
                'priority': 'medium',
            })

    # 4e. Sector handling — phase-aware recommendations
    for sector_name, sd in out['handling'].items():
        tendency = sd.get('tendency')
        if tendency not in ('understeer', 'oversteer'):
            continue

        phases = sd.get('phases', {})
        # Find which phases show the same issue
        issue_phases = [p for p, pd in phases.items() if pd.get('tendency') == tendency]

        if len(issue_phases) == 1:
            phase_label = issue_phases[0]
        else:
            phase_label = None  # throughout, or no phase data

        if tendency == 'understeer':
            if phase_label == 'entry':
                action = '① Shift brake bias rearward 0.5–1%  ② Increase trail-braking depth  ③ Soften front ARB 1 click'
                phase_str = ' — entry'
            elif phase_label == 'apex':
                action = '① Add 0.1–0.2° front negative camber  ② Soften front springs 1 step  ③ Soften front ARB 1 click'
                phase_str = ' — apex'
            elif phase_label == 'exit':
                action = '① Soften rear ARB 1–2 clicks  ② Increase rear toe-in 0.05°  ③ Raise rear ride height 1mm'
                phase_str = ' — exit'
            else:
                action = '① Soften front ARB 1–2 clicks  ② Stiffen rear ARB 1 click  ③ Add 0.1–0.2° front camber  ④ Shift brake bias rearward 0.5–1%'
                phase_str = ''
            recs.append({
                'category': 'Handling',
                'corner':   sector_name,
                'issue':    f'Understeer in {sector_name}{phase_str}',
                'action':   action,
                'priority': 'medium',
            })
        elif tendency == 'oversteer':
            if phase_label == 'entry':
                action = '① Shift brake bias forward 0.5–1%  ② Stiffen rear ARB 1 click  ③ Increase rear toe-in 0.05°'
                phase_str = ' — entry'
            elif phase_label == 'apex':
                action = '① Check rear camber (add 0.1°)  ② Soften front ARB 1 click  ③ Increase rear toe-in 0.05°'
                phase_str = ' — apex'
            elif phase_label == 'exit':
                action = '① Stiffen rear ARB 1–2 clicks  ② Reduce rear toe-in 0.05°  ③ Lower rear ride height 1mm'
                phase_str = ' — exit'
            else:
                action = '① Soften rear ARB 1–2 clicks  ② Increase rear toe-in 0.05°  ③ Stiffen front ARB 1 click  ④ Shift brake bias forward 0.5–1%'
                phase_str = ''
            recs.append({
                'category': 'Handling',
                'corner':   sector_name,
                'issue':    f'Oversteer in {sector_name}{phase_str}',
                'action':   action,
                'priority': 'medium',
            })

    _pri = {'high': 0, 'medium': 1, 'low': 2}
    recs.sort(key=lambda r: _pri.get(r['priority'], 3))

    # ── Post-process: strip ride-height-lowering actions that would breach tech limits ──
    _min_rh_check = (car_cfg or {}).get('tech_limits', {}).get('min_ride_height_mm', {})
    if _min_rh_check and out.get('ride_heights'):
        _rh_near_limit = set()
        for _c, _rv in out['ride_heights'].items():
            _min_c = _min_rh_check.get(_c, 0)
            _rv_mm = _rv if isinstance(_rv, (int, float)) and _rv is not None else 999
            if _rv_mm - _min_c < 8:   # within 8mm of minimum
                _rh_near_limit.add(_c[0])   # 'L' or 'R' → front/rear pair

        def _filter_rh_action(action_str):
            parts = [p.strip() for p in action_str.split('  ') if p.strip()]
            filtered = []
            for p in parts:
                low = p.lower()
                skip = False
                if 'lower front ride height' in low and ('L' in _rh_near_limit or 'R' in _rh_near_limit):
                    skip = True
                if 'lower rear ride height' in low and ('L' in _rh_near_limit or 'R' in _rh_near_limit):
                    skip = True
                if not skip:
                    filtered.append(p)
            return '  '.join(filtered) if filtered else action_str

        for _r in recs:
            if 'action' in _r and isinstance(_r['action'], str):
                _r['action'] = _filter_rh_action(_r['action'])

    # Flag toe recommendations — toe is not readable from telemetry, verify limits manually
    for _r in recs:
        if 'action' in _r and 'toe' in str(_r.get('action', '')).lower():
            _r['toe_verify'] = True

    # ── 5. Convert tyre temps to °F for output ────────────────────────────────
    for corner, td_c in _temps_c.items():
        if td_c is None:
            out['tyre_temps'][corner] = None
            continue
        out['tyre_temps'][corner] = {
            'inner':  round(_c_to_f(td_c['inner']), 1),
            'mid':    round(_c_to_f(td_c['mid']),   1),
            'outer':  round(_c_to_f(td_c['outer']), 1),
            'avg':    round(_c_to_f(td_c['avg']),   1),
            'spread': round(_diff_to_f(td_c['spread']), 1),
        }

    # ── 6. Lap times ──────────────────────────────────────────────────────────
    lap_time_ch = _ch(channels, 'LapLastLapTime')
    if lap_time_ch is not None:
        ltt = lap_time_ch[mask]
        valid = ltt[ltt > 20]
        if len(valid) > 10:
            med = float(np.median(valid))
            flying_times = valid[(valid >= med * 0.90) & (valid <= med * 1.10)]
            if len(flying_times) > 0:
                out['summary']['best_lap_s'] = round(float(np.min(flying_times)), 3)
                out['summary']['avg_lap_s']  = round(float(np.mean(flying_times)), 3)
            if len(flying_times) > 2:
                out['summary']['lap_consistency_s'] = round(float(np.std(flying_times)), 3)

    # ── 7. Fuel per lap & laps to empty ───────────────────────────────────────
    fuel_ch = _ch(channels, 'FuelLevel')
    lap_ch  = _ch(channels, 'Lap')
    if fuel_ch is not None and lap_ch is not None:
        lap_int     = lap_ch.astype(np.int32)
        laps_unique = np.unique(lap_int)
        usages = []
        for i in range(len(laps_unique) - 1):
            a_idx = np.where(lap_int == laps_unique[i])[0]
            b_idx = np.where(lap_int == laps_unique[i + 1])[0]
            if len(a_idx) == 0 or len(b_idx) == 0:
                continue
            usage = float(fuel_ch[a_idx[0]]) - float(fuel_ch[b_idx[0]])
            if 0.3 < usage < 8.0:
                usages.append(usage)
        if usages:
            fpl_gal = float(np.mean(usages)) * L_TO_GAL
            out['summary']['fuel_per_lap_gal'] = round(fpl_gal, 3)
            # Laps to empty based on last fuel reading
            last_fuel_gal = float(fuel_ch[-1]) * L_TO_GAL
            if fpl_gal > 0:
                out['summary']['laps_to_empty'] = round(last_fuel_gal / fpl_gal, 1)

    # ── 8. Speed summary ──────────────────────────────────────────────────────
    speed = _ch(channels, 'Speed')
    if speed is not None:
        s = speed[mask & (speed > 0)]
        if len(s):
            out['summary']['max_speed_mph'] = round(float(np.max(s))  * MS_TO_MPH, 1)
            out['summary']['avg_speed_mph'] = round(float(np.mean(s)) * MS_TO_MPH, 1)

    # ── 9. G-force peaks ──────────────────────────────────────────────────────
    lat_ch = _ch(channels, 'LatAccel')
    lon_ch = _ch(channels, 'LongAccel')
    if lat_ch is not None:
        lat_m = lat_ch[mask]
        out['summary']['peak_lat_g'] = round(float(np.max(np.abs(lat_m))) / G, 2)
    if lon_ch is not None:
        lon_m = lon_ch[mask]
        # Braking is negative longitudinal acceleration
        out['summary']['peak_brake_g'] = round(float(np.max(-lon_m)) / G, 2)

    # ── 10. Lap count ─────────────────────────────────────────────────────────
    lap = _ch(channels, 'Lap')
    if lap is not None:
        out['summary']['laps_analysed'] = int(np.max(lap))

    out['summary']['duration_s'] = n // tick_rate if tick_rate > 0 else None

    # ── 11. Brake analysis ────────────────────────────────────────────────────
    brake_ch = _ch(channels, 'Brake')
    br = {}

    # Bias setting from driver-control channel (front % as 0–1)
    for bias_name in ('dcBrakeBias', 'BrakeBias'):
        bias_ch = _ch(channels, bias_name)
        if bias_ch is not None:
            bias_m = bias_ch[mask]
            if brake_ch is not None:
                brk_m_b = brake_ch[mask]
                heavy   = brk_m_b > 0.3
                if heavy.sum() > 50:
                    br['avg_bias_front_pct'] = round(float(np.mean(bias_m[heavy])) * 100, 1)
                    break
            br['avg_bias_front_pct'] = round(float(np.mean(bias_m)) * 100, 1)
            break

    # Actual front/rear split from individual brake line pressures
    lf_bp = _ch(channels, 'LFbrakeLinePress')
    rf_bp = _ch(channels, 'RFbrakeLinePress')
    lr_bp = _ch(channels, 'LRbrakeLinePress')
    rr_bp = _ch(channels, 'RRbrakeLinePress')
    if lf_bp is not None and lr_bp is not None:
        lf_m  = lf_bp[mask]
        rf_m  = rf_bp[mask] if rf_bp is not None else lf_m
        lr_m  = lr_bp[mask]
        rr_m  = rr_bp[mask] if rr_bp is not None else lr_m
        f_pr  = lf_m + rf_m
        r_pr  = lr_m + rr_m
        tot   = f_pr + r_pr
        mx    = float(np.max(tot))
        heavy = (tot > mx * 0.25) if mx > 0 else np.zeros(len(tot), dtype=bool)
        if heavy.sum() > 100:
            br['actual_front_bias_pct'] = round(
                float(np.mean(f_pr[heavy] / (tot[heavy] + 1e-9))) * 100, 1)
            br['peak_brake_press_psi']  = round(mx * PA_TO_PSI, 1)

    # Brake zone consistency — detect individual brake events, measure peak
    if brake_ch is not None:
        brk_m = brake_ch[mask]
        in_zone = brk_m > 0.5
        entries = np.where(np.diff(in_zone.astype(np.int8)) > 0)[0]
        peaks   = []
        for idx in entries:
            end  = min(idx + 300, len(brk_m))
            zone = brk_m[idx:end]
            stop = np.where(zone < 0.15)[0]
            if len(stop):
                zone = zone[:stop[0]]
            if len(zone) >= 5:
                peaks.append(float(np.max(zone)))
        if len(peaks) >= 3:
            br['brake_events']       = len(peaks)
            br['avg_peak_brake_pct'] = round(float(np.mean(peaks)) * 100, 1)
            br['brake_consistency']  = round(float(np.std(peaks)) * 100, 1)

    out['brake'] = br

    # ── 12. Throttle / brake overlap ─────────────────────────────────────────
    throttle_ch = _ch(channels, 'Throttle')
    if throttle_ch is not None and brake_ch is not None:
        thr_m   = throttle_ch[mask]
        brk_m   = brake_ch[mask]
        n_m     = len(thr_m)
        overlap = (thr_m > 0.05) & (brk_m > 0.05)

        ovlp = {'overall_pct': round(float(overlap.sum()) / n_m * 100, 1) if n_m else 0.0}

        # Per-sector breakdown
        dp = _ch(channels, 'LapDistPct')
        if dp is not None:
            dp_m = dp[mask]
            sector_pcts = {}
            for sec_name, s0, s1 in sectors:
                in_sec = (dp_m >= s0) & (dp_m < s1)
                if in_sec.sum() > 100:
                    sector_pcts[sec_name] = round(
                        float(overlap[in_sec].sum()) / float(in_sec.sum()) * 100, 1)
            if sector_pcts:
                ovlp['by_sector'] = sector_pcts

        # Flag if overall overlap is high enough to warrant a recommendation
        if ovlp['overall_pct'] > 8.0:
            recs.append({
                'category': 'Technique',
                'corner':   'ALL',
                'issue':    f'Throttle/brake overlap {ovlp["overall_pct"]:.1f}% of lap time — simultaneous pedal input',
                'action':   'Check for unintentional left-foot braking or review trail-brake technique',
                'priority': 'medium',
            })

        out['throttle_overlap'] = ovlp

    # ── 13. Individual lap times ──────────────────────────────────────────────
    if lap is not None and lap_time_ch is not None:
        _lap_int = lap.astype(np.int32)
        max_lap  = int(np.max(_lap_int))
        laps_list = []
        for lap_num in range(1, max_lap + 1):
            # LapLastLapTime holds lap_num's time while Lap == lap_num + 1.
            # Read from the middle of that period to avoid boundary glitches
            # where the channel hasn't updated yet.
            in_next = np.where(_lap_int == lap_num + 1)[0]
            if len(in_next) == 0:
                continue
            mid_idx = in_next[len(in_next) // 2]
            lt = float(lap_time_ch[mid_idx])
            if 20.0 < lt < 600.0:
                laps_list.append({'lap': lap_num, 'time_s': round(lt, 3)})
        if laps_list:
            out['lap_times'] = laps_list
        # Debug: always include channel availability and raw sample for diagnosis
        out['_lap_debug'] = {
            'has_lap_ch':     True,
            'has_laptime_ch': True,
            'max_lap':        max_lap,
            'laps_found':     len(laps_list),
            'sample_lt':      round(float(lap_time_ch[len(lap_time_ch) // 2]), 3),
        }
    else:
        out['_lap_debug'] = {
            'has_lap_ch':     lap is not None,
            'has_laptime_ch': lap_time_ch is not None,
        }

    # ── 14. Setup Card ────────────────────────────────────────────────────────
    # Structured, garage-ready summary: tyres + suspension grouped by priority.
    sc = {'tyres': {'pressures': [], 'camber': []}, 'suspension': []}

    # Tyre pressure targets
    for _corner in ('LF', 'RF', 'LR', 'RR'):
        hot = out['tyre_pressures'].get(_corner)
        if hot is None:
            sc['tyres']['pressures'].append({'corner': _corner, 'status': 'no_data'})
            continue
        tgt  = target_hot_psi[_corner]
        diff = hot - tgt
        cold_adj = round(-diff * 0.6 + temp_correction, 1)
        sc['tyres']['pressures'].append({
            'corner':         _corner,
            'hot_psi':        round(hot, 1),
            'target_hot_psi': tgt,
            'cold_adj':       cold_adj,
            'status':         'over' if diff > 0.5 else 'under' if diff < -0.5 else 'ok',
        })

    # Camber (inner–outer spread)
    for _corner, _td in _temps_c.items():
        if _td is None or abs(_td['spread']) < 8.0:
            continue
        _sp = _td['spread']
        _adj = abs(_sp) / 10.0
        sc['tyres']['camber'].append({
            'corner':    _corner,
            'spread_f':  round(_diff_to_f(_sp), 1),
            'direction': 'add' if _sp > 0 else 'reduce',
            'range':     f'{max(_adj - 0.1, 0.1):.1f}–{_adj + 0.1:.1f}°',
        })

    # Balance (front-rear, suspension priority)
    _f_ok = [_temps_c[c]['avg'] for c in ('LF', 'RF') if _temps_c.get(c)]
    _r_ok = [_temps_c[c]['avg'] for c in ('LR', 'RR') if _temps_c.get(c)]
    if _f_ok and _r_ok:
        _fa = float(np.mean(_f_ok))
        _ra = float(np.mean(_r_ok))
        _adj2 = (_ra - _fa) - rear_bias_c
        if _adj2 > 12:
            sc['suspension'].append({
                'priority': 'high', 'sector': 'ALL', 'issue': 'rear_hot',
                'label':   'Rear temp bias',
                'options': [
                    'Soften rear ARB 1–2 clicks',
                    'Raise rear ride height 1–2mm',
                    'Increase rear wing 1 step (if adjustable)',
                    'Reduce rear toe-in 0.05°',
                ],
            })
        elif _adj2 < -12:
            sc['suspension'].append({
                'priority': 'high', 'sector': 'ALL', 'issue': 'front_hot',
                'label':   'Front temp bias',
                'options': [
                    'Stiffen rear ARB 1 click',
                    'Lower front ride height 1mm',
                    'Increase front wing 1 step (if adjustable)',
                    'Increase front toe-out 0.05°',
                ],
            })

    # Sector handling
    _phase_us_opts = {
        'entry': ['Shift brake bias rearward 0.5–1%', 'Increase trail-braking depth', 'Soften front ARB 1 click'],
        'apex':  ['Add 0.1–0.2° front negative camber', 'Soften front springs 1 step', 'Soften front ARB 1 click'],
        'exit':  ['Soften rear ARB 1–2 clicks', 'Increase rear toe-in 0.05°', 'Raise rear ride height 1mm'],
        None:    ['Soften front ARB 1–2 clicks', 'Stiffen rear ARB 1 click', 'Add 0.1–0.2° front negative camber', 'Shift brake bias rearward 0.5–1%'],
    }
    _phase_os_opts = {
        'entry': ['Shift brake bias forward 0.5–1%', 'Stiffen rear ARB 1 click', 'Increase rear toe-in 0.05°'],
        'apex':  ['Check rear camber (add 0.1°)', 'Soften front ARB 1 click', 'Increase rear toe-in 0.05°'],
        'exit':  ['Stiffen rear ARB 1–2 clicks', 'Reduce rear toe-in 0.05°', 'Lower rear ride height 1mm'],
        None:    ['Soften rear ARB 1–2 clicks', 'Increase rear toe-in 0.05°', 'Stiffen front ARB 1 click', 'Shift brake bias forward 0.5–1%'],
    }
    for _sec, _sd in out['handling'].items():
        _t = _sd.get('tendency')
        if _t not in ('understeer', 'oversteer'):
            continue
        _phases = _sd.get('phases', {})
        _issue_phases = [p for p, pd in _phases.items() if pd.get('tendency') == _t]
        _phase_key = _issue_phases[0] if len(_issue_phases) == 1 else None
        _opts_map = _phase_us_opts if _t == 'understeer' else _phase_os_opts
        _phase_suffix = f' — {_phase_key}' if _phase_key else ''
        sc['suspension'].append({
            'priority': 'medium', 'sector': _sec, 'issue': _t,
            'label': f'{_sec}{_phase_suffix} — {_t}',
            'options': _opts_map[_phase_key],
        })

    _pri2 = {'high': 0, 'medium': 1, 'low': 2}
    sc['suspension'].sort(key=lambda x: _pri2.get(x['priority'], 3))
    out['setup_card'] = sc

    # ── 15. Track map (GPS-based colour-coded path) ───────────────────────────
    _lat_ch = _ch(channels, 'Lat')
    _lon_ch = _ch(channels, 'Lon')

    if (_lat_ch is not None and _lon_ch is not None
            and lap is not None and speed is not None):
        _map_lap_int = lap.astype(np.int32)

        # Choose best flying lap; fall back to a median flying lap
        _map_lap = None
        if out.get('lap_times'):
            _map_lap = min(out['lap_times'], key=lambda _l: _l['time_s'])['lap']
        else:
            _fl_laps = np.unique(_map_lap_int[mask])
            if len(_fl_laps) > 0:
                _map_lap = int(_fl_laps[len(_fl_laps) // 2])

        if _map_lap is not None:
            _lm = _map_lap_int == _map_lap
            if _lm.sum() >= 100:
                _lats = _lat_ch[_lm].astype(float)
                _lons = _lon_ch[_lm].astype(float)

                # Verify GPS data has real variance (not zeroed out)
                if float(np.std(_lats)) > 1e-6 and float(np.std(_lons)) > 1e-6:
                    _spds = speed[_lm] * MS_TO_MPH
                    _thrs = (throttle_ch[_lm] if throttle_ch is not None
                             else np.zeros(int(_lm.sum())))
                    _brks = (brake_ch[_lm]    if brake_ch   is not None
                             else np.zeros(int(_lm.sum())))
                    _dpct = (dist_pct[_lm]    if dist_pct   is not None
                             else np.linspace(0.0, 1.0, int(_lm.sum())))
                    _gear_ch2 = _ch(channels, 'Gear')
                    _gears = (_gear_ch2[_lm].astype(int) if _gear_ch2 is not None
                              else np.zeros(int(_lm.sum()), dtype=int))

                    # Per-sample US/OS index (steer/lat_g ratio normalised by lap median)
                    _lat_map_ch  = _ch(channels, 'LatAccel')
                    _steer_map_ch = _ch(channels, 'SteeringWheelAngle')
                    if _lat_map_ch is not None and _steer_map_ch is not None:
                        _lat_lap_m   = _lat_map_ch[_lm].astype(float)
                        _steer_lap_m = _steer_map_ch[_lm].astype(float)
                        _lat_g_lap   = _lat_lap_m / G
                        _ratio_raw   = np.abs(_steer_lap_m) / (np.abs(_lat_g_lap) + 0.01)
                        _wlen = max(1, min(30, int(_lm.sum()) // 8))
                        _ratio_sm = (np.convolve(_ratio_raw, np.ones(_wlen) / _wlen, mode='same')
                                     if _wlen > 1 else _ratio_raw)
                        _ratio_med = float(np.median(_ratio_sm))
                        _us_idx_arr = (_ratio_sm / _ratio_med
                                       if _ratio_med > 0 else np.ones(len(_ratio_sm)))
                    else:
                        _us_idx_arr = np.ones(int(_lm.sum()))

                    # Downsample to ≤800 evenly-spaced points
                    _n   = min(800, int(_lm.sum()))
                    _idx = np.round(np.linspace(0, int(_lm.sum()) - 1, _n)).astype(int)
                    _lats = _lats[_idx];  _lons = _lons[_idx]
                    _spds = _spds[_idx];  _thrs = _thrs[_idx]
                    _brks = _brks[_idx];  _dpct = _dpct[_idx]
                    _gears = _gears[_idx]
                    _us_idx_arr = _us_idx_arr[_idx]

                    # Equirectangular projection → local XY (metres); N = up
                    _lat_c = float(np.mean(_lats))
                    _R     = 6_371_000.0
                    _xs = ((_lons - float(np.mean(_lons)))
                           * np.cos(np.radians(_lat_c)) * (np.pi / 180.0) * _R)
                    _ys = -(_lats - _lat_c) * (np.pi / 180.0) * _R

                    # Normalise to 0–1000 coordinate space (aspect ratio preserved)
                    _xr = float(np.max(_xs) - np.min(_xs))
                    _yr = float(np.max(_ys) - np.min(_ys))
                    _sc = 1000.0 / max(_xr, _yr, 1.0)
                    _xs = (_xs - float(np.min(_xs))) * _sc
                    _ys = (_ys - float(np.min(_ys))) * _sc

                    out['track_map'] = {
                        'lap':       int(_map_lap),
                        'max_speed': round(float(np.max(_spds)), 1),
                        'sectors': [
                            {'name': s[0], 'start': round(s[1], 3), 'end': round(s[2], 3)}
                            for s in sectors
                        ],
                        'points': [
                            {
                                'x':   round(float(_xs[i]), 1),
                                'y':   round(float(_ys[i]), 1),
                                'spd': round(float(_spds[i]), 1),
                                'thr': round(float(_thrs[i]), 2),
                                'brk': round(float(_brks[i]), 2),
                                'pct': round(float(_dpct[i]), 3),
                                'gear': int(_gears[i]),
                                'us':  round(float(_us_idx_arr[i]), 3),
                            }
                            for i in range(_n)
                        ],
                    }

    # ── 16. Speed trace (best 5 laps, speed vs LapDistPct) ───────────────────────
    if (speed is not None and dist_pct is not None
            and lap is not None and out.get('lap_times')):
        _lap_int_st = lap.astype(np.int32)
        _sorted_laps_st = sorted(out['lap_times'], key=lambda _l: _l['time_s'])
        _trace_laps_st  = _sorted_laps_st[:5]
        _best_lap_st    = _sorted_laps_st[0]['lap'] if _sorted_laps_st else None
        _trace_list_st  = []
        for _tl in _trace_laps_st:
            _tl_num = _tl['lap']
            _tlm = _lap_int_st == _tl_num
            if _tlm.sum() < 50:
                continue
            _t_spds = speed[_tlm] * MS_TO_MPH
            _t_pcts = dist_pct[_tlm]
            _t_thrs = (throttle_ch[_tlm] if throttle_ch is not None
                       else np.zeros(int(_tlm.sum())))
            _t_brks = (brake_ch[_tlm]    if brake_ch    is not None
                       else np.zeros(int(_tlm.sum())))
            _tn = min(200, int(_tlm.sum()))
            _tidx = np.round(np.linspace(0, int(_tlm.sum()) - 1, _tn)).astype(int)
            _t_spds = _t_spds[_tidx]
            _t_pcts = _t_pcts[_tidx]
            _t_thrs = _t_thrs[_tidx]
            _t_brks = _t_brks[_tidx]
            _trace_list_st.append({
                'lap':     _tl_num,
                'time_s':  _tl['time_s'],
                'is_best': _tl_num == _best_lap_st,
                'points':  [
                    {
                        'pct': round(float(_t_pcts[i]), 3),
                        'spd': round(float(_t_spds[i]), 1),
                        'thr': round(float(_t_thrs[i]), 2),
                        'brk': round(float(_t_brks[i]), 2),
                    }
                    for i in range(_tn)
                ],
            })
        if _trace_list_st:
            out['speed_trace'] = {'laps': _trace_list_st}

    # ── 17. Stint analysis (pit detection via FuelLevel jumps) ───────────────────
    if fuel_ch is not None and lap is not None and out.get('lap_times'):
        _lap_int_pi = lap.astype(np.int32)
        _laps_unique_pi = np.unique(_lap_int_pi)
        # Fuel level at start of each lap (litres, raw)
        _lap_fuel_start_pi = {}
        for _ln in _laps_unique_pi:
            _ln_idx = np.where(_lap_int_pi == _ln)[0]
            if len(_ln_idx) > 0:
                _lap_fuel_start_pi[int(_ln)] = float(fuel_ch[_ln_idx[0]])
        _lt_dict_pi = {_l['lap']: _l['time_s'] for _l in out['lap_times']}
        _all_laps_pi = sorted(_lt_dict_pi.keys())
        if len(_all_laps_pi) >= 2:
            # Detect pit stops: fuel went UP between consecutive lap starts
            _pit_after_pi = []
            for _pi in range(len(_all_laps_pi) - 1):
                _la = _all_laps_pi[_pi]
                _lb = _all_laps_pi[_pi + 1]
                _fa = _lap_fuel_start_pi.get(_la)
                _fb = _lap_fuel_start_pi.get(_lb)
                if _fa is not None and _fb is not None and _fb > _fa:
                    _pit_after_pi.append(_la)
            # Build stint boundaries
            _stint_start_laps = [_all_laps_pi[0]]
            for _pa in _pit_after_pi:
                _pa_i = _all_laps_pi.index(_pa)
                if _pa_i + 1 < len(_all_laps_pi):
                    _stint_start_laps.append(_all_laps_pi[_pa_i + 1])
            _stint_end_laps = _pit_after_pi + [_all_laps_pi[-1]]
            _stints_out = []
            for _si, (_ss, _se) in enumerate(zip(_stint_start_laps, _stint_end_laps)):
                _stint_lap_nums = [_l for _l in _all_laps_pi if _ss <= _l <= _se]
                _stint_times_pi = [_lt_dict_pi[_l] for _l in _stint_lap_nums if _l in _lt_dict_pi]
                if not _stint_times_pi:
                    continue
                _f_start_pi = _lap_fuel_start_pi.get(_ss)
                _next_ss = _stint_start_laps[_si + 1] if _si + 1 < len(_stint_start_laps) else None
                _f_end_pi = (_lap_fuel_start_pi.get(_next_ss) if _next_ss
                             else float(fuel_ch[-1]))
                _fuel_used_pi = (round((_f_start_pi - _f_end_pi) * L_TO_GAL, 3)
                                 if (_f_start_pi and _f_end_pi and _f_start_pi > _f_end_pi)
                                 else None)
                _stints_out.append({
                    'stint':         _si + 1,
                    'start_lap':     _ss,
                    'end_lap':       _se,
                    'lap_count':     len(_stint_lap_nums),
                    'avg_lap_s':     round(float(np.mean(_stint_times_pi)), 3),
                    'best_lap_s':    round(float(np.min(_stint_times_pi)), 3),
                    'fuel_used_gal': _fuel_used_pi,
                })
            if _stints_out:
                out['stints'] = _stints_out

    # ── 18. Sector split times ────────────────────────────────────────────────────
    if (dist_pct is not None and lap is not None
            and out.get('lap_times') and len(sectors) >= 2):
        _lap_int_sc = lap.astype(np.int32)
        _lt_dict_sc = {_l['lap']: _l['time_s'] for _l in out['lap_times']}
        _sector_laps_sc = []
        for _lap_num_sc, _lap_total_sc in _lt_dict_sc.items():
            _lm_sc = _lap_int_sc == _lap_num_sc
            if _lm_sc.sum() < 20:
                continue
            _dp_sc = dist_pct[_lm_sc]
            _total_sc = float(_lm_sc.sum())
            _splits = []
            for _sec_name, _s0, _s1 in sectors:
                _in_sec = float(np.sum((_dp_sc >= _s0) & (_dp_sc < _s1)))
                _splits.append(_lap_total_sc * _in_sec / max(_total_sc, 1))
            # Renormalise so splits sum exactly to lap total
            _split_sum = sum(_splits)
            if _split_sum > 0:
                _splits = [round(s * _lap_total_sc / _split_sum, 3) for s in _splits]
            _sector_laps_sc.append({
                'lap':     _lap_num_sc,
                'total_s': round(_lap_total_sc, 3),
                'splits':  _splits,
            })
        if _sector_laps_sc:
            _num_sec_sc = len(sectors)
            _best_splits_sc = []
            for _si_sc in range(_num_sec_sc):
                _vs = [_l['splits'][_si_sc] for _l in _sector_laps_sc
                       if len(_l['splits']) > _si_sc and _l['splits'][_si_sc] > 0]
                _best_splits_sc.append(round(min(_vs), 3) if _vs else None)
            out['sector_times'] = {
                'sectors':     [{'name': s[0], 'start': round(s[1], 3), 'end': round(s[2], 3)}
                                 for s in sectors],
                'laps':        _sector_laps_sc,
                'best_splits': _best_splits_sc,
            }

    # ── 19. Per-lap tyre temperature trend ───────────────────────────────────────
    if lap is not None and out.get('lap_times'):
        _lap_int_tt = lap.astype(np.int32)
        _trend_rows = []
        for _tl_tt in out['lap_times']:
            _ln_tt = _tl_tt['lap']
            _lm_tt = _lap_int_tt == _ln_tt
            if _lm_tt.sum() < 20:
                continue
            _row_tt = {'lap': _ln_tt}
            for _corner_tt, (_iv_tt, _mv_tt, _ov_tt) in TEMP_VARS.items():
                _ic = _ch(channels, _iv_tt)
                _mc = _ch(channels, _mv_tt)
                _oc = _ch(channels, _ov_tt)
                if _ic is None:
                    _row_tt[_corner_tt] = None
                    continue
                _im_tt = _ic[_lm_tt]
                _mm_tt = _mc[_lm_tt] if _mc is not None else _im_tt
                _om_tt = _oc[_lm_tt] if _oc is not None else _im_tt
                _all_tt = np.concatenate([_im_tt, _mm_tt, _om_tt])
                _valid_tt = _all_tt[_all_tt > 20]
                if len(_valid_tt) < 10:
                    _row_tt[_corner_tt] = None
                    continue
                _row_tt[_corner_tt] = round(_c_to_f(float(np.mean(_valid_tt))), 1)
            _trend_rows.append(_row_tt)
        if _trend_rows:
            out['tyre_trend'] = {'laps': _trend_rows}

    # ── 21. Ride heights (optional — not all cars expose these channels) ──────
    _rh_names = {'LF': 'LFrideHeight', 'RF': 'RFrideHeight',
                 'LR': 'LRrideHeight', 'RR': 'RRrideHeight'}
    _rh_out   = {}
    _any_rh   = False
    for _rh_corner, _rh_name in _rh_names.items():
        _rh_ch = _ch(channels, _rh_name)
        if _rh_ch is None:
            _rh_out[_rh_corner] = None
            continue
        _any_rh = True
        _rh_lm  = _rh_ch[mask]
        _rh_valid = _rh_lm[_rh_lm > 0.001]   # filter parked / zero
        if len(_rh_valid) < 100:
            _rh_out[_rh_corner] = None
            continue
        _rh_mm = round(float(np.mean(_rh_valid)) * 1000.0, 1)   # m → mm
        _rh_out[_rh_corner] = _rh_mm
        if _rh_mm < 15.0:
            recs.append({
                'category': 'Ride Height',
                'corner':   _rh_corner,
                'issue':    f'{_rh_corner} ride height {_rh_mm:.1f} mm — risk of bottoming',
                'action':   'Raise ride height by ≥5 mm or stiffen spring to reduce dynamic compression',
                'priority': 'high',
            })
    if _any_rh:
        out['ride_heights'] = _rh_out

    # ── 21b. Tech inspection status ──────────────────────────────────────────
    _tech_limits = (car_cfg or {}).get('tech_limits', {})
    _min_rh      = _tech_limits.get('min_ride_height_mm', {})
    if _min_rh and out.get('ride_heights'):
        _tech_corners = {}
        _tech_pass    = True
        for _corner, _rh_val in out['ride_heights'].items():
            _min = _min_rh.get(_corner)
            if _min is None:
                continue
            _measured = _rh_val if isinstance(_rh_val, (int, float)) and _rh_val is not None else 0
            _margin   = round(_measured - _min, 1)
            if _margin < 0:
                _status = 'fail'
                _tech_pass = False
            elif _margin < 5:
                _status = 'warning'
            else:
                _status = 'ok'
            _tech_corners[_corner] = {
                'measured_mm': round(_measured, 1),
                'min_mm':      _min,
                'margin_mm':   _margin,
                'status':      _status,
            }
        if _tech_corners:
            out['tech_status'] = {
                'pass':       _tech_pass,
                'corners':    _tech_corners,
                'series_note': _tech_limits.get('series_note', ''),
            }

    # ── 20. Throttle application points & corner minimum speeds ──────────────
    if (out.get('track_map') and throttle_ch is not None and brake_ch is not None
            and speed is not None and dist_pct is not None and lap is not None):
        _tm_lap   = out['track_map']['lap']
        _lap_int_ta = lap.astype(np.int32)
        _lm_ta    = _lap_int_ta == _tm_lap
        if _lm_ta.sum() >= 100:
            _spd_ta  = speed[_lm_ta] * MS_TO_MPH
            _thr_ta  = throttle_ch[_lm_ta].astype(float)
            _brk_ta  = brake_ch[_lm_ta].astype(float)
            _pct_ta  = dist_pct[_lm_ta].astype(float)
            _n_ta    = int(_lm_ta.sum())

            # ── Throttle application points ───────────────────────────────
            # Find indices where brake was active (>0.2) then went below 0.05,
            # and the first sample where throttle then exceeds 0.15.
            _tap_events = []
            _braking = False
            _post_brake = False
            for _i in range(_n_ta):
                if _brk_ta[_i] > 0.2:
                    _braking = True
                    _post_brake = False
                elif _braking and _brk_ta[_i] < 0.05:
                    _post_brake = True
                    _braking = False
                if _post_brake and _thr_ta[_i] > 0.15:
                    _tap_events.append(_i)
                    _post_brake = False

            # Map raw indices → normalised track map XY (use downsampled _idx array)
            # We re-build the downsampled index mapping: the track map has ≤800 pts
            # sampled with linspace over the lap. We find the closest downsampled pt.
            _tm_pts = out['track_map']['points']
            _n_ds   = len(_tm_pts)
            _ds_idx = np.round(np.linspace(0, _n_ta - 1, _n_ds)).astype(int)

            _throttle_apps = []
            for _raw_i in _tap_events:
                # Find the downsampled point whose raw index is closest to _raw_i
                _closest_ds = int(np.argmin(np.abs(_ds_idx - _raw_i)))
                _pt = _tm_pts[_closest_ds]
                _throttle_apps.append({
                    'x':   _pt['x'],
                    'y':   _pt['y'],
                    'pct': round(float(_pct_ta[_raw_i]), 3),
                    'spd': round(float(_spd_ta[_raw_i]), 1),
                })
            if _throttle_apps:
                out['track_map']['throttle_apps'] = _throttle_apps

            # ── Corner minimum speeds ─────────────────────────────────────
            # Find local speed minima that are at least 15 mph below local max,
            # deduplicated within a 3% lap-distance window. Cap at 15 corners.
            _wsize = max(1, _n_ta // 20)  # ~5% lap window for local context
            _corner_mins_raw = []
            for _i in range(_wsize, _n_ta - _wsize):
                _local_min = float(np.min(_spd_ta[max(0, _i - _wsize):_i + _wsize]))
                _local_max = float(np.max(_spd_ta[max(0, _i - _wsize):_i + _wsize]))
                if _spd_ta[_i] == _local_min and (_local_max - _local_min) >= 15:
                    _corner_mins_raw.append((_i, float(_spd_ta[_i]), float(_pct_ta[_i])))

            # Deduplicate: keep lowest speed within any 3% window
            _corner_mins_raw.sort(key=lambda _x: _x[2])  # sort by pct
            _deduped = []
            for _cm in _corner_mins_raw:
                if not _deduped or (_cm[2] - _deduped[-1][2]) > 0.03:
                    _deduped.append(_cm)
                elif _cm[1] < _deduped[-1][1]:
                    _deduped[-1] = _cm  # replace with slower

            # Sort by speed, keep top 15 slowest
            _deduped.sort(key=lambda _x: _x[1])
            _deduped = _deduped[:15]

            _corner_mins_out = []
            for _raw_i, _spd_v, _pct_v in _deduped:
                _closest_ds = int(np.argmin(np.abs(_ds_idx - _raw_i)))
                _pt = _tm_pts[_closest_ds]
                _corner_mins_out.append({
                    'x':   _pt['x'],
                    'y':   _pt['y'],
                    'pct': round(_pct_v, 3),
                    'spd': round(_spd_v, 1),
                })
            if _corner_mins_out:
                out['track_map']['corner_mins'] = _corner_mins_out

    # ── 22. Confidence scoring ────────────────────────────────────────────────
    _conf_expected = [
        'Speed', 'Throttle', 'Brake', 'Lap', 'LapDistPct',
        'LFtempCL', 'RFtempCL', 'LRtempCL', 'RRtempCL',
        'LFpressure', 'RFpressure', 'LRpressure', 'RRpressure',
        'LatAccel', 'LongAccel', 'SteeringWheelAngle',
    ]
    _conf_missing   = [c for c in _conf_expected if _ch(channels, c) is None]
    _conf_lap_count = len(out.get('lap_times') or [])
    _conf_issues    = list(_signal_warnings)
    if _conf_missing:
        _conf_issues.append(f"Missing channels: {', '.join(_conf_missing)}")
    if _conf_lap_count < 3:
        _conf_issues.append(
            f"Only {_conf_lap_count} flying lap(s) — more laps improve recommendation reliability"
        )
    if ambient_temp_f is None:
        _conf_issues.append(
            "No ambient temperature — cold pressure targets use uncorrected baseline"
        )
    # Quality score: starts at 1.0, penalised for missing channels and few laps
    _dq = 1.0 - (len(_conf_missing) / len(_conf_expected)) * 0.5
    _dq -= max(0, (5 - _conf_lap_count)) / 5.0 * 0.3
    _dq  = max(0.0, round(_dq, 2))
    out['confidence'] = {
        'data_quality':    _dq,
        'flying_laps':     _conf_lap_count,
        'missing_channels': _conf_missing,
        'issues':          _conf_issues,
    }
    if _signal_warnings:
        out['signal_warnings'] = _signal_warnings

    return out


def analyze_ibt(ibt_bytes, car_cfg=None, track_cfg=None,
                ambient_temp_f=None, cars_dict=None, tracks_dict=None):
    """
    Full pipeline for Pyodide: parse bytes → auto-detect car/track → analyze.

    Parameters
    ----------
    ibt_bytes      : bytes  — raw .ibt file content
    car_cfg        : dict   — car config (overrides auto-detection if provided)
    track_cfg      : dict   — track config (overrides auto-detection if provided)
    ambient_temp_f : float  — manual ambient temp override
    cars_dict      : dict   — {stem: config} for all cars (used for auto-detection)
    tracks_dict    : dict   — {stem: config} for all tracks (used for auto-detection)
    """
    import re as _re_ai

    from ibt_parser import parse_ibt_bytes
    channels, session_info, tick_rate, record_count = parse_ibt_bytes(bytes(ibt_bytes))

    # ── Auto ambient + track temp from session YAML ───────────────────────────
    track_temp_f = None
    if session_info:
        _at_m = _re_ai.search(r'AirTemp:\s*([\d.]+)\s*C', session_info)
        if _at_m and ambient_temp_f is None:
            ambient_temp_f = round(float(_at_m.group(1)) * 9 / 5 + 32, 1)
        _tt_m = _re_ai.search(r'TrackTemp:\s*([\d.]+)\s*C', session_info)
        if _tt_m:
            track_temp_f = round(float(_tt_m.group(1)) * 9 / 5 + 32, 1)

    # ── Auto-detect car and track from session YAML ───────────────────────────
    detected_car_id    = None
    detected_track_id  = None
    raw_car_path       = None
    raw_track_name     = None
    auto_detected_car  = False
    auto_detected_track = False

    if session_info and (cars_dict or tracks_dict):
        # Build lookup indexes from the provided dicts
        _car_idx   = {}
        _track_idx = {}

        def _as_list_ai(v):
            if isinstance(v, list): return v
            if v: return [v]
            return []

        if cars_dict:
            for _k, _cfg in cars_dict.items():
                _car_idx[_k.lower()] = _k
                for _p in _as_list_ai(_cfg.get('iracing_car_path', [])):
                    _car_idx[_p.lower()] = _k

        if tracks_dict:
            for _k, _cfg in tracks_dict.items():
                _track_idx[_k.lower()] = _k
                for _n in _as_list_ai(_cfg.get('iracing_track_name', [])):
                    _track_idx[_n.lower()] = _k

        # Track detection
        for _pat in (r'TrackName:\s*(.+)', r'TrackDisplayName:\s*(.+)'):
            _m = _re_ai.search(_pat, session_info)
            if _m:
                raw_track_name = _m.group(1).strip()
                _tn = raw_track_name.lower()
                detected_track_id = _track_idx.get(_tn)
                if detected_track_id is None:
                    for _k in _track_idx:
                        if _k in _tn or _tn in _k:
                            detected_track_id = _track_idx[_k]
                            break
                if detected_track_id:
                    break

        # Car detection
        _idx_m = _re_ai.search(r'DriverCarIdx:\s*(\d+)', session_info)
        _car_idx_str = _idx_m.group(1) if _idx_m else '0'
        _blk_m = _re_ai.search(
            r'(?<![A-Za-z])CarIdx:\s*' + _re_ai.escape(_car_idx_str) + r'\b.*?CarPath:\s*(\S+)',
            session_info, _re_ai.DOTALL
        )
        if _blk_m:
            raw_car_path = _blk_m.group(1).strip()
            _cp = raw_car_path.lower()
            detected_car_id = _car_idx.get(_cp)
            if detected_car_id is None:
                for _k in _car_idx:
                    if _k in _cp or _cp in _k:
                        detected_car_id = _car_idx[_k]
                        break

        if car_cfg is None and detected_car_id and cars_dict:
            car_cfg = cars_dict.get(detected_car_id)
            auto_detected_car = True

        if track_cfg is None and detected_track_id and tracks_dict:
            track_cfg = tracks_dict.get(detected_track_id)
            auto_detected_track = True

    # ── Run analysis ──────────────────────────────────────────────────────────
    result = analyze(channels, tick_rate, car_cfg=car_cfg, track_cfg=track_cfg,
                     ambient_temp_f=ambient_temp_f)

    result['meta'] = {
        'filename':       '',
        'tick_rate':      tick_rate,
        'record_count':   record_count,
        'session_type':   None,
        'ambient_temp_f': ambient_temp_f,
        'track_temp_f':   track_temp_f,
    }
    result['detected'] = {
        'car_id':              detected_car_id  if auto_detected_car   else None,
        'track_id':            detected_track_id if auto_detected_track else None,
        'auto_detected_car':   auto_detected_car,
        'auto_detected_track': auto_detected_track,
        'raw_car_path':        raw_car_path,
        'raw_track_name':      raw_track_name,
    }
    return result
