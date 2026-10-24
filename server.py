"""Local browser app entry point. Python 3.14+, standard library only."""
from __future__ import annotations
from datetime import date
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

from benchmark.categories import CATEGORIES
from benchmark.cloud import EFFORTS, generation_prompt
from benchmark.core import content_hash, validate_pack
from benchmark.ollama import Ollama
from benchmark.projection import project_run, valid_limits
from benchmark.runner import Runner
from benchmark.store import Store, now
from benchmark.system import codex_exe, snapshot
from benchmark.telemetry import read_samples

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 37843
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/comparison.js": ("comparison.js", "text/javascript"),
          "/results_ui.js": ("results_ui.js", "text/javascript"),
          "/results.css": ("results.css", "text/css")}
CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"


class App:
    def __init__(self, root=ROOT, store=None, ollama=None, system_snapshot=snapshot,
                 runner=None):
        self.root = Path(root)
        self.store = store or Store(Path(os.environ.get("BENCHMARK_DATA_DIR", self.root / "data")))
        built_in = self.root / "packs" / "broad-draft-v0.2.json"
        if built_in.is_file():
            pack = json.loads(built_in.read_text(encoding="utf-8"))
            digest = content_hash(pack)
            if not any(item.get("hash") == digest for item in self.store.state["packs"]):
                result = self.store.add_pack(pack, "built-in draft")
                if result.get("errors"):
                    raise ValueError("Built-in pack is invalid: " + "; ".join(result["errors"]))
        self.ollama = ollama or Ollama()
        self.system_snapshot = system_snapshot
        self.runner = runner or Runner(self.store, self.ollama, self.root, system_snapshot)
        self.token = secrets.token_hex(32)

    def get(self, path, params=None):
        params = params or {}
        if path == "/api/health":
            return 200, {"name": "llm-benchmark", "version": "0.3.0"}
        if path == "/api/state":
            return 200, {**self.store.snapshot(),
                         "categories": [{"id": c.id, "label": c.label, "type": c.type}
                                        for c in CATEGORIES],
                         "job": self.runner.status(), "token": self.token,
                         "codexAvailable": bool(codex_exe())}
        if path == "/api/models":
            models = self.ollama.models()
            running = self.ollama.running()
            version = self.ollama.version()
            return 200, {"models": models.get("models", []),
                         "running": running.get("models", []),
                         "version": version.get("version")}
        if path == "/api/system":
            return 200, self.system_snapshot()
        if path == "/api/prompt":
            return 200, {"prompt": generation_prompt(), "efforts": EFFORTS}
        if path == "/api/job":
            return 200, self.runner.status()
        if path == "/api/job/check":
            return 200, self.runner.check()
        if path == "/api/telemetry":
            run_id = params.get("runId", [None])[0]
            if not self.store.run(run_id):
                raise LookupError("Run not found")
            return 200, {"runId": run_id, "samples": read_samples(self.store.file.parent, run_id)}
        if path == "/api/export":
            exported = self.store.snapshot()
            for run in exported["runs"]:
                if run.get("telemetry"):
                    run["telemetrySamples"] = read_samples(self.store.file.parent, run["id"])
            return 200, exported
        raise LookupError("Unknown API endpoint")

    def post(self, path, body):
        if path == "/api/settings":
            limits = body.get("limitsSec")
            repetitions = body.get("repetitions")
            if not valid_limits(limits):
                raise ValueError("Five limits between 1 and 86400 seconds required")
            if type(repetitions) is not int or not 1 <= repetitions <= 5:
                raise ValueError("Repetitions must be 1–5")
            def save(state):
                state["settings"].update(limitsSec=limits, repetitions=repetitions,
                                         judgeModel=str(body.get("judgeModel") or ""))
            self.store.update(save)
            return 200, self.store.state["settings"]
        if path == "/api/packs/import":
            pack = body.get("pack")
            if isinstance(pack, str):
                pack = json.loads(pack)
            result = self.store.add_pack(pack)
            return (422 if "errors" in result else 200), result
        if path == "/api/packs/approve":
            pack = self.store.pack(body.get("id"))
            if not pack:
                raise ValueError("Pack not found")
            errors = validate_pack(pack)
            if errors:
                return 422, {"errors": errors}
            approved = bool(body.get("approved"))
            self.store.update(lambda state: pack.update(
                approved=approved, approvedAt=now() if approved else None))
            return 200, {"pack": pack}
        if path == "/api/run":
            return 202, {"run": self.runner.start_benchmark(body)}
        if path == "/api/runs/retry":
            prior = self.store.run(body.get("runId"))
            if not prior:
                raise ValueError("Run not found")
            setup = prior["setup"]
            options = setup["options"]
            config = {"packId": prior["packId"], "model": setup["model"],
                      "digest": setup["digest"], "details": setup.get("details"),
                      "think": setup["think"], "numCtx": options["num_ctx"],
                      "temperature": options["temperature"], "numPredict": options["num_predict"],
                      "limitsSec": body.get("limitsSec") or prior["limitsSec"],
                      "repetitions": prior["repetitions"],
                      "overrideWarnings": bool(body.get("overrideWarnings"))}
            return 202, {"run": self.runner.start_benchmark(config, prior["id"])}
        if path == "/api/runs/project":
            run = self.store.run(body.get("runId"))
            if not run:
                raise ValueError("Run not found")
            return 200, project_run(run, body.get("limitsSec"))
        if path == "/api/models/pull":
            return 202, {"job": self.runner.start_pull(body.get("model"))}
        if path == "/api/models/unload":
            if self.runner.busy():
                raise ValueError("A job is running")
            model = body.get("model")
            loaded = self.ollama.running()
            if not any(item.get("name") == model for item in loaded.get("models", [])):
                raise ValueError("Model is not currently loaded")
            self.ollama.unload(model)
            return 200, {"unloaded": model}
        if path == "/api/models/delete":
            if self.runner.busy():
                raise ValueError("Finish or stop the current job before deleting a model")
            name, digest = body.get("model"), body.get("digest")
            if not isinstance(name, str) or not isinstance(digest, str):
                raise ValueError("Model name and digest required")
            installed = next((item for item in self.ollama.models().get("models", [])
                              if item.get("name") == name), None)
            if not installed or installed.get("digest") != digest:
                raise ValueError("The installed model changed; refresh the model list")
            if any(item.get("name") == name for item in self.ollama.running().get("models", [])):
                raise ValueError("Unload this model before deleting it")
            self.ollama.delete(name)
            return 200, {"deleted": name}
        if path == "/api/cloud/generate":
            return 202, {"job": self.runner.start_cloud(body.get("model"),
                                                         body.get("effort"))}
        if path == "/api/judge":
            return 202, {"job": self.runner.start_judge(body.get("runId"),
                                                         body.get("judgeModel"),
                                                         bool(body.get("rejudge")))}
        if path == "/api/job/abort":
            self.runner.abort(body.get("mode"))
            return 200, {"job": self.runner.status()}
        raise LookupError("Unknown API endpoint")


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"{self.address_string()} - {fmt % args}", flush=True)

        def _json(self, code, value, export=False):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if export:
                self.send_header("Content-Disposition",
                                 f'attachment; filename="llm-benchmark-{date.today().isoformat()}.json"')
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, exc):
            code = 404 if isinstance(exc, LookupError) else 503 if isinstance(exc, ConnectionError) else 400
            self._json(code, {"error": str(exc)})

        def _host_ok(self):
            return self.headers.get("Host") in (HOST, f"{HOST}:{PORT}")

        def _static(self, path):
            if re.fullmatch(r"/assets/(?:ocr|board)-[1-5]\.png", path):
                payload = (app.root / path.lstrip("/")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            entry = STATIC.get(path)
            if not entry:
                raise LookupError("Not found")
            name, media = entry
            payload = (app.root / "public" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", media + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                if not self._host_ok():
                    self._json(403, {"error": "Invalid host"})
                    return
                path = urlsplit(self.path).path
                if path.startswith("/api/"):
                    code, value = app.get(path, parse_qs(urlsplit(self.path).query))
                    self._json(code, value, export=path == "/api/export")
                else:
                    self._static(path)
            except (ValueError, TypeError, RuntimeError, OSError, LookupError, KeyError) as exc:
                self._error(exc)

        def do_POST(self):
            try:
                if not self._host_ok():
                    self._json(403, {"error": "Invalid host"})
                    return
                if self.headers.get("X-Benchmark-Token") != app.token:
                    self._json(403, {"error": "Missing app token"})
                    return
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{HOST}:{PORT}":
                    self._json(403, {"error": "Origin rejected"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > 5_000_000 or length < 0:
                    raise ValueError("Request exceeds 5 MB")
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                code, value = app.post(urlsplit(self.path).path, body)
                self._json(code, value)
            except (ValueError, TypeError, RuntimeError, OSError, LookupError, KeyError) as exc:
                self._error(exc)

    return Handler


def main():
    app = App()
    try:
        server = ThreadingHTTPServer((HOST, PORT), handler_for(app))
    except OSError as exc:
        print(f"Could not start on http://{HOST}:{PORT}: {exc}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    reviewed = app.store.backfill_format_reviews()
    if reviewed:
        print(f"Reviewed {reviewed} saved answers; original scores remain unchanged.", flush=True)
    print(f"LLM Benchmark is running at http://{HOST}:{PORT}", flush=True)
    print("Close this window to stop the app.", flush=True)
    if os.environ.get("BENCHMARK_NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
