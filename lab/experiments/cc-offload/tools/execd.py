#!/usr/bin/env python3
"""Exec server for the tools container: the far end of Claude Code's offload hooks.

  POST /exec   {"cmd", "cwd", "timeout"}       -> combined stdout+stderr, header X-Exit-Code
  GET  /file?path=ABS                          -> file bytes (404 if missing), header X-Sha256
  PUT  /file?path=ABS                          <- file bytes (parent folders created)
  POST /rg     {"args": [...], "cwd", "limit"} -> ripgrep output (first `limit` lines), header X-Exit-Code

Commands run with bash -lc in the tools container, on its own local copy of the workspace.
"""
import hashlib, json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.reply(200, b"ok")
        if u.path == "/file":
            p = parse_qs(u.query).get("path", [""])[0]
            try:
                data = open(p, "rb").read()
            except (FileNotFoundError, IsADirectoryError):
                return self.reply(404)
            return self.reply(200, data, {"X-Sha256": hashlib.sha256(data).hexdigest()})
        self.reply(404)

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path != "/file":
            return self.reply(404)
        p = parse_qs(u.query).get("path", [""])[0]
        data = self.body()
        os.makedirs(os.path.dirname(p) or "/", exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        self.reply(200, b"ok")

    def do_POST(self):
        u = urlparse(self.path)
        req = json.loads(self.body() or b"{}")
        if u.path == "/exec":
            try:
                r = subprocess.run(["bash", "-lc", req["cmd"]], cwd=req.get("cwd") or "/work", stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=req.get("timeout") or 600)
                out, code = r.stdout, r.returncode
            except subprocess.TimeoutExpired as e:
                out, code = (e.stdout or b"") + b"\n[timed out]\n", 124
            return self.reply(200, out, {"X-Exit-Code": code})
        if u.path == "/rg":
            r = subprocess.run(["rg", *req["args"]], cwd=req.get("cwd") or "/work", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            lines = r.stdout.splitlines(keepends=True)
            limit = req.get("limit")
            out = b"".join(lines[:limit] if limit else lines)
            return self.reply(200, out, {"X-Exit-Code": r.returncode, "X-Total-Lines": len(lines)})
        self.reply(404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7070
    print(f"execd on {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
