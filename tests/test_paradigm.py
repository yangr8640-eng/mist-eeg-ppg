import math
import unittest

from mist_app.paradigm import (
    FEEDBACK_SECONDS,
    FIXATION_SECONDS,
    MistTask,
)


class MistTaskTests(unittest.TestCase):
    def test_question_ranges_and_arithmetic(self):
        expected = {
            "practice": ((2, 12), (2, 12), {"+", "-"}, 8.0),
            "control": ((6, 24), (2, 18), {"+", "-", "*"}, 10.0),
            "stress": ((12, 48), (3, 19), {"+", "-", "*"}, 3.2),
        }
        for stage, (a_range, b_range, operators, limit) in expected.items():
            task = MistTask(stage, seed=23)
            observed = set()
            for _ in range(300):
                q = task.new_question()
                a, b, op = q["a"], q["b"], q["operator"]
                self.assertTrue(a_range[0] <= a <= a_range[1])
                self.assertTrue(b_range[0] <= b <= b_range[1])
                self.assertIn(op, operators)
                self.assertEqual(q["correct_answer"],
                                 {"+": a+b, "-": a-b, "*": a*b}[op])
                self.assertEqual(q["limit_seconds"], limit)
                self.assertEqual(bool(q["comparison"]), stage == "stress")
                observed.add(op)
            self.assertEqual(observed, operators)

    def test_calibration_uses_upper_middle_of_correct_control_rts(self):
        trials = [{"correct": True, "rt": rt} for rt in (4, 1, 3, 2)]
        trials.append({"correct": False, "rt": 0.1})
        self.assertAlmostEqual(MistTask("stress", trials).limit_seconds, 2.55)

    def test_calibration_fallback_and_invalid_rts(self):
        trials = [{"correct": True, "rt": value}
                  for value in (None, math.nan, math.inf, -1, True, "bad", 1, 2)]
        self.assertEqual(MistTask("stress", trials).limit_seconds, 3.2)
        self.assertEqual(MistTask("stress").limit_seconds, 3.2)

    def test_initial_calibration_bounds(self):
        for rt, expected in ((0.1, 1.1), (100, 4.5)):
            trials = [{"correct": True, "rt": rt}] * 3
            self.assertEqual(MistTask("stress", trials).limit_seconds, expected)

    def test_adaptation_starts_after_five_results(self):
        task = MistTask("stress")
        for _ in range(4):
            task.record_result(True, 1.0)
            self.assertEqual(task.limit_seconds, 3.2)
        task.record_result(True, 1.0)
        self.assertAlmostEqual(task.limit_seconds, 3.02)

    def test_adaptation_uses_recent_eight_and_bounds(self):
        task = MistTask("stress")
        for _ in range(100):
            task.record_result(True, 0.5)
        self.assertEqual(task.limit_seconds, 1.1)
        for _ in range(8):
            task.record_result(False, None, timed_out=True)
        before = task.limit_seconds
        task.record_result(False, 1.0)
        self.assertAlmostEqual(task.limit_seconds, before + 0.12)
        for _ in range(100):
            task.record_result(False, None)
        self.assertEqual(task.limit_seconds, 4.5)

    def test_threshold_and_timeout_are_errors(self):
        task = MistTask("stress")
        for correct in (True, True, False, False):
            task.record_result(correct, 1)
        task.record_result(True, None, timed_out=True)
        self.assertAlmostEqual(task.limit_seconds, 3.32)
        self.assertEqual(task.summary()["correct_count"], 2)
        self.assertEqual(task.summary()["timeout_count"], 1)

    def test_neutral_stages_do_not_adapt(self):
        for stage, limit in (("practice", 8), ("control", 10)):
            task = MistTask(stage)
            for _ in range(25):
                task.record_result(True, 1)
            self.assertEqual(task.limit_seconds, limit)

    def test_comparison_defaults_and_bounds(self):
        low = MistTask("stress")
        high = MistTask("stress", [{"correct": True, "rt": 1}] * 10)
        self.assertEqual((low.target_percent, low.peer_percent), (75, 70))
        self.assertEqual((high.target_percent, high.peer_percent), (95, 93))
        mid = MistTask("stress", [{"correct": i < 7, "rt": 1} for i in range(10)])
        self.assertEqual((mid.target_percent, mid.peer_percent), (85, 78))

    def test_summary_and_deterministic_timing(self):
        task = MistTask("control", seed=2)
        task.record_result(True, 1.5)
        task.record_result(True, 2.5)
        task.record_result(False, 0.1)
        task.record_result(False, None, timed_out=True)
        summary = task.summary()
        self.assertEqual(summary["accuracy"], 0.5)
        self.assertEqual(summary["mean_correct_rt"], 2)
        self.assertEqual(summary["trial_count"], 4)
        left, right = MistTask("practice", seed=91), MistTask("practice", seed=91)
        for _ in range(25):
            self.assertEqual(left.new_question(), right.new_question())
            value = left.iti_seconds()
            self.assertEqual(value, right.iti_seconds())
            self.assertTrue(0.25 <= value <= 0.55)
        self.assertEqual((FIXATION_SECONDS, FEEDBACK_SECONDS), (0.35, 0.85))

    def test_unsupported_arithmetic_stage(self):
        with self.assertRaises(ValueError):
            MistTask("eyes_open")


if __name__ == "__main__":
    unittest.main()
