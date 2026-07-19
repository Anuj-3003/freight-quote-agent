"""Live HS-code duty rate lookup against the US International Trade
Commission's public Harmonized Tariff Schedule search API.

Proof-of-concept external data source: augments the hand-parsed "7.5% duty"
line from customs_notes.txt with a real, authoritative government rate.

This is the same JSON endpoint the hts.usitc.gov search box calls -- it's
public and unauthenticated, but undocumented and not a stable contract.
A production version would use USITC DataWeb's registered API instead, add
retries, and disambiguate to a specific 8-10 digit statistical suffix
(country of origin, e.g. Section 301 surtaxes on China-origin goods, changes
the applicable rate and isn't handled here).
"""

import requests

_HTS_SEARCH_URL = "https://hts.usitc.gov/reststop/search"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; freight-quote-agent/1.0)"}
_TIMEOUT_SECONDS = 10


def fetch_hs_duty_rate(hs_code: str) -> dict | None:
    """Return the first real dutiable HTS line under `hs_code`, or None if
    the lookup fails or nothing matches. `hs_code` is a heading prefix like
    "8517" -- results are filtered to lines whose HTS number starts with it
    and that actually carry a general-rate value (skips group headers).
    """
    try:
        response = requests.get(
            _HTS_SEARCH_URL, params={"keyword": hs_code}, headers=_HEADERS, timeout=_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError):
        return None

    prefix = hs_code.replace(".", "")
    matches = [r for r in results if r.get("htsno", "").replace(".", "").startswith(prefix) and r.get("general")]
    if not matches:
        return None

    match = matches[0]
    return {
        "htsno": match["htsno"],
        "description": match["description"],
        "general_rate": match["general"],
        "other_rate": match["other"],
        "source": "USITC Harmonized Tariff Schedule",
        "source_url": f"https://hts.usitc.gov/search?query={hs_code}",
    }
