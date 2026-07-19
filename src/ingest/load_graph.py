"""Loads cleaned rate + customs data into Neo4j.

Node/relationship shapes follow graph_schema.md. Constraint ingestion uses
the FOREACH-conditional-create idiom (`FOREACH (_ IN CASE WHEN ... )`) to
attach only the APPLIES_TO edges that a given rule actually has, without an
APOC dependency (not bundled with neo4j:5-community).
"""

import math
from pathlib import Path

from neo4j import GraphDatabase

from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
from src.ingest.clean import LOCATIONS, load_customs_notes, load_rates

SCHEMA_CYPHER_PATH = Path(__file__).resolve().parent.parent / "graph" / "schema.cypher"


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _none_if_nan(value):
    """Normalize pandas' missing-value representations (NaN) to None so
    Neo4j stores an absent value as a real null, not a float NaN -- which
    would otherwise break `IS NULL` checks used for open-ended validity."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def apply_constraints(driver):
    """Create uniqueness constraints / indexes from schema.cypher."""
    statements = [
        stmt.strip()
        for stmt in SCHEMA_CYPHER_PATH.read_text(encoding="utf-8").split(";")
        if stmt.strip() and not stmt.strip().startswith("//")
    ]
    with driver.session() as session:
        for stmt in statements:
            session.run(stmt)


def _rate_rows(rates_df):
    rows = []
    for record in rates_df.to_dict("records"):
        origin = LOCATIONS[record["origin"]]
        destination = LOCATIONS[record["destination"]]
        rows.append({
            "rate_id": f"rate_{record['source_row']:03d}",
            "carrier": record["carrier"],
            "origin": record["origin"],
            "origin_name": origin["name"],
            "origin_country": origin["country"],
            "origin_aliases": origin["aliases"],
            "destination": record["destination"],
            "destination_name": destination["name"],
            "destination_country": destination["country"],
            "destination_aliases": destination["aliases"],
            "lane_id": f"{record['origin']}-{record['destination']}",
            "rate_usd_per_kg": _none_if_nan(record["rate_usd_per_kg"]),
            "min_charge": _none_if_nan(record["min_charge"]),
            "weight_break_kg": record["weight_break_kg"],
            "valid_from": record["valid_from"],
            "valid_to": record["valid_to"],
            "notes": _none_if_nan(record["notes"]),
            "source_row": record["source_row"],
        })
    return rows


_RATE_INGEST_QUERY = """
UNWIND $rows AS row
MERGE (origin:Location {code: row.origin})
  ON CREATE SET origin.name = row.origin_name, origin.country = row.origin_country,
                 origin.aliases = row.origin_aliases
MERGE (dest:Location {code: row.destination})
  ON CREATE SET dest.name = row.destination_name, dest.country = row.destination_country,
                 dest.aliases = row.destination_aliases
MERGE (lane:Lane {id: row.lane_id})
MERGE (lane)-[:ORIGIN]->(origin)
MERGE (lane)-[:DESTINATION]->(dest)
MERGE (carrier:Carrier {name: row.carrier})
CREATE (rate:Rate {
  id: row.rate_id,
  rate_usd_per_kg: row.rate_usd_per_kg,
  min_charge: row.min_charge,
  weight_break_kg: row.weight_break_kg,
  valid_from: row.valid_from,
  valid_to: row.valid_to,
  currency: "USD",
  notes: row.notes,
  source_row: row.source_row
})
MERGE (carrier)-[:OFFERS]->(rate)
MERGE (rate)-[:APPLIES_TO]->(lane)
"""

_CONSTRAINT_INGEST_QUERY = """
CREATE (c:Constraint {
  id: $id,
  predicate: $predicate,
  description: $description,
  effective_date: $effective_date,
  value: $value,
  scope_attribute: $scope_attribute,
  scope_value: $scope_value
})
FOREACH (_ IN CASE WHEN $carrier IS NOT NULL THEN [1] ELSE [] END |
  MERGE (car:Carrier {name: $carrier})
  MERGE (c)-[:APPLIES_TO]->(car)
)
FOREACH (_ IN CASE WHEN $lane IS NOT NULL THEN [1] ELSE [] END |
  MERGE (l:Lane {id: $lane})
  MERGE (c)-[:APPLIES_TO]->(l)
)
FOREACH (_ IN CASE WHEN $location IS NOT NULL THEN [1] ELSE [] END |
  MERGE (loc:Location {code: $location})
  MERGE (c)-[:APPLIES_TO]->(loc)
)
FOREACH (_ IN CASE WHEN $hs_code IS NOT NULL THEN [1] ELSE [] END |
  MERGE (h:HSCode {code: $hs_code})
    ON CREATE SET h.description = $hs_description
  MERGE (c)-[:APPLIES_TO]->(h)
)
"""


def build_graph(driver, rates_df, customs_constraints):
    """Write nodes/relationships for rates and customs notes into Neo4j."""
    with driver.session() as session:
        session.run(_RATE_INGEST_QUERY, rows=_rate_rows(rates_df))

        for i, constraint in enumerate(customs_constraints, start=1):
            applies_to = constraint["applies_to"]
            session.run(
                _CONSTRAINT_INGEST_QUERY,
                id=f"constraint_{i:03d}",
                predicate=constraint["predicate"],
                description=constraint["description"],
                effective_date=constraint["effective_date"],
                value=constraint["value"],
                scope_attribute=constraint["scope_attribute"],
                scope_value=constraint["scope_value"],
                carrier=applies_to.get("carrier"),
                lane=applies_to.get("lane"),
                location=applies_to.get("location"),
                hs_code=applies_to.get("hs_code"),
                hs_description=applies_to.get("hs_description"),
            )


def reset_graph(driver):
    """Wipe the graph clean for repeatable ingestion runs (dev convenience)."""
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def run_ingestion(data_dir="data"):
    driver = get_driver()
    try:
        reset_graph(driver)
        apply_constraints(driver)
        rates_df = load_rates(f"{data_dir}/rates.csv")
        constraints = load_customs_notes(f"{data_dir}/customs_notes.txt")
        build_graph(driver, rates_df, constraints)
    finally:
        driver.close()


if __name__ == "__main__":
    run_ingestion()
