"""Generate the Entivia agent pipeline flow diagram for the README.

Usage:
    python scripts/diagrams/generate_pipeline_flow.py

Writes to: docker/images/pulse/pipeline-flow.png

Re-run this whenever the pipeline stages change. The output is a 1600x900
flat-design PNG suitable for embedding in README.md and the docs site.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUTPUT = Path("docker/images/pulse/pipeline-flow.png")

STAGES = [
    {
        "title": "Schema\nIntelligence",
        "subtitle": "Introspects client DB",
        "fill": "#1E40AF",
        "optional": False,
    },
    {
        "title": "Profiling",
        "subtitle": "Behavioural profiles\n+ Qdrant embed",
        "fill": "#4F46E5",
        "optional": False,
    },
    {
        "title": "Model Training",
        "subtitle": "Random Forest\n(optional)",
        "fill": "#7C3AED",
        "optional": True,
    },
    {
        "title": "Risk Scoring",
        "subtitle": "ML or rules\n+ narratives",
        "fill": "#9333EA",
        "optional": False,
    },
    {
        "title": "Recommendation",
        "subtitle": "RAG + LLM\natomic queue swap",
        "fill": "#DB2777",
        "optional": False,
    },
]

DATASTORES = [
    {
        "label": "Client DB\n(read-only)",
        "fill": "#FEF3C7",
        "edge": "#D97706",
        "text": "#92400E",
        "anchor_stages": [0, 1, 2, 3, 4],
    },
    {
        "label": "Qdrant\n(embeddings + RAG)",
        "fill": "#CCFBF1",
        "edge": "#0D9488",
        "text": "#115E59",
        "anchor_stages": [1, 3, 4],
    },
    {
        "label": "Pulse DB\n(recommendations,\npipeline_runs)",
        "fill": "#E5E7EB",
        "edge": "#6B7280",
        "text": "#374151",
        "anchor_stages": [4],
    },
]


def _add_stage_box(ax, x: float, y: float, w: float, h: float, stage: dict) -> None:
    style = "round,pad=0.02,rounding_size=0.18"
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=style,
        linewidth=2,
        edgecolor="white" if not stage["optional"] else stage["fill"],
        facecolor=stage["fill"] if not stage["optional"] else "white",
        linestyle="-" if not stage["optional"] else (0, (4, 3)),
        zorder=3,
    )
    ax.add_patch(box)

    title_color = "white" if not stage["optional"] else stage["fill"]
    subtitle_color = "#E5E7EB" if not stage["optional"] else "#4B5563"

    ax.text(
        x + w / 2, y + h * 0.62, stage["title"],
        ha="center", va="center",
        fontsize=13, fontweight="bold",
        color=title_color, zorder=4,
    )
    ax.text(
        x + w / 2, y + h * 0.22, stage["subtitle"],
        ha="center", va="center",
        fontsize=9, color=subtitle_color, zorder=4,
    )


def _add_arrow(ax, x1: float, y1: float, x2: float, y2: float) -> None:
    arrow = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle="-|>",
        mutation_scale=18,
        linewidth=2.0,
        color="#374151",
        zorder=2,
    )
    ax.add_patch(arrow)


def _add_datastore(
    ax, x: float, y: float, w: float, h: float, store: dict, stage_centers_x: list[float],
) -> None:
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.12",
        linewidth=1.5,
        edgecolor=store["edge"],
        facecolor=store["fill"],
        linestyle=(0, (3, 2)),
        zorder=3,
    )
    ax.add_patch(box)
    ax.text(
        x + w / 2, y + h / 2, store["label"],
        ha="center", va="center",
        fontsize=8.5, color=store["text"], zorder=4,
    )

    cx = x + w / 2
    cy_top = y + h
    for stage_idx in store["anchor_stages"]:
        ax.plot(
            [cx, stage_centers_x[stage_idx]],
            [cy_top, 4.5],
            color=store["edge"],
            linewidth=1.0,
            linestyle=(0, (1.5, 2)),
            alpha=0.55,
            zorder=1,
        )


def generate() -> None:
    fig, ax = plt.subplots(figsize=(16, 9), dpi=110)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 9)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.text(
        8, 8.3, "Entivia Agent Pipeline",
        ha="center", va="center",
        fontsize=22, fontweight="bold", color="#111827",
    )
    ax.text(
        8, 7.7, "Raw client tables  →  ranked recommendation queue",
        ha="center", va="center",
        fontsize=12, color="#6B7280",
    )

    n = len(STAGES)
    box_w, box_h = 2.5, 1.6
    total_w = n * box_w + (n - 1) * 0.55
    start_x = (16 - total_w) / 2
    y = 5.0
    centers_x: list[float] = []

    for i, stage in enumerate(STAGES):
        x = start_x + i * (box_w + 0.55)
        _add_stage_box(ax, x, y, box_w, box_h, stage)
        centers_x.append(x + box_w / 2)
        if i < n - 1:
            _add_arrow(
                ax,
                x + box_w + 0.03,
                y + box_h / 2,
                x + box_w + 0.52,
                y + box_h / 2,
            )

    ds_w, ds_h = 2.6, 1.1
    gap = 0.6
    ds_total_w = len(DATASTORES) * ds_w + (len(DATASTORES) - 1) * gap
    ds_start_x = (16 - ds_total_w) / 2
    ds_y = 2.4

    for i, store in enumerate(DATASTORES):
        x = ds_start_x + i * (ds_w + gap)
        _add_datastore(ax, x, ds_y, ds_w, ds_h, store, centers_x)

    ax.text(
        8, 1.4, "Backing stores",
        ha="center", va="center",
        fontsize=10, fontweight="bold", color="#9CA3AF",
    )

    legend_patches = [
        mpatches.Patch(facecolor="#7C3AED", edgecolor="#7C3AED", label="Required stage"),
        mpatches.Patch(
            facecolor="white", edgecolor="#7C3AED", linewidth=1.5,
            linestyle="--", label="Optional stage (graceful fallback)",
        ),
    ]
    legend = ax.legend(
        handles=legend_patches,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.02),
        ncol=2,
        frameon=False,
        fontsize=9,
    )
    for text in legend.get_texts():
        text.set_color("#4B5563")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    generate()
