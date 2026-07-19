"""Live-enrichment step: pulls real external data and merges it onto the
graph built by `load_graph.py`. Run *after* the main ingestion, as a
separate, re-runnable "refresh" step -- unlike rates/customs notes, this
data is live and expected to change between runs.

    python -m src.ingest.load_graph      # loads rates.csv + customs_notes.txt
    python -m src.ingest.enrich_external # then layer in live duty + market data

Network failures here are non-fatal: each source degrades to "no
enrichment for this node" rather than aborting the run, since the graph
must remain fully queryable on the static data alone.
"""

from datetime import datetime, timezone

from src.ingest.external.market_rate_scraper import fetch_market_benchmark
from src.ingest.external.tariff_lookup import fetch_hs_duty_rate
from src.ingest.load_graph import get_driver

_ENRICH_DUTY_QUERY = """
MATCH (c:Constraint {predicate: "duty_surcharge"})-[:APPLIES_TO]->(h:HSCode)
RETURN c.id AS constraint_id, h.code AS hs_code
"""

_SET_DUTY_QUERY = """
MATCH (c:Constraint {id: $constraint_id})
SET c.live_general_rate = $general_rate,
    c.live_other_rate = $other_rate,
    c.live_source = $source,
    c.live_source_url = $source_url,
    c.live_fetched_at = $fetched_at
"""

_SHANGHAI_LANES_QUERY = """
MATCH (lane:Lane)-[:ORIGIN]->(origin:Location {name: "Shanghai"})
MATCH (lane)-[:DESTINATION]->(dest:Location)
RETURN lane.id AS lane_id, dest.name AS destination_name
"""

_MERGE_BENCHMARK_QUERY = """
MATCH (lane:Lane {id: $lane_id})
MERGE (m:MarketBenchmark {id: $benchmark_id})
SET m.rate_usd_per_40ft = $rate_usd_per_40ft,
    m.trend = $trend,
    m.source = $source,
    m.source_url = $source_url,
    m.fetched_at = $fetched_at
MERGE (m)-[:APPLIES_TO]->(lane)
"""


def enrich_duty_rates(driver, fetched_at: str) -> int:
    with driver.session() as session:
        targets = [dict(r) for r in session.run(_ENRICH_DUTY_QUERY)]
        updated = 0
        for target in targets:
            live = fetch_hs_duty_rate(target["hs_code"])
            if live is None:
                continue
            session.run(
                _SET_DUTY_QUERY,
                constraint_id=target["constraint_id"],
                general_rate=live["general_rate"],
                other_rate=live["other_rate"],
                source=live["source"],
                source_url=live["source_url"],
                fetched_at=fetched_at,
            )
            updated += 1
        return updated


def enrich_market_benchmarks(driver, fetched_at: str) -> int:
    with driver.session() as session:
        lanes = [dict(r) for r in session.run(_SHANGHAI_LANES_QUERY)]
        updated = 0
        for lane in lanes:
            live = fetch_market_benchmark(lane["destination_name"])
            if live is None:
                continue
            session.run(
                _MERGE_BENCHMARK_QUERY,
                lane_id=lane["lane_id"],
                benchmark_id=f"benchmark_{lane['lane_id']}_drewry_wci",
                rate_usd_per_40ft=live["rate_usd_per_40ft"],
                trend=live["trend"],
                source=live["source"],
                source_url=live["source_url"],
                fetched_at=fetched_at,
            )
            updated += 1
        return updated


def run_enrichment():
    fetched_at = datetime.now(timezone.utc).isoformat()
    driver = get_driver()
    try:
        duty_count = enrich_duty_rates(driver, fetched_at)
        benchmark_count = enrich_market_benchmarks(driver, fetched_at)
        print(f"Enriched {duty_count} duty constraint(s), {benchmark_count} market benchmark(s)")
    finally:
        driver.close()


if __name__ == "__main__":
    run_enrichment()
