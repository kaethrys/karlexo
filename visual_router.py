import re
from dataclasses import dataclass


@dataclass
class VisualDecision:
    render_chart: bool
    render_table: bool
    reason: str


HORIZON_PATTERN = re.compile(
    r"\b(1w|1m|3m|6m|1y|3y|5y|10y|ytd|decade|historical|history|trend|chart|graph|pie|allocation|breakdown|vs\.?|compare)\b",
    re.IGNORECASE,
)
ASSET_PATTERN = re.compile(
    r"\b(s&p\s*500|snp\s*500|nifty|gold|silver|commodity|commodities|mutual\s*fund|nav|cagr|etf)\b",
    re.IGNORECASE,
)
DIRECT_PATTERN = re.compile(
    r"^(status|ping|ip|git\s+status|tmux|ssh|tailscale|help|version)\b",
    re.IGNORECASE,
)


def classify_visual_need(prompt: str) -> VisualDecision:
    text = prompt.strip()
    if not text or DIRECT_PATTERN.match(text):
        return VisualDecision(False, False, "Direct command or status check")

    has_horizon = bool(HORIZON_PATTERN.search(text))
    has_asset = bool(ASSET_PATTERN.search(text))

    if has_horizon and has_asset:
        return VisualDecision(True, True, "Multi-horizon financial/market query")
    if has_horizon:
        return VisualDecision(False, True, "Comparison, trend, or allocation query")
    if has_asset:
        return VisualDecision(True, True, "Market/asset query with multi-period context")

    return VisualDecision(False, False, "Standard query without visual trigger")


def render_bar_rows(items: list[tuple[str, float]], width: int = 24) -> str:
    max_val = max((abs(v) for _, v in items), default=1.0) or 1.0
    lines = []
    for label, val in items:
        filled = max(1, round((abs(val) / max_val) * width)) if val != 0 else 0
        bar = "#" * filled
        lines.append(f"{label:<8} | {bar:<{width}} {val:+.1f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_bar_rows([("1M", 1.8), ("1Y", 6.1), ("5Y/yr", 8.8), ("10Y/yr", 12.5)]))
