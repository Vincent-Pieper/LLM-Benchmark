"""Atomic JSON state store. Runtime data lives outside version control."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import socket
import threading
from datetime import datetime, timezone
from .core import content_hash, new_id, validate_pack


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = directory / "state.json"
        self.lock = threading.RLock()
        if self.file.exists():
            self.state = json.loads(self.file.read_text(encoding="utf-8-sig"))
        else:
            self.state = {
                "schemaVersion": 1,
                "profiles": [{"id": "local", "name": socket.gethostname(), "createdAt": now()}],
                "packs": [], "runs": [],
                "settings": {"limitsSec": [3600, 7200, 14400, 21600, 28800],
                             "repetitions": 1, "judgeModel": ""},
            }
            self.save()
        if not self.state.get("profiles"):
            self.state["profiles"] = [{"id": "local", "name": socket.gethostname(), "createdAt": now()}]
            self.save()

    def save(self):
        with self.lock:
            temporary = self.file.with_name(f"{self.file.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.file)

    def update(self, action):
        with self.lock:
            result = action(self.state)
            self.save()
            return result

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def add_pack(self, body, source="import"):
        errors = validate_pack(body)
        if errors:
            return {"errors": errors}
        entry = copy.deepcopy(body)
        entry.update({"id": new_id(), "hash": content_hash(body), "approved": False,
                      "createdAt": now(), "source": source})
        self.update(lambda state: state["packs"].append(entry))
        return {"pack": entry}

    def pack(self, pack_id):
        with self.lock:
            return next((pack for pack in self.state["packs"] if pack["id"] == pack_id), None)

    def run(self, run_id):
        with self.lock:
            return next((run for run in self.state["runs"] if run["id"] == run_id), None)
