import json, sys, re, glob, statistics as st, collections
D=sys.argv[1]
def kv(text, sep=':'):
    d={}
    for l in text.splitlines():
        if sep in l:
            k,v=l.split(sep,1); m=re.match(r'\s*(\d+)',v)
            if m: d[k.strip()]=int(m.group(1))
    return d
def eth(net):
    for l in net.splitlines():
        if 'eth0' in l:
            f=l.split(':')[1].split(); return dict(rxB=int(f[0]),rxP=int(f[1]),txB=int(f[8]),txP=int(f[9]))
    return dict(rxB=0,rxP=0,txB=0,txP=0)
def metrics(s, base):
    ru=s['ru']; io=kv(s['io']); stt=kv(s['status']); sm=kv(s['smaps']); n=eth(s['net']); b=eth(base['net']) if base else None
    th=collections.Counter(); cnt=collections.Counter(); rq=0
    for t in s['tasks']:
        name=re.sub(r'\d+$','',t['comm']).strip(); f=t['ss'].split()
        if len(f)>=3: th[name]+=int(f[0])/1e6; rq+=int(f[1])/1e6; cnt[name]+=1
    m=dict(wall_ms=s['t'], cpu_ms=(ru['userCPUTime']+ru['systemCPUTime'])/1000, user_ms=ru['userCPUTime']/1000, sys_ms=ru['systemCPUTime']/1000,
           rss_mib=s['mu']['rss']/2**20, peak_rss_mib=ru['maxRSS']/1024, heap_used_mib=s['mu']['heapUsed']/2**20, heap_total_mib=s['mu']['heapTotal']/2**20,
           external_mib=s['mu']['external']/2**20, rss_anon_mib=stt.get('RssAnon',0)/1024, rss_file_mib=stt.get('RssFile',0)/1024,
           pss_mib=sm.get('Pss',0)/1024, private_mib=(sm.get('Private_Clean',0)+sm.get('Private_Dirty',0))/1024,
           minflt=ru['minorPageFault'], majflt=ru['majorPageFault'], vol_cs=ru['voluntaryContextSwitches'], invol_cs=ru['involuntaryContextSwitches'],
           rchar_kb=io.get('rchar',0)/1000, wchar_kb=io.get('wchar',0)/1000, read_syscalls=io.get('syscr',0), write_syscalls=io.get('syscw',0),
           disk_read_b=io.get('read_bytes',0), disk_write_b=io.get('write_bytes',0), fsWrite_blocks=ru['fsWrite'],
           threads_live=stt.get('Threads',0), fds=s['fds'], runq_ms_live=rq)
    for k in ['pi','MainThread','V8Worker','libuv-worker','SignalInspector','DelayedTaskSche','node']:
        if k in th: m['cpu_thread_'+k]=th[k]
    if b: m.update(net_tx_b=n['txB']-b['txB'], net_rx_b=n['rxB']-b['rxB'], net_tx_pkts=n['txP']-b['txP'], net_rx_pkts=n['rxP']-b['rxP'])
    return m, cnt
for label in ['broken','cached']:
    rows=[json.loads(l) for l in open(f'{D}/{label}.jsonl')]
    runs=[rows[i:i+4] for i in range(0,len(rows),4)]
    agg=collections.defaultdict(lambda: collections.defaultdict(list)); thr=None
    prev_exit=None
    for r in runs:
        tags=[x['tag'] for x in r]; assert tags==['request_sent','first_byte','response_done','exit'], tags
        for s in r:
            m,cnt=metrics(s, prev_exit)
            if s['tag']=='request_sent': thr=cnt
            for k,v in m.items(): agg[s['tag']][k].append(v)
        prev_exit=r[-1]
    print(f"\n#### {label}  (n={len(runs)} runs; net deltas n={len(runs)-1})  threads at request: {dict(thr)}")
    keys=list(agg['exit'].keys())
    print(f"{'metric':<22}" + ''.join(f"{t:>15}" for t in ['request_sent','first_byte','response_done','exit']))
    for k in keys:
        vals=[st.median(agg[t][k]) if agg[t].get(k) else float('nan') for t in ['request_sent','first_byte','response_done','exit']]
        print(f"{k:<22}" + ''.join(f"{v:15.2f}" for v in vals))
def strace(files):
    tot=collections.defaultdict(list); calls=[]
    for f in files:
        txt=open(f).read(); c={}
        for l in txt.splitlines():
            p=l.split()
            if len(p)>=5 and p[0][0].isdigit() and p[-1].isidentifier() and p[-1]!='total':
                c[p[-1]]=int(p[3])
        for k,v in c.items(): tot[k].append(v)
        calls.append(sum(c.values()))
    return st.median(calls), sorted(((k,st.median(v)) for k,v in tot.items()), key=lambda kv:-kv[1])[:12]
for label in ['broken','cached']:
    a=strace(sorted(glob.glob(f'{D}/{label}-strace-[0-9].txt'))); b=strace(sorted(glob.glob(f'{D}/{label}-strace-full-*.txt')))
    print(f"\n{label}: syscalls until request sent (median of 5): {a[0]:.0f}  | whole run: {b[0]:.0f}")
    print("  until request:", ', '.join(f"{k} {v:.0f}" for k,v in a[1]))
    print("  whole run:    ", ', '.join(f"{k} {v:.0f}" for k,v in b[1]))
