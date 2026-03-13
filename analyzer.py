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


def analyze(channels, tick_rate, car_cfg=None, track_cfg=None):
    """
    Analyse iRacing telemetry.

    Parameters
    ----------
    channels   : dict returned by ibt_parser.parse_ibt()
    tick_rate  : int — samples per second
    car_cfg    : dict loaded from cars/<name>.json  (falls back to DEFAULT_CAR)
    track_cfg  : dict loaded from tracks/<name>.json (falls back to DEFAULT_TRACK)

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
                out['handling'][sector_name] = {'tendency': 'insufficient data', 'index': None}
                raw_indices.append(None)
                continue
            us_idx = float(np.mean(
                np.abs(steer_m[in_sector]) / (np.abs(lat_g[in_sector]) + 0.01)
            ))
            out['handling'][sector_name] = {'tendency': None, 'index': us_idx}
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

    # ── 4. Recommendations (logic in °C, display strings in °F) ──────────────

    # 4a. Tyre pressure
    for corner, hot_psi in out['tyre_pressures'].items():
        if hot_psi is None:
            continue
        target = target_hot_psi[corner]
        diff   = hot_psi - target
        if abs(diff) > 0.5:
            cold_adj  = -diff * 0.6
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
                'issue':    f'Rear tyres {diff_f:.1f} °F hotter than fronts (adjusted offset for {car.get("name","this car")}: {round(_diff_to_f(rear_bias_c))} °F)',
                'action':   'Soften rear ARB, raise rear ride height, increase rear wing (if adjustable), or reduce rear toe-in',
                'priority': 'high',
            })
        elif adjusted < -12:
            recs.append({
                'category': 'Balance',
                'corner':   'FRONT',
                'issue':    f'Front tyres {_diff_to_f(f_avg - r_avg):.1f} °F hotter than rears — push/understeer',
                'action':   'Stiffen rear ARB, lower front ride height, increase front wing, or increase front toe-out slightly',
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

    # 4e. Sector handling
    for sector_name, sd in out['handling'].items():
        tendency = sd.get('tendency')
        if tendency == 'understeer':
            recs.append({
                'category': 'Handling',
                'corner':   sector_name,
                'issue':    f'Above-average understeer in {sector_name}',
                'action':   'Soften front spring or ARB, add front camber, increase rear toe-in, or shift brake bias slightly rearward',
                'priority': 'medium',
            })
        elif tendency == 'oversteer':
            recs.append({
                'category': 'Handling',
                'corner':   sector_name,
                'issue':    f'Oversteer tendency in {sector_name}',
                'action':   'Stiffen rear spring or ARB, add rear camber, check rear toe-in, or shift brake bias slightly forward',
                'priority': 'medium',
            })

    _pri = {'high': 0, 'medium': 1, 'low': 2}
    recs.sort(key=lambda r: _pri.get(r['priority'], 3))

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
        changes  = np.where(np.diff(_lap_int) > 0)[0] + 1
        laps_list = []
        for idx in changes:
            lap_num  = int(_lap_int[idx]) - 1   # lap that just completed
            lt       = float(lap_time_ch[idx])
            if lap_num >= 1 and 20.0 < lt < 600.0:
                laps_list.append({'lap': lap_num, 'time_s': round(lt, 3)})
        if laps_list:
            out['lap_times'] = laps_list

    return out
