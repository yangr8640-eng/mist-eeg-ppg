"""Independent, deterministic implementation of the referenced MIST task rules.

The experimental controller owns presentation timing and recording.  This module
only generates questions and adapts the stress difficulty from completed trials.
"""

from __future__ import annotations

import math
import random
from typing import Any


FIXATION_SECONDS = 0.35
FEEDBACK_SECONDS = 0.85
STRESS_MIN_SECONDS = 1.1
STRESS_MAX_SECONDS = 4.5


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _valid_rt(value: Any) -> float | None:
    """Ignore missing or invalid reaction times when calibrating difficulty."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


class MistTask:
    """Arithmetic trials for practice, control, or stress.

    ``control_trials`` must contain only completed, accepted control trials.
    Reaction times are seconds from question presentation.  Call
    ``record_result`` once for each completed question, never for a question
    interrupted by the stage deadline or a device failure.
    """

    def __init__(
        self,
        stage_key: str,
        control_trials: list[dict] | None = None,
        seed: int | None = None,
    ) -> None:
        if stage_key not in {"practice", "control", "stress"}:
            raise ValueError(f"Unsupported arithmetic stage: {stage_key}")
        self.stage_key = stage_key
        self._random = random.Random(seed)
        self._results: list[dict[str, Any]] = []
        control = list(control_trials or [])
        control_accuracy = (
            100.0 * sum(bool(t.get("correct", False)) for t in control) / len(control)
            if control
            else 0.0
        )
        correct_rts = sorted(
            rt
            for t in control
            if t.get("correct", False)
            and (rt := _valid_rt(t.get("rt"))) is not None
        )
        initial_stress_limit = (
            _clamp(correct_rts[len(correct_rts) // 2] * 0.85,
                   STRESS_MIN_SECONDS, STRESS_MAX_SECONDS)
            if len(correct_rts) >= 3
            else 3.2
        )
        self.limit_seconds = {
            "practice": 8.0,
            "control": 10.0,
            "stress": initial_stress_limit,
        }[stage_key]
        self.target_percent = _clamp(control_accuracy + 15.0, 75.0, 95.0)
        self.peer_percent = _clamp(
            control_accuracy + 8.0, 70.0, max(70.0, self.target_percent - 2.0)
        )

    def new_question(self) -> dict:
        if self.stage_key == "practice":
            a = self._random.randint(2, 12)
            b = self._random.randint(2, 12)
            operator = self._random.choice(("+", "-"))
        elif self.stage_key == "control":
            a = self._random.randint(6, 24)
            b = self._random.randint(2, 18)
            operator = self._random.choice(("+", "-", "*"))
        else:
            a = self._random.randint(12, 48)
            b = self._random.randint(3, 19)
            operator = self._random.choice(("+", "-", "*"))
        answer = {"+": a + b, "-": a - b, "*": a * b}[operator]
        display_operator = "×" if operator == "*" else operator
        comparison = ""
        if self.stage_key == "stress":
            comparison = (
                f"当前正确率 {self._accuracy_percent():.0f}%　"
                f"同伴平均 {self.peer_percent:.0f}%　"
                f"目标 {self.target_percent:.0f}%"
            )
        return {
            "prompt": f"{a} {display_operator} {b} = ?",
            "a": a,
            "b": b,
            "operator": operator,
            "correct_answer": answer,
            "limit_seconds": self.limit_seconds,
            "comparison": comparison,
        }

    def feedback(self, correct: bool, timed_out: bool) -> str:
        if self.stage_key == "stress":
            if timed_out:
                return "回答超时，请加快速度，努力达到目标。"
            if correct:
                return "回答正确，请继续保持速度。"
            return "回答错误，表现低于目标，请更加努力。"
        if timed_out:
            return "本题时间已到。"
        return "回答正确。" if correct else "回答错误。"

    def record_result(
        self, correct: bool, rt: float | None, timed_out: bool = False
    ) -> None:
        self._results.append({
            "correct": bool(correct) and not timed_out,
            "rt": _valid_rt(rt),
            "timed_out": bool(timed_out),
        })
        if self.stage_key == "stress" and len(self._results) >= 5:
            recent = self._results[-8:]
            accuracy = sum(item["correct"] for item in recent) / len(recent)
            adjustment = -0.18 if accuracy > 0.55 else 0.12
            self.limit_seconds = _clamp(
                self.limit_seconds + adjustment,
                STRESS_MIN_SECONDS,
                STRESS_MAX_SECONDS,
            )

    def iti_seconds(self) -> float:
        return self._random.uniform(0.25, 0.55)

    def _accuracy_percent(self) -> float:
        return (
            100.0 * sum(r["correct"] for r in self._results) / len(self._results)
            if self._results
            else 0.0
        )

    def summary(self) -> dict:
        correct_rts = [
            r["rt"] for r in self._results if r["correct"] and r["rt"] is not None
        ]
        return {
            "stage_key": self.stage_key,
            "trial_count": len(self._results),
            "correct_count": sum(r["correct"] for r in self._results),
            "timeout_count": sum(r["timed_out"] for r in self._results),
            "accuracy": self._accuracy_percent() / 100.0,
            "accuracy_percent": self._accuracy_percent(),
            "mean_correct_rt": (
                sum(correct_rts) / len(correct_rts) if correct_rts else None
            ),
            "limit_seconds": self.limit_seconds,
            "target_percent": self.target_percent,
            "peer_percent": self.peer_percent,
        }
