"""Branded chart rendering for Ad Strategist [GRAPH_DATA] blocks.

The Ad Strategist agent (Haiku, text-only) cannot render images. To keep
chart styling uniform across every campaign plan we emit, agents output
data points wrapped in a [GRAPH_DATA]...[/GRAPH_DATA] block and this
module renders the standardized PNG via matplotlib.

Design constraints (from spec):
  - Single hardcoded style — no AI-written rendering code, no inline CSS
  - ARIA palette only: primary purple, warning gold, dark text grey,
    plus success green / danger orange for additional data series
  - Every chart labeled "PROJECTION" since v1 has no live Meta Ads data
  - Graceful fallback: malformed data leaves the campaign plan as
    text-only instead of crashing the agent run

Public API:
  - process_ad_strategist_text(tenant_id, text) -> str
      The single entry point. Scans markdown for [GRAPH_DATA] blocks,
      renders each, uploads to Supabase Storage, and replaces the
      block with a standard ![alt](url) markdown image. Returns the
      transformed text. Safe to call on text with zero blocks.

[GRAPH_DATA] block format (JSON inside the tags):
  [GRAPH_DATA]
  {
    "type": "pie" | "bar" | "funnel",
    "title": "Budget Allocation",
    "data": {"Awareness": 50, "Retargeting": 30, "Conversion": 20}
  }
  [/GRAPH_DATA]

Agents are instructed to emit JSON inside the block. We accept both
pretty-printed and single-line JSON, and tolerate trailing commas via
a permissive parser.
"""
from __future__ import annotations

# IMPORTANT: matplotlib must use the Agg backend in headless server
# environments (Docker, CI). Set BEFORE the first pyplot / figure
# import or matplotlib auto-detects a GUI backend that fails to load.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import io
import json
import logging
import re
import uuid
from typing import Any

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.visualizer")

# ── Brand palette (sourced from frontend/app/globals.css) ──────────────
ARIA_PURPLE = "#534AB7"
ARIA_GOLD = "#BA7517"
ARIA_DARK_GREY = "#2C2C2A"
ARIA_TEXT_SECONDARY = "#5F5E5A"
ARIA_SUCCESS = "#1D9E75"
ARIA_DANGER = "#D85A30"
ARIA_BG = "#FFFFFF"
ARIA_BG_SECONDARY = "#F8F8F6"
ARIA_BORDER = "#E0DED8"

# Multi-series palette — purple first (primary brand), then gold, green,
# danger orange, dark grey. Cycles for >5 series. Order matters: charts
# with a "main" data point always read brand-first.
ARIA_PALETTE = [ARIA_PURPLE, ARIA_GOLD, ARIA_SUCCESS, ARIA_DANGER, ARIA_DARK_GREY]

# Storage bucket — same one media_agent already uses, so the public-URL
# semantics are identical.
STORAGE_BUCKET = "content"
STORAGE_PATH_TEMPLATE = "charts/{tenant_id}/{uuid}.png"

# Block delimiters — the regex matches case-insensitively + with optional
# whitespace inside the tags so the agent can emit either tight
# `[GRAPH_DATA]{...}[/GRAPH_DATA]` or pretty-printed multi-line forms.
_GRAPH_BLOCK_RE = re.compile(
    r"\[GRAPH_DATA\]\s*(.*?)\s*\[/GRAPH_DATA\]",
    re.IGNORECASE | re.DOTALL,
)

# Cap rendering at a sane number per agent run to bound CPU + storage
# costs. A single ad campaign plan needs at most 2-3 charts; anything
# higher is the agent hallucinating extra blocks.
_MAX_CHARTS_PER_RUN = 4


def _apply_brand_style(fig, ax) -> None:
    """Single source of truth for chart-wide visual settings.

    Called by every renderer so style stays uniform regardless of which
    chart type (pie / bar / funnel) the agent requested. Don't add
    chart-specific tweaks here — only things every chart needs.
    """
    fig.patch.set_facecolor(ARIA_BG)
    ax.set_facecolor(ARIA_BG)
    for spine in ax.spines.values():
        spine.set_color(ARIA_BORDER)
        spine.set_linewidth(0.5)
    ax.tick_params(colors=ARIA_TEXT_SECONDARY, labelsize=9)
    ax.grid(True, color=ARIA_BORDER, linestyle="--", linewidth=0.5, alpha=0.5)


def _add_projection_footer(fig, label: str = "PROJECTION") -> None:
    """Watermark every chart with a 'PROJECTION' label so the user
    understands these aren't live ad-platform metrics. Per spec: 'These
    graphs must be clearly labeled as Projections or Strategy
    Visualizations' since v1 doesn't have the Meta Ads API yet."""
    fig.text(
        0.99, 0.01, label,
        ha="right", va="bottom",
        fontsize=8, color=ARIA_TEXT_SECONDARY,
        weight="bold", alpha=0.6,
    )
    fig.text(
        0.01, 0.01, "ARIA",
        ha="left", va="bottom",
        fontsize=8, color=ARIA_PURPLE,
        weight="bold", alpha=0.7,
    )


def _normalize_data(raw: Any) -> dict[str, float]:
    """Convert the agent's `data` field into a canonical {label: value}
    dict. Accepts:
      - {"Awareness": 50, "Retargeting": 30}  (preferred)
      - [["Awareness", 50], ["Retargeting", 30]]
      - [{"label": "Awareness", "value": 50}, ...]
    Returns {} for unparseable input so the caller can bail to text-only.
    """
    if isinstance(raw, dict):
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict):
                label = item.get("label") or item.get("name") or item.get("category")
                value = item.get("value") or item.get("count") or item.get("amount")
                if label is not None and value is not None:
                    try:
                        out[str(label)] = float(value)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    out[str(item[0])] = float(item[1])
                except (TypeError, ValueError):
                    continue
        return out
    return {}


def _render_pie(data: dict[str, float], title: str) -> bytes | None:
    """Branded pie chart — used for budget allocation, channel mix,
    audience tier splits. Donut style (wedge width 0.4) reads cleaner
    than a solid pie at small sizes."""
    if not data:
        return None
    labels = list(data.keys())
    values = list(data.values())
    if sum(values) <= 0:
        return None
    colors = [ARIA_PALETTE[i % len(ARIA_PALETTE)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(7, 5), dpi=150)
    wedges, _, autotexts = ax.pie(
        values,
        labels=labels,
        colors=colors,
        autopct="%1.0f%%",
        startangle=90,
        wedgeprops={"width": 0.4, "edgecolor": ARIA_BG, "linewidth": 2},
        textprops={"color": ARIA_DARK_GREY, "fontsize": 10, "weight": "bold"},
    )
    for at in autotexts:
        at.set_color(ARIA_BG)
        at.set_fontsize(10)
        at.set_weight("bold")
    ax.set_title(
        title or "Allocation",
        color=ARIA_DARK_GREY, fontsize=14, weight="bold", pad=18,
    )
    ax.set(aspect="equal")
    _add_projection_footer(fig)
    return _fig_to_png(fig)


def _render_bar(data: dict[str, float], title: str) -> bytes | None:
    """Branded vertical bar chart — used for audience demographic
    breakdowns, age/interest weights, channel performance projections."""
    if not data:
        return None
    labels = list(data.keys())
    values = list(data.values())
    colors = [ARIA_PALETTE[i % len(ARIA_PALETTE)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    bars = ax.bar(labels, values, color=colors, edgecolor=ARIA_BG, linewidth=1.5)
    _apply_brand_style(fig, ax)
    ax.set_title(
        title or "Breakdown",
        color=ARIA_DARK_GREY, fontsize=14, weight="bold", pad=18,
    )
    # Value labels on top of each bar — matches the readable, beginner-
    # friendly voice the Ad Strategist uses.
    for bar, value in zip(bars, values):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            height,
            f"{int(value) if value == int(value) else value:g}",
            ha="center", va="bottom",
            color=ARIA_DARK_GREY, fontsize=10, weight="bold",
        )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    _add_projection_footer(fig)
    return _fig_to_png(fig)


def _render_funnel(data: dict[str, float], title: str) -> bytes | None:
    """Branded horizontal funnel — used for conversion projections
    (Impressions -> Clicks -> Leads -> Customers). Renders as a
    horizontal bar chart sorted descending so the funnel shape is
    visually obvious without a custom funnel polygon."""
    if not data:
        return None
    # Sort largest-first so the chart reads top-down as a funnel
    sorted_items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in sorted_items]
    values = [v for _, v in sorted_items]
    colors = [ARIA_PALETTE[i % len(ARIA_PALETTE)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    bars = ax.barh(labels, values, color=colors, edgecolor=ARIA_BG, linewidth=1.5)
    _apply_brand_style(fig, ax)
    ax.invert_yaxis()  # largest at the top — funnel reads top-down
    ax.set_title(
        title or "Funnel Projection",
        color=ARIA_DARK_GREY, fontsize=14, weight="bold", pad=18,
    )
    for bar, value in zip(bars, values):
        width = bar.get_width()
        ax.text(
            width,
            bar.get_y() + bar.get_height() / 2,
            f"  {int(value) if value == int(value) else value:g}",
            ha="left", va="center",
            color=ARIA_DARK_GREY, fontsize=10, weight="bold",
        )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    _add_projection_footer(fig)
    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    """Save a matplotlib Figure to PNG bytes and immediately close it
    to release memory. Always close — leaked figures accumulate in
    long-running server processes."""
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor=ARIA_BG)
        return buf.getvalue()
    finally:
        plt.close(fig)


_RENDERERS: dict[str, Any] = {
    "pie": _render_pie,
    "donut": _render_pie,
    "bar": _render_bar,
    "column": _render_bar,
    "funnel": _render_funnel,
}


def render_chart_from_block(graph_data: dict) -> bytes | None:
    """Render a single [GRAPH_DATA] block to PNG bytes.

    Returns None on:
      - missing / unknown chart type
      - malformed data (no parseable {label: value} pairs)
      - matplotlib runtime errors
    The caller (process_ad_strategist_text) treats None as "skip the
    chart, leave the text block alone" so the campaign plan still
    ships even when one chart fails to render.
    """
    chart_type = (graph_data.get("type") or "").strip().lower()
    title = (graph_data.get("title") or "").strip()
    data = _normalize_data(graph_data.get("data"))
    renderer = _RENDERERS.get(chart_type)
    if renderer is None:
        logger.warning("[visualizer] unknown chart type: %r", chart_type)
        return None
    if not data:
        logger.warning("[visualizer] chart %r has no parseable data", title or chart_type)
        return None
    try:
        return renderer(data, title)
    except Exception as e:
        logger.error("[visualizer] render failed for %r: %s", title or chart_type, e)
        return None


def upload_chart_to_storage(tenant_id: str, image_data: bytes) -> str | None:
    """Upload a rendered chart PNG to Supabase Storage and return the
    public URL. Mirrors media_agent._store_image's contract — same
    bucket, same path scheme, same None-on-failure semantics."""
    if not tenant_id or not image_data:
        return None
    filename = STORAGE_PATH_TEMPLATE.format(tenant_id=tenant_id, uuid=uuid.uuid4().hex)
    try:
        sb = get_db()
        sb.storage.from_(STORAGE_BUCKET).upload(
            filename,
            image_data,
            {"content-type": "image/png"},
        )
        url = sb.storage.from_(STORAGE_BUCKET).get_public_url(filename)
        logger.info("[visualizer] stored chart at %s", url)
        return url
    except Exception as e:
        msg = str(e).lower()
        if "bucket" in msg and ("not found" in msg or "does not exist" in msg):
            logger.error(
                "[visualizer] Supabase storage bucket %r missing — create it in "
                "Supabase Dashboard -> Storage -> New bucket (name: %s, public: ON).",
                STORAGE_BUCKET, STORAGE_BUCKET,
            )
        else:
            logger.error("[visualizer] upload failed: %s", e)
        return None


def _parse_block_json(raw: str) -> dict | None:
    """Parse the JSON inside a [GRAPH_DATA] block. Tolerates trailing
    commas and stripped surrounding code-fences (``` markers Haiku
    sometimes wraps the JSON in)."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop opening fence (``` or ```json) up to first newline
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Defensive: strip trailing commas before } or ]
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as e:
        logger.warning("[visualizer] block JSON parse failed: %s", e)
    return None


def process_ad_strategist_text(tenant_id: str, text: str) -> str:
    """Scan `text` for [GRAPH_DATA]...[/GRAPH_DATA] blocks, render each
    to a branded PNG, upload, and replace the block with a markdown
    image. Blocks that fail to parse / render are LEFT IN PLACE as
    text-only fallback so the upstream content never crashes — per spec:
    'if the data is malformed, it defaults to the standard text-only
    output without crashing.'

    Despite the function name, this is now used by the AI Report flow
    (campaign_analyzer.py) — NOT for Ad Strategist campaign briefs.
    Campaign briefs are deliberately chart-free; charts only render
    against real uploaded performance metrics in the AI Report path.

    Returns the transformed text. If `text` has no blocks, returns it
    unchanged (zero overhead).
    """
    if not text or "[GRAPH_DATA]" not in text.upper():
        return text

    rendered_count = 0
    pos = 0
    out_parts: list[str] = []
    for match in _GRAPH_BLOCK_RE.finditer(text):
        out_parts.append(text[pos:match.start()])
        pos = match.end()

        if rendered_count >= _MAX_CHARTS_PER_RUN:
            # Hit the cap — drop the remaining blocks silently rather
            # than ship 10 hallucinated charts. The first N already
            # rendered carry the strategy.
            logger.warning(
                "[visualizer] chart cap (%d) reached for tenant %s — dropping further blocks",
                _MAX_CHARTS_PER_RUN, tenant_id,
            )
            continue

        block_text = match.group(1)
        graph_data = _parse_block_json(block_text)
        if not graph_data:
            # Malformed JSON — keep the original block as text fallback
            out_parts.append(match.group(0))
            continue

        png_bytes = render_chart_from_block(graph_data)
        if not png_bytes:
            out_parts.append(match.group(0))
            continue

        url = upload_chart_to_storage(tenant_id, png_bytes)
        if not url:
            out_parts.append(match.group(0))
            continue

        title = (graph_data.get("title") or "Strategy Visualization").strip()
        out_parts.append(f"\n![{title}]({url})\n")
        rendered_count += 1

    out_parts.append(text[pos:])
    if rendered_count > 0:
        logger.info(
            "[visualizer] rendered %d chart(s) for tenant %s",
            rendered_count, tenant_id,
        )
    return "".join(out_parts)
