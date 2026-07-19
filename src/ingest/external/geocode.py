"""Live geocoding fallback for city names outside the assignment's static
Location alias table (src/ingest/clean.py) -- lets the agent resolve *any*
real-world city (e.g. "Bangalore") instead of crashing on an unrecognized
location.

Uses OpenStreetMap's free, unauthenticated Nominatim API. Per Nominatim's
usage policy: a descriptive User-Agent is required, and requests should be
rate-limited to roughly 1/second -- fine for this per-request use case, but
not something to hammer in a loop.
"""

import requests

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "freight-quote-agent/1.0 (take-home assignment)"}
_TIMEOUT_SECONDS = 10


def resolve_location(name: str) -> dict | None:
    """Resolve a free-text city name to a canonical name + ISO country code
    via Nominatim. Returns None on no match or any network/parse failure --
    callers should treat that as "can't resolve this location" and degrade
    gracefully, not crash.
    """
    try:
        response = requests.get(
            _NOMINATIM_URL,
            params={"q": name, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers=_HEADERS,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError):
        return None

    if not results:
        return None

    match = results[0]
    address = match.get("address", {})
    country_code = address.get("country_code")
    if not country_code:
        return None

    return {
        "name": match.get("name") or name,
        "country": country_code.upper(),
        "lat": float(match["lat"]),
        "lon": float(match["lon"]),
    }
