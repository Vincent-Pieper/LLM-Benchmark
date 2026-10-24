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
from benchmark.task_io import run_tools, run_vision
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
        self.deleted = []

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

    def delete(self, model):
        self.deleted.append(model)

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
        fenced = grade_objective(pack["tasks"][0], '```json\n{"result":[{"id":"one"}]}\n```')
        self.assertEqual(10, fenced["score"])
        self.assertFalse(fenced["formatValid"])

    def test_broad_pack_has_five_valid_levels_per_area_and_fixed_images(self):
        root = Path(__file__).resolve().parents[1]
        pack = json.loads((root / "packs" / "broad-draft-v0.2.json").read_text(encoding="utf-8"))
        self.assertEqual([], validate_pack(pack))
        self.assertEqual(12, len(pack["categoryIds"]))
        self.assertEqual(60, len(pack["tasks"]))
        for category in pack["categoryIds"]:
            self.assertEqual([1, 2, 3, 4, 5], sorted(
                task["level"] for task in pack["tasks"] if task["category"] == category))
        vision = [task for task in pack["tasks"] if task["category"] in {"ocr", "image_recognition"}]
        self.assertEqual(10, len(vision))
        for task in vision:
            self.assertTrue((root / task["image"]).is_file())

        class VisionFake:
            def generate(self, model, prompt, think, options, stop, images):
                self.assertion = (model, bool(images), len(images[0]))
                return {"response": "{}", "thinking": "", "metrics": {}}

        fake = VisionFake()
        run_vision(fake, root, vision[0], "fake:1", False, {}, None)
        self.assertEqual("fake:1", fake.assertion[0])
        self.assertTrue(fake.assertion[1])
        self.assertGreater(fake.assertion[2], 100)

    def test_broad_pack_is_installed_once_as_unapproved_draft(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            fake = FakeOllama()
            system = lambda: {"warnings": []}
            app = App(project, store=store, ollama=fake, system_snapshot=system)
            self.assertEqual(1, len(app.get("/api/state")[1]["packs"]))
            self.assertFalse(store.state["packs"][0]["approved"])
            self.assertEqual(60, len(store.state["packs"][0]["tasks"]))
            App(project, store=store, ollama=fake, system_snapshot=system)
            self.assertEqual(1, len(store.state["packs"]))
            self.assertEqual([], store.state["runs"])

    def test_broad_pack_image_and_delete_endpoint_over_http(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeOllama()
            app = App(project, store=Store(Path(directory)), ollama=fake,
                      system_snapshot=lambda: {"warnings": []})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Host": "127.0.0.1:37843"}
            try:
                connection.request("GET", "/api/state", headers=headers)
                response = connection.getresponse()
                state = json.loads(response.read())
                self.assertEqual(200, response.status)
                self.assertEqual(60, len(state["packs"][0]["tasks"]))

                connection.request("GET", "/assets/board-5.png", headers=headers)
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual("image/png", response.getheader("Content-Type"))
                self.assertTrue(response.read().startswith(b"\x89PNG"))

                body = json.dumps({"model": "fake:1", "digest": "fake-digest"})
                connection.request("POST", "/api/models/delete", body, headers={
                    **headers, "Content-Type": "application/json",
                    "X-Benchmark-Token": state["token"]})
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual({"deleted": "fake:1"}, json.loads(response.read()))
                self.assertEqual(["fake:1"], fake.deleted)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()

    def test_research_tool_reads_only_pack_source(self):
        task = {"category": "research", "material": "Use the fixed local sources to answer.",
                "questions": [{"id": "cap", "question": "What is the cap?", "answer": 13}],
                "sources": [{"id": "S1", "title": "Policy", "text": "The cap is 13."}]}
        class ToolFake:
            def __init__(self):
                self.calls = 0

            def chat(self, model, messages, tools, think, options, stop):
                self.calls += 1
                if self.calls == 1:
                    return {"message": {"role": "assistant", "content": "", "tool_calls": [
                        {"function": {"name": "open_source", "arguments": {"id": "S1"}}}]}}
                self.last_messages = messages
                return {"message": {"role": "assistant", "content": '{"answers":[{"id":"cap","answer":13}]}'}}

        fake = ToolFake()
        result = run_tools(fake, task, "fake:1", False, {}, None)
        self.assertEqual(1, result["validToolCalls"])
        self.assertEqual(13, json.loads(result["response"])["answers"][0]["answer"])
        self.assertIn("The cap is 13", fake.last_messages[-1]["content"])

    def test_full_broad_run_completes_with_fake_model_only(self):
        project = Path(__file__).resolve().parents[1]
        pack_body = json.loads((project / "packs" / "broad-draft-v0.2.json").read_text(encoding="utf-8"))

        class BroadFake(FakeOllama):
            def show(self, model):
                return {"capabilities": ["completion", "vision", "tools"]}

            def generate(self, model, prompt, think, options, stop=None, images=None):
                return {"response": "{}", "thinking": "", "metrics": {"doneReason": "stop"}}

            def chat(self, model, messages, tools, think, options, stop=None):
                return {"message": {"role": "assistant", "content": "{}"},
                        "done_reason": "stop"}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            pack = store.add_pack(pack_body)["pack"]
            store.update(lambda state: pack.update(approved=True))
            runner = Runner(store, BroadFake(), project,
                            system_snapshot=lambda: {"warnings": [], "at": "test"})
            runner.start_benchmark({"packId": pack["id"], "model": "fake:1",
                                    "digest": "fake-digest", "think": False,
                                    "numCtx": 4096, "temperature": 0, "numPredict": 1024,
                                    "limitsSec": [60] * 5, "repetitions": 1})
            deadline = time.monotonic() + 10
            while runner.busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual("completed", runner.status()["status"], runner.status())
            self.assertEqual(60, len(store.state["runs"][0]["tasks"]))
            self.assertEqual(12, len(project_run(store.state["runs"][0], [60] * 5)["categories"]))

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
            self.assertEqual(12, len(state["categories"]))
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
                self.assertEqual(12, len(state["categories"]))
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

    def test_model_delete_requires_current_digest_and_idle_unloaded_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeOllama()
            store = Store(root / "data")
            runner = Runner(store, fake, root, system_snapshot=lambda: {"warnings": []})
            app = App(root, store=store, ollama=fake, runner=runner)
            with self.assertRaisesRegex(ValueError, "changed"):
                app.post("/api/models/delete", {"model": "fake:1", "digest": "old"})
            self.assertEqual((200, {"deleted": "fake:1"}), app.post(
                "/api/models/delete", {"model": "fake:1", "digest": "fake-digest"}))
            self.assertEqual(["fake:1"], fake.deleted)

            fake.running = lambda: {"models": [{"name": "fake:1"}]}
            with self.assertRaisesRegex(ValueError, "Unload"):
                app.post("/api/models/delete", {"model": "fake:1", "digest": "fake-digest"})
            self.assertEqual(["fake:1"], fake.deleted)
            fake.running = lambda: {"models": []}
            runner._start("pull", "fake:1")
            with self.assertRaisesRegex(ValueError, "current job"):
                app.post("/api/models/delete", {"model": "fake:1", "digest": "fake-digest"})
            self.assertEqual(["fake:1"], fake.deleted)

    def test_ollama_delete_uses_delete_method_and_accepts_empty_response(self):
        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_DELETE(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.seen = (self.path, body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual({}, Ollama(port=server.server_port).delete("fake:1"))
            self.assertEqual(("/api/delete", {"model": "fake:1"}), server.seen)
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
