# Freight Quote Agent 

Agentic workflow that ingests messy freight-rate/customs data into Neo4j and
answers a shipping quote request by reasoning over the graph. See
`graph_schema.md` for the full schema writeup and design rationale.

## Structure

```
data/                   raw input files (rates.csv, customs_notes.txt)
src/
  config.py             env-driven config (Neo4j URI, API keys, model)
  ingest/
    clean.py             normalization: city aliases, date formats, missing rate
    load_graph.py         writes rates.csv + customs_notes.txt into Neo4j
    enrich_external.py    proof-of-concept live-data refresh step (see below)
    external/
      tariff_lookup.py     live USITC HTS duty-rate lookup
      market_rate_scraper.py live Drewry World Container Index scrape
      geocode.py            live Nominatim geocoding fallback for unknown cities
  graph/
    schema.cypher         constraints/indexes for all node labels
    queries.py             Cypher: applicable rates, holds, compliance flags, benchmarks
  agent/
    state.py               LangGraph state
    graph_agent.py          LangGraph workflow (parse -> query -> compute -> validate)
    models.py               Pydantic QuoteDecision/RateOption output schema
  main.py                 CLI entrypoint, runs the sample request
tests/
  test_ingest.py           cleaning/parsing checks (no external deps)
  test_agent.py             decision-logic checks (no external deps)
  test_queries.py           Cypher regression checks -- needs a live Neo4j
  test_external.py          live checks against the two real external sources
docker-compose.yml       neo4j + app services
Dockerfile
requirements.txt
```

## Setup

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
docker compose up -d neo4j
python -m venv .venv && .venv/Scripts/activate  # or source .venv/bin/activate
pip install -r requirements.txt
```

Neo4j Browser: http://localhost:7474 (auth `neo4j` / `assignment`)

## Ingest

```bash
python -m src.ingest.load_graph       # loads rates.csv + customs_notes.txt
python -m src.ingest.enrich_external  # optional: layer in live external data (see below)
```

## Run

```bash
python -m src.main
```

## Test

```bash
pytest
```

`test_queries.py` needs Neo4j running (`docker compose up -d neo4j`) and skips
cleanly otherwise. `test_external.py`/`test_geocode.py` hit real, unauthenticated
public APIs and skip (not fail) if a source is unreachable or changes shape.

**Note**: `test_queries.py`'s fixture resets and reloads the graph from
`rates.csv`/`customs_notes.txt` each run, for deterministic Cypher tests. If
you've run `enrich_external.py`, running `pytest` afterward wipes that
enrichment data — re-run `enrich_external.py` again if you want it back.

## Global location handling

`parse_request` resolves any real-world city name, not just the 3 in the
synthetic dataset: it checks the static alias table first, then falls back to
live geocoding (`external/geocode.py`, OpenStreetMap Nominatim, free/no key)
for anything else — so asking about an unsupported city (e.g. "Bangalore")
resolves cleanly instead of crashing. There's still no carrier *rate* data for
lanes outside the CSV (that's a data problem, not an engineering one — see
`graph_schema.md`'s roadmap section), so those requests raise a distinct
`NoLaneDataError` with a clear message, while still surfacing whatever
compliance/duty info is available (the duty-rate check now runs live for any
HS code on any US-bound shipment, regardless of whether the static
`customs_notes.txt` already covers that destination).

## Live data enrichment (proof of concept)

`src/ingest/enrich_external.py` is a separate, re-runnable step that pulls
real external data into the same graph, run *after* the main CSV/text
ingestion:

- **Duty rate** — `external/tariff_lookup.py` queries the public USITC
  Harmonized Tariff Schedule search API for the real duty rate on HS 8517 and
  writes it onto the existing `duty_surcharge` `Constraint` node
  (`live_general_rate`, `live_other_rate`, `live_source`).
- **Market benchmark** — `external/market_rate_scraper.py` scrapes Drewry's
  public World Container Index page for a live Shanghai-origin reference rate
  (e.g. "$7,879 per 40ft container" to New York), stored as a new
  `MarketBenchmark` node linked via the same `APPLIES_TO` relationship
  `Rate`/`Constraint` already use.

Both surface only as extra warnings in the agent's decision — they never
affect carrier selection or computed cost, which stay driven entirely by the
carrier CSV as the assignment requires. See the "Live enrichment" section of
`graph_schema.md` for the schema rationale and the module docstrings for the
fragility caveats (undocumented API, narrative-text scraping) worth hardening
before this touched production.

## The task

> "Quote 250 kg of consumer electronics (HS 8517), Shanghai to New York,
> shipping August 2026. Cheapest valid option, and flag anything we should
> know."

