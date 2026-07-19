"""Live scrape of Drewry's public World Container Index (WCI) page for a
market-benchmark ocean freight rate on a given lane.

Proof-of-concept external data source: gives an independent, market-wide
reference point (e.g. Shanghai to New York) to sit alongside the carrier
CSV rates -- context, not a substitute for a carrier-specific quote.

Fragile by nature: this parses Drewry's narrative summary sentences
("Shanghai to New York remained stable at $7,879 per 40ft container"), not
a stable API. A production version would need a real data contract
(Drewry/Freightos/Xeneta all sell one) and should treat a parse failure as
"benchmark unavailable", never as "rate is zero" -- which is exactly what
this function does.
"""

import re

import requests

_WCI_URL = "https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; freight-quote-agent/1.0)"}
_TIMEOUT_SECONDS = 15

_LANE_SENTENCE = re.compile(
    r"Shanghai to (?P<dest>[A-Za-z ]+?) "
    r"(?P<trend>decreased|increased|remained stable|dropped|rose|fell)[^.$]*"
    r"\$(?P<rate>[\d,]+)\s*per 40ft container",
    re.IGNORECASE,
)


def fetch_market_benchmark(destination_name: str) -> dict | None:
    """Return Drewry's latest published Shanghai -> `destination_name` rate,
    or None if the page can't be fetched or no sentence matches that lane.
    Only Shanghai-origin lanes are covered -- that's what the source page
    reports on.
    """
    try:
        response = requests.get(_WCI_URL, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        return None

    text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", response.text))
    for match in _LANE_SENTENCE.finditer(text):
        if match["dest"].strip().lower() == destination_name.lower():
            return {
                "origin": "Shanghai",
                "destination": match["dest"].strip(),
                "rate_usd_per_40ft": float(match["rate"].replace(",", "")),
                "trend": match["trend"].lower(),
                "source": "Drewry World Container Index",
                "source_url": _WCI_URL,
            }
    return None
