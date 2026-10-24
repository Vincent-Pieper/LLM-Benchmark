"""Pack validation and grading rules, independent of HTTP and Ollama."""
import hashlib
import json
import math
import re
import uuid
from .categories import REGISTRY


def new_id():
    return str(uuid.uuid4())


def content_hash(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pack_categories(pack):
    """Keep legacy three-category packs valid while allowing versioned subsets."""
    declared = pack.get("categoryIds") if isinstance(pack, dict) else None
    if declared is not None:
        return declared
    tasks = pack.get("tasks", []) if isinstance(pack, dict) else []
    return list(dict.fromkeys(task.get("category") for task in tasks if isinstance(task, dict)))


def grade_objective(task, raw):
    category = REGISTRY[task["category"]]
    if category.type != "objective":
        raise ValueError("Subjective tasks need a judge")
    clean = raw.strip() if isinstance(raw, str) else ""
    fenced = bool(re.fullmatch(r"```(?:json)?\s*.*\s*```", clean, re.I | re.S))
    if fenced:
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        answer = json.loads(clean)
    except (ValueError, AttributeError):
        return {"score": 0, "reason": "Invalid JSON", "formatValid": False}
    result = category.grade(task, answer)
    result["formatValid"] = not fenced
    if fenced:
        result["reason"] += "; JSON wrapped in Markdown"
    return result


def validate_pack(pack):
    errors = []
    if not isinstance(pack, dict):
        return ["Pack must be an object"]
    if not isinstance(pack.get("name"), str) or not pack["name"].strip():
        errors.append("Pack name required")
    tasks = pack.get("tasks")
    if not isinstance(tasks, list):
        return errors + ["Tasks must be an array"]
    category_ids = pack_categories(pack)
    if (not isinstance(category_ids, list) or not category_ids
            or any(not isinstance(item, str) or item not in REGISTRY for item in category_ids)
            or len(set(category_ids)) != len(category_ids)):
        return errors + ["categoryIds must list unique supported categories"]
    if len(tasks) != 5 * len(category_ids):
        errors.append(f"Exactly {5 * len(category_ids)} tasks required")
    for category_id in category_ids:
        relevant = [task for task in tasks if isinstance(task, dict) and task.get("category") == category_id]
        if len(relevant) != 5:
            errors.append(f"{category_id}: exactly five tasks required")
        for level in range(1, 6):
            if sum(task.get("level") == level for task in relevant) != 1:
                errors.append(f"{category_id}: one task at level {level} required")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"].strip() or task["id"] in ids:
            errors.append("Task ids must be unique nonempty strings")
            continue
        ids.add(task["id"])
        category = REGISTRY.get(task.get("category"))
        if not category or task.get("category") not in category_ids:
            errors.append(f'{task["id"]}: unknown category')
            continue
        level = task.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
            errors.append(f'{task["id"]}: level 1–5 required')
        try:
            category.validate(task)
            if category.type == "objective":
                expected = category.expected(task)
                if grade_objective(task, json.dumps(expected)).get("score") != 10:
                    raise ValueError("self-test failed")
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            errors.append(f'{task["id"]}: {exc}')
    return errors


def normalize_judge(task, judgment):
    scores = judgment.get("scores") if isinstance(judgment, dict) else None
    criteria = task["criteria"]
    if not isinstance(scores, list) or len(scores) != len(criteria):
        raise ValueError("Judge must score every criterion exactly once")
    by_id = {entry.get("id"): entry.get("earned") for entry in scores if isinstance(entry, dict)}
    if len(by_id) != len(criteria):
        raise ValueError("Judge must score every criterion exactly once")
    total = maximum = 0
    breakdown = []
    for criterion in criteria:
        earned = by_id.get(criterion["id"])
        if isinstance(earned, bool) or not isinstance(earned, (int, float)) or not math.isfinite(earned) or not 0 <= earned <= criterion["max"]:
            raise ValueError(f'Invalid score for {criterion["id"]}')
        total += earned
        maximum += criterion["max"]
        breakdown.append({"id": criterion["id"], "earned": earned, "max": criterion["max"]})
    return {"score": round(10 * total / maximum, 2),
            "reason": str(judgment.get("rationale") or "")[:3000],
            "breakdown": breakdown}
