"""Cleaning/normalization for the messy rates.csv + customs_notes.txt inputs.

See graph_schema.md for the graph model these functions feed into.
"""

import re
from datetime import date

import pandas as pd

# Canonical Location registry. `code` is what the graph keys on; every raw
# spelling seen in the source data maps to one of these via ALIASES.
LOCATIONS = {
    "SHA": {"name": "Shanghai", "country": "CN", "aliases": ["SHA", "Shanghai"]},
    "NYC": {"name": "New York", "country": "US", "aliases": ["NYC", "New York"]},
    "LAX": {"name": "Los Angeles", "country": "US", "aliases": ["LAX", "Los Angeles"]},
}

ALIASES = {
    alias.lower(): code for code, info in LOCATIONS.items() for alias in info["aliases"]
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def normalize_location(raw: str) -> str:
    """Map a raw location string (e.g. 'SHA', 'Shanghai') to its canonical code."""
    key = raw.strip().lower()
    if key not in ALIASES:
        raise ValueError(f"Unrecognized location: {raw!r}")
    return ALIASES[key]


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).upper()[:6] or "UNK"


def resolve_request_location(raw: str) -> dict:
    """Resolve a free-text city name from an incoming quote request: the
    static alias table first (fast path -- this assignment's 3 known
    cities), falling back to live geocoding (src/ingest/external/geocode.py)
    for any other real-world city, so an unrecognized city name doesn't
    crash the agent.

    Returns {"code", "name", "country"}. For a geocoded (non-catalog) city,
    `code` is synthesized from the resolved name/country -- it only needs to
    be a stable key for the Cypher lane lookup, which will correctly find no
    rate data for it, since there's no rate data for arbitrary cities yet.
    """
    key = raw.strip().lower()
    if key in ALIASES:
        code = ALIASES[key]
        info = LOCATIONS[code]
        return {"code": code, "name": info["name"], "country": info["country"]}

    from src.ingest.external.geocode import resolve_location

    geocoded = resolve_location(raw)
    if geocoded is None:
        raise ValueError(f"Unrecognized location: {raw!r}")

    # The geocoder normalizes phrasing (e.g. "New York City" -> "New York")
    # that the raw input didn't match -- re-check the static table against
    # its canonical name so known cities never fall through to a mismatched
    # synthetic code just because the user phrased it differently.
    canonical_key = geocoded["name"].strip().lower()
    if canonical_key in ALIASES:
        code = ALIASES[canonical_key]
        info = LOCATIONS[code]
        return {"code": code, "name": info["name"], "country": info["country"]}

    return {
        "code": f"{_slug(geocoded['name'])}-{geocoded['country']}",
        "name": geocoded["name"],
        "country": geocoded["country"],
    }


def parse_date(raw):
    """Parse a date string that may be ISO (YYYY-MM-DD) or day-first (DD/MM/YYYY).

    Returns None for missing/blank values (used for open-ended validity).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    if _ISO_DATE.match(raw):
        return date.fromisoformat(raw)
    m = _DMY_DATE.match(raw)
    if m:
        day, month, year = (int(g) for g in m.groups())
        return date(year, month, day)
    raise ValueError(f"Unrecognized date format: {raw!r}")


def load_rates(csv_path: str) -> pd.DataFrame:
    """Read rates.csv and return a cleaned DataFrame ready for graph ingestion.

    - origin/destination are normalized to canonical Location codes
    - valid_from/valid_to are parsed to python `date` (valid_to may be None)
    - rate_usd_per_kg is float or None (SwiftCargo's TBD row is kept, not dropped)
    - source_row preserves the original CSV row number for traceability
    """
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    df["source_row"] = df.index + 2  # +2: 1-indexed, plus the header row

    df["origin"] = df["origin"].map(normalize_location)
    df["destination"] = df["destination"].map(normalize_location)
    df["valid_from"] = df["valid_from"].map(parse_date)
    df["valid_to"] = df["valid_to"].map(parse_date)

    df["rate_usd_per_kg"] = df["rate_usd_per_kg"].map(lambda v: float(v) if v.strip() else None)
    df["min_charge"] = df["min_charge"].astype(float)
    df["weight_break_kg"] = df["weight_break_kg"].astype(int)
    df["notes"] = df["notes"].map(lambda v: v.strip() or None)

    return df


# --- customs_notes.txt --------------------------------------------------
#
# Three known sentence shapes in the source data; this is a deliberately
# small, rule-based parser tailored to those three patterns rather than a
# general free-text extractor. In production this step is exactly where
# you'd swap in an LLM extraction pass (or a proper NER/regex pipeline) to
# handle new sentence shapes without hand-writing a new regex per rule.

_DUTY_RE = re.compile(
    r"(?P<location>\w[\w\s]*) import: (?P<hs_description>[\w\s]+?) HS (?P<hs_code>\d+) "
    r"subject to additional (?P<pct>[\d.]+)% duty as of (?P<year>\d{4})-(?P<month>\d{2})",
    re.IGNORECASE,
)
_FILING_RE = re.compile(
    r"All (?P<scope_value>[\w-]+)-bound shipments require ISF filing "
    r"(?P<hours>\d+)h before loading",
    re.IGNORECASE,
)
_SUSPENSION_RE = re.compile(
    r"(?P<carrier>\w+) suspended for (?P<reason>\w+) on "
    r"(?P<origin>[A-Z]+)-(?P<destination>[A-Z]+) until further notice "
    r"\(compliance hold, (?P<year>\d{4})-(?P<month>\d{2})\)",
    re.IGNORECASE,
)


def load_customs_notes(txt_path: str):
    """Parse customs_notes.txt into Constraint dicts (see graph_schema.md).

    Each dict: predicate, description (raw sentence), effective_date, value,
    scope_attribute/scope_value (attribute-anchored rules), and applies_to
    (entity-anchored rules: carrier / lane / location / hs_code).
    """
    with open(txt_path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    constraints = []
    for line in lines:
        m = _DUTY_RE.search(line)
        if m:
            constraints.append({
                "predicate": "duty_surcharge",
                "description": line,
                "effective_date": date(int(m["year"]), int(m["month"]), 1),
                "value": float(m["pct"]),
                "scope_attribute": None,
                "scope_value": None,
                "applies_to": {
                    "location": normalize_location(m["location"]),
                    "hs_code": m["hs_code"],
                    "hs_description": m["hs_description"].strip(),
                },
            })
            continue

        m = _FILING_RE.search(line)
        if m:
            constraints.append({
                "predicate": "filing_requirement",
                "description": line,
                "effective_date": None,
                "value": float(m["hours"]),
                "scope_attribute": "country",
                "scope_value": m["scope_value"].upper(),
                "applies_to": {},
            })
            continue

        m = _SUSPENSION_RE.search(line)
        if m:
            constraints.append({
                "predicate": "hazmat_suspension",
                "description": line,
                "effective_date": date(int(m["year"]), int(m["month"]), 1),
                "value": None,
                "scope_attribute": None,
                "scope_value": None,
                "applies_to": {
                    "carrier": m["carrier"],
                    "lane": f"{normalize_location(m['origin'])}-{normalize_location(m['destination'])}",
                },
            })
            continue

        raise ValueError(f"Unrecognized customs note pattern: {line!r}")

    return constraints
