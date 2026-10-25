"""Persistent, manually started sequence of local benchmark setups."""
from __future__ import annotations

import copy
import threading
import time

from .core import new_id
from .runner import reusable_record
from .store import now


def setup_key(config):
    return (config["packId"], config["model"], config["digest"], config["think"],
            config["numCtx"], config["temperature"], config["numPredict"],
            config["repetitions"])


def queue_key(config):
    return setup_key(config), tuple(config["limitsSec"])


class BenchmarkQueue:
    def __init__(self, store, runner, ollama):
        self.store, self.runner, self.ollama = store, runner, ollama
        self.lock = threading.RLock()
        self.active = False
        self.pause_requested = threading.Event()

        def recover(state):
            queue = state.setdefault("queue", {"status": "idle", "items": [],
                                                "activeItemId": None})
            if queue["status"] == "running":
                queue["status"] = "paused"
                queue["activeItemId"] = None
                for item in queue["items"]:
                    if item["status"] == "running":
                        item.update(status="pending", note="Interrupted by app shutdown; saved tasks can be reused")
        with self.store.lock:
            needs_recovery = ("queue" not in self.store.state or
                              self.store.state["queue"]["status"] == "running")
        if needs_recovery:
            self.store.update(recover)

    def snapshot(self):
        with self.store.lock:
            queue = copy.deepcopy(self.store.state["queue"])
        for item in queue["items"]:
            try:
                item["preview"] = self.preview(item["config"])
            except ValueError as exc:
                item["preview"] = {"error": str(exc)}
        return queue

    def preview(self, config):
        pack = self.runner.validate_benchmark(config)
        all_keys = {(task["id"], repetition)
                    for repetition in range(1, config["repetitions"] + 1)
                    for task in pack["tasks"]}
        best_run, best_covered = None, set()
        with self.store.lock:
            for run in self.store.state["runs"]:
                setup = run.get("setup", {})
                options = setup.get("options", {})
                if (run.get("packHash") != pack["hash"] or
                        (run.get("packId"), setup.get("model"), setup.get("digest"),
                         setup.get("think"), options.get("num_ctx"), options.get("temperature"),
                         options.get("num_predict"), run.get("repetitions")) != setup_key(config)):
                    continue
                covered = {(record["taskId"], record["repetition"]) for record in run["tasks"]
                           if reusable_record(record, config["limitsSec"])} & all_keys
                if len(covered) >= len(best_covered):
                    best_run, best_covered = run, covered
        missing = all_keys - best_covered
        by_category = {}
        for task in pack["tasks"]:
            count = sum((task["id"], rep) in missing
                        for rep in range(1, config["repetitions"] + 1))
            if count:
                by_category[task["category"]] = by_category.get(task["category"], 0) + count
        return {"totalTasks": len(all_keys), "missingTasks": len(missing),
                "reusedTasks": len(best_covered), "retryOf": best_run["id"] if best_run else None,
                "missingByCategory": by_category}

    def add(self, configs):
        if not isinstance(configs, list) or not 1 <= len(configs) <= 50:
            raise ValueError("Add between 1 and 50 setups at a time")
        installed = {model.get("name") or model.get("model"): model
                     for model in self.ollama.models().get("models", [])}
        prepared, incoming = [], set()
        for config in configs:
            if not isinstance(config, dict):
                raise ValueError("Each queue setup must be an object")
            self.runner.validate_benchmark(config)
            model = installed.get(config["model"])
            if not model or model.get("digest") != config["digest"]:
                raise ValueError(f'{config["model"]}: installed model or digest changed; refresh models')
            key = queue_key(config)
            if key in incoming:
                raise ValueError("The selection contains the same setup twice")
            incoming.add(key)
            clean = {field: copy.deepcopy(config[field]) for field in
                     ("packId", "model", "digest", "think", "numCtx", "temperature",
                      "numPredict", "limitsSec", "repetitions")}
            prepared.append({"id": new_id(), "status": "pending", "config": clean,
                             "addedAt": now(), "runId": None, "note": None})
        with self.lock:
            if self.active:
                raise ValueError("Pause the queue before adding setups")
            def insert(state):
                items = state["queue"]["items"]
                if len(items) + len(prepared) > 100:
                    raise ValueError("Queue can hold at most 100 setups")
                existing = {queue_key(item["config"]) for item in items
                            if item["status"] in {"pending", "running"}}
                if existing & incoming:
                    raise ValueError("A selected setup is already waiting in the queue")
                items.extend(prepared)
            self.store.update(insert)
        return self.snapshot()

    def remove(self, item_id):
        with self.lock:
            if self.active:
                raise ValueError("Pause the queue before editing it")
            def remove_item(state):
                items = state["queue"]["items"]
                item = next((item for item in items if item["id"] == item_id), None)
                if not item:
                    raise ValueError("Queue item not found")
                if item["status"] == "running":
                    raise ValueError("Pause the active setup before removing it")
                items.remove(item)
            self.store.update(remove_item)
        return self.snapshot()

    def move(self, item_id, direction):
        if direction not in (-1, 1):
            raise ValueError("Direction must be up or down")
        with self.lock:
            if self.active:
                raise ValueError("Pause the queue before editing it")
            def move_item(state):
                items = state["queue"]["items"]
                index = next((index for index, item in enumerate(items)
                              if item["id"] == item_id), -1)
                target = index + direction
                if (index < 0 or target < 0 or target >= len(items) or
                        items[index]["status"] != "pending" or
                        items[target]["status"] != "pending"):
                    raise ValueError("Only adjacent pending setups can be reordered")
                items[index], items[target] = items[target], items[index]
            self.store.update(move_item)
        return self.snapshot()

    def retry(self, item_id):
        with self.lock:
            if self.active:
                raise ValueError("Pause the queue before editing it")
            def reset(state):
                item = next((item for item in state["queue"]["items"]
                             if item["id"] == item_id), None)
                if not item or item["status"] != "failed":
                    raise ValueError("Choose a failed queue setup")
                item.update(status="pending", note=None)
            self.store.update(reset)
        return self.snapshot()

    def start(self, override_warnings=False):
        with self.lock, self.runner.lock:
            if self.active or self.runner.busy() or self.runner.queue_active:
                raise ValueError("Another operation or queue is running")
            if not any(item["status"] == "pending" for item in self.store.snapshot()["queue"]["items"]):
                raise ValueError("Queue has no pending setups")
            system = self.runner.system_snapshot()
            if system["warnings"] and not override_warnings:
                raise ValueError("System Check needs acknowledgement: " + " ".join(system["warnings"]))
            self.pause_requested.clear()
            self.active = True
            self.runner.queue_active = True
            try:
                self.store.update(lambda state: state["queue"].update(
                    status="running", activeItemId=None, pauseRequested=False,
                    startedAt=now(), endedAt=None))
            except Exception:
                self.runner.queue_active = False
                self.active = False
                raise
            threading.Thread(target=self._work, args=(bool(override_warnings),), daemon=True).start()
        return self.snapshot()

    def pause(self, mode="after-task"):
        if mode not in {"after-task", "now"}:
            raise ValueError("Choose after-task or now")
        with self.lock:
            if not self.active:
                raise ValueError("Queue is not running")
            self.pause_requested.set()
            self.store.update(lambda state: state["queue"].update(pauseRequested=True))
            if self.runner.busy():
                self.runner.abort(mode)
        return self.snapshot()

    def _set_item(self, item_id, **changes):
        def update(state):
            item = next(item for item in state["queue"]["items"] if item["id"] == item_id)
            item.update(changes)
        self.store.update(update)

    def _work(self, override_warnings):
        try:
            while not self.pause_requested.is_set():
                queue = self.store.snapshot()["queue"]
                item = next((item for item in queue["items"] if item["status"] == "pending"), None)
                if not item:
                    break
                item_id = item["id"]
                self.store.update(lambda state: state["queue"].update(activeItemId=item_id))
                try:
                    plan = self.preview(item["config"])
                    if not plan["missingTasks"]:
                        self._set_item(item_id, status="skipped", note="All tasks already recorded",
                                       runId=plan["retryOf"], endedAt=now())
                        continue
                    config = {**item["config"], "overrideWarnings": override_warnings}
                    with self.lock:
                        if self.pause_requested.is_set():
                            break
                        run = self.runner.start_benchmark(config, retry_of=plan["retryOf"],
                                                          from_queue=True)
                    self._set_item(item_id, status="running", runId=run["id"],
                                   startedAt=now(), note=f'{plan["missingTasks"]} task(s) to run')
                    while self.runner.busy():
                        time.sleep(0.1)
                    result = self.store.run(run["id"])
                    if result["status"] == "completed":
                        self._set_item(item_id, status="completed", endedAt=now(), note=None)
                    elif result["status"] == "aborted":
                        self.pause_requested.set()
                        self._set_item(item_id, status="pending", endedAt=now(),
                                       note="Stopped; saved tasks will be reused on resume")
                    else:
                        self._set_item(item_id, status="failed", endedAt=now(),
                                       note=result.get("error") or self.runner.status().get("detail"))
                except Exception as exc:
                    if str(exc).startswith("System Check needs acknowledgement"):
                        self.pause_requested.set()
                        self._set_item(item_id, status="pending", note=str(exc))
                    else:
                        self._set_item(item_id, status="failed", endedAt=now(), note=str(exc))
                finally:
                    self.store.update(lambda state: state["queue"].update(activeItemId=None))
        finally:
            with self.lock, self.runner.lock:
                self.store.update(lambda state: state["queue"].update(
                    status="paused" if self.pause_requested.is_set() else "idle",
                    pauseRequested=False, activeItemId=None, endedAt=now()))
                self.runner.queue_active = False
                self.active = False
