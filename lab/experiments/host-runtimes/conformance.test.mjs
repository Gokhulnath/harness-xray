// pi-durable's own storage conformance suite, run against the bun:sqlite adapter with bun:test.
import { describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { registerStorageConformance } from "@earendil-works/pi-durable/testing";
import { openBunSqliteStorage } from "./bun-sqlite.mjs";

registerStorageConformance({ describe, expect, it }, "bun:sqlite", async (use) => {
	const dir = mkdtempSync(`${tmpdir()}/conf-`);
	try {
		await use(await openBunSqliteStorage(`${dir}/db.sqlite`));
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
