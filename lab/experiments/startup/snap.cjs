// Records cumulative process counters at key moments of the first turn.
const dc = require('diagnostics_channel'); const fs = require('fs');
const rd = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch { return ''; } };
let sent = 0, hdr = 0, done = 0;
function snap(tag) {
  const out = { tag, t: performance.now(), ru: process.resourceUsage(), mu: process.memoryUsage(),
    io: rd('/proc/self/io'), status: rd('/proc/self/status'), net: rd('/proc/self/net/dev'),
    smaps: rd('/proc/self/smaps_rollup'), fds: fs.readdirSync('/proc/self/fd').length,
    tasks: fs.readdirSync('/proc/self/task').map((t) => ({ comm: rd(`/proc/self/task/${t}/comm`).trim(), ss: rd(`/proc/self/task/${t}/schedstat`).trim() })) };
  fs.appendFileSync(process.env.SNAP_FILE, JSON.stringify(out) + '\n');
  if (tag === 'request_sent' && process.env.SNAP_KILL) process.kill(process.pid, 'SIGKILL');
}
dc.subscribe('undici:client:sendHeaders', () => { if (!sent++) snap('request_sent'); });
dc.subscribe('undici:request:headers', () => { if (!hdr++) snap('first_byte'); });
dc.subscribe('undici:request:trailers', () => { if (!done++) snap('response_done'); });
process.on('exit', () => snap('exit'));
