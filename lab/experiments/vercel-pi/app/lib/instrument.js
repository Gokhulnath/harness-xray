// Per-instance and per-request measurements that work inside a Vercel function (Linux /proc + Node APIs).
import { readFileSync, statfsSync } from "node:fs";

const g = globalThis;
g.__lab ??= {
	instanceId: Math.random().toString(36).slice(2, 10),
	bootedAt: Date.now(),
	bootCpuMs: cpuMs(),
	invocations: 0,
	inFlight: 0,
	maxInFlight: 0,
};
export const lab = g.__lab;

export function cpuMs() {
	const u = process.cpuUsage();
	return (u.user + u.system) / 1000;
}

const TICK_MS = 10; // CLK_TCK 100 on Linux
/** CPU of this process and of its reaped children (tool processes), from /proc/self/stat. */
export function procStat() {
	try {
		const s = readFileSync("/proc/self/stat", "utf8");
		const f = s.slice(s.lastIndexOf(")") + 2).split(" ");
		// fields after comm: index 0 = state(3) ... utime(14)=11, stime(15)=12, cutime(16)=13, cstime(17)=14, num_threads(20)=17
		return { utimeMs: +f[11] * TICK_MS, stimeMs: +f[12] * TICK_MS, childMs: (+f[13] + +f[14]) * TICK_MS, threads: +f[17] };
	} catch {
		return null;
	}
}

export function memStatus() {
	try {
		const s = readFileSync("/proc/self/status", "utf8");
		const kb = (k) => {
			const m = s.match(new RegExp(`^${k}:\\s+(\\d+) kB`, "m"));
			return m ? +(+m[1] / 1024).toFixed(1) : null;
		};
		return { rssMiB: kb("VmRSS"), peakRssMiB: kb("VmHWM"), anonMiB: kb("RssAnon"), fileMiB: kb("RssFile") };
	} catch {
		const m = process.memoryUsage();
		return { rssMiB: +(m.rss / 1048576).toFixed(1) };
	}
}

/** Machine-level memory (/proc/meminfo) and free space in /tmp: what tool processes compete for. */
export function system() {
	const out = {};
	try {
		const mi = readFileSync("/proc/meminfo", "utf8");
		const kb = (k) => +(mi.match(new RegExp(`^${k}:\\s+(\\d+) kB`, "m"))?.[1] ?? NaN);
		out.memTotalMiB = Math.round(kb("MemTotal") / 1024);
		out.memAvailableMiB = Math.round(kb("MemAvailable") / 1024);
	} catch {}
	try {
		const s = statfsSync("/tmp");
		out.tmpFreeMiB = Math.round((s.bavail * s.bsize) / 1048576);
	} catch {}
	return out;
}

export function snapshot() {
	const m = process.memoryUsage();
	return {
		instanceId: lab.instanceId,
		instanceAgeS: +((Date.now() - lab.bootedAt) / 1000).toFixed(1),
		invocations: lab.invocations,
		inFlight: lab.inFlight,
		cpuMs: +cpuMs().toFixed(1),
		bootCpuMs: +lab.bootCpuMs.toFixed(1),
		heapUsedMiB: +(m.heapUsed / 1048576).toFixed(1),
		...memStatus(),
		proc: procStat(),
		region: process.env.VERCEL_REGION || null,
		system: system(),
	};
}

/** Wrap one request: returns a finish() that reports deltas for this request. */
export function begin() {
	lab.invocations++;
	lab.inFlight++;
	lab.maxInFlight = Math.max(lab.maxInFlight, lab.inFlight);
	const cold = lab.invocations === 1;
	const t0 = performance.now();
	const c0 = cpuMs();
	const p0 = procStat();
	const m0 = memStatus();
	return () => {
		lab.inFlight--;
		const p1 = procStat();
		const m1 = memStatus();
		return {
			cold,
			instanceId: lab.instanceId,
			wallMs: Math.round(performance.now() - t0),
			processCpuMs: +(cpuMs() - c0).toFixed(1),
			toolChildCpuMs: p0 && p1 ? p1.childMs - p0.childMs : null,
			rssBeforeMiB: m0.rssMiB,
			rssAfterMiB: m1.rssMiB,
			peakRssMiB: m1.peakRssMiB,
			concurrentAtEnd: lab.inFlight,
			maxInFlight: lab.maxInFlight,
		};
	};
}
