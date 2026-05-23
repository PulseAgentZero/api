"""Generate hackathon/paper/architecture.png — architecture diagram for the README.

Usage:
    python hackathon/paper/architecture.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent / "architecture.png"

INK = "#0F172A"
MUTED = "#475569"
LINE = "#94A3B8"
ACCENT = "#1D4ED8"

COLORS = {
    "data": ("#FEF3C7", "#92400E"),
    "store": ("#DBEAFE", "#1E3A8A"),
    "compute": ("#DCFCE7", "#14532D"),
    "agent": ("#EDE9FE", "#5B21B6"),
    "llm": ("#FEE2E2", "#7F1D1D"),
    "client": ("#F1F5F9", "#0F172A"),
}


def box(ax, x, y, w, h, label, kind, sub=None, fontsize=10):
    fill, edge = COLORS[kind]
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.20",
            linewidth=1.6,
            edgecolor=edge,
            facecolor=fill,
            zorder=2,
        )
    )
    if sub:
        ax.text(
            x + w / 2,
            y + h / 2 + 0.20,
            label,
            ha="center",
            va="center",
            fontsize=fontsize,
            fontweight="bold",
            color=edge,
            zorder=3,
        )
        ax.text(
            x + w / 2,
            y + h / 2 - 0.26,
            sub,
            ha="center",
            va="center",
            fontsize=fontsize - 2,
            color=MUTED,
            zorder=3,
            linespacing=1.4,
        )
    else:
        ax.text(
            x + w / 2,
            y + h / 2,
            label,
            ha="center",
            va="center",
            fontsize=fontsize,
            fontweight="bold",
            color=edge,
            zorder=3,
        )
    return (x, y, w, h)


def arrow(ax, start, end, label=None, curve=0.0, fontsize=8, color=LINE, lw=1.4):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=lw,
            color=color,
            connectionstyle=f"arc3,rad={curve}",
            zorder=1,
        )
    )
    if label:
        mx = (start[0] + end[0]) / 2
        my = (start[1] + end[1]) / 2
        ax.text(
            mx,
            my,
            label,
            ha="center",
            va="center",
            fontsize=fontsize,
            color=MUTED,
            style="italic",
            zorder=4,
            bbox=dict(boxstyle="round,pad=0.18", fc="white", ec="none", alpha=0.95),
        )


def top(b): return (b[0] + b[2] / 2, b[1] + b[3])
def bot(b): return (b[0] + b[2] / 2, b[1])
def left(b): return (b[0], b[1] + b[3] / 2)
def right(b): return (b[0] + b[2], b[1] + b[3] / 2)


def main() -> None:
    fig, ax = plt.subplots(figsize=(14, 10), dpi=180)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 11)
    ax.set_axis_off()

    ax.text(
        0.4,
        10.55,
        "Entivia — DSN × Bluechip LLM Agent Challenge",
        fontsize=15,
        fontweight="bold",
        color=INK,
    )
    ax.text(
        0.4,
        10.18,
        "Two containerized agents · one Docker image · real Yelp data",
        fontsize=11,
        color=MUTED,
    )

    judge = box(ax, 10.6, 10.0, 3.2, 0.8, "Reviewer / Judge", "client", sub="curl · Swagger UI")

    yelp = box(ax, 1.0, 8.4, 3.6, 0.9, "Yelp Open Dataset", "data", sub="JSON loader")
    good = box(ax, 5.2, 8.4, 3.6, 0.9, "Goodreads", "data", sub="cross-domain loader")

    pg = box(ax, 1.0, 6.6, 5.0, 1.0, "Postgres", "store", sub="users · items · reviews (slice)")
    qd = box(ax, 8.0, 6.6, 5.0, 1.0, "Qdrant", "store", sub="item ANN · persona vectors (384-d)")

    fe = box(
        ax,
        3.5,
        5.0,
        7.0,
        0.9,
        "fastembed · BAAI/bge-small-en-v1.5",
        "compute",
        sub="local · no embedding API · cached in Docker volume",
    )

    task_a = box(
        ax,
        0.6,
        3.0,
        6.0,
        1.3,
        "Task A · task-a-api  :8011",
        "agent",
        sub="ReviewSimulationAgent\nPOST /simulate-review · persona + product → {stars, text}",
        fontsize=10,
    )
    task_b = box(
        ax,
        7.4,
        3.0,
        6.0,
        1.3,
        "Task B · task-b-api  :8012",
        "agent",
        sub="RecommendationAgent\nPOST /recommend · warm · cold · multi-turn · cross-domain",
        fontsize=10,
    )

    tools_a = box(
        ax,
        0.6,
        1.85,
        6.0,
        0.65,
        "tools: fetch_user_profile · fetch_item",
        "client",
        fontsize=9,
    )
    tools_b = box(
        ax,
        7.4,
        1.85,
        6.0,
        0.65,
        "tools: fetch_user_history · fetch_item · ann_search_items",
        "client",
        fontsize=9,
    )

    base = box(
        ax,
        2.4,
        0.5,
        9.2,
        0.95,
        "Entivia BaseAgent — ReAct loop · JSON validation · tool calling",
        "llm",
        sub="Anthropic Claude (primary)   ⟷   Groq (automatic fallback)",
        fontsize=10,
    )

    arrow(ax, bot(yelp), (pg[0] + pg[2] / 2 - 0.6, pg[1] + pg[3]))
    arrow(ax, bot(good), (pg[0] + pg[2] / 2 + 0.6, pg[1] + pg[3]))
    arrow(ax, bot(good), (qd[0] + 0.8, qd[1] + qd[3]), curve=-0.15)

    arrow(ax, (pg[0] + pg[2], pg[1] + pg[3] / 2 - 0.15), (fe[0], fe[1] + fe[3] / 2 + 0.15),
          curve=-0.18, label="text")
    arrow(ax, (fe[0] + fe[2], fe[1] + fe[3] / 2 + 0.15), (qd[0], qd[1] + qd[3] / 2 - 0.15),
          curve=-0.18, label="vectors")

    arrow(ax, (pg[0] + 0.6, pg[1]), (task_a[0] + 1.0, task_a[1] + task_a[3]),
          curve=-0.05, label="SQL")
    arrow(ax, (qd[0] + qd[2] - 0.6, qd[1]), (task_b[0] + task_b[2] - 1.0, task_b[1] + task_b[3]),
          curve=0.05, label="ANN")

    arrow(ax, bot(task_a), top(tools_a))
    arrow(ax, bot(task_b), top(tools_b))

    arrow(ax, bot(tools_a), (base[0] + 2.0, base[1] + base[3]), curve=-0.1)
    arrow(ax, bot(tools_b), (base[0] + base[2] - 2.0, base[1] + base[3]), curve=0.1)

    arrow(
        ax,
        (judge[0] + judge[2] - 0.6, judge[1]),
        (task_b[0] + task_b[2] - 0.8, task_b[1] + task_b[3]),
        curve=0.0,
        color=ACCENT,
        lw=1.6,
    )
    arrow(
        ax,
        (judge[0] + 0.6, judge[1]),
        (task_a[0] + task_a[2] - 0.6, task_a[1] + task_a[3]),
        curve=-0.32,
        color=ACCENT,
        lw=1.6,
    )
    ax.text(
        judge[0] + judge[2] / 2,
        judge[1] - 0.32,
        "HTTP requests",
        ha="center",
        va="center",
        fontsize=8,
        color=ACCENT,
        style="italic",
        zorder=4,
    )

    legend_items = [
        ("Data source", "data"),
        ("Storage", "store"),
        ("Embedding", "compute"),
        ("Task container", "agent"),
        ("LLM runtime", "llm"),
        ("Client / tool", "client"),
    ]
    lx, ly = 0.4, 9.5
    for i, (label, kind) in enumerate(legend_items):
        fill, edge = COLORS[kind]
        ax.add_patch(
            FancyBboxPatch(
                (lx + i * 1.55, ly),
                0.30,
                0.24,
                boxstyle="round,pad=0.02",
                linewidth=1.0,
                edgecolor=edge,
                facecolor=fill,
            )
        )
        ax.text(
            lx + i * 1.55 + 0.36,
            ly + 0.12,
            label,
            fontsize=8,
            color=MUTED,
            va="center",
        )

    fig.savefig(OUT, dpi=180, bbox_inches="tight", facecolor="white", pad_inches=0.25)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
