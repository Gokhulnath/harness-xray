/** pi is a large ESM package with its own bundle: load it as a normal Node module, not through webpack.
 *  PRELOAD=0 stops Next.js from loading every route (and so pi) at server start, to measure each separately. */
export default {
	serverExternalPackages: ["@earendil-works/pi-coding-agent"],
	experimental: { preloadEntriesOnStart: process.env.PRELOAD !== "0" },
};
