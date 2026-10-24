# LLM Benchmark 0.2

Local Ollama benchmark app with a Python backend and a browser interface. Run
`Start Benchmark.cmd` to open the app. It listens only on
`http://127.0.0.1:37843` and requires Python 3.14 or newer. The backend uses
only the Python standard library.

The app keeps its state in `data/state.json`. This folder is ignored by Git:
it contains model answers, benchmark packs, hardware details and run history.
The existing JavaScript version is preserved in a separate local folder.

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
scores. Existing grades are retained unchanged.
