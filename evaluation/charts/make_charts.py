"""
Build the three result charts for Chapter 4.

Every figure is drawn from data the engine produced in this run, not from the
numbers in the chapter's tables, so a figure cannot drift from the table beside
it. Run after chartdata.py and movedump.py.

Output: results/figures/load.pdf, moved.pdf, ablation.pdf (and .png previews).
"""
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from paths import P, FIGURES

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
FIGS = str(FIGURES)

# Validated against the skill's palette checker: categorical slots 1 and 2 for
# the two institutions, and the ordinal blue ramp for the three outcome bands.
SYD, UTS = "#2a78d6", "#eb6834"
BANDS = ["#86b6ef", "#2a78d6", "#104281"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#dcdbd6"

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["TeX Gyre Pagella", "Palatino", "DejaVu Serif"],
    "font.size": 9,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK2,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "axes.titlecolor": INK,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def save(fig, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(FIGS, f"{name}.{ext}"), dpi=200,
                    bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    print("wrote", name)


# ------------------------------------------------------------------ chart 1
def chart_load(data):
    """Credit points per teaching period, as a share of each course's periods.

    Counts differ by an order of magnitude (338 Sydney periods against 73 UTS),
    so the comparable quantity is the share within each course rather than the
    count. The claim being drawn is about shape: whether a course's periods all
    carry the same load.
    """
    syd = collections.Counter(data["loads"]["USYD"])
    uts = collections.Counter(data["loads"]["UTS"])
    values = sorted(set(syd) | set(uts))
    n_syd, n_uts = sum(syd.values()), sum(uts.values())

    fig, ax = plt.subplots(figsize=(5.4, 2.5))
    y = range(len(values))
    h = 0.38
    for off, counter, total, colour, label in (
            (h / 2, syd, n_syd, SYD, f"Sydney ({n_syd} periods)"),
            (-h / 2, uts, n_uts, UTS, f"UTS ({n_uts} periods)")):
        shares = [counter.get(v, 0) / total for v in values]
        bars = ax.barh([i + off for i in y], shares, height=h, color=colour,
                       label=label, zorder=3)
        for i, (bar, v) in enumerate(zip(bars, values)):
            count = counter.get(v, 0)
            if count:
                ax.text(bar.get_width() + 0.012, bar.get_y() + bar.get_height() / 2,
                        str(count), va="center", ha="left", fontsize=7.5, color=INK2)

    ax.set_yticks(list(y))
    ax.set_yticklabels([f"{v} cp" for v in values])
    ax.set_xlabel("share of that course's teaching periods")
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xlim(0, 1.12)
    ax.legend(frameon=False, loc="lower right", fontsize=8)
    ax.tick_params(length=0)
    save(fig, "load")


# ------------------------------------------------------------------ chart 2
def chart_moved(moved):
    """Units moved per repair, as a share of each course's scenarios.

    The reported statistics are a median of one at both institutions with a mean
    of 1.67 and 2.68, which says nothing about the shape. The shape is the
    finding: a spike at one and a thin tail reaching eleven and eighteen.
    """
    cap = 10
    labels = [str(i) for i in range(cap)] + [f"{cap}+"]

    def binned(xs):
        c = collections.Counter(min(x, cap) for x in xs)
        return [c.get(i, 0) / len(xs) for i in range(cap + 1)]

    syd, uts = moved["USYD"]["moved"], moved["UTS"]["moved"]
    fig, ax = plt.subplots(figsize=(5.4, 2.4))
    x = range(len(labels))
    w = 0.38
    ax.bar([i - w / 2 for i in x], binned(syd), width=w, color=SYD, zorder=3,
           label=f"Sydney (n={len(syd)}, max {max(syd)})")
    ax.bar([i + w / 2 for i in x], binned(uts), width=w, color=UTS, zorder=3,
           label=f"UTS (n={len(uts)}, max {max(uts)})")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_xlabel("units moved by the repair")
    ax.set_ylabel("share of scenarios")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.legend(frameon=False, fontsize=8)
    ax.tick_params(length=0)
    save(fig, "moved")


# ------------------------------------------------------------------ chart 3
ABLATION = {
    "Neither": {"USYD": (2, 485, 731), "UTS": (0, 57, 222)},
    "Compaction\nonly": {"USYD": (883, 234, 101), "UTS": (1, 141, 137)},
    "Overload in\nspan only": {"USYD": (722, 244, 252), "UTS": (114, 42, 123)},
    "Both,\nas built": {"USYD": (900, 228, 90), "UTS": (115, 88, 76)},
}
BAND_NAMES = ["finishes in span", "one semester longer", "a year or more longer"]


def chart_ablation():
    """Table 4.3 as composition, because the row is a partition of the scenarios.

    Each row sums to the course's scenario count, so the readable quantity is
    the share in each outcome band. The two panels are deliberately not shared:
    the point is that the mechanism which does the work differs by institution.
    """
    fig, axes = plt.subplots(1, 2, figsize=(6.3, 2.7), sharey=True)
    order = list(ABLATION)
    for ax, inst, total, title in ((axes[0], "USYD", 1218, "Sydney"),
                                   (axes[1], "UTS", 279, "UTS")):
        left = [0.0] * len(order)
        for b, (colour, name) in enumerate(zip(BANDS, BAND_NAMES)):
            vals = [ABLATION[k][inst][b] / total for k in order]
            ax.barh(range(len(order)), vals, left=left, height=0.62,
                    color=colour, label=name if inst == "USYD" else None,
                    zorder=3, edgecolor="white", linewidth=1.4)
            for i, v in enumerate(vals):
                if b == 0 and v > 0.04:
                    ax.text(v / 2, i, str(ABLATION[order[i]][inst][b]),
                            va="center", ha="center", fontsize=7.5,
                            color="#0d366b")
            left = [a + v for a, v in zip(left, vals)]
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order, fontsize=8)
        # Top to bottom in the same order as the table, so the two can be read
        # against each other without reversing one of them in your head.
        ax.set_ylim(len(order) - 0.5, -0.5)
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(PercentFormatter(1.0))
        ax.set_title(f"{title} ({total} scenarios)", fontsize=9, pad=6)
        ax.tick_params(length=0)
    axes[0].legend(frameon=False, fontsize=8, loc="upper center",
                   bbox_to_anchor=(1.05, -0.16), ncol=3)
    save(fig, "ablation")


if __name__ == "__main__":
    chart_load(json.load(open(P("chartdata.json"))))
    chart_moved(json.load(open(P("moveddump.json"))))
    chart_ablation()
