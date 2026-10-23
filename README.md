# LLM Benchmark 0.1

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

The initial Python migration preserves the three categories and the existing
JSON state format. To add a category later, implement and register a
`Category` in `benchmark/categories.py`; the runner and results API use
that registry.

Run automated checks without Ollama:

```powershell
py -3 -m unittest discover -s tests -v
```

The scorer for subjective answers is still experimental. A local judge can
over-credit an empty or incomplete answer; inspect its raw answer and rubric
before relying on those scores. Existing grades are retained unchanged.
