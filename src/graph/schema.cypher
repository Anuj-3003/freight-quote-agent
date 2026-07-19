// Neo4j schema: constraints and indexes.
// See graph_schema.md for the full design writeup and rationale.

CREATE CONSTRAINT location_code IF NOT EXISTS
FOR (l:Location) REQUIRE l.code IS UNIQUE;

CREATE CONSTRAINT carrier_name IF NOT EXISTS
FOR (c:Carrier) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT lane_id IF NOT EXISTS
FOR (l:Lane) REQUIRE l.id IS UNIQUE;

CREATE CONSTRAINT rate_id IF NOT EXISTS
FOR (r:Rate) REQUIRE r.id IS UNIQUE;

CREATE CONSTRAINT hscode_code IF NOT EXISTS
FOR (h:HSCode) REQUIRE h.code IS UNIQUE;

CREATE CONSTRAINT constraint_id IF NOT EXISTS
FOR (c:Constraint) REQUIRE c.id IS UNIQUE;

// Live-enrichment proof of concept -- see src/ingest/enrich_external.py
CREATE CONSTRAINT market_benchmark_id IF NOT EXISTS
FOR (m:MarketBenchmark) REQUIRE m.id IS UNIQUE;

// Rate lookups filter heavily on validity window and weight break during
// quoting, so index them explicitly rather than relying on the label scan.
CREATE INDEX rate_validity IF NOT EXISTS
FOR (r:Rate) ON (r.valid_from, r.valid_to);

CREATE INDEX rate_weight_break IF NOT EXISTS
FOR (r:Rate) ON (r.weight_break_kg);

CREATE INDEX constraint_predicate IF NOT EXISTS
FOR (c:Constraint) ON (c.predicate);
