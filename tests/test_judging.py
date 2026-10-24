"""Offline judge checks: schema transport, failures, and manual retry."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from benchmark.judging import JudgeOutputError, judge_schema, parse_judge_answer
from benchmark.ollama import Ollama
from benchmark.runner import Runner
from benchmark.store import Store


TASK = {
    "id": "interpretation-1", "category": "interpretation", "level": 1,
    "passage": "The train leaves at nine. The platform is not named.",
    "question": "What is known?", "reference": "Nine is known; platform unknown.",
    "criteria": [{"id": "time", "description": "Time", "max": 3},
                 {"id": "platform", "description": "Uncertainty", "max": 2}],
}
FULL_GRADE = json.dumps({"scores": [{"id": "time", "earned": 3},
                                    {"id": "platform", "earned": 2}],
                         "rationale": "Both facts are present."})


class FakeJudge:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.unloaded = []

    def models(self):
        return {"models": [{"name": "judge:1", "digest": "mock"}]}

    def running(self):
        return {"models": []}

    def preload(self, model, stop=None):
        return {}

    def unload(self, model):
        self.unloaded.append(model)

    def generate(self, model, prompt, think, options, stop=None, images=None,
                 format_schema=None):
        self.requests.append({"model": model, "think": think, "options": options,
                              "format_schema": format_schema})
        return next(self.responses)


class JudgingTests(unittest.TestCase):
    def test_judge_result_classifies_empty_json_and_rubric_errors(self):
        for result, expected in [
            ({"response": "", "metrics": {"doneReason": "length"}}, "empty_final"),
            ({"response": "{bad"}, "invalid_json"),
            ({"response": '{"scores":[]}'}, "invalid_grade"),
        ]:
            with self.subTest(expected=expected), self.assertRaises(JudgeOutputError) as error:
                parse_judge_answer(TASK, result)
            self.assertEqual(expected, error.exception.kind)
        self.assertEqual(10, parse_judge_answer(TASK, {"response": FULL_GRADE})["score"])
        scores_only = json.dumps({"scores": [{"id": "time", "earned": 3},
                                            {"id": "platform", "earned": 2}]})
        self.assertEqual(10, parse_judge_answer(TASK, {"response": scores_only})["score"])

    def test_failed_attempt_is_saved_and_retry_scores_only_that_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data")
            records = [{"taskId": TASK["id"], "category": "interpretation", "level": 1,
                        "repetition": rep, "status": "subjective_pending", "score": None,
                        "raw": "The train leaves at nine; platform unknown."}
                       for rep in (1, 2)]
            run = {"id": "run", "status": "completed",
                   "setup": {"model": "candidate:1"}, "tasks": records}
            store.update(lambda state: state["runs"].append(run))
            fake = FakeJudge([
                {"response": "", "thinking": "Could not finish", "metrics":
                 {"doneReason": "length", "evalCount": 2048}},
                {"response": FULL_GRADE, "thinking": "", "metrics":
                 {"doneReason": "stop", "evalCount": 60}},
                {"response": FULL_GRADE, "thinking": "", "metrics":
                 {"doneReason": "stop", "evalCount": 60}},
            ])
            runner = Runner(store, fake, root)
            runner._start("judge", "offline fixture")
            runner._judge(run, {"tasks": [TASK]}, "judge:1", records)
            self.assertEqual("completed_with_errors", runner.status()["status"])
            self.assertEqual("judge_error", records[0]["status"])
            self.assertIsNone(records[0]["score"])
            failure = records[0]["judgeFailures"][0]
            self.assertEqual("judge:1", failure["model"])
            self.assertEqual("empty_final", failure["kind"])
            self.assertEqual("Could not finish", failure["thinking"])
            self.assertEqual("length", failure["metrics"]["doneReason"])
            self.assertEqual(10, records[1]["score"])
            self.assertEqual("stop", records[1]["judge"]["metrics"]["doneReason"])
            self.assertEqual(["time", "platform"],
                             fake.requests[0]["format_schema"]["properties"]["scores"]
                             ["items"]["properties"]["id"]["enum"])
            self.assertFalse(fake.requests[0]["think"])
            self.assertGreaterEqual(fake.requests[0]["options"]["num_predict"], 2048)
            runner._start("judge", "manual retry")
            runner._judge(run, {"tasks": [TASK]}, "judge:1", records[:1])
            self.assertEqual("completed", runner.status()["status"])
            self.assertEqual(10, records[0]["score"])
            self.assertEqual(1, len(records[0]["judgeFailures"]))
            self.assertEqual(2, len(fake.unloaded))

    def test_generate_passes_json_schema_to_local_ollama_api(self):
        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.server.path = self.path
                self.server.request_body = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"])))
                payload = (json.dumps({"response": FULL_GRADE, "done": True,
                                       "done_reason": "stop"}) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            Ollama(port=server.server_port).generate(
                "judge:1", "prompt", False, {"num_predict": 2048},
                format_schema=judge_schema(TASK))
            self.assertEqual("/api/generate", server.path)
            self.assertEqual("object", server.request_body["format"]["type"])
            self.assertFalse(server.request_body["think"])
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_regrade_preserves_existing_grade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data")
            old_judge = {"model": "older:1", "raw": FULL_GRADE}
            record = {"taskId": TASK["id"], "category": "interpretation", "level": 1,
                      "repetition": 1, "status": "scored", "score": 8,
                      "reason": "Previous grade", "judge": old_judge,
                      "raw": "The train leaves at nine."}
            run = {"id": "run", "status": "completed",
                   "setup": {"model": "candidate:1"}, "tasks": [record]}
            store.update(lambda state: state["runs"].append(run))
            fake = FakeJudge([{"response": "not json", "thinking": "",
                               "metrics": {"doneReason": "stop", "evalCount": 3}}])
            runner = Runner(store, fake, root)
            runner._start("judge", "regrade")
            runner._judge(run, {"tasks": [TASK]}, "judge:1", [record])
            self.assertEqual("completed_with_errors", runner.status()["status"])
            self.assertEqual(8, record["score"])
            self.assertEqual("scored", record["status"])
            self.assertEqual(old_judge, record["judge"])
            self.assertIn("invalid JSON", record["regradeError"])
            self.assertEqual("not json", record["judgeFailures"][0]["raw"])


if __name__ == "__main__":
    unittest.main()
