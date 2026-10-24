"""Structured local judging contract and failure classification."""
from __future__ import annotations

import json

from .core import normalize_judge


SCHEMA_VERSION = "judge-json-schema-v1"
JUDGE_OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 4096}


class JudgeOutputError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def judge_schema(task: dict) -> dict:
    criteria = task["criteria"]
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array", "minItems": len(criteria), "maxItems": len(criteria),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": [item["id"] for item in criteria]},
                        "earned": {"type": "number", "minimum": 0,
                                   "maximum": max(item["max"] for item in criteria)},
                    },
                    "required": ["id", "earned"], "additionalProperties": False,
                },
            },
            "rationale": {"type": "string"},
        },
        "required": ["scores"], "additionalProperties": False,
    }


def judge_prompt(task: dict, candidate: str) -> str:
    schema = judge_schema(task)
    return (
        "You are grading a blind candidate answer. The model identity is hidden. "
        "Compare meaning, not wording; accept other valid reasoning. Use the reference "
        "only as a full-credit guide, not as the only allowed wording. Penalize claims "
        "unsupported by the passage. Score every rubric criterion exactly once. "
        "Return only a JSON object matching this schema. Keep the rationale to one "
        f"concise sentence. Schema: {json.dumps(schema, ensure_ascii=False)}\n\n"
        f'Passage: {task["passage"]}\nQuestion: {task["question"]}\n'
        f'Full-credit reference: {task["reference"]}\n'
        f'Rubric: {json.dumps(task["criteria"], ensure_ascii=False)}\n'
        f'Candidate answer: {candidate}'
    )


def parse_judge_answer(task: dict, result: dict) -> dict:
    raw = result.get("response") or ""
    if not raw.strip():
        reason = (result.get("metrics") or {}).get("doneReason") or "unknown"
        raise JudgeOutputError("empty_final", f"Judge returned no final answer (done reason: {reason})")
    try:
        judgment = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JudgeOutputError("invalid_json", f"Judge returned invalid JSON: {exc}") from exc
    try:
        return normalize_judge(task, judgment)
    except (ValueError, TypeError) as exc:
        raise JudgeOutputError("invalid_grade", f"Judge grade failed rubric validation: {exc}") from exc
