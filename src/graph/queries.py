"""Cypher queries the agent uses to reason over the graph.

These queries surface facts (which rate tier applies, is a carrier on hold,
which compliance rules match) -- they don't decide what to *do* with those
facts (exclude vs. warn). That decision lives in the agent layer
(src/agent/graph_agent.py) so the query layer stays a straightforward
translation of "what does the graph say."
"""

_APPLICABLE_RATES_QUERY = """
MATCH (carrier:Carrier)-[:OFFERS]->(rate:Rate)-[:APPLIES_TO]->(lane:Lane {id: $lane_id})
WHERE rate.weight_break_kg <= $weight_kg
  AND rate.valid_from <= date($ship_date)
  AND (rate.valid_to IS NULL OR rate.valid_to >= date($ship_date))
WITH carrier, rate
ORDER BY rate.weight_break_kg DESC
WITH carrier, collect(rate)[0] AS best_rate
OPTIONAL MATCH (hold:Constraint {predicate: "hazmat_suspension"})-[:APPLIES_TO]->(carrier)
OPTIONAL MATCH (hold)-[:APPLIES_TO]->(:Lane {id: $lane_id})
WITH carrier, best_rate,
     (hold IS NOT NULL AND hold.effective_date <= date($ship_date)) AS on_hold,
     hold.description AS hold_reason
RETURN carrier.name AS carrier,
       best_rate.id AS rate_id,
       best_rate.rate_usd_per_kg AS rate_usd_per_kg,
       best_rate.min_charge AS min_charge,
       best_rate.weight_break_kg AS weight_break_kg,
       best_rate.valid_from AS valid_from,
       best_rate.valid_to AS valid_to,
       best_rate.notes AS notes,
       coalesce(on_hold, false) AS on_hold,
       hold_reason
ORDER BY carrier.name
"""


def find_applicable_rates(driver, origin: str, destination: str, weight_kg: float, ship_date: str):
    """Return, per carrier, the single weight-break tier that applies to
    this shipment on this lane and is valid on `ship_date` -- i.e. the
    highest `weight_break_kg` not exceeding `weight_kg` (a 250kg shipment
    does not qualify for a 300kg-break rate). Includes tiers with a null
    price (e.g. SwiftCargo's "TBD" row) and flags any active hazmat hold on
    the carrier for this lane; the caller decides how to treat those.

    `ship_date` is an ISO date string (YYYY-MM-DD).
    """
    lane_id = f"{origin}-{destination}"
    with driver.session() as session:
        result = session.run(
            _APPLICABLE_RATES_QUERY,
            lane_id=lane_id,
            weight_kg=weight_kg,
            ship_date=ship_date,
        )
        return [dict(record) for record in result]


_COMPLIANCE_FLAGS_QUERY = """
MATCH (constraint:Constraint)
WHERE (constraint.effective_date IS NULL OR constraint.effective_date <= date($ship_date))
  AND (
    (
      EXISTS { (constraint)-[:APPLIES_TO]->() }
      AND NOT EXISTS {
        MATCH (constraint)-[:APPLIES_TO]->(target)
        WHERE NOT (
          (target:Carrier AND target.name = $carrier)
          OR (target:Lane AND target.id = $lane_id)
          OR (target:Location AND target.code = $destination)
          OR (target:HSCode AND target.code = $hs_code)
        )
      }
    )
    OR (
      constraint.scope_attribute = "country"
      AND EXISTS {
        MATCH (loc:Location {code: $destination})
        WHERE loc.country = constraint.scope_value
      }
    )
  )
RETURN DISTINCT constraint.predicate AS predicate,
       constraint.description AS description,
       constraint.value AS value,
       constraint.effective_date AS effective_date,
       constraint.scope_attribute AS scope_attribute,
       constraint.scope_value AS scope_value,
       constraint.live_general_rate AS live_general_rate,
       constraint.live_other_rate AS live_other_rate,
       constraint.live_source AS live_source
ORDER BY predicate
"""


_ACTIVE_HOLDS_QUERY = """
MATCH (hold:Constraint {predicate: "hazmat_suspension"})-[:APPLIES_TO]->(carrier:Carrier)
MATCH (hold)-[:APPLIES_TO]->(:Lane {id: $lane_id})
WHERE hold.effective_date IS NULL OR hold.effective_date <= date($ship_date)
RETURN DISTINCT carrier.name AS carrier, hold.description AS reason
"""


def get_active_holds_for_lane(driver, origin: str, destination: str, ship_date: str):
    """Return carriers under an active compliance hold on this lane, even if
    they have no rate tier that would otherwise apply to this shipment --
    a held carrier is worth flagging regardless of whether it could price."""
    lane_id = f"{origin}-{destination}"
    with driver.session() as session:
        result = session.run(_ACTIVE_HOLDS_QUERY, lane_id=lane_id, ship_date=ship_date)
        return [dict(record) for record in result]


_MARKET_BENCHMARK_QUERY = """
MATCH (m:MarketBenchmark)-[:APPLIES_TO]->(:Lane {id: $lane_id})
RETURN m.rate_usd_per_40ft AS rate_usd_per_40ft, m.trend AS trend, m.source AS source, m.fetched_at AS fetched_at
"""


def get_market_benchmark(driver, origin: str, destination: str):
    """Return the live-scraped market benchmark rate for this lane, if the
    proof-of-concept enrichment step (src/ingest/enrich_external.py) has
    populated one -- None otherwise. Informational only; never used to
    pick a carrier or compute cost."""
    lane_id = f"{origin}-{destination}"
    with driver.session() as session:
        record = session.run(_MARKET_BENCHMARK_QUERY, lane_id=lane_id).single()
        return dict(record) if record else None


def get_compliance_flags(driver, carrier: str, origin: str, destination: str, hs_code: str, ship_date: str):
    """Return compliance/customs Constraint rows relevant to this shipment:
    entity-anchored rules where every APPLIES_TO target matches (carrier,
    lane, destination location, HS code), plus attribute-anchored rules
    (e.g. "any US destination") matched against the destination's country.
    """
    lane_id = f"{origin}-{destination}"
    with driver.session() as session:
        result = session.run(
            _COMPLIANCE_FLAGS_QUERY,
            carrier=carrier,
            lane_id=lane_id,
            destination=destination,
            hs_code=hs_code or "",
            ship_date=ship_date,
        )
        return [dict(record) for record in result]
