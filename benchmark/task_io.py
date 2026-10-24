"""Deterministic local inputs for vision and tool-use benchmark tasks."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path

from .categories import compact, structured_prompt


def run_vision(ollama, root: Path, task, model, think, options, stop):
    relative = Path(task["image"])
    asset_root = (root / "assets").resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(asset_root) or not path.is_file():
        raise ValueError("Image fixture is unavailable")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != task["imageSha256"]:
        raise ValueError("Image fixture differs from the versioned task")
    return ollama.generate(model, structured_prompt(task), think, options, stop,
                           images=[base64.b64encode(data).decode("ascii")])


def _calculate(expression):
    if not isinstance(expression, str) or len(expression) > 100:
        raise ValueError("invalid expression")
    tree = ast.parse(expression, mode="eval")

    def value(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = value(node.operand)
            return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = value(node.left), value(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise ValueError("unsupported expression")

    result = value(tree.body)
    if not isinstance(result, (int, float)) or abs(result) > 1e12:
        raise ValueError("result out of range")
    return result


def _tool_definitions(task):
    if task["category"] == "research":
        return [{"type": "function", "function": {"name": "open_source",
                "description": "Read one source from the local, fixed benchmark dossier",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                               "required": ["id"]}}}]
    return [
        {"type": "function", "function": {"name": "lookup_entry",
         "description": "Read the value of a local benchmark record by id",
         "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"]}}},
        {"type": "function", "function": {"name": "calculate",
         "description": "Calculate a simple arithmetic expression using +, -, *, / and parentheses",
         "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                        "required": ["expression"]}}},
    ]


def _call_local(task, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if task["category"] == "research" and name == "open_source":
        item = next((item for item in task["sources"] if item["id"] == arguments.get("id")), None)
        if item is None:
            raise ValueError("source not found")
        return {"id": item["id"], "title": item["title"], "text": item["text"]}
    if task["category"] == "tools" and name == "lookup_entry":
        item = next((item for item in task["entries"] if item["id"] == arguments.get("id")), None)
        if item is None:
            raise ValueError("entry not found")
        return {"id": item["id"], "value": item["value"]}
    if task["category"] == "tools" and name == "calculate":
        return {"result": _calculate(arguments.get("expression"))}
    raise ValueError("tool not available")


def run_tools(ollama, task, model, think, options, stop):
    if task["category"] == "research":
        catalog = [{"id": item["id"], "title": item["title"]} for item in task["sources"]]
        material = task["material"] + "\nAvailable local sources: " + compact(catalog)
    else:
        material = task["material"] + "\nAvailable local record IDs: " + compact(
            [item["id"] for item in task["entries"]])
    prompt = structured_prompt({**task, "material": material})
    messages = [{"role": "user", "content": prompt + "\nUse the supplied local tools before answering."}]
    tools = _tool_definitions(task)
    trace, thinking = [], []
    totals = {"totalNs": 0, "loadNs": 0, "promptEvalCount": 0, "promptEvalNs": 0,
              "evalCount": 0, "evalNs": 0, "doneReason": "unknown"}
    mapping = {"totalNs": "total_duration", "loadNs": "load_duration",
               "promptEvalCount": "prompt_eval_count", "promptEvalNs": "prompt_eval_duration",
               "evalCount": "eval_count", "evalNs": "eval_duration"}
    for _ in range(12):
        result = ollama.chat(model, messages, tools, think, options, stop)
        for key, source in mapping.items():
            totals[key] += result.get(source) or 0
        totals["doneReason"] = result.get("done_reason") or "unknown"
        message = result.get("message") or {}
        thinking.append(message.get("thinking") or "")
        calls = message.get("tool_calls") or []
        if not calls:
            return {"response": message.get("content") or "", "thinking": "".join(thinking),
                    "metrics": totals, "toolTrace": trace,
                    "validToolCalls": sum(item["ok"] for item in trace)}
        messages.append(message)
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name") or "unknown"
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            try:
                output = _call_local(task, name, arguments)
                ok = True
            except (ValueError, TypeError, ZeroDivisionError, SyntaxError) as exc:
                output, ok = {"error": str(exc)}, False
            trace.append({"name": name, "arguments": arguments, "output": output, "ok": ok})
            messages.append({"role": "tool", "tool_name": name, "content": compact(output)})
    return {"response": "", "thinking": "".join(thinking), "metrics": totals,
            "toolTrace": trace, "validToolCalls": sum(item["ok"] for item in trace)}
