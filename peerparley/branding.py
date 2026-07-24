"""PeerParley brand tokens and logo."""
from __future__ import annotations

from reportlab.lib.colors import HexColor

NAVY = HexColor("#0E2A3B")
GREEN = HexColor("#0B7A4B")
GREEN_LIGHT = HexColor("#3FB07A")
GREY = HexColor("#6B7A80")
GREY_LIGHT = HexColor("#E7ECEA")
AMBER = HexColor("#E8A13A")
RED = HexColor("#C0392B")
WHITE = HexColor("#FFFFFF")

TAGLINE = "Peer evaluation, made clear."
BRAND = "PeerParley"

# Minimal inline SVG logo (navy speech bubble + green check)
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">
<rect x="8" y="14" rx="16" ry="16" width="104" height="74" fill="#0E2A3B"/>
<polygon points="34,88 34,110 58,88" fill="#0E2A3B"/>
<path d="M38 52 L54 68 L84 36" stroke="#3FB07A" stroke-width="11"
 fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def grade_color(signed_pct: float):
    if signed_pct >= 2:
        return GREEN
    if signed_pct <= -2:
        return RED
    return GREY
