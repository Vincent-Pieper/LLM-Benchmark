"""Versioned, deterministic review of recoverable objective output formats."""
from __future__ import annotations

import json
import re

from .categories import REGISTRY
from .core import grade_objective


REVIEW_VERSION = "format-review-v1"
ARRAY_ADAPTER = "answers-array-v1"
FENCE_ADAPTER = "json-fence-v1"


def review_format(task: dict, raw: str, strict_grade: dict | None = None) -> dict | None:
    """Return a separate content grade only for a safe, explicit transformation.

    The original score and raw answer are never changed. A bare answers array is
    accepted only when every expected question id appears exactly once and each
    item consists solely of ``id`` and ``answer``. No missing answer is invented.
    """
    if REGISTRY[task["category"]].type != "objective" or not isinstance(raw, str):
        return None
    strict = strict_grade if strict_grade is not None else grade_objective(task, raw)
    clean = raw.strip()
    fenced = bool(re.fullmatch(r"```(?:json)?\s*.*\s*```", clean, re.I | re.S))
    if fenced:
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
    except ValueError:
        return None
    if isinstance(value, list) and isinstance(task.get("questions"), list):
        expected_ids = [item["id"] for item in task["questions"]]
        if (len(value) != len(expected_ids)
                or any(not isinstance(item, dict) or set(item) != {"id", "answer"}
                       or not isinstance(item["id"], str) for item in value)
                or sorted(item["id"] for item in value) != sorted(expected_ids)):
            return None
        adapted = grade_objective(task, json.dumps({"answers": value}, ensure_ascii=False))
        return {"version": REVIEW_VERSION, "adapter": ARRAY_ADAPTER,
                "strictScore": strict.get("score"), "contentScore": adapted["score"],
                "reason": adapted.get("reason", ""), "originalFormatValid": False}
    if fenced and isinstance(value, dict):
        return {"version": REVIEW_VERSION, "adapter": FENCE_ADAPTER,
                "strictScore": strict.get("score"), "contentScore": strict.get("score"),
                "reason": strict.get("reason", ""), "originalFormatValid": False}
    return None
