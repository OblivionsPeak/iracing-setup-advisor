#!/usr/bin/env python3
"""
build_web.py — regenerate docs/ for GitHub Pages deployment.
Run this after adding/changing car or track configs, or after updating analyzer.py / ibt_parser.py.
"""
import json, os, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, 'docs')
os.makedirs(DOCS, exist_ok=True)

def load_dir(subdir):
    out = {}
    folder = os.path.join(BASE, subdir)
    for fname in sorted(os.listdir(folder)):
        if fname.endswith('.json'):
            stem = fname[:-5]
            with open(os.path.join(folder, fname), encoding='utf-8') as f:
                out[stem] = json.load(f)
    return out

# Generate cars_data.js
cars = load_dir('cars')
with open(os.path.join(DOCS, 'cars_data.js'), 'w', encoding='utf-8') as f:
    f.write('window.CARS_DATA = ')
    json.dump(cars, f, ensure_ascii=False, indent=2)
    f.write(';\n')
print(f'  cars_data.js   — {len(cars)} cars')

# Generate tracks_data.js
tracks = load_dir('tracks')
with open(os.path.join(DOCS, 'tracks_data.js'), 'w', encoding='utf-8') as f:
    f.write('window.TRACKS_DATA = ')
    json.dump(tracks, f, ensure_ascii=False, indent=2)
    f.write(';\n')
print(f'  tracks_data.js — {len(tracks)} tracks')

# Copy Python modules
for src_name in ('ibt_parser.py', 'analyzer.py'):
    shutil.copy2(os.path.join(BASE, src_name), os.path.join(DOCS, src_name))
    print(f'  {src_name} copied')

# Read version
try:
    with open(os.path.join(BASE, 'version.txt')) as f:
        ver = f.read().strip()
except OSError:
    ver = '?.?.?'

print(f'\ndocs/ ready — v{ver}')
