// In-process probe for pi (or any node process). Load with:
//   NODE_OPTIONS="--require /lab/probe/probe.cjs"
// Writes JSONL to $PROBE_DIR/probe-<pid>.jsonl:
//   start   process identity
//   s       periodic sample: own CPU (user/sys), reaped-children CPU, memory, heap,
//           event-loop utilization + delay, active handle counts
//   heap    V8 heap spaces (every PROBE_HEAP_MS)
//   gc      each GC pause (kind, duration)
//   spawn / exit / spawnSync / worker   every child process and worker thread pi creates
//   http    every outbound HTTP request (undici diagnostics channels: fetch + OpenAI SDK)
//   net     every outbound TCP connect
//   end     final resourceUsage() at exit
"use strict";
if (process.env.PROBE_OFF === "1") return;

const fs = require("fs");
const path = require("path");
const v8 = require("v8");
const { performance, monitorEventLoopDelay, PerformanceObserver } = require("perf_hooks");

const DIR = process.env.PROBE_DIR || "/lab/out/current";
const EVERY = Number(process.env.PROBE_MS || 100);
const HEAP_EVERY = Number(process.env.PROBE_HEAP_MS || 2000);
const now = () => performance.timeOrigin + performance.now();

let fd = null;
try {
	fs.mkdirSync(DIR, { recursive: true });
	fd = fs.openSync(path.join(DIR, `probe-${process.pid}.jsonl`), "a");
} catch (e) {
	return; // never break the host process
}
function out(rec) {
	try {
		fs.writeSync(fd, JSON.stringify(rec) + "\n");
	} catch {}
}

// ---------- /proc/self/stat: children CPU (ticks of reaped descendants) ----------
const CLK = 100; // USER_HZ on linux
function selfStat() {
	try {
		const s = fs.readFileSync("/proc/self/stat", "utf8");
		const f = s.slice(s.lastIndexOf(")") + 2).split(" ");
		// f[0]=state(3) ... utime=f[11] stime=f[12] cutime=f[13] cstime=f[14] threads=f[17]
		return { cut: Number(f[13]) / CLK, cst: Number(f[14]) / CLK, thr: Number(f[17]) };
	} catch {
		return { cut: 0, cst: 0, thr: 0 };
	}
}

out({
	k: "start",
	t: now(),
	pid: process.pid,
	ppid: process.ppid,
	argv: process.argv,
	execArgv: process.execArgv,
	node: process.version,
	cwd: process.cwd(),
	uid: process.getuid && process.getuid(),
});

// ---------- periodic sampling ----------
const hist = monitorEventLoopDelay({ resolution: 5 });
hist.enable();
let lastCpu = process.cpuUsage();
let lastElu = performance.eventLoopUtilization();
let lastT = now();
let lastHeapT = 0;

function sample(final) {
	const t = now();
	const cpu = process.cpuUsage(lastCpu);
	lastCpu = process.cpuUsage();
	const elu = performance.eventLoopUtilization(lastElu);
	lastElu = performance.eventLoopUtilization();
	const mem = process.memoryUsage();
	const hs = v8.getHeapStatistics();
	const st = selfStat();
	const handles = {};
	try {
		for (const r of process.getActiveResourcesInfo()) handles[r] = (handles[r] || 0) + 1;
	} catch {}
	out({
		k: "s",
		t,
		dt: t - lastT,
		u: cpu.user / 1000, // ms of user CPU in interval
		sy: cpu.system / 1000, // ms of system CPU in interval
		elu: +elu.utilization.toFixed(4),
		lag50: +(hist.percentile(50) / 1e6).toFixed(2),
		lag99: +(hist.percentile(99) / 1e6).toFixed(2),
		lagMax: +(hist.max / 1e6).toFixed(2),
		rss: mem.rss,
		heapUsed: mem.heapUsed,
		heapTotal: mem.heapTotal,
		ext: mem.external,
		ab: mem.arrayBuffers,
		mallocd: hs.malloced_memory,
		ctx: hs.number_of_native_contexts,
		cut: st.cut,
		cst: st.cst,
		thr: st.thr,
		h: handles,
		...(final ? { final: true } : {}),
	});
	hist.reset();
	lastT = t;
	if (t - lastHeapT >= HEAP_EVERY || final) {
		lastHeapT = t;
		out({
			k: "heap",
			t,
			spaces: v8.getHeapSpaceStatistics().map((s) => [s.space_name, s.space_used_size, s.space_size]),
		});
	}
}
const timer = setInterval(sample, EVERY);
timer.unref();
// process.resourceUsage() once a second: fs ops, page faults, context switches, max RSS
setInterval(() => {
	try {
		const ru = process.resourceUsage();
		out({ k: "ru", t: now(), maxRssKB: ru.maxRSS, fsRead: ru.fsRead, fsWrite: ru.fsWrite, minflt: ru.minorPageFault, majflt: ru.majorPageFault, volCtx: ru.voluntaryContextSwitches, involCtx: ru.involuntaryContextSwitches, userMs: ru.userCPUTime / 1000, sysMs: ru.systemCPUTime / 1000 });
	} catch {}
}, 1000).unref();

// ---------- GC ----------
try {
	const kinds = { 1: "minor", 2: "major", 4: "incremental", 8: "weakcb", 16: "minor-ms" };
	const obs = new PerformanceObserver((list) => {
		for (const e of list.getEntries()) {
			const kind = e.detail ? e.detail.kind : e.kind;
			out({ k: "gc", t: performance.timeOrigin + e.startTime, dur: +e.duration.toFixed(3), kind: kinds[kind] || kind });
		}
	});
	obs.observe({ entryTypes: ["gc"] });
} catch {}

// ---------- child processes ----------
const cp = require("child_process");
let spawnSeq = 0;
const trunc = (s, n = 400) => (s && s.length > n ? s.slice(0, n) + `…(+${s.length - n})` : s);
function describe(file, args) {
	return { file: String(file), args: trunc(Array.isArray(args) ? args.map(String).join(" ") : "") };
}
function wrapAsync(name) {
	const orig = cp[name];
	if (typeof orig !== "function") return;
	cp[name] = function (file, args, opts) {
		const id = ++spawnSeq;
		const t0 = now();
		let a = Array.isArray(args) ? args : [];
		let o = Array.isArray(args) ? opts : args;
		if (name === "exec") a = [];
		const child = orig.apply(this, arguments);
		const rec = { k: "spawn", t: t0, id, fn: name, ...describe(file, a), detached: !!(o && typeof o === "object" && o.detached), cwd: o && typeof o === "object" ? o.cwd : undefined, ppid: process.pid };
		try {
			if (child && child.pid) out({ ...rec, pid: child.pid });
			else if (child)
				child.once("spawn", () => out({ ...rec, pid: child.pid, tSpawned: now() }));
			if (child)
				child.once("exit", (code, signal) => out({ k: "exit", t: now(), id, pid: child.pid, code, signal, durMs: +(now() - t0).toFixed(2) }));
			if (child) child.once("error", (err) => out({ k: "spawnerr", t: now(), id, err: String(err && err.message) }));
		} catch {}
		return child;
	};
}
function wrapSync(name) {
	const orig = cp[name];
	if (typeof orig !== "function") return;
	cp[name] = function (file, args) {
		const t0 = now();
		const a = Array.isArray(args) ? args : [];
		let status;
		try {
			const r = orig.apply(this, arguments);
			status = r && typeof r === "object" && "status" in r ? r.status : 0;
			return r;
		} catch (e) {
			status = "throw:" + (e && e.code);
			throw e;
		} finally {
			out({ k: "spawnSync", t: t0, fn: name, ...describe(file, a), durMs: +(now() - t0).toFixed(2), status });
		}
	};
}
["spawn", "execFile", "exec", "fork"].forEach(wrapAsync);
["spawnSync", "execFileSync", "execSync"].forEach(wrapSync);

// worker threads
try {
	const wt = require("worker_threads");
	const OrigWorker = wt.Worker;
	wt.Worker = class ProbedWorker extends OrigWorker {
		constructor(filename, options) {
			const t0 = now();
			super(filename, options);
			out({ k: "worker", t: t0, file: String(filename), threadId: this.threadId });
			this.once("exit", (code) => out({ k: "workerexit", t: now(), threadId: this.threadId, code }));
		}
	};
} catch {}

try {
	require("module").syncBuiltinESMExports();
} catch {}

// ---------- network ----------
try {
	const dc = require("diagnostics_channel");
	const reqs = new WeakMap();
	dc.subscribe("undici:request:create", ({ request }) => {
		reqs.set(request, { t0: now(), origin: String(request.origin), method: request.method, path: trunc(request.path, 120) });
	});
	dc.subscribe("undici:request:headers", ({ request, response }) => {
		const r = reqs.get(request);
		if (r) {
			r.status = response.statusCode;
			r.ttfb = +(now() - r.t0).toFixed(2);
		}
	});
	dc.subscribe("undici:request:trailers", ({ request }) => {
		const r = reqs.get(request);
		if (r) out({ k: "http", t: r.t0, origin: r.origin, method: r.method, path: r.path, status: r.status, ttfb: r.ttfb, durMs: +(now() - r.t0).toFixed(2) });
	});
	dc.subscribe("undici:request:error", ({ request, error }) => {
		const r = reqs.get(request);
		out({ k: "http", t: r ? r.t0 : now(), origin: r && r.origin, path: r && r.path, error: String(error && error.message), durMs: r ? +(now() - r.t0).toFixed(2) : null });
	});
	dc.subscribe("net.client.socket", ({ socket }) => {
		const t0 = now();
		socket.once("connect", () => out({ k: "net", t: t0, host: socket.remoteAddress, port: socket.remotePort, connectMs: +(now() - t0).toFixed(2) }));
		// bytes moved over the connection's whole life (keep-alive reuse included)
		socket.once("close", () => out({ k: "sockclose", t: now(), opened: t0, host: socket.remoteAddress, port: socket.remotePort, bytesRead: socket.bytesRead, bytesWritten: socket.bytesWritten, lifeMs: +(now() - t0).toFixed(1) }));
	});
	dc.subscribe("http.client.request.start", ({ request }) => {
		out({ k: "http1", t: now(), host: request.host, method: request.method, path: trunc(request.path, 120) });
	});
} catch {}

// ---------- pi JSON/RPC event timestamps (stdout tap) ----------
// pi's output guard binds the stdout.write it finds at startup, so wrapping it here
// sees every JSONL event. message_update (per token) is only counted, everything
// else is recorded with an in-process timestamp.
let tokenEvents = 0;
if (process.env.PROBE_EVENTS !== "0") {
	try {
		const so = process.stdout;
		const origWrite = so.write;
		const typeRe = /^\{"type":"([a-z_]+)"/;
		const idRe = /"toolCallId":"([^"]+)"/;
		const nameRe = /"toolName":"([^"]+)"/;
		so.write = function (chunk, enc, cb) {
			try {
				const s = typeof chunk === "string" ? chunk : Buffer.isBuffer(chunk) ? chunk.toString("utf8", 0, Math.min(chunk.length, 400)) : "";
				const m = typeRe.exec(s);
				if (m) {
					if (m[1] === "message_update") tokenEvents++;
					else {
						const head = s.slice(0, 400);
						const id = idRe.exec(head);
						const nm = nameRe.exec(head);
						out({ k: "ev", t: now(), type: m[1], ...(id ? { id: id[1] } : {}), ...(nm ? { tool: nm[1] } : {}), bytes: typeof chunk === "string" ? chunk.length : chunk.length });
					}
				}
			} catch {}
			return origWrite.apply(this, arguments);
		};
		setInterval(() => {
			if (tokenEvents) {
				out({ k: "tok", t: now(), n: tokenEvents });
				tokenEvents = 0;
			}
		}, 250).unref();
	} catch {}
}

// ---------- exit ----------
process.on("exit", (code) => {
	try {
		sample(true);
		const ru = process.resourceUsage();
		out({ k: "end", t: now(), code, maxRssKB: ru.maxRSS, userMs: ru.userCPUTime / 1000, sysMs: ru.systemCPUTime / 1000, fsRead: ru.fsRead, fsWrite: ru.fsWrite, volCtx: ru.voluntaryContextSwitches, involCtx: ru.involuntaryContextSwitches, ...selfStat() });
	} catch {}
});
