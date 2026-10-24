"""Offline checks for format reviews and resource telemetry."""
import json
from pathlib import Path
import tempfile
import unittest

from benchmark.core import grade_objective
from benchmark.format_review import ARRAY_ADAPTER, FENCE_ADAPTER, review_format
from benchmark.projection import project_run
from benchmark.store import Store
from benchmark.telemetry import TelemetryRecorder, read_samples, summarize


ROOT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = json.loads((ROOT / "packs" / "broad-draft-v0.2.json").read_text(encoding="utf-8"))
        cls.ocr = next(task for task in cls.pack["tasks"] if task["id"] == "ocr-3")

    def test_bare_answers_array_is_scored_without_changing_strict_grade(self):
        answers = [{"id": item["id"], "answer": item["answer"]}
                   for item in self.ocr["questions"]]
        raw = "```json\n" + json.dumps(answers) + "\n```"
        strict = grade_objective(self.ocr, raw)
        review = review_format(self.ocr, raw, strict)
        self.assertEqual(0, strict["score"])
        self.assertEqual(10, review["contentScore"])
        self.assertEqual(0, review["strictScore"])
        self.assertEqual(ARRAY_ADAPTER, review["adapter"])
        self.assertFalse(review["originalFormatValid"])

    def test_adapter_rejects_missing_or_ambiguous_answers(self):
        answer = {"id": self.ocr["questions"][0]["id"], "answer": "correct"}
        for raw in (json.dumps([answer]), json.dumps([answer] * 3),
                    json.dumps([answer, {"id": "unknown", "answer": "x"}, answer]),
                    json.dumps([{**answer, "extra": "unsupported"}] * 3)):
            self.assertIsNone(review_format(self.ocr, raw))

    def test_markdown_fence_keeps_content_and_marks_format(self):
        expected = {"answers": [{"id": item["id"], "answer": item["answer"]}
                                for item in self.ocr["questions"]]}
        review = review_format(self.ocr, "```json\n" + json.dumps(expected) + "\n```")
        self.assertEqual(FENCE_ADAPTER, review["adapter"])
        self.assertEqual(10, review["strictScore"])
        self.assertEqual(10, review["contentScore"])

    def test_saved_results_keep_original_grade_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            pack = store.add_pack(self.pack)["pack"]
            answers = [{"id": item["id"], "answer": item["answer"]}
                       for item in self.ocr["questions"]]
            record = {"taskId": self.ocr["id"], "category": "ocr", "status": "scored",
                      "score": 0, "raw": json.dumps(answers)}
            store.state["runs"].append({"packId": pack["id"], "packHash": pack["hash"],
                                        "tasks": [record]})
            store.save()
            self.assertEqual(1, store.backfill_format_reviews())
            self.assertEqual(0, record["score"])
            self.assertEqual(10, record["formatReview"]["contentScore"])
            backup = Path(directory) / "state.before-format-review-v1.json"
            self.assertTrue(backup.is_file())
            original = json.loads(backup.read_text(encoding="utf-8"))
            self.assertNotIn("formatReview", original["runs"][0]["tasks"][0])
            self.assertEqual(0, store.backfill_format_reviews())

    def test_projection_uses_reviewed_content_without_erasing_strict_score(self):
        tasks = [{"repetition": 1, "category": "ocr", "level": level,
                  "status": "scored", "processingMs": 1000, "score": 0,
                  **({"formatReview": {"contentScore": 10}} if level == 1 else {})}
                 for level in range(1, 6)]
        projected = project_run({"id": "test", "categoryIds": ["ocr"],
                                 "repetitions": 1, "tasks": tasks}, [60] * 5)
        self.assertEqual(10, projected["categories"][0]["score"])
        self.assertEqual(0, projected["records"][0]["strictScore"])

    def test_telemetry_is_separate_and_energy_does_not_bridge_long_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            run_id = "96b9898e-bf76-4cff-b95c-0503621264fa"
            times = iter(("2026-10-24T12:00:00.000Z", "2026-10-24T12:00:10.000Z"))
            def sampler(previous):
                return ({"at": next(times), "cpuPercent": 20, "freeRamGiB": 40,
                         "gpu": {"powerW": 100}, "sampleMs": 12}, None)
            recorder = TelemetryRecorder(Path(directory), run_id, sampler=sampler)
            recorder.start()
            summary = recorder.stop()
            self.assertEqual(2, len(read_samples(Path(directory), run_id)))
            self.assertEqual(0.278, summary["gpuEnergyWh"])
            self.assertEqual(12, summary["medianSampleMs"])
            distant = [{"at": "2026-10-24T12:00:00Z", "gpu": {"powerW": 100}},
                       {"at": "2026-10-24T12:10:00Z", "gpu": {"powerW": 100}}]
            self.assertIsNone(summarize(distant)["gpuEnergyWh"])


if __name__ == "__main__":
    unittest.main()
