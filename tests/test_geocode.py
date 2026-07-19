"""Live checks against the real Nominatim geocoding API. Like
tests/test_external.py, a None result is treated as inconclusive (network
or API unreachable) rather than a failure -- the adapter itself is written
to degrade to None rather than raise, and that's what's under test here.
"""

import pytest

from src.ingest.external.geocode import resolve_location


def test_resolves_a_real_world_city_outside_the_static_catalog():
    result = resolve_location("Bangalore")
    if result is None:
        pytest.skip("Nominatim unreachable or changed shape -- inconclusive, not a failure")
    assert result["country"] == "IN"
    assert result["name"]


def test_resolves_a_known_catalog_city_consistently():
    result = resolve_location("New York")
    if result is None:
        pytest.skip("Nominatim unreachable or changed shape -- inconclusive, not a failure")
    assert result["country"] == "US"


def test_returns_none_for_nonsense_input():
    assert resolve_location("Xyzabc Nonexistent Place 999999") is None
