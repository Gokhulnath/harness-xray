/** pi loads as a normal Node module (not through webpack); Vercel's file tracing ships it.
 *  e2b is bundled instead: as an external, Next require()s its CJS build, which require()s ESM-only chalk,
 *  and Vercel's Node runtime rejects require() of ESM. */
export default {
	serverExternalPackages: ["@earendil-works/pi-coding-agent"],
};
