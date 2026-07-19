"""Integration checks against a live Neo4j instance (see docker-compose.yml:
`docker compose up -d neo4j`). These are the regression checks the
assignment calls out explicitly: wrong weight break, expired rate, missed
compliance flag. Skipped automatically if Neo4j isn't reachable.
"""

import pytest
from neo4j.exceptions import ServiceUnavailable

from src.graph.queries import find_applicable_rates, get_active_holds_for_lane, get_compliance_flags
from src.ingest.clean import load_customs_notes, load_rates
from src.ingest.load_graph import apply_constraints, build_graph, get_driver, reset_graph


@pytest.fixture(scope="module")
def driver():
    d = get_driver()
    try:
        d.verify_connectivity()
    except ServiceUnavailable:
        d.close()
        pytest.skip("Neo4j not reachable at NEO4J_URI -- run `docker compose up -d neo4j` first")

    with d.session() as session:
        already_seeded = session.run("MATCH (r:Rate) RETURN count(r) AS n").single()["n"] > 0

    # Only seed a fresh/empty graph -- Rate ingestion uses CREATE against a
    # unique id constraint, so re-running it on an already-seeded graph
    # would error. This also means running these tests never wipes any
    # live-enrichment data (src/ingest/enrich_external.py) layered on top of
    # a graph you'd already ingested.
    if not already_seeded:
        reset_graph(d)
        apply_constraints(d)
        build_graph(d, load_rates("data/rates.csv"), load_customs_notes("data/customs_notes.txt"))

    yield d
    d.close()


def _by_carrier(rates):
    return {r["carrier"]: r for r in rates}


def test_weight_break_selection_picks_highest_tier_not_exceeding_weight(driver):
    rates = _by_carrier(find_applicable_rates(driver, "SHA", "NYC", 250, "2026-08-01"))
    # 250kg doesn't reach TransGlobal's 300kg break -- must fall back to the 45kg tier,
    # not silently apply the cheaper 300kg rate a 250kg shipment doesn't qualify for.
    assert rates["TransGlobal"]["weight_break_kg"] == 45
    assert rates["TransGlobal"]["rate_usd_per_kg"] == 4.05


def test_weight_break_selection_uses_higher_tier_when_weight_qualifies(driver):
    rates = _by_carrier(find_applicable_rates(driver, "SHA", "NYC", 300, "2026-08-01"))
    assert rates["TransGlobal"]["weight_break_kg"] == 300
    assert rates["TransGlobal"]["rate_usd_per_kg"] == 2.95


def test_expired_rate_excluded_from_candidates(driver):
    # OceanLink's only SHA-LAX rate is valid through 2026-06-30.
    rates = _by_carrier(find_applicable_rates(driver, "SHA", "LAX", 50, "2026-08-01"))
    assert "OceanLink" not in rates


def test_rate_included_when_ship_date_within_validity_window(driver):
    rates = _by_carrier(find_applicable_rates(driver, "SHA", "LAX", 50, "2026-03-01"))
    assert rates["OceanLink"]["rate_usd_per_kg"] == 3.60


def test_unpriced_tier_surfaces_with_null_rate_not_a_fallback_price(driver):
    rates = _by_carrier(find_applicable_rates(driver, "SHA", "NYC", 250, "2026-08-01"))
    assert rates["SwiftCargo"]["weight_break_kg"] == 100
    assert rates["SwiftCargo"]["rate_usd_per_kg"] is None


def test_compliance_hold_flagged_for_oceanlink_sha_nyc(driver):
    holds = get_active_holds_for_lane(driver, "SHA", "NYC", "2026-08-01")
    assert any(h["carrier"] == "OceanLink" for h in holds)


def test_compliance_hold_not_active_before_effective_date(driver):
    holds = get_active_holds_for_lane(driver, "SHA", "NYC", "2026-02-01")
    assert holds == []


def test_duty_surcharge_flag_surfaces_for_nyc_electronics(driver):
    flags = get_compliance_flags(driver, "", "SHA", "NYC", "8517", "2026-08-01")
    assert any(f["predicate"] == "duty_surcharge" for f in flags)


def test_filing_requirement_flag_surfaces_for_any_us_destination(driver):
    flags = get_compliance_flags(driver, "", "SHA", "NYC", "8517", "2026-08-01")
    assert any(f["predicate"] == "filing_requirement" for f in flags)
