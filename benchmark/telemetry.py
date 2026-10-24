"""Low-frequency, local benchmark resource samples in a separate JSONL file."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import statistics
import threading
import time
import uuid

from .store import now
from .system import command, memory


INTERVAL_SECONDS = 30


def _cpu_times():
    if os.name != "nt":
        return None
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                  ctypes.byref(user)):
        return None
    def ticks(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime
    return ticks(idle), ticks(kernel) + ticks(user)


def _gpu_sample():
    candidates = [shutil.which("nvidia-smi.exe"),
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
    for executable in filter(None, candidates):
        result = command([executable, "--query-gpu=memory.used,utilization.gpu,clocks.gr,power.draw,temperature.gpu",
                          "--format=csv,noheader,nounits"], timeout=4)
        if not result or result.returncode or not result.stdout.strip():
            continue
        values = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
        if len(values) != 5:
            continue
        def number(value):
            try:
                return float(value)
            except ValueError:
                return None
        used, busy, clock, watts, temperature = map(number, values)
        return {"usedMiB": used, "utilizationPercent": busy, "clockMHz": clock,
                "powerW": watts, "temperatureC": temperature}
    return None


def capture(previous_cpu=None):
    """Return one sample and CPU counters for the next interval."""
    started = time.perf_counter()
    current_cpu = _cpu_times()
    cpu_percent = None
    if current_cpu and previous_cpu:
        idle = current_cpu[0] - previous_cpu[0]
        total = current_cpu[1] - previous_cpu[1]
        if total > 0:
            cpu_percent = round(max(0, min(100, 100 * (1 - idle / total))), 1)
    total_ram, free_ram = memory()
    gpu = _gpu_sample()
    sample = {"at": now(), "cpuPercent": cpu_percent,
              "freeRamGiB": round(free_ram / 2**30, 2) if total_ram else None,
              "totalRamGiB": round(total_ram / 2**30, 2) if total_ram else None,
              "gpu": gpu, "sampleMs": round((time.perf_counter() - started) * 1000, 2)}
    return sample, current_cpu


def telemetry_path(data_directory: Path, run_id: str):
    return data_directory / "telemetry" / f"{uuid.UUID(run_id)}.jsonl"


def read_samples(data_directory: Path, run_id: str):
    path = telemetry_path(data_directory, run_id)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def summarize(samples, interval=INTERVAL_SECONDS):
    costs = [sample["sampleMs"] for sample in samples if isinstance(sample.get("sampleMs"), (int, float))]
    energy, covered = 0.0, 0.0
    for before, after in zip(samples, samples[1:]):
        watts_a = (before.get("gpu") or {}).get("powerW")
        watts_b = (after.get("gpu") or {}).get("powerW")
        if not isinstance(watts_a, (int, float)) or not isinstance(watts_b, (int, float)):
            continue
        try:
            seconds = (datetime.fromisoformat(after["at"].replace("Z", "+00:00")) -
                       datetime.fromisoformat(before["at"].replace("Z", "+00:00"))).total_seconds()
        except (KeyError, ValueError):
            continue
        if 0 < seconds <= interval * 2.5:
            energy += (watts_a + watts_b) / 2 * seconds / 3600
            covered += seconds
    return {"intervalSec": interval, "sampleCount": len(samples),
            "medianSampleMs": round(statistics.median(costs), 2) if costs else None,
            "maxSampleMs": round(max(costs), 2) if costs else None,
            "gpuEnergyWh": round(energy, 3) if covered else None,
            "energyCoverageSec": round(covered, 1)}


class TelemetryRecorder:
    def __init__(self, data_directory: Path, run_id: str, sampler=capture,
                 interval=INTERVAL_SECONDS):
        self.path = telemetry_path(data_directory, run_id)
        self.sampler, self.interval = sampler, interval
        self.samples = []
        self.previous_cpu = None
        self.stop_event = threading.Event()
        self.thread = None
        self.errors = []

    def _sample(self):
        try:
            sample, self.previous_cpu = self.sampler(self.previous_cpu)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
            self.samples.append(sample)
        except Exception as exc:
            self.errors.append(str(exc)[:300])

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sample()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            self._sample()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self._sample()
        return {**summarize(self.samples, self.interval), "errors": self.errors[:5]}
