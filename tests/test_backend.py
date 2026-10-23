"""Offline checks: no Ollama process, downloads, or cloud calls."""
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from benchmark.categories import schema_expected, decision_expected
from benchmark.core import grade_objective, normalize_judge, validate_pack
from benchmark.ollama import Ollama
from benchmark.projection import project_run
from benchmark.runner import Runner
from benchmark.store import Store
from server import App, handler_for


def pack_fixture():
    tasks = []
    for level in range(1, 6):
        tasks += [
            {"id": f"s{level}", "category": "schema", "level": level,
             "records": [{"id": "one", "keep": True}, {"id": "two", "keep": False}],
             "steps": [{"op": "filter", "where": {"field": "keep", "op": "eq", "value": True}},
                       {"op": "select", "fields": ["id"]}]},
            {"id": f"d{level}", "category": "decision", "level": level,
             "cases": [{"id": "a", "risk": 9}, {"id": "b", "risk": 1}],
             "rules": [{"when": {"field": "risk", "op": "gte", "value": 5},
                        "choice": "review"}], "defaultChoice": "accept"},
            {"id": f"i{level}", "category": "interpretation", "level": level,
             "passage": "The train leaves at nine. The notice does not name the platform.",
             "question": "What is known?", "reference": "Nine is known; the platform is not.",
             "criteria": [{"id": "time", "description": "States the departure time", "max": 3},
                          {"id": "platform", "description": "States platform uncertainty", "max": 2}]},
        ]
    return {"name": "Offline fixture", "version": "1", "language": "en", "tasks": tasks}


class FakeOllama:
    def __init__(self):
        self.calls = 0
        self.unloaded = []

    def models(self):
        return {"models": [{"name": "fake:1", "digest": "fake-digest",
                            "details": {"quantization_level": "mock"}}]}

    def running(self):
        return {"models": []}

    def version(self):
        return {"version": "fake"}

    def preload(self, model, stop=None):
        return {}

    def unload(self, model):
        self.unloaded.append(model)

    def cancel(self):
        pass

    def generate(self, model, prompt, think, options, stop=None):
        self.calls += 1
        if prompt.startswith("Apply the following data operations"):
            answer = {"result": [{"id": "one"}]}
        elif prompt.startswith("Apply the policy rules"):
            answer = {"decisions": [{"id": "a", "choice": "review"},
                                    {"id": "b", "choice": "accept"}]}
        else:
            answer = "The time is nine; the platform is unknown."
        return {"response": json.dumps(answer) if isinstance(answer, dict) else answer,
                "thinking": "", "metrics": {"doneReason": "stop", "evalCount": 10,
                                           "evalNs": 1_000_000_000}}


class SlowOllama(FakeOllama):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def generate(self, model, prompt, think, options, stop=None):
        self.entered.set()
        stop.wait(2)
        raise InterruptedError("stopped")


class BackendTests(unittest.TestCase):
    def test_objective_truth_and_pack_validation(self):
        pack = pack_fixture()
        self.assertEqual([], validate_pack(pack))
        self.assertEqual({"result": [{"id": "one"}]}, schema_expected(pack["tasks"][0]))
        self.assertEqual(10, grade_objective(pack["tasks"][0],
                         '{"result":[{"id":"one"}]}')["score"])
        self.assertEqual(["review", "accept"], [item["choice"] for item in
                         decision_expected(pack["tasks"][1])["decisions"]])
        self.assertEqual(0, grade_objective(pack["tasks"][1], "bad JSON")["score"])

    def test_subjective_normalization_and_projection(self):
        task = pack_fixture()["tasks"][2]
        grade = normalize_judge(task, {"scores": [{"id": "time", "earned": 3},
                                                   {"id": "platform", "earned": 1}],
                                       "rationale": "partial"})
        self.assertEqual(8, grade["score"])
        with self.assertRaises(ValueError):
            normalize_judge(task, {"scores": []})
        run = {"id": "r", "repetitions": 1, "tasks": [
            {"category": "schema", "level": 1, "repetition": 1,
             "status": "scored", "score": 10, "processingMs": 2000}]}
        result = project_run(run, [1, 10, 10, 10, 10])
        self.assertEqual("timed_out", result["records"][0]["status"])
        self.assertEqual("skipped_due_to_limit", result["records"][1]["status"])
        self.assertIsNone(result["total"])

    def test_runner_checkpoints_and_api_state_without_real_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data")
            added = store.add_pack(pack_fixture())["pack"]
            store.update(lambda state: added.update(approved=True))
            fake = FakeOllama()
            system = lambda: {"warnings": [], "at": "test"}
            runner = Runner(store, fake, root, system_snapshot=system)
            app = App(root, store=store, ollama=fake, system_snapshot=system,
                      runner=runner)
            code, state = app.get("/api/state")
            self.assertEqual(200, code)
            self.assertEqual(3, len(state["categories"]))
            config = {"packId": added["id"], "model": "fake:1",
                      "digest": "fake-digest", "details": {},
                      "think": False, "numCtx": 4096, "temperature": 0,
                      "numPredict": 1024, "limitsSec": [60] * 5,
                      "repetitions": 1, "overrideWarnings": False}
            app.post("/api/run", config)
            deadline = time.monotonic() + 5
            while runner.busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(runner.busy(), runner.status())
            self.assertEqual("completed", runner.status()["status"])
            self.assertEqual(15, fake.calls)
            run = store.state["runs"][0]
            self.assertEqual(15, len(run["tasks"]))
            self.assertEqual(5, sum(item["status"] == "subjective_pending"
                                    for item in run["tasks"]))
            self.assertEqual(10, len([item for item in run["tasks"]
                                      if item.get("score") == 10]))
            self.assertEqual(15, len(Store(root / "data").state["runs"][0]["tasks"]))
            self.assertEqual(["fake:1"], fake.unloaded)

    def test_http_serves_existing_frontend_and_rejects_unauthorized_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data")
            fake = FakeOllama()
            system = lambda: {"warnings": [], "at": "test"}
            runner = Runner(store, fake, root, system_snapshot=system)
            project = Path(__file__).resolve().parents[1]
            app = App(project, store=store, ollama=fake, system_snapshot=system,
                      runner=runner)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Host": "127.0.0.1:37843"}
                connection.request("GET", "/", headers=headers)
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertIn(b"<html", response.read().lower())
                connection.request("GET", "/api/state", headers=headers)
                response = connection.getresponse()
                state = json.loads(response.read())
                self.assertEqual(200, response.status)
                self.assertEqual(3, len(state["categories"]))
                body = json.dumps({"limitsSec": [60] * 5, "repetitions": 1})
                connection.request("POST", "/api/settings", body, headers={
                    **headers, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(403, response.status)
                response.read()
                connection.request("POST", "/api/settings", body, headers={
                    **headers, "Content-Type": "application/json",
                    "X-Benchmark-Token": state["token"]})
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual([60] * 5, json.loads(response.read())["limitsSec"])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_abort_discard_current_answer_but_keeps_run_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data")
            pack = store.add_pack(pack_fixture())["pack"]
            store.update(lambda state: pack.update(approved=True))
            fake = SlowOllama()
            runner = Runner(store, fake, root,
                            system_snapshot=lambda: {"warnings": []})
            config = {"packId": pack["id"], "model": "fake:1",
                      "digest": "fake-digest", "details": {}, "think": False,
                      "numCtx": 4096, "temperature": 0, "numPredict": 1024,
                      "limitsSec": [60] * 5, "repetitions": 1}
            runner.start_benchmark(config)
            self.assertTrue(fake.entered.wait(2))
            runner.abort("now")
            deadline = time.monotonic() + 3
            while runner.busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(runner.busy())
            run = store.state["runs"][0]
            self.assertEqual("aborted", run["status"])
            self.assertEqual(1, len(run["tasks"]))
            self.assertEqual("aborted", run["tasks"][0]["status"])
            self.assertIsNone(run["tasks"][0]["score"])

    def test_ollama_stream_parser_with_local_stub(self):
        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/generate" or request["model"] != "fake:1":
                    self.send_error(400)
                    return
                body = (b'{"response":"first ","thinking":"step"}\n'
                        b'{"response":"second","done":true,"done_reason":"stop",'
                        b'"eval_count":2,"eval_duration":1000000000}\n')
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = Ollama(port=server.server_port).generate(
                "fake:1", "prompt", False, {"num_ctx": 4096})
            self.assertEqual("first second", result["response"])
            self.assertEqual("step", result["thinking"])
            self.assertEqual(2, result["metrics"]["evalCount"])
        finally:
            server.shutdown()
            server.server_close()

    def test_ollama_stream_can_be_cancelled_while_waiting(self):
        entered = threading.Event()
        release = threading.Event()
        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b'{"response":"first "}\n')
                self.wfile.flush()
                entered.set()
                release.wait(3)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Ollama(port=server.server_port)
        result = []
        def generate():
            try:
                client.generate("fake:1", "prompt", False, {})
            except Exception as exc:
                result.append(exc)
        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            client.cancel()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "cancel did not interrupt the stream")
            self.assertTrue(result)
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_ollama_stream_tolerates_long_pause_between_tokens(self):
        class Paused(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                first = b'{"response":"first "}\n'
                final = b'{"response":"second","done":true}\n'
                self.send_response(200)
                self.send_header("Content-Length", str(len(first) + len(final)))
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                time.sleep(1.2)
                self.wfile.write(final)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Paused)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = Ollama(port=server.server_port).generate("fake:1", "prompt", False, {})
            self.assertEqual("first second", result["response"])
        finally:
            server.shutdown()
            server.server_close()

    def test_ollama_chunked_response_with_split_json_line(self):
        class Chunked(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in (b'{"response":"hello', b' world"}\n',
                              b'{"done":true,"eval_count":3}\n'):
                    self.wfile.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Chunked)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = Ollama(port=server.server_port).generate("fake:1", "prompt", False, {})
            self.assertEqual("hello world", result["response"])
            self.assertEqual(3, result["metrics"]["evalCount"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
