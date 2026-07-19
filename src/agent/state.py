"""LangGraph state definition for the quote agent."""

from typing import TypedDict


class QuoteRequest(TypedDict):
    origin: str  # canonical Location code, e.g. "SHA" (synthesized for cities
    # outside the static alias table -- see resolve_request_location in clean.py)
    destination: str  # canonical Location code, e.g. "NYC"
    destination_country: str  # ISO country code, e.g. "US" -- gates the
    # US-only live duty lookup in query_graph
    weight_kg: float
    hs_code: str
    ship_date: str  # ISO date (YYYY-MM-DD); free-text month/year requests
    # resolve to the 1st of that month -- see parse_request in graph_agent.py


class AgentState(TypedDict, total=False):
    raw_text: str
    request: QuoteRequest
    candidate_rates: list
    active_holds: list
    compliance_flags: list
    market_benchmark: dict | None
    decision: dict
