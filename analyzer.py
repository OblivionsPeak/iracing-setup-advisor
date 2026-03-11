#!/usr/bin/env python3
"""
Setup analyzer for iRacing telemetry.
Car and track knowledge is supplied via JSON config dicts — no hardcoding.
"""

import numpy as np

# ── Unit conversions ──────────────────────────────────────────────────────────
PA_TO_PSI = 1.0 / 6894.757
MS_TO_KPH = 3.6
G         = 9.80665   # m/s²

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
    """
    car   = car_cfg   or DEFAULT_CAR
    track = track_cfg or DEFAULT_TRACK

    target_hot_psi  = car['target_hot_psi']
    temp_min        = car['temp_min']
    temp_max        = car['temp_max']
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

    # ── 1. Tyre temperatures ──────────────────────────────────────────────────
    for corner, (iv, mv, ov) in TEMP_VARS.items():
        inner = _ch(channels, iv)
        mid   = _ch(channels, mv)
        outer = _ch(channels, ov)
        if inner is None:
            out['tyre_temps'][corner] = None
            continue

        im, mm, om = inner[mask], mid[mask], outer[mask]
        valid = (im > 20) & (mm > 20) & (om > 20)
        if valid.sum() < 100:
            out['tyre_temps'][corner] = None
            continue

        out['tyre_temps'][corner] = {
            'inner':  float(np.mean(im[valid])),
            'mid':    float(np.mean(mm[valid])),
            'outer':  float(np.mean(om[valid])),
            'avg':    float(np.mean(np.concatenate([im[valid], mm[valid], om[valid]]))),
            'spread': float(np.mean(om[valid]) - np.mean(im[valid])),
        }

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

    # ── 4. Recommendations ────────────────────────────────────────────────────

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
    for corner, td in out['tyre_temps'].items():
        if td is None:
            continue
        spread = td['spread']
        if abs(spread) < 8.0:
            continue
        adj = abs(spread) / 10.0
        if spread > 0:
            recs.append({
                'category': 'Camber',
                'corner':   corner,
                'issue':    f'Outer {spread:.1f} °C hotter than inner — tyre rolling onto outside edge',
                'action':   f'Add {adj:.1f}° negative camber to {corner}',
                'priority': 'high' if abs(spread) > 15 else 'medium',
            })
        else:
            recs.append({
                'category': 'Camber',
                'corner':   corner,
                'issue':    f'Inner {abs(spread):.1f} °C hotter than outer — too much negative camber',
                'action':   f'Reduce {corner} negative camber by {adj:.1f}°',
                'priority': 'high' if abs(spread) > 15 else 'medium',
            })

    # 4c. Temperature window
    for corner, td in out['tyre_temps'].items():
        if td is None:
            continue
        avg = td['avg']
        if avg < temp_min:
            recs.append({
                'category': 'Tyre Temperature',
                'corner':   corner,
                'issue':    f'Average {avg:.0f} °C below optimal window ({temp_min}–{temp_max} °C)',
                'action':   'Increase cold tyre pressure by 0.5 psi, or add a warm-up lap before pushing',
                'priority': 'medium',
            })
        elif avg > temp_max:
            recs.append({
                'category': 'Tyre Temperature',
                'corner':   corner,
                'issue':    f'Average {avg:.0f} °C above optimal window ({temp_min}–{temp_max} °C)',
                'action':   'Reduce cold tyre pressure by 0.5 psi, or soften the spring rate at this corner',
                'priority': 'medium',
            })

    # 4d. Front-rear balance
    temps  = out['tyre_temps']
    f_vals = [temps[c]['avg'] for c in ('LF', 'RF') if temps.get(c)]
    r_vals = [temps[c]['avg'] for c in ('LR', 'RR') if temps.get(c)]
    if f_vals and r_vals:
        f_avg = float(np.mean(f_vals))
        r_avg = float(np.mean(r_vals))
        out['balance']['front_avg']       = round(f_avg, 1)
        out['balance']['rear_avg']        = round(r_avg, 1)
        out['balance']['front_rear_diff'] = round(r_avg - f_avg, 1)

        adjusted = (r_avg - f_avg) - rear_bias_c
        if adjusted > 12:
            recs.append({
                'category': 'Balance',
                'corner':   'REAR',
                'issue':    f'Rear tyres {r_avg - f_avg:.1f} °C hotter than fronts (adjusted offset for {car.get("name","this car")}: {rear_bias_c:.0f} °C)',
                'action':   'Soften rear ARB, raise rear ride height, increase rear wing (if adjustable), or reduce rear toe-in',
                'priority': 'high',
            })
        elif adjusted < -12:
            recs.append({
                'category': 'Balance',
                'corner':   'FRONT',
                'issue':    f'Front tyres {f_avg - r_avg:.1f} °C hotter than rears — push/understeer',
                'action':   'Stiffen rear ARB, lower front ride height, increase front wing, or increase front toe-out slightly',
                'priority': 'high',
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

    # ── 5. Summary ────────────────────────────────────────────────────────────
    speed = _ch(channels, 'Speed')
    if speed is not None:
        s = speed[mask & (speed > 0)]
        if len(s):
            out['summary']['max_speed_kph'] = round(float(np.max(s))  * MS_TO_KPH, 1)
            out['summary']['avg_speed_kph'] = round(float(np.mean(s)) * MS_TO_KPH, 1)

    lap = _ch(channels, 'Lap')
    if lap is not None:
        out['summary']['laps_analysed'] = int(np.max(lap))

    out['summary']['duration_s'] = n // tick_rate if tick_rate > 0 else None

    return out
