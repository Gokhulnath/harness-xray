import json, sys, glob, collections, statistics as st, re, bisect
src=open(sys.argv[2], encoding='utf-8').read()
lines=src.split('\n'); off=[0]
for l in lines: off.append(off[-1]+len(l)+1)
marks=[(m.start(), m.group(1)) for m in re.finditer(r'"((?:\.\./)*(?:node_modules|src|packages)/[^"]{3,160})"\(', src)]
mpos=[m[0] for m in marks]
def where(line,col):
    o=off[line]+col; i=bisect.bisect_right(mpos,o)-1
    return o, (marks[i][1] if i>=0 and o-mpos[i]<60000 else '-')
agg=collections.defaultdict(list); n=0
for f in sorted(glob.glob(sys.argv[1]+'/*.cpuprofile')):
    p=json.load(open(f)); nodes={x['id']:x for x in p['nodes']}; su=collections.Counter()
    for sid,dt in zip(p['samples'],p['timeDeltas']): su[sid]+=dt
    c=collections.Counter()
    for nid,us in su.items():
        cf=nodes[nid]['callFrame']
        if 'chunk-OJP47DM6' in cf['url']: c[(cf['functionName'],cf['lineNumber'],cf['columnNumber'])]+=us
    for k in set(c)|set(agg): agg[k].append(c.get(k,0))
    n+=1
rows=sorted(agg.items(), key=lambda kv:-sum(kv[1]))[:22]
for (fn,l,cn),v in rows:
    o,m=where(l,cn); snip=src[o:o+70].replace('\n',' ')
    print(f"{sum(v)/n/1000:6.2f} ms  fn={fn or '(top-level)':<14.14} L{l}:{cn:<8} near={m[-45:]:<45} | {snip}")
