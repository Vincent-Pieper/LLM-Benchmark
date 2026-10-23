"""Optional Codex CLI pack generation; invoked only by an explicit UI action."""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess
import threading
from .system import codex_exe

EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]


def generation_prompt():
    return """Create a fresh English-language, versioned mini-benchmark pack for LOCAL language models. Output exactly one JSON object and nothing else. Do not read files or use tools; all needed requirements are below. Do not use personal data or current events.

JSON shape: {"name":"...","version":"1.0","language":"en","description":"...","tasks":[15 task objects]}.
Exactly 3 categories, 5 difficulty levels per category, unique ids. Categories: schema, decision, interpretation. Every task has id (string), category, level (integer 1..5). Difficulty should rise materially in input size, interacting rules, ambiguity, or number of constraints. Avoid trick questions and vague labels.

Schema Recognition tasks: {id,category:"schema",level,records:[flat JSON objects],steps:[operations]}. Allowed operations in order: {op:"filter",where:condition}; {op:"select",fields:[field names]}; {op:"dedupe",field:"field"}; {op:"group_sum",by:"group_field",field:"numeric_field",as:"total"}; {op:"sort",field:"field",direction:"asc"|"desc"}. Condition: {field:"name",op:"eq"|"ne"|"gt"|"gte"|"lt"|"lte"|"in",value:any}, or {all:[conditions]}, or {any:[conditions]}. The application computes the answer, so do not supply expected outputs. Keep all steps valid in sequence: fields referenced later must still exist.

Decision Logic tasks: {id,category:"decision",level,cases:[{id:"...",...flat attributes}],rules:[{when:condition,choice:"..."}],defaultChoice:"..."}. Rules apply in order; first match wins. Cover boundary values, precedence and exceptions. Every case must have a unique id. The application computes expected decisions.

Evidence-Based Interpretation tasks: {id,category:"interpretation",level,passage:"...",question:"...",reference:"...",criteria:[{id:"...",description:"specific checkable fact or reasoning step",max:number}]}. This category is subjective and graded separately by a stateless local judge. Include a defensible full-credit reference and rubric. Permit alternative valid wording and reasoning, and explicitly reward warranted uncertainty. Use distinct criterion ids. Criterion max values can sum to any positive amount and will be normalized to 10.

The benchmark must measure useful work, not English prose style. Include data cleaning, patterns, conflicting signals, rule precedence, evidence vs inference, and uncertainty. Target strong local models; level 5 should challenge them without relying on obscure trivia. All tasks must be safe, self-contained, and under 100 records/cases. Verify internally that each objective task is well-formed and solvable. Do not include executable code, paths, secrets, network calls, or Markdown. Return JSON only."""


def parse_json(text):
    clean = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.I)
    clean = re.sub(r"\s*```\s*$", "", clean)
    return json.loads(clean)


def generate_pack(model, effort, directory: Path, on_log, stop: threading.Event):
    if not re.fullmatch(r"gpt-[a-z0-9.-]+", model):
        raise ValueError("Choose a Codex GPT model")
    if effort not in EFFORTS:
        raise ValueError("Invalid reasoning effort")
    executable = codex_exe()
    if not executable:
        raise RuntimeError("Codex CLI not found")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "codex-output.txt"
    args = [str(executable), "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "-s", "read-only", "-C", str(directory), "--skip-git-repo-check",
            "--ephemeral", "--ignore-user-config", "-o", str(output), "-"]
    process = subprocess.Popen(args, cwd=directory, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finished = threading.Event()
    def stop_when_requested():
        while not finished.wait(0.1):
            if stop.is_set():
                if process.poll() is None:
                    process.terminate()
                return
    threading.Thread(target=stop_when_requested, daemon=True).start()
    try:
        process.stdin.write(generation_prompt())
        process.stdin.close()
        for line in process.stdout:
            on_log(line)
            if stop.is_set():
                process.terminate()
                raise InterruptedError("Cloud generation stopped")
        code = process.wait()
        if code:
            raise RuntimeError(f"Codex CLI exited {code}")
        if not output.exists():
            raise RuntimeError("Codex CLI returned no result file")
        return parse_json(output.read_text(encoding="utf-8"))
    finally:
        finished.set()
        if process.poll() is None:
            process.terminate()
        output.unlink(missing_ok=True)
