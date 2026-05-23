"""Read the latest evaluation artifact as structured JSON."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVAL_PATH = Path(__file__).resolve().parent / "data" / "EVAL.md"


@dataclass(frozen=True)
class EvalSnapshot:
    available: bool
    path: str
    generated_from: str
    task_a: list[dict[str, Any]]
    baselines: list[dict[str, Any]]
    task_b: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "path": self.path,
            "generated_from": self.generated_from,
            "task_a": self.task_a,
            "baselines": self.baselines,
            "task_b": self.task_b,
        }


def load_eval_snapshot(path: Path = EVAL_PATH) -> EvalSnapshot:
    """Parse the Markdown eval report produced by ``hackathon.eval.run``."""
    if not path.exists():
        return EvalSnapshot(
            available=False,
            path=str(path),
            generated_from="Run `make hackathon-eval` to create this artifact.",
            task_a=[],
            baselines=[],
            task_b=[],
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    task_a: list[dict[str, Any]] = []
    baselines: list[dict[str, Any]] = []
    task_b: list[dict[str, Any]] = []
    section = ""
    note = ""

    for line in lines:
        if line.startswith("Metrics on"):
            note = line
        elif line.startswith("## Task A — Review simulation"):
            section = "task_a"
        elif line.startswith("## Task A — Baselines"):
            section = "baselines"
        elif line.startswith("## Task B — Recommendation"):
            section = "task_b"
        elif not line.startswith("|") or line.startswith("|---"):
            continue
        elif section == "task_a" and not line.startswith("| Voice"):
            cells = _cells(line)
            if len(cells) >= 5:
                task_a.append(
                    {
                        "voice": cells[0],
                        "n": _int(cells[1]),
                        "rmse": _float(cells[2]),
                        "rouge_l": _float(cells[3]),
                        "bert_f1": _float_or_none(cells[4]),
                    }
                )
        elif section == "baselines" and not line.startswith("| Mode"):
            cells = _cells(line)
            if len(cells) >= 3:
                baselines.append(
                    {"mode": cells[0], "n": _int(cells[1]), "rmse": _float(cells[2])}
                )
        elif section == "task_b" and not line.startswith("| Mode"):
            cells = _cells(line)
            if len(cells) >= 5:
                task_b.append(
                    {
                        "mode": cells[0],
                        "n": _int(cells[1]),
                        "k": _int(cells[2]),
                        "hit_at_k": _float(cells[3]),
                        "ndcg_at_k": _float_or_none(cells[4]),
                    }
                )

    return EvalSnapshot(
        available=True,
        path=str(path),
        generated_from=note or "Latest `hackathon.eval.run` output.",
        task_a=task_a,
        baselines=baselines,
        task_b=task_b,
    )


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _int(value: str) -> int:
    return int(float(value))


def _float(value: str) -> float:
    return float(value)


def _float_or_none(value: str) -> float | None:
    return None if value in {"—", "-", ""} else float(value)
