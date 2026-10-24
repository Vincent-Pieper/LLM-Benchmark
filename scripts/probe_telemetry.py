"""Measure the cost of resource polling without starting a model or benchmark."""
from __future__ import annotations

import argparse
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.telemetry import INTERVAL_SECONDS, capture  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()
    if not 2 <= args.samples <= 100:
        parser.error("samples must be between 2 and 100")
    costs = []
    previous = None
    for _ in range(args.samples):
        sample, previous = capture(previous)
        costs.append(sample["sampleMs"])
        time.sleep(0.5)
    median = statistics.median(costs)
    print(f"Samples: {len(costs)}")
    print(f"Query wall time: median {median:.2f} ms, max {max(costs):.2f} ms")
    print(f"Fraction of a {INTERVAL_SECONDS}s interval occupied by median query: "
          f"{median/(INTERVAL_SECONDS*1000)*100:.3f}%")
    print("This measures query time, not its effect on model throughput. "
          "A model A/B comparison still needs a user-started run.")


if __name__ == "__main__":
    main()
