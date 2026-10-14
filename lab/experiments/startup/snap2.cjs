const dc = require('diagnostics_channel'); const fs = require('fs');
let sent = 0;
dc.subscribe('undici:client:sendHeaders', () => {
  if (sent++) return;
  const ru = process.resourceUsage();
  fs.appendFileSync(process.env.SNAP_FILE, JSON.stringify({ abs: performance.timeOrigin + performance.now(), t: performance.now(),
    cpu: (ru.userCPUTime + ru.systemCPUTime) / 1000, rss: process.memoryUsage().rss }) + '\n');
});
