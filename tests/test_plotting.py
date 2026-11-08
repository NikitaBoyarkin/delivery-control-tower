"""Tests for delivery.plotting - the shared style and save contract.

The contract these tests bind: every figure is saved through ``save_figure`` at
300 dpi with a tight bounding box, no figure is left open, and the font chain can
always resolve Cyrillic. The last test is a static guard against a module quietly
re-introducing its own ``fig.savefig`` and drifting off that contract.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest
from PIL import Image

from delivery import plotting

ROOT = Path(__file__).resolve().parent.parent


def test_apply_style_keeps_a_cyrillic_fallback_chain():
    plotting.apply_style()
    family = plt.rcParams["font.family"]
    assert "DejaVu Sans" in family, "DejaVu Sans ships inside matplotlib and must stay in the chain"
    assert family[-1] == "sans-serif", (
        "a single hard-coded family is what turns Cyrillic into boxes"
    )
    assert plt.rcParams["axes.unicode_minus"] is False


def test_save_figure_writes_300_dpi_with_a_tight_box(tmp_path):
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.plot([0, 1], [0, 1])
    path = plotting.save_figure(fig, tmp_path / "nested" / "figure.png")

    assert path.exists()
    with Image.open(path) as image:
        dpi_x, dpi_y = image.info["dpi"]
        assert dpi_x == pytest.approx(plotting.SAVE_DPI, abs=1)
        assert dpi_y == pytest.approx(plotting.SAVE_DPI, abs=1)
        # A tight bounding box only crops the nominal canvas, never grows it.
        nominal = (4 * plotting.SAVE_DPI, 3 * plotting.SAVE_DPI)
        assert 0.7 * nominal[0] <= image.size[0] <= nominal[0]
        assert 0.7 * nominal[1] <= image.size[1] <= nominal[1]


def test_save_figure_closes_the_figure(tmp_path):
    before = len(plt.get_fignums())
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    plotting.save_figure(fig, tmp_path / "closed.png")
    assert len(plt.get_fignums()) == before


@pytest.mark.parametrize(
    ("value", "expected"),
    [(24.0, plotting.INK), (40.0, "white")],
)
def test_readable_text_color_flips_on_a_dark_cell(value, expected):
    fig, ax = plt.subplots()
    image = ax.imshow(np.array([[24.0, 40.0]]), cmap="YlOrRd")
    try:
        assert plotting.readable_text_color(image, value) == expected
    finally:
        plt.close(fig)


def test_padded_range_zooms_around_the_data():
    low, high = plotting.padded_range([31.24, 31.88], pad=0.4)
    assert low < 31.24 < 31.88 < high
    assert high - low == pytest.approx(0.64 * 1.8)


def test_only_plotting_calls_savefig():
    """Static guard: a module that saves its own figure silently drops dpi/tight bbox."""
    offenders = sorted(
        path.name
        for path in (ROOT / "delivery").glob("*.py")
        if path.name != "plotting.py" and ".savefig(" in path.read_text(encoding="utf-8")
    )
    assert offenders == [], f"call plotting.save_figure instead of fig.savefig: {offenders}"
