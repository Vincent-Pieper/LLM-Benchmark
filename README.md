# LLM Benchmark 0.3

Local Ollama benchmark app with a Python backend and a browser interface. Run
`Start Benchmark.cmd` to open the app. It listens only on
`http://127.0.0.1:37843` and requires Python 3.14 or newer. The backend uses
only the Python standard library.

The app keeps its state in `data/state.json`. This folder is ignored by Git:
it contains model answers, benchmark packs, hardware details and run history.
The existing JavaScript version is preserved in a separate local folder.

The Results table can group runs by model or show every setup separately.
Three fixed-width setup tags show thinking mode, temperature, and output limit.
Thinking on/off have distinct colors, temperature uses a fixed 0–2 color
range, and finite output limits use a blue range anchored to the minimum and
maximum across all saved runs; open output has a separate pink color. Filters
still cover quantization, context size, model version, and the other setup
options. Click a score, task time, or
token-speed heading to sort; further clicks on other headings add tie-breakers.
Each area shows five level bars, score variation across repetitions, median
area time, and output tokens per second. Expand a run for time, input and
output token counts, output speed, and the raw answer for every task. Cold
model load is displayed separately. The wide-view button expands the table.

Some objective answers have correct content in a simple wrong wrapper, such
as an unambiguous bare `[{"id": ..., "answer": ...}]` array instead of an
`{"answers": [...]}` object. A versioned, deterministic review can score that
content without changing the original answer or strict grade. A small F marker
identifies results using such a review. Ambiguous, incomplete, or invalid
answers are never repaired. On the first v0.3 launch, saved objective answers
are reviewed too; `data/state.before-format-review-v1.json` is created before
any changes. The original strict scores remain in `data/state.json`, and the
interface shows both scores for reviewed tasks. Historical pack versions and
the draft warning are retained.

New benchmark runs also collect local CPU, RAM, and NVIDIA GPU samples every
30 seconds, with a sample at the start and end. GPU utilization, used VRAM,
clock, power and temperature are stored in `data/telemetry/<run-id>.jsonl`,
apart from the main state file. The run detail displays those samples, and
the data export includes them. Any energy estimate represents the *whole
GPU* during covered sample intervals, including other activity; it is not a
measurement of Ollama alone or of total PC energy. Old runs have no telemetry.
The `scripts/probe_telemetry.py` command measures the sampling query cost
without starting a model; it does not establish the impact on model throughput.

Set Max output tokens to `-1` for open-ended output when testing thinking
models. Per-task processing time limits still apply. No model is loaded or
tested merely by opening the app.

Imported packs start as drafts. Review and approve them in the app before a
benchmark can run. Clicking a run, download, cloud generation, or judge action
asks for confirmation. Opening the app by itself does not load a model or
start a benchmark. The optional cloud generator invokes the installed Codex
CLI only after the corresponding button is confirmed.

The existing three-category pack and all saved runs remain available. On
startup, the app also adds a **draft** pack with twelve areas and five levels
each (60 fixed tasks). It never starts those tasks automatically. The draft
contains Text Production, Text Understanding, Long Context, Local Research,
Tool Use, Safety Decisions, Code Reasoning, Code + Self-test, OCR, Schema
Recognition, Decision Logic, and Image Recognition. Results are shown per
selected pack; scores from different packs are not mixed.

Local Research uses fixed documents and Tool Use uses local lookup and
arithmetic functions. Neither accesses the Internet. OCR and Image
Recognition use versioned PNGs in `assets/`. Code tasks currently select
patches and tests; they do not execute generated code. These tasks are a
first comparison draft, not a calibrated benchmark. The optional Codex CLI
generator still creates a three-category draft.

Models can be deleted from the Models panel only after unloading them and
typing their full name to confirm. Deleting an Ollama model does not delete
saved benchmark results. The backend checks the selected model digest again
before deletion.

To add a category, implement and register a `Category` in
`benchmark/categories.py`; each versioned pack declares its own category
list. Rebuild the broad draft and its images with
`py -3 scripts/build_broad_pack.py` after changing the generator. Existing
pack versions and runs remain separate.

Run automated checks without Ollama:

```powershell
py -3 -m unittest discover -s tests -v
```

The scorer for subjective answers is still experimental. Empty final answers
now receive zero automatically, but a local judge can still over-credit an
incomplete answer; inspect its raw answer and rubric before relying on those
scores. Existing strict grades are retained unchanged.

Local judging now requests a JSON schema from Ollama and validates every
criterion after parsing. It requests non-thinking output with a 4096-token
ceiling; there is no automatic retry. A failed judge attempt keeps its model
name, available raw response and thinking output, stop reason, token metrics
and error in the local run record. It does not overwrite an existing grade.
The job reports `completed_with_errors` when any answer remains ungraded or a
regrade fails;
open the run and choose `Judge N answer(s)` to retry only ungraded answers.
Judging starts only after you confirm that action. Older failed attempts lack
raw judge output because earlier versions did not record it.
