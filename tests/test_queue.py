"""Queue checks use a fake local model and a temporary state directory only."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from benchmark.queue import BenchmarkQueue
from benchmark.runner import Runner
from benchmark.store import Store
from server import App


def pack_fixture():
    tasks = [{"id": f"s{level}", "category": "schema", "level": level,
              "records": [{"id": "one", "keep": True}],
              "steps": [{"op": "select", "fields": ["id"]}]}
             for level in range(1, 6)]
    return {"name": "Queue fixture", "version": "1", "language": "en", "tasks": tasks}


class FakeOllama:
    def __init__(self):
        self.calls = 0
        self.fail_thinking = False
        self.slow = False
        self.entered = threading.Event()

    def models(self):
        return {"models": [{"name": "fake:1", "digest": "digest-1"}]}

    def running(self):
        return {"models": []}

    def show(self, model):
        return {"capabilities": ["completion"]}

    def preload(self, model, stop):
        pass

    def version(self):
        return {"version": "fake"}

    def unload(self, model):
        pass

    def cancel(self):
        pass

    def generate(self, model, prompt, think, options, stop):
        self.calls += 1
        if self.fail_thinking and think:
            raise RuntimeError("simulated model failure")
        if self.slow:
            self.entered.set()
            stop.wait(2)
            if stop.is_set():
                raise InterruptedError("stopped")
        return {"response": json.dumps({"result": [{"id": "one"}]}),
                "thinking": "", "metrics": {"doneReason": "stop", "evalCount": 5,
                                            "evalNs": 1_000_000_000}}


class FakeTelemetry:
    def __init__(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        return {"errors": [], "sampleCount": 0}


def await_queue(queue, seconds=5):
    deadline = time.monotonic() + seconds
    while queue.active and time.monotonic() < deadline:
        time.sleep(0.02)
    if queue.active:
        raise AssertionError("queue did not finish")


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "data")
        self.pack = self.store.add_pack(pack_fixture())["pack"]
        self.store.update(lambda state: self.pack.update(approved=True))
        self.ollama = FakeOllama()
        self.runner = Runner(self.store, self.ollama, self.root,
                             system_snapshot=lambda: {"warnings": []},
                             telemetry_factory=FakeTelemetry)
        self.app = App(self.root, store=self.store, ollama=self.ollama,
                       runner=self.runner)
        self.queue = self.app.queue

    def config(self, think=False, predict=1024):
        return {"packId": self.pack["id"], "model": "fake:1", "digest": "digest-1",
                "think": think, "numCtx": 4096, "temperature": 0,
                "numPredict": predict, "limitsSec": [60] * 5, "repetitions": 1}

    def test_enqueuing_is_persistent_and_never_starts_a_model(self):
        variants = [self.config(True, 4096), self.config(True, -1), self.config(False, 4096)]
        code, saved = self.app.post("/api/queue/add", {"configs": variants})
        self.assertEqual(200, code)
        self.assertEqual(3, len(self.app.get("/api/state")[1]["queue"]["items"]))
        self.assertEqual(3, len(saved["items"]))
        self.assertEqual("idle", saved["status"])
        self.assertEqual(0, self.ollama.calls)
        reloaded = Store(self.root / "data")
        self.assertEqual(3, len(reloaded.state["queue"]["items"]))
        self.assertEqual(0, len(reloaded.state["runs"]))
        with self.assertRaisesRegex(ValueError, "already waiting"):
            self.queue.add([variants[0]])

    def test_queue_runs_sequentially_and_skips_fully_covered_setup(self):
        self.queue.add([self.config(True, 4096), self.config(True, -1),
                        self.config(False, 4096)])
        self.queue.start()
        await_queue(self.queue)
        self.assertEqual(15, self.ollama.calls)
        self.assertEqual(["completed"] * 3,
                         [item["status"] for item in self.queue.snapshot()["items"]])
        self.assertEqual("idle", self.queue.snapshot()["status"])
        self.queue.add([self.config(False, 4096)])
        self.queue.start()
        await_queue(self.queue)
        self.assertEqual(15, self.ollama.calls)
        self.assertEqual("skipped", self.queue.snapshot()["items"][-1]["status"])

    def test_only_missing_task_is_run_and_prior_results_are_copied(self):
        original = self.runner.start_benchmark(self.config())
        deadline = time.monotonic() + 5
        while self.runner.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(5, self.ollama.calls)
        self.store.update(lambda state: original["tasks"].pop())
        self.queue.add([self.config()])
        self.assertEqual(1, self.queue.snapshot()["items"][0]["preview"]["missingTasks"])
        self.queue.start()
        await_queue(self.queue)
        self.assertEqual(6, self.ollama.calls)
        resumed = self.store.state["runs"][-1]
        self.assertEqual(original["id"], resumed["retryOf"])
        self.assertEqual(5, len(resumed["tasks"]))

    def test_task_above_new_time_limit_gets_another_attempt(self):
        original = self.runner.start_benchmark(self.config())
        deadline = time.monotonic() + 5
        while self.runner.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.store.update(lambda state: original["tasks"][0].update(processingMs=120_000))
        self.queue.add([self.config()])
        self.assertEqual(1, self.queue.snapshot()["items"][0]["preview"]["missingTasks"])
        self.queue.start()
        await_queue(self.queue)
        self.assertEqual(6, self.ollama.calls)
        self.assertEqual(5, len(self.store.state["runs"][-1]["tasks"]))

    def test_failed_setup_is_marked_and_next_setup_runs(self):
        self.ollama.fail_thinking = True
        self.queue.add([self.config(True), self.config(False)])
        self.queue.start()
        await_queue(self.queue)
        self.assertEqual(["failed", "completed"],
                         [item["status"] for item in self.queue.snapshot()["items"]])
        self.assertEqual(6, self.ollama.calls)

    def test_pause_then_restart_requires_another_explicit_start(self):
        self.ollama.slow = True
        self.queue.add([self.config()])
        self.queue.start()
        self.assertTrue(self.ollama.entered.wait(2))
        with self.assertRaisesRegex(ValueError, "queue is running"):
            self.runner.start_benchmark(self.config())
        self.queue.pause("now")
        await_queue(self.queue)
        self.assertEqual("paused", self.queue.snapshot()["status"])
        self.assertEqual("pending", self.queue.snapshot()["items"][0]["status"])
        reloaded = Store(self.root / "data")
        another_runner = Runner(reloaded, self.ollama, self.root,
                                system_snapshot=lambda: {"warnings": []},
                                telemetry_factory=FakeTelemetry)
        another_queue = BenchmarkQueue(reloaded, another_runner, self.ollama)
        self.assertEqual("paused", another_queue.snapshot()["status"])
        self.assertFalse(another_queue.active)

    def test_recovery_from_closed_app_marks_running_entry_pending(self):
        self.queue.add([self.config()])
        def simulate_crash(state):
            state["queue"].update(status="running", activeItemId=state["queue"]["items"][0]["id"])
            state["queue"]["items"][0]["status"] = "running"
        self.store.update(simulate_crash)
        recovered = Store(self.root / "data")
        runner = Runner(recovered, self.ollama, self.root,
                        system_snapshot=lambda: {"warnings": []},
                        telemetry_factory=FakeTelemetry)
        queue = BenchmarkQueue(recovered, runner, self.ollama)
        self.assertEqual("paused", queue.snapshot()["status"])
        self.assertEqual("pending", queue.snapshot()["items"][0]["status"])
        self.assertEqual(0, self.ollama.calls)


if __name__ == "__main__":
    unittest.main()
