"""Shared matplotlib style, palette and figure saving for the report figures.

Every figure in the project is drawn in the OO style (``fig, ax = plt.subplots``) and
persisted through :func:`save_figure`, so dpi, tight bounding box and closing are
identical across modules. :func:`apply_style` is idempotent and is called once per
module at import time.

The font chain ends with a generic family on purpose: a single hard-coded name
(``Arial``, ``Calibri``) resolves locally and silently falls back on CI, which is how
Cyrillic labels turn into squares. ``DejaVu Sans`` ships inside matplotlib, so the
chain always resolves somewhere.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.colors import Colormap, Normalize
from matplotlib.figure import Figure
from matplotlib.image import AxesImage
from matplotlib.ticker import FuncFormatter

#: Cyrillic-capable fallback chain; the generic family must stay last.
FONT_FAMILY = ["PT Sans", "DejaVu Sans", "sans-serif"]

#: Categorical palette shared by all figures (muted, readable on white and in print).
BLUE = "#4C72B0"
RED = "#C44E52"
GREEN = "#55A868"
ORANGE = "#DD8452"
GREY = "#7F7F7F"
INK = "#1A1A1A"

#: Saved raster resolution. 150 dpi stays for on-screen sizing only.
SAVE_DPI = 300
FIGURE_DPI = 150


def apply_style() -> None:
    """Set the project rcParams. Idempotent: safe to call on every import."""
    plt.rcParams.update(
        {
            "font.family": FONT_FAMILY,
            # Hyphen-minus instead of U+2212: a font without the minus glyph would
            # otherwise draw a box on every negative tick.
            "axes.unicode_minus": False,
            "figure.dpi": FIGURE_DPI,
            "savefig.dpi": SAVE_DPI,
            "grid.alpha": 0.3,
            "grid.linewidth": 0.6,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 9,
            "legend.frameon": False,
        }
    )


def save_figure(fig: Figure, path: Path | str, dpi: int = SAVE_DPI) -> Path:
    """Write ``fig`` to ``path`` at ``dpi`` with a tight box, then close it.

    ``bbox_inches="tight"`` is what keeps a long title or a legend outside the axes
    from being clipped; ``facecolor=fig.get_facecolor()`` keeps the background of a
    styled figure instead of defaulting to white on save.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        target,
        dpi=dpi,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
        edgecolor="none",
    )
    plt.close(fig)
    return target


def thousands(ax: Axes, axis: str = "y") -> None:
    """Format ticks with a space as the thousands separator (``180 000``)."""
    formatter = FuncFormatter(lambda value, _pos: f"{value:,.0f}".replace(",", " "))
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(formatter)


def percent(ax: Axes, axis: str = "x", decimals: int = 0) -> None:
    """Format ticks as percentages (``0.06`` -> ``6%``)."""
    formatter = FuncFormatter(lambda value, _pos: f"{value * 100:.{decimals}f}%")
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(formatter)


def readable_text_color(image: AxesImage, value: float) -> str:
    """Return dark or white ink depending on the luminance of the cell under ``value``.

    Black labels on the dark end of ``YlOrRd`` are the usual contrast failure in a
    heatmap; the threshold is the standard sRGB relative luminance.
    """
    cmap: Colormap = image.cmap
    norm: Normalize = image.norm
    red, green, blue, _alpha = cmap(norm(value))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return INK if luminance > 0.5 else "white"


def padded_range(values: list[float], pad: float = 0.4) -> tuple[float, float]:
    """Zoom the axis to the data with a relative margin, for a truncated-scale chart.

    Used by charts whose whole message is a small variation around a large baseline.
    The caller must label the axis as not starting at zero.
    """
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        span = abs(high) * 0.1 or 1.0
    return low - span * pad, high + span * pad
