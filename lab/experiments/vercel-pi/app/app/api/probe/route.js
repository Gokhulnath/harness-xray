// What does a Vercel function actually contain? Binaries, OS, limits, and which env var NAMES a child
// process would inherit (values are never returned).
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, readFileSync, statfsSync } from "node:fs";
import os from "node:os";
import { snapshot } from "../../../lib/instrument.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const read = (p) => {
	try {
		return readFileSync(p, "utf8");
	} catch {
		return null;
	}
};

export async function GET() {
	const which = {};
	for (const b of ["bash", "sh", "node", "npm", "git", "python3", "rg", "fd", "curl", "tar", "ls", "cat", "grep", "sed"]) {
		const r = spawnSync("/bin/sh", ["-c", `command -v ${b}`], { encoding: "utf8" });
		which[b] = r.status === 0 ? r.stdout.trim() : null;
	}
	let childEnvNames = null;
	try {
		childEnvNames = execFileSync("/bin/sh", ["-c", "env | cut -d= -f1 | sort"], { encoding: "utf8" }).split("\n").filter(Boolean);
	} catch (e) {
		childEnvNames = String(e?.message || e);
	}
	let tmp = null;
	try {
		const s = statfsSync("/tmp");
		tmp = { totalMiB: Math.round((s.blocks * s.bsize) / 1048576), freeMiB: Math.round((s.bavail * s.bsize) / 1048576) };
	} catch {}
	const meminfo = read("/proc/meminfo");
	return Response.json({
		node: process.version,
		execPath: process.execPath,
		cwd: process.cwd(),
		uid: process.getuid?.(),
		platform: `${os.type()} ${os.release()} ${os.arch()}`,
		osRelease: read("/etc/os-release")?.split("\n").find((l) => l.startsWith("PRETTY_NAME")) || null,
		cpus: os.cpus().length,
		cpuModel: os.cpus()[0]?.model,
		memTotalMiB: meminfo ? Math.round(+meminfo.match(/MemTotal:\s+(\d+)/)[1] / 1024) : null,
		cgroupMemMax: read("/sys/fs/cgroup/memory.max")?.trim() || null,
		cgroupCpuMax: read("/sys/fs/cgroup/cpu.max")?.trim() || null,
		tmp,
		rootWritable: (() => {
			try {
				execFileSync("/bin/sh", ["-c", "touch /var/task/.probe 2>/dev/null && echo yes || echo no"], { encoding: "utf8" });
				return existsSync("/var/task/.probe");
			} catch {
				return false;
			}
		})(),
		which,
		path: process.env.PATH,
		childEnvNames,
		secretsVisibleToTools: Array.isArray(childEnvNames) ? childEnvNames.filter((n) => /KEY|TOKEN|SECRET|PASSWORD/i.test(n)) : null,
		snapshot: snapshot(),
	});
}
