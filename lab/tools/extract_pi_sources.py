#!/usr/bin/env python3
"""Recover the TypeScript sources embedded in an npm package's source maps (sourcesContent).
  python3 extract_pi_sources.py <package-dir> <out-dir>
Used by lab.sh to build the 500+ file workspace repo that the grep/find scenarios search."""
import json, os, sys, glob
root = sys.argv[1]; out = sys.argv[2]
n = 0; seen = set()
for mp in glob.glob(os.path.join(root, '**', '*.js.map'), recursive=True):
    if '/bundle/' in mp: continue
    try: m = json.load(open(mp))
    except Exception: continue
    srcs = m.get('sources') or []; cont = m.get('sourcesContent') or []
    rel_pkg = os.path.relpath(os.path.dirname(mp), root)
    for s, c in zip(srcs, cont):
        if c is None: continue
        p = os.path.normpath(os.path.join(os.path.dirname(mp), s))
        p = os.path.relpath(p, root)
        if p.startswith('..') or 'node_modules/' in p.replace('node_modules/@earendil-works/','@EW/'):
            if not p.startswith('node_modules/@earendil-works/'): continue
        dest = os.path.join(out, p.replace('node_modules/@earendil-works/', 'packages/'))
        if dest in seen: continue
        seen.add(dest); os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, 'w').write(c); n += 1
print('wrote', n)
