"""End-to-end check of the decision logic in compute_quote -- pure function,
no Neo4j or LLM required. Candidate rates below mirror exactly what
find_applicable_rates/get_active_holds_for_lane/get_compliance_flags return
for the sample request (250kg SHA->NYC, August 2026); see
tests/test_queries.py for the live-graph checks that produce this shape."""

import pytest

from src.agent.graph_agent import compute_quote
from src.agent.models import NoLaneDataError, QuoteDecision

SAMPLE_STATE = {
    "request": {"origin": "SHA", "destination": "NYC", "weight_kg": 250.0, "hs_code": "8517", "ship_date": "2026-08-01"},
    "candidate_rates": [
        {
            "carrier": "TransGlobal", "rate_id": "rate_005", "rate_usd_per_kg": 4.05, "min_charge": 90.0,
            "weight_break_kg": 45, "valid_from": "2026-01-15", "valid_to": "2026-12-31",
            "notes": "fuel surcharge included", "on_hold": False, "hold_reason": None,
        },
        {
            "carrier": "SwiftCargo", "rate_id": "rate_008", "rate_usd_per_kg": None, "min_charge": 70.0,
            "weight_break_kg": 100, "valid_from": "2026-03-01", "valid_to": None,
            "notes": "TBD - contact pricing desk", "on_hold": False, "hold_reason": None,
        },
    ],
    "active_holds": [
        {"carrier": "OceanLink", "reason": "OceanLink suspended for hazmat on SHA-NYC until further notice (compliance hold, 2026-05)."},
    ],
    "compliance_flags": [
        {"predicate": "duty_surcharge", "description": "NYC import: electronics HS 8517 subject to additional 7.5% duty as of 2026-03.",
         "value": 7.5, "effective_date": "2026-03-01", "scope_attribute": None, "scope_value": None},
        {"predicate": "filing_requirement", "description": "All US-bound shipments require ISF filing 24h before loading.",
         "value": 24.0, "effective_date": None, "scope_attribute": "country", "scope_value": "US"},
    ],
}


def test_sample_request_chooses_transglobal_at_the_correct_tier():
    result = compute_quote(dict(SAMPLE_STATE))
    decision = result["decision"]
    assert decision["chosen_carrier"] == "TransGlobal"
    assert decision["weight_break_kg"] == 45  # not the 300kg tier -- 250kg doesn't qualify
    assert decision["applicable_rate"] == 4.05
    assert decision["computed_cost"] == pytest.approx(250 * 4.05)


def test_oceanlink_excluded_and_flagged_despite_no_candidate_row():
    result = compute_quote(dict(SAMPLE_STATE))
    decision = result["decision"]
    oceanlink = next(a for a in decision["alternatives_considered"] if a["carrier"] == "OceanLink")
    assert oceanlink["status"] == "excluded_hold"
    assert any("OceanLink" in w and "hazmat" in w for w in decision["warnings"])


def test_swiftcargo_unpriced_tier_excluded_not_downgraded():
    result = compute_quote(dict(SAMPLE_STATE))
    decision = result["decision"]
    swiftcargo = next(a for a in decision["alternatives_considered"] if a["carrier"] == "SwiftCargo")
    assert swiftcargo["status"] == "excluded_unpriced"
    assert swiftcargo["computed_cost"] is None


def test_compliance_flags_surface_as_warnings():
    result = compute_quote(dict(SAMPLE_STATE))
    warnings = " ".join(result["decision"]["warnings"])
    assert "duty" in warnings.lower()
    assert "isf" in warnings.lower()


def test_live_enrichment_surfaces_as_extra_warning_without_affecting_cost():
    """Synthetic stand-in for what src/ingest/enrich_external.py would have
    written onto the graph -- no network involved, just checking that
    live-enriched fields flow into warnings and never touch cost/carrier
    choice (see tests/test_external.py for the real adapters)."""
    state = dict(SAMPLE_STATE)
    state["compliance_flags"] = [
        {**SAMPLE_STATE["compliance_flags"][0], "live_general_rate": "Free", "live_other_rate": "35%", "live_source": "USITC Harmonized Tariff Schedule"},
        SAMPLE_STATE["compliance_flags"][1],
    ]
    state["market_benchmark"] = {
        "rate_usd_per_40ft": 7879.0, "trend": "remained stable", "source": "Drewry World Container Index",
    }

    result = compute_quote(state)
    decision = result["decision"]

    assert decision["chosen_carrier"] == "TransGlobal"
    assert decision["computed_cost"] == pytest.approx(250 * 4.05)

    warnings = " ".join(decision["warnings"])
    assert "USITC" in warnings and "Free" in warnings
    assert "Drewry" in warnings and "7,879" in warnings


def test_no_valid_candidates_raises():
    state = dict(SAMPLE_STATE)
    state["candidate_rates"] = [
        {**SAMPLE_STATE["candidate_rates"][0], "on_hold": True, "hold_reason": "suspended"},
    ]
    state["active_holds"] = []
    with pytest.raises(ValueError):
        compute_quote(state)


def test_no_lane_data_raises_distinct_error_and_preserves_warnings():
    """Zero candidate rows AND zero holds -- a lane with no carrier coverage
    at all (e.g. Bangalore->New York) -- is a different case from 'rates
    exist but none are valid' above, and should raise NoLaneDataError
    (carrying any compliance warnings gathered) instead of a plain
    ValueError."""
    state = dict(SAMPLE_STATE)
    state["candidate_rates"] = []
    state["active_holds"] = []
    state["market_benchmark"] = None

    with pytest.raises(NoLaneDataError) as exc_info:
        compute_quote(state)

    assert "SHA" in str(exc_info.value) and "NYC" in str(exc_info.value)
    assert any("duty" in w.lower() for w in exc_info.value.warnings)


def test_quote_decision_rejects_chosen_carrier_missing_from_alternatives():
    with pytest.raises(Exception):
        QuoteDecision(
            chosen_carrier="TransGlobal",
            applicable_rate=4.05,
            weight_break_kg=45,
            computed_cost=1012.5,
            alternatives_considered=[],
            warnings=[],
        )
