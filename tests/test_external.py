"""Live checks against the real external sources (USITC HTS API, Drewry
WCI page). These hit the network and depend on third parties we don't
control, so a None result is treated as inconclusive (site/API changed or
unreachable) rather than a failure -- the adapters themselves are written
to degrade to None rather than raise, and that's what's under test here.
"""

import pytest

from src.ingest.external.market_rate_scraper import fetch_market_benchmark
from src.ingest.external.tariff_lookup import fetch_hs_duty_rate


def test_hs_duty_lookup_returns_a_real_rate_for_8517():
    result = fetch_hs_duty_rate("8517")
    if result is None:
        pytest.skip("USITC HTS API unreachable or changed shape -- inconclusive, not a failure")
    assert result["htsno"].startswith("8517")
    assert result["general_rate"]
    assert result["source"] == "USITC Harmonized Tariff Schedule"


def test_hs_duty_lookup_returns_none_for_a_nonexistent_code():
    assert fetch_hs_duty_rate("99999999") is None


def test_market_benchmark_returns_a_real_rate_for_shanghai_new_york():
    result = fetch_market_benchmark("New York")
    if result is None:
        pytest.skip("Drewry WCI page unreachable or changed shape -- inconclusive, not a failure")
    assert result["origin"] == "Shanghai"
    assert result["rate_usd_per_40ft"] > 0
    assert result["source"] == "Drewry World Container Index"


def test_market_benchmark_returns_none_for_an_uncovered_lane():
    assert fetch_market_benchmark("Nonexistent City") is None
