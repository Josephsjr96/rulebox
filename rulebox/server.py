"""A loopback-only, dependency-free local UI. Not an internet web server."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import webbrowser

from .core import CodexAdapter, Store, draft_rule

STATIC = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not persist instructions, URL parameters or session secrets.

    def respond(self, status, value, content_type="application/json; charset=utf-8"):
        data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return host == self.server.authority and origin in (None, self.server.origin)

    def do_GET(self):
        if not self.allowed():
            return self.respond(403, {"error": "Local origin required"})
        if self.path == "/api/state":
            return self.respond(200, {"rules": self.server.store.all(),
                "project": str(self.server.adapter.project),
                "database": str(self.server.store.path), "token": self.server.token,
                "engine": "Local LoRA rule writer" if self.server.writer else "Local formatter · no model required"})
        routes = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in routes:
            filename, kind = routes[self.path]
            return self.respond(200, (STATIC / filename).read_bytes(), kind)
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.allowed() or not secrets.compare_digest(self.headers.get("X-Rulebox-Token", ""), self.server.token):
            return self.respond(403, {"error": "Reload Rulebox to obtain a valid local session"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, {"error": "JSON required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Request must be between 1 byte and 64 KiB")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            # Serialize UI writes and previews to keep review/apply consistent.
            with self.server.mutation_lock:
                if self.path == "/api/draft":
                    result = self.server.writer.draft(data.get("source")) if self.server.writer else draft_rule(data.get("source"))
                elif self.path == "/api/save":
                    rule_id = data.get("id")
                    if rule_id is not None and (type(rule_id) is not int or rule_id < 1):
                        raise ValueError("Invalid rule ID")
                    result = {"id": self.server.store.save(data, rule_id)}
                elif self.path == "/api/delete":
                    if type(data.get("id")) is not int or data["id"] < 1:
                        raise ValueError("Invalid rule ID")
                    self.server.store.delete(data["id"])
                    result = {"deleted": True}
                elif self.path == "/api/preview":
                    result = self.server.adapter.preview(self.server.store.all())
                elif self.path == "/api/apply":
                    result = self.server.adapter.apply(self.server.store.all(), data.get("revision"))
                else:
                    return self.respond(404, {"error": "Not found"})
            self.respond(200, result)
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except OSError:
            self.respond(500, {"error": "Cannot access a local file. Check project and database permissions."})
        except Exception:
            self.respond(500, {"error": "Operation failed. No success was confirmed; reload and inspect local state."})


def make_server(project, database, port=0, writer=None):
    adapter = CodexAdapter(project)
    store = Store(database)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.adapter, server.store = adapter, store
    server.writer = writer
    server.token = secrets.token_urlsafe(32)
    server.mutation_lock = threading.Lock()
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def main():
    parser = argparse.ArgumentParser(description="Rulebox — local Codex instruction manager")
    parser.add_argument("--project", required=True, help="Existing project directory whose AGENTS.md you want to manage")
    parser.add_argument("--db", default=str(Path.home() / ".local/share/rulebox/rules.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the UI in your browser")
    parser.add_argument("--adapter", help="Local trained LoRA adapter directory (optional)")
    parser.add_argument("--base", help="Local base model directory, required with --adapter")
    args = parser.parse_args()
    writer = None
    if args.adapter:
        if not args.base:
            parser.error("--base is required with --adapter")
        from .lora import LocalRuleWriter
        writer = LocalRuleWriter(args.base, args.adapter)
    try:
        server = make_server(args.project, args.db, args.port, writer)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Rulebox → {server.origin}\nProject: {server.adapter.project}\nCtrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
