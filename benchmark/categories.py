"""Independent benchmark categories. Register additional categories in CATEGORIES."""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Callable


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def condition(record: dict, rule: dict) -> bool:
    if not isinstance(rule, dict):
        raise ValueError("Invalid condition")
    if isinstance(rule.get("all"), list):
        if not rule["all"]:
            raise ValueError("Empty all condition")
        return all(condition(record, sub) for sub in rule["all"])
    if isinstance(rule.get("any"), list):
        if not rule["any"]:
            raise ValueError("Empty any condition")
        return any(condition(record, sub) for sub in rule["any"])
    field, op, target = rule.get("field"), rule.get("op"), rule.get("value")
    if not isinstance(field, str):
        raise ValueError("Condition field required")
    value = record.get(field)
    if op == "eq":
        return type(value) is type(target) and value == target
    if op == "ne":
        return type(value) is not type(target) or value != target
    if op == "in":
        return isinstance(target, list) and any(
            type(value) is type(item) and value == item for item in target)
    if op in {"gt", "gte", "lt", "lte"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return {"gt": lambda: value > target, "gte": lambda: value >= target,
                    "lt": lambda: value < target, "lte": lambda: value <= target}[op]()
        except TypeError:
            return False
    raise ValueError(f"Unknown comparison: {op}")


def schema_expected(task: dict) -> dict:
    rows = copy.deepcopy(task["records"])
    for step in task["steps"]:
        op = step.get("op")
        if op == "filter":
            rows = [row for row in rows if condition(row, step["where"])]
        elif op == "select":
            rows = [{field: row.get(field) for field in step["fields"]} for row in rows]
        elif op == "dedupe":
            seen = set()
            unique = []
            for row in rows:
                key = canonical(row.get(step["field"]))
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
            rows = unique
        elif op == "group_sum":
            groups = {}
            for row in rows:
                amount = row[step["field"]]
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
                    raise ValueError("group_sum requires finite numeric values")
                group = row.get(step["by"])
                key = canonical(group)
                if key not in groups:
                    groups[key] = [group, 0]
                groups[key][1] += amount
            rows = [{step["by"]: group, step.get("as") or "total": total}
                    for group, total in groups.values()]
        elif op == "sort":
            rows.sort(key=lambda row: (row.get(step["field"]) is None, row.get(step["field"])),
                      reverse=step["direction"] == "desc")
        else:
            raise ValueError(f"Unknown schema step: {op}")
    return {"result": rows}


def schema_prompt(task: dict) -> str:
    return "\n\n".join([
        'Apply the following data operations in order. Return only a JSON object with the key "result" containing the resulting array. Preserve sort order if a sort step exists; otherwise row order is not important.',
        f'Records: {compact(task["records"])}', f'Steps: {compact(task["steps"])}'])


def schema_grade(task: dict, answer: Any) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("result"), list):
        return {"score": 0, "reason": "Missing result array"}
    truth = [canonical(row) for row in schema_expected(task)["result"]]
    got = [canonical(row) for row in answer["result"]]
    matches = 0
    if any(step["op"] == "sort" for step in task["steps"]):
        matches = sum(a == b for a, b in zip(truth, got))
    else:
        remaining = got.copy()
        for row in truth:
            if row in remaining:
                matches += 1
                remaining.remove(row)
    denominator = len(truth) + len(got)
    return {"score": round(20 * matches / denominator, 2) if denominator else 10,
            "reason": f"{matches}/{len(truth)} rows matched; {len(got)} returned"}


def schema_validate(task: dict) -> None:
    records, steps = task.get("records"), task.get("steps")
    if not isinstance(records, list) or not isinstance(steps, list) or not steps:
        raise ValueError("records and steps required")
    if len(records) > 100 or len(steps) > 12:
        raise ValueError("task too large")
    for step in steps:
        if not isinstance(step, dict) or step.get("op") not in {"filter", "select", "dedupe", "group_sum", "sort"}:
            raise ValueError(f"invalid step {step}")
        if step["op"] == "select" and (not isinstance(step.get("fields"), list) or not step["fields"]):
            raise ValueError("select fields required")
        if step["op"] == "sort" and step.get("direction") not in {"asc", "desc"}:
            raise ValueError("sort direction required")
    schema_expected(task)


def decision_expected(task: dict) -> dict:
    decisions = []
    for case in task["cases"]:
        matched = next((rule for rule in task["rules"] if condition(case, rule["when"])), None)
        decisions.append({"id": case["id"], "choice": matched["choice"] if matched else task["defaultChoice"]})
    return {"decisions": decisions}


def decision_prompt(task: dict) -> str:
    return "\n\n".join([
        'Apply the policy rules in order; the first matching rule wins. Return only a JSON object with "decisions", an array of objects containing each case id and its choice.',
        f'Cases: {compact(task["cases"])}', f'Rules: {compact(task["rules"])}',
        f'Default choice: {compact(task["defaultChoice"])}'])


def decision_grade(task: dict, answer: Any) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("decisions"), list):
        return {"score": 0, "reason": "Missing decisions array"}
    truth = decision_expected(task)["decisions"]
    got = {}
    for item in answer["decisions"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            got.setdefault(item["id"], item.get("choice"))
    correct = sum(item["id"] in got and got[item["id"]] == item["choice"] for item in truth)
    truth_ids = {item["id"] for item in truth}
    extras = sum(not isinstance(item, dict) or item.get("id") not in truth_ids for item in answer["decisions"])
    return {"score": round(10 * max(0, correct - extras) / len(truth), 2),
            "reason": f"{correct}/{len(truth)} decisions correct" + (f", {extras} extra" if extras else "")}


def decision_validate(task: dict) -> None:
    cases, rules = task.get("cases"), task.get("rules")
    if not isinstance(cases, list) or not cases or not isinstance(rules, list) or not isinstance(task.get("defaultChoice"), str):
        raise ValueError("cases, rules and defaultChoice required")
    if len(cases) > 100 or len(rules) > 30:
        raise ValueError("task too large")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique strings")
    if any(not isinstance(rule.get("choice"), str) for rule in rules):
        raise ValueError("each rule needs a string choice")
    decision_expected(task)


def interpretation_prompt(task: dict) -> str:
    return (f'{task["passage"]}\n\nQuestion: {task["question"]}\n'
            'Answer using evidence from the passage. State uncertainty when the passage does not settle a claim.')


def interpretation_validate(task: dict) -> None:
    criteria = task.get("criteria")
    if (not isinstance(task.get("passage"), str) or len(task["passage"]) < 20
            or not isinstance(task.get("question"), str) or not isinstance(task.get("reference"), str)
            or not isinstance(criteria, list) or not criteria):
        raise ValueError("passage, question, reference and criteria required")
    ids = [criterion.get("id") for criterion in criteria]
    if (len(set(ids)) != len(ids)
            or any(not isinstance(c.get("id"), str) or not isinstance(c.get("description"), str)
                   or isinstance(c.get("max"), bool) or not isinstance(c.get("max"), (int, float))
                   or not math.isfinite(c["max"]) or c["max"] <= 0 for c in criteria)):
        raise ValueError("invalid rubric criteria")


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    type: str
    prompt: Callable[[dict], str]
    validate: Callable[[dict], None]
    expected: Callable[[dict], dict] | None = None
    grade: Callable[[dict, Any], dict] | None = None
    requires: tuple[str, ...] = ()


CATEGORIES = (
    Category("schema", "Schema Recognition", "objective", schema_prompt, schema_validate, schema_expected, schema_grade),
    Category("decision", "Decision Logic", "objective", decision_prompt, decision_validate, decision_expected, decision_grade),
    Category("interpretation", "Evidence-Based Interpretation", "subjective", interpretation_prompt, interpretation_validate),
)
REGISTRY = {category.id: category for category in CATEGORIES}
