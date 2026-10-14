import json, sys, glob, collections, statistics as st, re
def load(f):
    p=json.load(open(f)); nodes={n['id']:n for n in p['nodes']}
    parent={}
    for n in p['nodes']:
        for c in n.get('children',[]): parent[c]=n['id']
    self_us=collections.Counter()
    for sid, dt in zip(p['samples'], p['timeDeltas']): self_us[sid]+=dt
    return p, nodes, parent, self_us
def cat(cf):
    u=cf['url']; fn=cf['functionName']
    if fn in ('(program)','(garbage collector)','(idle)','(root)'): return fn
    if u.startswith('node:'): return 'node internals ('+u.split('/')[1] if '/' in u else u
    if '/chunks/' in u: return 'pi bundle: '+u.split('/chunks/')[1]
    if '/bundle/' in u: return 'pi bundle: '+u.split('/bundle/')[1]
    if 'node_modules' in u: return 'node_modules: '+u.split('node_modules/')[-1].split('/')[0]
    return u or '(native/'+fn+')'
for d in sys.argv[1:]:
    files=sorted(glob.glob(d+'/*.cpuprofile'))
    per=collections.defaultdict(list); tot=[]
    for f in files:
        p,nodes,parent,su=load(f)
        c=collections.Counter()
        for nid,us in su.items(): c[cat(nodes[nid]['callFrame'])]+=us
        T=sum(su.values()); tot.append(T)
        for k in set(list(c)+list(per)): per[k].append(c.get(k,0))
    print(f"\n=== {d.split('/')[-1]}: profiled main-thread time median {st.median(tot)/1000:.1f} ms (n={len(files)})")
    for k,v in sorted(per.items(), key=lambda kv:-st.median(kv[1]))[:14]:
        print(f"  {st.median(v)/1000:7.1f} ms  {k}")
