"""Checks that the messy-data cleaning does what it claims: city aliases
resolve to one code, both date formats parse, and the missing SwiftCargo
rate is preserved (not dropped or coerced into 0)."""

from datetime import date

import pandas as pd

from src.ingest.clean import load_customs_notes, load_rates, normalize_location, parse_date


def test_city_aliases_normalize_to_same_location():
    assert normalize_location("SHA") == normalize_location("Shanghai") == "SHA"
    assert normalize_location("NYC") == normalize_location("New York") == "NYC"


def test_both_date_formats_parse_correctly():
    assert parse_date("2026-01-01") == date(2026, 1, 1)
    assert parse_date("01/07/2026") == date(2026, 7, 1)  # day-first, not month-first
    assert parse_date("") is None


def test_missing_rate_is_kept_as_null_not_dropped():
    df = load_rates("data/rates.csv")
    swiftcargo_100kg = df[(df["carrier"] == "SwiftCargo") & (df["weight_break_kg"] == 100)]
    assert len(swiftcargo_100kg) == 1
    assert pd.isna(swiftcargo_100kg.iloc[0]["rate_usd_per_kg"])


def test_open_ended_validity_has_no_valid_to():
    df = load_rates("data/rates.csv")
    swiftcargo_45kg = df[(df["carrier"] == "SwiftCargo") & (df["weight_break_kg"] == 45)]
    assert swiftcargo_45kg.iloc[0]["valid_to"] is None


def test_transglobal_alias_row_normalizes_to_same_lane_as_code_row():
    df = load_rates("data/rates.csv")
    transglobal = df[df["carrier"] == "TransGlobal"]
    assert set(transglobal["origin"]) == {"SHA"}
    assert set(transglobal["destination"]) == {"NYC"}


def test_customs_notes_parse_into_three_constraints():
    constraints = load_customs_notes("data/customs_notes.txt")
    predicates = {c["predicate"] for c in constraints}
    assert predicates == {"duty_surcharge", "filing_requirement", "hazmat_suspension"}

    hold = next(c for c in constraints if c["predicate"] == "hazmat_suspension")
    assert hold["applies_to"] == {"carrier": "OceanLink", "lane": "SHA-NYC"}
    assert hold["effective_date"] == date(2026, 5, 1)
