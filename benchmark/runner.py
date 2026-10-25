"""Single-job benchmark coordinator. Every completed task is checkpointed."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import math
import re
import statistics
import threading
import time
from .categories import CATEGORIES, REGISTRY
from .cloud import generate_pack
from .core import content_hash, grade_objective, new_id, pack_categories
from .format_review import review_format
from .judging import (JUDGE_OPTIONS, SCHEMA_VERSION, JudgeOutputError,
                      judge_prompt, judge_schema, parse_judge_answer)
from .projection import valid_limits
from .store import now
from .system import snapshot
from .telemetry import TelemetryRecorder, INTERVAL_SECONDS
from .task_io import run_tools, run_vision


COMPLETE = {"scored", "subjective_pending", "unsupported", "timed_out", "skipped_due_to_limit"}
REUSABLE = {"scored", "subjective_pending", "unsupported"}


def reusable_record(record, limits):
    """A saved answer can be reused only if it meets the new processing limit."""
    if record.get("status") not in REUSABLE:
        return False
    elapsed = record.get("processingMs")
    return (record["status"] == "unsupported" or
            (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
             and elapsed <= limits[record["level"] - 1] * 1000))


def ordered_tasks(pack):
    order = {category.id: index for index, category in enumerate(CATEGORIES)}
    return sorted(pack["tasks"], key=lambda task: (order[task["category"]], task["level"]))


class Runner:
    def __init__(self, store, ollama, root, system_snapshot=snapshot, cloud_generator=generate_pack,
                 telemetry_factory=TelemetryRecorder):
        self.store, self.ollama, self.root = store, ollama, root
        self.system_snapshot, self.cloud_generator = system_snapshot, cloud_generator
        self.telemetry_factory = telemetry_factory
        self.lock = threading.RLock()
        self.job = None
        self.stop = None
        self.queue_active = False
        if any(run["status"] == "running" for run in store.state["runs"]):
            def interrupt(state):
                for run in state["runs"]:
                    if run["status"] == "running":
                        run.update(status="interrupted", endedAt=now())
            store.update(interrupt)
        (root / "cloud-work").mkdir(exist_ok=True)

    def busy(self):
        with self.lock:
            return bool(self.job and self.job["status"] == "running")

    def status(self):
        with self.lock:
            return copy.deepcopy(self.job)

    def _start(self, kind, detail, from_queue=False):
        with self.lock:
            if self.queue_active and not from_queue:
                raise ValueError("The benchmark queue is running")
            if self.busy():
                raise ValueError("Another operation is running")
            self.stop = threading.Event()
            self.job = {"id": new_id(), "type": kind, "status": "running",
                        "startedAt": now(), "detail": detail, "progress": None,
                        "log": "", "stopAfterTask": False, "abortReason": None}
            return self.status()

    def _finish(self, status, detail):
        with self.lock:
            self.job.update(status=status, detail=detail, endedAt=now())

    def _launch(self, target, *args):
        def execute():
            try:
                target(*args)
            except Exception as exc:
                self._finish("aborted" if self.job.get("abortReason") == "user" else "failed",
                             str(exc))
        threading.Thread(target=execute, daemon=True).start()

    def abort(self, mode="now"):
        with self.lock:
            if not self.busy():
                raise ValueError("No active operation")
            if mode == "after-task" and self.job["type"] == "benchmark":
                self.job["stopAfterTask"] = True
                return
            self.job["abortReason"] = "user"
            self.stop.set()
        self.ollama.cancel()

    def _require_model(self, name):
        model = next((item for item in self.ollama.models().get("models", [])
                      if item.get("name") == name or item.get("model") == name), None)
        if not model:
            raise ValueError(f"Model {name} is not installed in Ollama")
        return model

    def _unload_all(self):
        for model in self.ollama.running().get("models", []):
            self.ollama.unload(model.get("name") or model.get("model"))

    def validate_benchmark(self, config):
        pack = self.store.pack(config.get("packId"))
        if not pack or not pack.get("approved"):
            raise ValueError("Choose an approved pack")
        limits = config.get("limitsSec")
        if not valid_limits(limits):
            raise ValueError("Five task limits (1–86400 seconds) required")
        repetitions = config.get("repetitions")
        if type(repetitions) is not int or not 1 <= repetitions <= 5:
            raise ValueError("Repetitions must be 1–5")
        context, temperature, predict = config.get("numCtx"), config.get("temperature"), config.get("numPredict")
        if type(context) is not int or not 512 <= context <= 131072:
            raise ValueError("Invalid context size")
        if type(temperature) not in (float, int) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("Invalid temperature")
        if type(predict) is not int or (predict != -1 and not 32 <= predict <= 32768):
            raise ValueError("Output token limit must be -1 (open-ended) or 32–32768")
        if config.get("think") not in (True, False, None):
            raise ValueError("think must be true, false, or default")
        if not isinstance(config.get("model"), str) or not config["model"]:
            raise ValueError("Model name required")
        if not isinstance(config.get("digest"), str) or not config["digest"]:
            raise ValueError("Model digest required")
        return pack

    def start_benchmark(self, config, retry_of=None, from_queue=False):
        pack = self.validate_benchmark(config)
        limits = config["limitsSec"]
        repetitions = config["repetitions"]
        context, temperature, predict = config["numCtx"], config["temperature"], config["numPredict"]
        prior = self.store.run(retry_of) if retry_of else None
        if retry_of and (not prior or prior["packHash"] != pack["hash"]
                         or prior["setup"]["model"] != config.get("model")
                         or prior["setup"]["digest"] != config.get("digest")
                         or prior["setup"].get("think") != config.get("think")
                         or prior["setup"]["options"] != {"num_ctx": context,
                                                             "temperature": temperature,
                                                             "num_predict": predict}
                         or prior["repetitions"] != repetitions):
            raise ValueError("Retry requires the same pack, setup, and installed model digest")
        system_before = self.system_snapshot()
        if system_before["warnings"] and not config.get("overrideWarnings"):
            raise ValueError("System Check needs acknowledgement: " + " ".join(system_before["warnings"]))
        old_records = ([copy.deepcopy(record) for record in prior["tasks"]
                        if reusable_record(record, limits)] if prior else [])
        run = {"id": new_id(), "profileId": "local", "status": "running", "startedAt": now(),
               "packId": pack["id"], "packHash": pack["hash"], "packName": pack["name"],
               "packVersion": pack["version"], "categoryIds": pack_categories(pack),
               "setup": {"model": config["model"], "digest": config["digest"],
                         "details": config.get("details"), "think": config.get("think"),
                         "options": {"num_ctx": context, "temperature": temperature,
                                     "num_predict": predict}},
               "limitsSec": list(limits), "repetitions": repetitions, "tasks": old_records,
               "retryOf": retry_of, "systemBefore": system_before, "coldLoadMs": None,
               "ollamaVersion": None, "error": None,
               "telemetry": {"intervalSec": INTERVAL_SECONDS, "status": "pending"}}
        self._start("benchmark", f'{config["model"]} · {pack["name"]}', from_queue=from_queue)
        with self.lock:
            self.job["runId"] = run["id"]
        try:
            self.store.update(lambda state: state["runs"].append(run))
        except Exception:
            self._finish("failed", "Could not save new run")
            raise
        self._launch(self._benchmark_safe, run, pack)
        return run

    def _benchmark_safe(self, run, pack):
        recorder = None
        final_status = None
        final_detail = None
        try:
            try:
                recorder = self.telemetry_factory(self.store.file.parent, run["id"])
                recorder.start()
                self.store.update(lambda state: run["telemetry"].update(status="recording"))
            except Exception as exc:
                recorder = None
                self.store.update(lambda state: run["telemetry"].update(
                    status="unavailable", errors=[str(exc)[:300]]))
            self._benchmark(run, pack)
        except Exception as exc:
            try:
                self.ollama.unload(run["setup"]["model"])
            except Exception:
                pass
            stopped = self.job.get("abortReason") == "user"
            self.store.update(lambda state: run.update(
                status="aborted" if stopped else "failed", error=str(exc), endedAt=now()))
            final_status = "aborted" if stopped else "failed"
            final_detail = "Stopped by user; completed tasks saved" if stopped else str(exc)
        finally:
            if recorder is not None:
                try:
                    summary = recorder.stop()
                    self.store.update(lambda state: run["telemetry"].update(
                        status="partial" if summary["errors"] else "complete", **summary))
                except Exception as exc:
                    self.store.update(lambda state: run["telemetry"].update(
                        status="unavailable", errors=[str(exc)[:300]]))
            self._finish(final_status or run["status"], final_detail or
                         ("Stopped; completed tasks saved" if run["status"] == "aborted" else
                          "Benchmark complete; subjective answers may await judging"))

    def _benchmark(self, run, pack):
        model = self._require_model(run["setup"]["model"])
        if model.get("digest") != run["setup"]["digest"]:
            raise ValueError("The model digest changed since selection. Refresh models and start a new run.")
        self.store.update(lambda state: run["setup"].update(details=model.get("details")))
        required = {capability for category_id in pack_categories(pack)
                    for capability in REGISTRY[category_id].requires}
        capabilities = self.ollama.show(run["setup"]["model"]).get("capabilities", []) if required else []
        self.store.update(lambda state: run["setup"].update(capabilities=capabilities))
        self._unload_all()
        try:
            version = self.ollama.version().get("version")
        except Exception:
            version = None
        started = time.monotonic()
        self.ollama.preload(run["setup"]["model"], self.stop)
        self.store.update(lambda state: run.update(
            coldLoadMs=round((time.monotonic() - started) * 1000), ollamaVersion=version))
        for repetition in range(1, run["repetitions"] + 1):
            for task in ordered_tasks(pack):
                if self.stop.is_set() or self.job["stopAfterTask"]:
                    break
                if any(record["repetition"] == repetition and record["taskId"] == task["id"]
                       and record["status"] in COMPLETE for record in run["tasks"]):
                    continue
                missing = [cap for cap in REGISTRY[task["category"]].requires if cap not in capabilities]
                if missing:
                    self._save_task(run, task, repetition, "unsupported", score=0,
                                    processingMs=None, reason=f'Model lacks {", ".join(missing)}')
                    continue
                blocked = any(record["repetition"] == repetition and
                              record["category"] == task["category"] and record["level"] < task["level"] and
                              record["status"] in {"timed_out", "skipped_due_to_limit"}
                              for record in run["tasks"])
                if blocked:
                    self._save_task(run, task, repetition, "skipped_due_to_limit", score=0,
                                    processingMs=None, reason="Lower level exceeded its time limit")
                    continue
                with self.lock:
                    self.job["progress"] = {"repetition": repetition, "taskId": task["id"],
                                             "category": task["category"], "level": task["level"],
                                             "startedAt": now()}
                limit = run["limitsSec"][task["level"] - 1]
                timed_out = threading.Event()
                def on_timeout():
                    timed_out.set()
                    self.stop.set()
                    self.ollama.cancel()
                timer = threading.Timer(limit, on_timeout)
                started = time.monotonic()
                timer.start()
                try:
                    category = REGISTRY[task["category"]]
                    if category.mode == "vision":
                        result = run_vision(self.ollama, self.root, task, run["setup"]["model"],
                                            run["setup"]["think"], run["setup"]["options"], self.stop)
                    elif category.mode == "tools":
                        result = run_tools(self.ollama, task, run["setup"]["model"],
                                           run["setup"]["think"], run["setup"]["options"], self.stop)
                    else:
                        result = self.ollama.generate(run["setup"]["model"], category.prompt(task),
                                                      run["setup"]["think"], run["setup"]["options"], self.stop)
                    elapsed = round((time.monotonic() - started) * 1000)
                    if timed_out.is_set():
                        raise TimeoutError("Processing limit exceeded")
                    if self.job.get("abortReason") == "user":
                        raise InterruptedError("Stopped by user")
                    subjective = REGISTRY[task["category"]].type == "subjective"
                    base = {"score": None, "processingMs": elapsed,
                            "raw": result["response"], "thinking": result["thinking"],
                            "metrics": result["metrics"],
                            "truncated": result["metrics"]["doneReason"] == "length",
                            "recordedAt": now()}
                    if "toolTrace" in result:
                        base["toolTrace"] = result["toolTrace"]
                    if not subjective:
                        base.update(grade_objective(task, result["response"]))
                        if category.mode == "tools" and not result["validToolCalls"]:
                            base.update(score=0, reason="No successful local tool call")
                        else:
                            format_review = review_format(task, result["response"], base)
                            if format_review:
                                base["formatReview"] = format_review
                    elif not result["response"].strip():
                        base.update(score=0, reason="Empty final answer")
                    self._save_task(run, task, repetition,
                                    "subjective_pending" if subjective and base["score"] is None else "scored", **base)
                except Exception as exc:
                    elapsed = round((time.monotonic() - started) * 1000)
                    if timed_out.is_set():
                        self._save_task(run, task, repetition, "timed_out", score=0,
                                        processingMs=elapsed, limitSec=limit,
                                        reason="Processing limit exceeded", recordedAt=now())
                    elif self.job.get("abortReason") == "user":
                        self._save_task(run, task, repetition, "aborted", score=None,
                                        processingMs=elapsed, reason="Stopped by user; partial answer discarded",
                                        recordedAt=now())
                        break
                    else:
                        self._save_task(run, task, repetition, "technical_error", score=None,
                                        processingMs=elapsed, reason=str(exc), recordedAt=now())
                        raise
                finally:
                    timer.cancel()
                    if timed_out.is_set() and self.job.get("abortReason") != "user":
                        self.stop.clear()
            if self.stop.is_set() or self.job["stopAfterTask"]:
                break
        stopped = self.stop.is_set() or self.job["stopAfterTask"]
        self.store.update(lambda state: run.update(
            status="aborted" if stopped else "completed", endedAt=now()))
        try:
            self.ollama.unload(run["setup"]["model"])
        except Exception as exc:
            self.store.update(lambda state: run.update(unloadWarning=str(exc)))

    def _save_task(self, run, task, repetition, status, **data):
        record = {"taskId": task["id"], "category": task["category"], "level": task["level"],
                  "repetition": repetition, "status": status, **data}
        def save(state):
            for index, previous in enumerate(run["tasks"]):
                if previous["taskId"] == task["id"] and previous["repetition"] == repetition:
                    run["tasks"][index] = record
                    break
            else:
                run["tasks"].append(record)
        self.store.update(save)

    def start_pull(self, name):
        if not isinstance(name, str) or len(name) > 120 or ".." in name or not re.fullmatch(r"[a-zA-Z0-9._:/-]+", name):
            raise ValueError("Invalid Ollama model name")
        self._start("pull", name)
        def work():
            self.ollama.pull(name, lambda item: self.job.update(progress={
                key: item.get(key) for key in ("status", "digest", "completed", "total")}), self.stop)
            self._finish("aborted" if self.stop.is_set() else "completed",
                         "Download stopped by user" if self.stop.is_set() else
                         f"{name} downloaded and verified by Ollama")
        self._launch(work)
        return self.status()

    def start_cloud(self, model, effort):
        self._start("cloud", f"{model} · {effort}")
        def log(line):
            with self.lock:
                self.job["log"] = (self.job["log"] + line)[-4000:]
        def work():
            pack = self.cloud_generator(model, effort, self.root / "cloud-work", log, self.stop)
            if self.stop.is_set():
                raise InterruptedError("Cloud generation stopped")
            result = self.store.add_pack(pack, f"Codex CLI · {model} · {effort}")
            if result.get("errors"):
                raise ValueError("Generated pack failed validation: " + "; ".join(result["errors"]))
            with self.lock:
                self.job["packId"] = result["pack"]["id"]
            self._finish("completed", f'Draft pack created: {result["pack"]["name"]}. Review and approve it before running.')
        self._launch(work)
        return self.status()

    def start_judge(self, run_id, judge_model, rejudge=False):
        run = self.store.run(run_id)
        pack = self.store.pack(run["packId"]) if run else None
        if not run or not pack or pack["hash"] != run["packHash"]:
            raise ValueError("Run or pack unavailable")
        eligible = [record for record in run["tasks"] if
                    (REGISTRY[record["category"]].type == "subjective" and isinstance(record.get("raw"), str)
                     and record["raw"].strip())
                    if rejudge or record["status"] in {"subjective_pending", "judge_error"}]
        if not eligible:
            raise ValueError("No subjective answers available for judging")
        self._start("judge", f'{judge_model} judging {run["setup"]["model"]}')
        with self.lock:
            self.job["runId"] = run_id
        self._launch(self._judge, run, pack, judge_model, eligible)
        return self.status()

    def _judge(self, run, pack, judge_model, eligible):
        failures = 0
        try:
            self._require_model(judge_model)
            self._unload_all()
            self.ollama.preload(judge_model, self.stop)
            for record in eligible:
                if self.stop.is_set():
                    break
                task = next(task for task in pack["tasks"] if task["id"] == record["taskId"])
                with self.lock:
                    self.job["progress"] = {"taskId": record["taskId"],
                                             "repetition": record["repetition"], "startedAt": now()}
                result = None
                try:
                    result = self.ollama.generate(
                        judge_model, judge_prompt(task, record["raw"]), False,
                        dict(JUDGE_OPTIONS), self.stop, format_schema=judge_schema(task))
                    grade = parse_judge_answer(task, result)
                    def apply(state):
                        if isinstance(record.get("score"), (int, float)) and record.get("judge"):
                            record.setdefault("gradeHistory", []).append({
                                "score": record["score"], "reason": record.get("reason"),
                                "breakdown": record.get("breakdown"), "judge": record["judge"]})
                        record.update(grade)
                        record.update(status="scored", judge={
                            "model": judge_model, "gradedAt": now(),
                            "rubricHash": content_hash(task["criteria"]),
                            "schemaVersion": SCHEMA_VERSION, "options": dict(JUDGE_OPTIONS),
                            "raw": result["response"], "metrics": result.get("metrics")},
                            regradeError=None)
                    self.store.update(apply)
                except Exception as exc:
                    if self.stop.is_set():
                        break
                    failures += 1
                    failure = {"model": judge_model, "failedAt": now(),
                               "kind": exc.kind if isinstance(exc, JudgeOutputError) else "request_error",
                               "message": str(exc)[:600], "schemaVersion": SCHEMA_VERSION,
                               "options": dict(JUDGE_OPTIONS),
                               "raw": result.get("response") if result else None,
                               "thinking": result.get("thinking") if result else None,
                               "metrics": result.get("metrics") if result else None}
                    def save_failure(state):
                        record.setdefault("judgeFailures", []).append(failure)
                        if isinstance(record.get("score"), (int, float)):
                            record["regradeError"] = str(exc)
                        else:
                            record.update(status="judge_error", reason=str(exc))
                    self.store.update(save_failure)
        finally:
            try:
                self.ollama.unload(judge_model)
            except Exception:
                pass
        if self.stop.is_set():
            self._finish("aborted", "Judging stopped by user")
        elif failures:
            self._finish("completed_with_errors",
                         f"Judging finished with {failures} ungraded or failed regrade(s)")
        else:
            self._finish("completed", "Judging finished; inspect the rubric breakdowns")

    def check(self):
        if not self.busy() or self.job["type"] != "benchmark":
            return {"job": self.status(), "eta": None}
        run = self.store.run(self.job["runId"])
        pack = self.store.pack(run["packId"])
        history = [task for prior in self.store.state["runs"] for task in prior["tasks"]
                   if task["status"] in {"scored", "subjective_pending"}
                   and isinstance(task.get("processingMs"), (int, float))]
        remaining, evidence, total, covered, variance = 0, 0, 0, 0, 0.0
        details = []
        for repetition in range(1, run["repetitions"] + 1):
            for task in ordered_tasks(pack):
                if any(record["taskId"] == task["id"] and record["repetition"] == repetition
                       and record["status"] in COMPLETE for record in run["tasks"]):
                    continue
                samples = [item["processingMs"] for item in history if item["taskId"] == task["id"]]
                prior = [item["processingMs"] for item in history
                         if item["category"] == task["category"] and item["level"] == task["level"] - 1]
                estimate = statistics.median(samples) if samples else (
                    statistics.median(prior) * 1.5 if prior else 60000)
                progress = self.job.get("progress")
                elapsed = 0
                if progress and progress["taskId"] == task["id"] and progress["repetition"] == repetition:
                    elapsed = max(0, (datetime.now(timezone.utc) -
                                      datetime.fromisoformat(progress["startedAt"].replace("Z", "+00:00"))).total_seconds() * 1000)
                remaining += max(0, estimate - elapsed)
                evidence += len(samples)
                total += 1
                if samples:
                    covered += 1
                if len(samples) > 1:
                    mean = statistics.mean(samples)
                    variance += min(1, statistics.pstdev(samples) / mean) if mean else 0
                details.append({"taskId": task["id"], "repetition": repetition,
                                "estimateMs": round(estimate), "sampleCount": len(samples)})
        depth = min(1, evidence / (total * 3)) if total else 0
        coverage = covered / total if total else 0
        consistency = 1 - variance / covered if covered else 0
        score = round(100 * (0.05 + 0.65 * coverage * depth + 0.3 * coverage * consistency))
        return {"job": self.status(), "eta": {
            "remainingMs": round(remaining),
            "finishAt": (datetime.now(timezone.utc) + timedelta(milliseconds=remaining)).isoformat().replace("+00:00", "Z"),
            "confidence": "high" if score >= 70 else "medium" if score >= 35 else "low",
            "confidenceScore": score, "completedSamples": evidence,
            "remainingTasks": total, "details": details}}
