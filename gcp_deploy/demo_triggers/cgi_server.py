#!/usr/bin/env python3
import http.server, subprocess, os

CGI_DIR = "/www/cgi-bin"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_cgi()

    def do_POST(self):
        self.handle_cgi()

    def handle_cgi(self):
        if not self.path.startswith("/cgi-bin/"):
            self.send_response(404); self.end_headers(); return

        script_name = self.path.split("?")[0].rsplit("/", 1)[-1]
        script_path = os.path.join(CGI_DIR, script_name)
        if not os.path.isfile(script_path):
            self.send_response(404); self.end_headers(); return

        env = os.environ.copy()
        env["REQUEST_METHOD"] = self.command
        env["SCRIPT_NAME"] = "/cgi-bin/" + script_name
        for h, v in self.headers.items():
            env["HTTP_" + h.upper().replace("-", "_")] = v

        try:
            result = subprocess.run(["/bin/sh", script_path], capture_output=True, env=env, timeout=30)
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(str(e).encode()); return

        raw = result.stdout
        status = 200
        headers = []
        body = raw
        if b"\r\n\r\n" in raw:
            head, body = raw.split(b"\r\n\r\n", 1)
        elif b"\n\n" in raw:
            head, body = raw.split(b"\n\n", 1)
        else:
            head = b""

        for line in head.decode(errors="replace").splitlines():
            if line.lower().startswith("status:"):
                try:
                    status = int(line.split(":", 1)[1].strip().split()[0])
                except Exception:
                    pass
            elif ":" in line:
                k, v = line.split(":", 1)
                headers.append((k.strip(), v.strip()))

        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if not headers:
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

http.server.HTTPServer(("0.0.0.0", 5001), Handler).serve_forever()
