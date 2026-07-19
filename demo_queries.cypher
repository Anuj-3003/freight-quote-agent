// Demo script for the Loom recording -- paste these one at a time into
// Neo4j Browser (http://localhost:7474, auth neo4j/assignment) after
// running `python -m src.ingest.load_graph`.

// 1. Whole-graph overview -- good opening shot for the schema segment.
MATCH (n) RETURN n LIMIT 100;

// 2. The rate ladder for one carrier/lane -- shows weight breaks + validity
// windows living directly on Rate nodes.
MATCH (c:Carrier {name: "TransGlobal"})-[:OFFERS]->(r:Rate)-[:APPLIES_TO]->(l:Lane {id: "SHA-NYC"})
RETURN c.name, r.weight_break_kg, r.rate_usd_per_kg, r.valid_from, r.valid_to
ORDER BY r.weight_break_kg;

// 3. Every rate on the SHA-NYC lane across all carriers, for comparison.
MATCH (c:Carrier)-[:OFFERS]->(r:Rate)-[:APPLIES_TO]->(l:Lane {id: "SHA-NYC"})
RETURN c.name, r.weight_break_kg, r.rate_usd_per_kg, r.valid_from, r.valid_to, r.notes
ORDER BY c.name, r.weight_break_kg;

// 4. The three compliance Constraints, and what each APPLIES_TO.
MATCH (con:Constraint)
OPTIONAL MATCH (con)-[:APPLIES_TO]->(target)
RETURN con.predicate, con.description, con.scope_attribute, con.scope_value, collect(labels(target)) AS target_labels, collect(coalesce(target.name, target.code, target.id)) AS targets;

// 5. The actual query the agent runs to answer the sample request --
// this is find_applicable_rates() from src/graph/queries.py, inlined with
// literal params so it can be pasted directly. Highlights the weight-break
// selection (highest tier <= 250kg) and the hazmat-hold flag together.
MATCH (carrier:Carrier)-[:OFFERS]->(rate:Rate)-[:APPLIES_TO]->(lane:Lane {id: "SHA-NYC"})
WHERE rate.weight_break_kg <= 250
  AND rate.valid_from <= date("2026-08-01")
  AND (rate.valid_to IS NULL OR rate.valid_to >= date("2026-08-01"))
WITH carrier, rate
ORDER BY rate.weight_break_kg DESC
WITH carrier, collect(rate)[0] AS best_rate
OPTIONAL MATCH (hold:Constraint {predicate: "hazmat_suspension"})-[:APPLIES_TO]->(carrier)
OPTIONAL MATCH (hold)-[:APPLIES_TO]->(:Lane {id: "SHA-NYC"})
RETURN carrier.name,
       best_rate.weight_break_kg,
       best_rate.rate_usd_per_kg,
       (hold IS NOT NULL AND hold.effective_date <= date("2026-08-01")) AS on_hold,
       hold.description AS hold_reason
ORDER BY carrier.name;

// 6. Prove the weight-break logic with the alternate scenario: a 300kg
// shipment DOES qualify for TransGlobal's cheaper tier. Good "before/after"
// beat for explaining the highest-tier-<=-weight rule.
MATCH (carrier:Carrier)-[:OFFERS]->(rate:Rate)-[:APPLIES_TO]->(lane:Lane {id: "SHA-NYC"})
WHERE rate.weight_break_kg <= 300
  AND rate.valid_from <= date("2026-08-01")
  AND (rate.valid_to IS NULL OR rate.valid_to >= date("2026-08-01"))
WITH carrier, rate ORDER BY rate.weight_break_kg DESC
WITH carrier, collect(rate)[0] AS best_rate
RETURN carrier.name, best_rate.weight_break_kg, best_rate.rate_usd_per_kg;

// 7. If you ran enrich_external.py: show the live-enriched Constraint and
// the MarketBenchmark node.
MATCH (con:Constraint {predicate: "duty_surcharge"})
RETURN con.description, con.live_general_rate, con.live_other_rate, con.live_source;

MATCH (m:MarketBenchmark)-[:APPLIES_TO]->(l:Lane)
RETURN l.id, m.rate_usd_per_40ft, m.trend, m.source;
