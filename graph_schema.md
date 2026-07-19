# Freight Quote Engine — Neo4j Graph Schema

This document describes the graph schema used to model carrier rates, lanes, and compliance
constraints for the freight quote agent. It reflects the ingestion of `rates.csv` and
`customs_notes.txt`.

## Design principles

1. **`Rate` is a node, not an edge property** — a single (carrier, lane) pair has multiple rates,
   one per weight break, sometimes with overlapping or successive validity windows. A property on
   a `Carrier→Lane` edge could only hold one price; a node can hold as many rate rows as the
   source data actually has.
2. **`Lane` is a first-class node** — compliance constraints and rates both need to reference "this
   lane" as a single unit (e.g. a hazmat suspension is scoped to a carrier *on* a lane, not to a
   location). Anchoring both `Rate` and `Constraint` to the same `Lane` node avoids duplicating
   origin/destination logic everywhere and keeps lane-level queries to one traversal.
3. **City names are normalized at ingestion, not in the graph** — `"SHA"`/`"Shanghai"` and
   `"NYC"`/`"New York"` resolve to one canonical `Location` node via a lookup table in the ETL
   step. This is a deterministic, testable step done once in Python rather than fuzzy-matched at
   query time.
4. **Country lives as a property on `Location`**, not as a separate node — sufficient to scope
   country-wide rules (e.g. "all US-bound shipments") without building out a full geography
   hierarchy the current data doesn't need.
5. **Compliance rules are modeled generically** — a single `Constraint` node type with a
   polymorphic `APPLIES_TO` relationship, rather than a typed node per rule (e.g.
   `HazmatSuspension`, `DutySurcharge`). New rule types can be added later as new `predicate`
   values with no schema change.
6. **Missing data is preserved, not dropped** — a rate row with no price (SwiftCargo's TBD
   listing) is still ingested as a `Rate` node with `rate_usd_per_kg: null`, so the graph doesn't
   silently lose the fact that an unpriced offering exists.

## Nodes

### `Location`

| property   | type            | example                                  |
|------------|-----------------|-------------------------------------------|
| `code`     | string (key)    | `"SHA"`, `"NYC"`, `"LAX"`                  |
| `name`     | string          | `"Shanghai"`, `"New York"`, `"Los Angeles"`|
| `aliases`  | list[string]    | `["SHA", "Shanghai"]`                      |
| `country`  | string          | `"CN"`, `"US"`                             |

Canonical representation of a place. Raw source strings (`"SHA"`, `"Shanghai"`) are resolved to
one node via an ingestion-time lookup table and retained in `aliases` for traceability.

### `Carrier`

| property | type   | example                              |
|----------|--------|----------------------------------------|
| `name`   | string | `"OceanLink"`, `"TransGlobal"`, `"SwiftCargo"` |

No aliasing problem in the source data — kept minimal.

### `Lane`

| property | type   | example    |
|----------|--------|------------|
| `id`     | string | `"SHA-NYC"`|

Derived join point representing a directed origin→destination pair. Not present as a column in
the source CSV — created during ingestion from each rate row's origin/destination.

### `Rate`

| property          | type            | example                                  |
|-------------------|-----------------|--------------------------------------------|
| `id`              | string          | `"rate_001"`                               |
| `rate_usd_per_kg` | float, nullable | `4.20`, or `null` (SwiftCargo TBD row)     |
| `min_charge`      | float           | `85`, `90`, `70`                           |
| `weight_break_kg` | int             | `45`, `100`, `300`                         |
| `valid_from`      | date            | `2026-01-01`                               |
| `valid_to`        | date, nullable  | `2026-06-30`, or `null` (open-ended)       |
| `currency`        | string          | `"USD"`                                    |
| `notes`           | string          | `"rate revision H2"`, `"fuel surcharge included"` |
| `source_row`      | int             | original CSV row number, for traceability  |

Both source date formats (`2026-01-01` and `01/07/2026`) are parsed to Neo4j's native `date` type
at ingestion. `valid_to: null` means open-ended (still valid), not unknown — Cypher checks treat
`IS NULL` as always-passing. Cost is computed as `max(weight_kg * rate_usd_per_kg, min_charge)`.

### `HSCode`

| property      | type   | example         |
|----------------|--------|-----------------|
| `code`        | string | `"8517"`        |
| `description` | string | `"electronics"` |

Only used by `Constraint` in the current data — no rate varies by commodity, so `Rate` does not
link to `HSCode`.

### `Constraint`

| property         | type            | example                                              |
|-------------------|-----------------|-------------------------------------------------------|
| `id`             | string          | generated                                              |
| `predicate`      | string          | `"hazmat_suspension"`, `"duty_surcharge"`, `"filing_requirement"` |
| `description`    | string          | plain-language text derived from the source sentence   |
| `effective_date` | date            | `2026-05-01`, `2026-03-01`                             |
| `value`          | float, nullable | `7.5` (duty %), `24` (filing lead time, hrs), `null`   |
| `scope_attribute`| string, nullable| `"country"`                                            |
| `scope_value`    | string, nullable| `"US"`                                                 |

Generic replacement for a typed compliance node. Scoped in one of two ways:

- **Entity-anchored** — one or more `APPLIES_TO` edges to specific `Carrier`, `Lane`, `Location`,
  or `HSCode` nodes. All targets must match the shipment being evaluated (AND semantics).
- **Attribute-anchored** — no `APPLIES_TO` edges; instead `scope_attribute`/`scope_value` is
  matched against a property at query time (used when the rule targets a category, e.g. "any US
  destination," rather than a specific node).

## Relationships

| relationship  | direction              | represents                                                        |
|---------------|-------------------------|--------------------------------------------------------------------|
| `OFFERS`      | `Carrier → Rate`        | this carrier is the source of this specific price row              |
| `APPLIES_TO`  | `Rate → Lane`           | this price is valid on this origin–destination pair                |
| `ORIGIN`      | `Lane → Location`       | the lane's starting point                                          |
| `DESTINATION` | `Lane → Location`       | the lane's endpoint                                                 |
| `APPLIES_TO`  | `Constraint → Carrier`  | (polymorphic) this rule targets a specific carrier                 |
| `APPLIES_TO`  | `Constraint → Lane`     | (polymorphic) this rule targets a specific lane                    |
| `APPLIES_TO`  | `Constraint → Location` | (polymorphic) this rule targets a specific location                |
| `APPLIES_TO`  | `Constraint → HSCode`   | (polymorphic) this rule targets a specific commodity classification|

`APPLIES_TO` is reused across both the pricing chain (`Rate → Lane`) and the compliance layer
(`Constraint → *`) deliberately — in each case it means the same thing: "this node's applicability
is scoped by the target." The compliance-side edges are polymorphic (same relationship name,
different possible target types) so new constraint scopes never require a new relationship type.

## Mapping the three compliance sentences

| source sentence (`customs_notes.txt`)                                            | `predicate`          | `value` | scoping                                                          |
|-------------------------------------------------------------------------------------|------------------------|---------|---------------------------------------------------------------------|
| "OceanLink suspended for hazmat on SHA-NYC until further notice"                   | `hazmat_suspension`   | `null`  | `APPLIES_TO → Carrier(OceanLink)`, `APPLIES_TO → Lane(SHA-NYC)`     |
| "NYC import: electronics HS 8517 subject to additional 7.5% duty as of 2026-03"    | `duty_surcharge`      | `7.5`   | `APPLIES_TO → Location(NYC)`, `APPLIES_TO → HSCode(8517)`          |
| "All US-bound shipments require ISF filing 24h before loading"                     | `filing_requirement`  | `24`    | no edges — `scope_attribute: "country"`, `scope_value: "US"`       |

## Known trade-offs

- The generic `Constraint` model costs some self-documentation compared to a typed node per rule
  (e.g. no relationship literally named `ON_LANE` or `SUBJECT_TO`) and pushes more evaluation
  logic into the agent/query layer, which must check both `APPLIES_TO` edges and the attribute
  filter. This is worth it for a domain — compliance rules — that grows unpredictably over time.
- `HSCode` is currently only reachable via `Constraint`. If future rate data becomes
  commodity-specific, `Rate` would gain its own `APPLIES_TO → HSCode` edge; not added now since
  no source data requires it.
- Rate revisions (e.g. OceanLink's 300kg break changing at H2 2026) are represented via
  overlapping `valid_from`/`valid_to` windows on separate `Rate` nodes, not an explicit
  `SUPERSEDES` version chain. Sufficient for point-in-time quoting; would need revisiting if rate
  history/audit trail becomes a requirement.

## Live enrichment (proof of concept)

Two optional, separately-run adapters (`src/ingest/external/`) pull real external data
alongside the static CSV/text sources, feeding the same graph rather than a side channel:

- **`tariff_lookup.py`** queries the public USITC Harmonized Tariff Schedule search API for a
  real duty rate on a given HS code, and the properties land directly on the existing
  `duty_surcharge` `Constraint` node (`live_general_rate`, `live_other_rate`, `live_source`) --
  no schema change needed, since duty rate is exactly what that node already models.
- **`market_rate_scraper.py`** scrapes Drewry's public World Container Index page for a
  market-wide reference rate on a Shanghai-origin lane (e.g. "$7,879 per 40ft container" to New
  York). This doesn't fit `Rate` (carrier-specific) or `Constraint` (compliance, not pricing), so
  it's a new minimal node: `MarketBenchmark {id, rate_usd_per_40ft, trend, source, source_url,
  fetched_at}`, linked the same way `Rate` is: `(:MarketBenchmark)-[:APPLIES_TO]->(:Lane)` --
  reusing the existing polymorphic `APPLIES_TO` relationship rather than inventing a new one.

Both are surfaced only as informational warnings in the agent's decision (`compute_quote`) --
they never affect carrier selection or computed cost, which stay driven entirely by the CSV-derived
`Rate` nodes as the assignment requires. See `src/ingest/enrich_external.py` for the ingestion step
and the module docstrings for the fragility caveats (undocumented API, narrative-text scraping) that
would need hardening before this touched production.
