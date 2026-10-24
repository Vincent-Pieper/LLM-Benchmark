"""Preview historical scores under different time limits without changing data."""
import math


def valid_limits(limits):
    return (isinstance(limits, list) and len(limits) == 5
            and all(type(value) is int and 1 <= value <= 86400 for value in limits))


def project_run(run, limits):
    if not valid_limits(limits):
        raise ValueError("Five valid processing limits required")
    category_ids = run.get("categoryIds") or ["schema", "decision", "interpretation"]
    records = []
    for repetition in range(1, run["repetitions"] + 1):
        for category_id in category_ids:
            blocked = False
            for level in range(1, 6):
                original = next((task for task in run["tasks"] if task.get("repetition") == repetition
                                 and task.get("category") == category_id and task.get("level") == level), None)
                base = {"repetition": repetition, "category": category_id, "level": level}
                if blocked:
                    records.append({**base, "status": "skipped_due_to_limit", "score": 0,
                                    "reason": "Earlier level exceeds current limit"})
                elif original is None:
                    records.append({**base, "status": "not_tested", "score": None})
                elif original["status"] in {"scored", "subjective_pending"} and isinstance(original.get("processingMs"), (int, float)) and original["processingMs"] > limits[level - 1] * 1000:
                    records.append({**base, "status": "timed_out", "score": 0,
                                    "reason": "Recorded processing time exceeds current limit"})
                    blocked = True
                elif original["status"] == "timed_out" and limits[level - 1] * 1000 > original.get("processingMs", 0):
                    records.append({**base, "status": "needs_retry", "score": None,
                                    "reason": "The previous timeout does not prove this model would exceed the new limit"})
                elif original["status"] == "skipped_due_to_limit":
                    records.append({**base, "status": "needs_retry", "score": None,
                                    "reason": "Task was skipped under previous limits"})
                else:
                    if original["status"] == "timed_out":
                        blocked = True
                    review = original.get("formatReview") or {}
                    effective = review.get("contentScore", original.get("score"))
                    records.append({**base, "status": original["status"], "score": effective,
                                    "strictScore": original.get("score") if review else None,
                                    "reason": original.get("reason", "")})
    category_scores = []
    for category_id in category_ids:
        relevant = [item for item in records if item["category"] == category_id]
        complete = len(relevant) == 5 * run["repetitions"] and all(
            isinstance(item.get("score"), (int, float)) and math.isfinite(item["score"]) for item in relevant)
        category_scores.append({"category": category_id,
                                "score": round(sum(item["score"] for item in relevant) / run["repetitions"], 2) if complete else None,
                                "pending": sum(item.get("score") is None for item in relevant)})
    complete = all(item["score"] is not None for item in category_scores)
    return {"runId": run["id"], "limitsSec": limits, "records": records,
            "categories": category_scores,
            "total": round(sum(item["score"] for item in category_scores), 2) if complete else None}
