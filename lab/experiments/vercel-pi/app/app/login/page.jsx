"use client";
import { useState } from "react";

export default function Login() {
	const [token, setToken] = useState("");
	const [err, setErr] = useState("");
	async function submit(e) {
		e.preventDefault();
		setErr("");
		const r = await fetch("/api/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token: token.trim() }) });
		if (r.ok) location.href = "/";
		else setErr("That token didn't match.");
	}
	return (
		<main className="login">
			<form onSubmit={submit}>
				<h1>harness-xray lab</h1>
				<p>Paste the lab token (on your Mac: <code>pbcopy &lt; ~/.config/agent-lab/lab-token</code>).</p>
				<input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="lab token" autoFocus />
				<button disabled={!token.trim()}>Enter</button>
				{err ? <p className="error">{err}</p> : null}
			</form>
		</main>
	);
}
