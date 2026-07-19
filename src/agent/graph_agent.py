"""LangGraph workflow that answers a quote request by reasoning over Neo4j.

Flow: parse_request (LLM extraction) -> query_graph (Cypher) -> compute_quote
(business rules) -> validate_output (Pydantic). The LLM only extracts
structured fields from free text -- it never sees the rate data and never
computes the quote; that's the graph's and the business logic's job.
"""

import json
from datetime import date

from langgraph.graph import StateGraph, END

from src.agent.models import NoLaneDataError, QuoteDecision, RateOption
from src.agent.state import AgentState
from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GROQ_API_KEY, GROQ_MODEL, LLM_PROVIDER
from src.graph.queries import (
    find_applicable_rates,
    get_active_holds_for_lane,
    get_compliance_flags,
    get_market_benchmark,
)
from src.ingest.clean import resolve_request_location
from src.ingest.external.tariff_lookup import fetch_hs_duty_rate
from src.ingest.load_graph import get_driver

# Shared extraction schema -- each provider wraps this the same way regardless
# of which LLM is actually doing the extraction.
_EXTRACT_PARAMS = {
    "type": "object",
    "properties": {
        "origin_city": {"type": "string", "description": "Origin city as named in the request, e.g. 'Shanghai'"},
        "destination_city": {"type": "string", "description": "Destination city as named in the request, e.g. 'New York'"},
        "weight_kg": {"type": "number"},
        "hs_code": {"type": "string", "description": "HS commodity code digits only, e.g. '8517'"},
        "ship_year": {"type": "integer", "description": "Omit entirely if no shipping date/month is mentioned in the request -- do not guess one."},
        "ship_month": {"type": "integer", "description": "1-12. Omit entirely if no shipping date/month is mentioned in the request -- do not guess one."},
    },
    "required": ["origin_city", "destination_city", "weight_kg", "hs_code"],
}
_TOOL_NAME = "extract_quote_request"
_TOOL_DESCRIPTION = "Extract structured shipment details from a freight quote request."
_NOT_A_REQUEST_ERROR = "This doesn't look like a shipment quote request -- try including a cargo, weight, and origin/destination."

# tool_choice is deliberately "auto", not forced: forcing the tool call means
# the model MUST invent a shipment even for input that isn't a quote request
# at all (verified: "ignore previous instructions..." produced a fully
# fabricated Shanghai->New York quote). Letting the model decline is what
# makes _NOT_A_REQUEST_ERROR reachable instead of silently hallucinating.
_SYSTEM_PROMPT = (
    "You extract shipment details from freight quote requests. Call "
    f"{_TOOL_NAME} only if the message actually describes or implies a "
    "cargo shipment (some combination of a commodity, weight, origin, or "
    "destination). For anything else -- questions, instructions, unrelated "
    "text -- do not call any tool."
)


def _extract_via_groq(raw_text: str) -> dict:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        tools=[{
            "type": "function",
            "function": {"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION, "parameters": _EXTRACT_PARAMS},
        }],
        tool_choice="auto",
    )
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise ValueError(_NOT_A_REQUEST_ERROR)
    return json.loads(tool_calls[0].function.arguments)


def _extract_via_anthropic(raw_text: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[{"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION, "input_schema": _EXTRACT_PARAMS}],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": raw_text}],
    )
    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise ValueError(_NOT_A_REQUEST_ERROR)
    return tool_use.input


def parse_request(state: AgentState) -> AgentState:
    """Turn the free-text quote request into a structured QuoteRequest.

    Skipped if `request` is already populated (lets callers/tests bypass the
    LLM and hand in a structured request directly). Provider is selected via
    LLM_PROVIDER ("groq" by default, or "anthropic") -- swapping providers is
    a config change, not a rewrite of the extraction logic.
    """
    if state.get("request"):
        return state

    extractor = _extract_via_groq if LLM_PROVIDER == "groq" else _extract_via_anthropic

    # LLM tool-calling occasionally produces a malformed call (a model
    # reliability quirk -- observed ~50% failure rate on some prompts with
    # Groq's llama-3.3-70b-versatile -- not something the caller did wrong).
    # A single retry only clears about 75% of that; a few attempts gets the
    # combined failure rate low enough to be a rare, not routine, error.
    last_error = None
    extracted = None
    for _ in range(4):
        try:
            extracted = extractor(state["raw_text"])
            break
        except ValueError as e:
            if str(e) == _NOT_A_REQUEST_ERROR:
                raise  # model correctly declined -- retrying won't change that
            last_error = e
        except Exception as e:
            last_error = e
    else:
        raise ValueError(
            "Couldn't parse this request into a shipment after several attempts. "
            "Try rephrasing with an explicit weight in kg, HS code, and origin/destination cities."
        ) from last_error

    origin = resolve_request_location(extracted["origin_city"])
    destination = resolve_request_location(extracted["destination_city"])

    ship_year, ship_month = extracted.get("ship_year"), extracted.get("ship_month")
    if ship_year and ship_month:
        ship_date = date(int(ship_year), int(ship_month), 1)
    else:
        # No shipping date/month in the request -- default to today rather
        # than let the LLM invent one (it will otherwise guess a plausible
        # but arbitrary date, which can miss every rate's validity window).
        today = date.today()
        ship_date = date(today.year, today.month, 1)

    request = {
        "origin": origin["code"],
        "destination": destination["code"],
        "destination_country": destination["country"],
        "weight_kg": float(extracted["weight_kg"]),
        "hs_code": extracted["hs_code"],
        "ship_date": ship_date.isoformat(),
    }
    return {**state, "request": request}


def _live_duty_check_flag(req: dict, existing_flags: list) -> dict | None:
    """Always-on duty check for the requested HS code, independent of
    whether the static customs_notes.txt already covers this destination --
    but only for US destinations, since USITC is a US-specific data source
    (calling it for other countries would be misleading). Skipped if the
    graph already surfaced a duty_surcharge flag for this request."""
    if req["destination_country"] != "US":
        return None
    if any(f["predicate"] == "duty_surcharge" for f in existing_flags):
        return None

    live_duty = fetch_hs_duty_rate(req["hs_code"])
    if live_duty is None:
        return None
    return {
        "predicate": "duty_surcharge",
        "description": (
            f"Live duty check ({live_duty['source']}): HS {req['hs_code']} into "
            f"{req['destination_country']} -- general rate '{live_duty['general_rate']}', "
            f"non-preferential 'other' rate '{live_duty['other_rate']}'."
        ),
        "value": None,
        "effective_date": None,
        "scope_attribute": None,
        "scope_value": None,
        "live_general_rate": None,
        "live_other_rate": None,
        "live_source": None,
    }


def query_graph(state: AgentState) -> AgentState:
    """Query Neo4j for candidate rates and compliance flags."""
    req = state["request"]
    driver = get_driver()
    try:
        candidate_rates = find_applicable_rates(
            driver, req["origin"], req["destination"], req["weight_kg"], req["ship_date"]
        )
        active_holds = get_active_holds_for_lane(driver, req["origin"], req["destination"], req["ship_date"])
        compliance_flags = get_compliance_flags(
            driver, "", req["origin"], req["destination"], req["hs_code"], req["ship_date"]
        )
        market_benchmark = get_market_benchmark(driver, req["origin"], req["destination"])
    finally:
        driver.close()

    live_flag = _live_duty_check_flag(req, compliance_flags)
    if live_flag:
        compliance_flags = compliance_flags + [live_flag]

    return {
        **state,
        "candidate_rates": candidate_rates,
        "active_holds": active_holds,
        "compliance_flags": compliance_flags,
        "market_benchmark": market_benchmark,
    }


def compute_quote(state: AgentState) -> AgentState:
    """Pick the cheapest valid option, compute cost, assemble alternatives.

    A candidate is valid only if it isn't on hold and has a real price --
    an eligible-but-unpriced tier (e.g. SwiftCargo's "TBD" row) is excluded
    rather than silently priced off a lower, non-applicable tier.
    """
    weight_kg = state["request"]["weight_kg"]
    options = []
    warnings = []

    for rate in state["candidate_rates"]:
        weight_break = rate["weight_break_kg"]
        cost = None
        if rate["on_hold"]:
            status = "excluded_hold"
            reason = rate["hold_reason"]
            warnings.append(f"{rate['carrier']}: excluded, {reason}")
        elif rate["rate_usd_per_kg"] is None:
            status = "excluded_unpriced"
            reason = f"no rate on file for the {weight_break}kg+ tier that applies to this shipment"
            warnings.append(f"{rate['carrier']}: {reason}" + (f" ({rate['notes']})" if rate["notes"] else ""))
        else:
            status = "valid"
            reason = None
            cost = max(weight_kg * rate["rate_usd_per_kg"], rate["min_charge"])

        options.append(RateOption(
            carrier=rate["carrier"],
            weight_break_kg=weight_break,
            rate_usd_per_kg=rate["rate_usd_per_kg"],
            min_charge=rate["min_charge"],
            computed_cost=cost,
            status=status,
            reason=reason,
        ))

    quoted_carriers = {rate["carrier"] for rate in state["candidate_rates"]}
    for hold in state["active_holds"]:
        if hold["carrier"] not in quoted_carriers:
            warnings.append(f"{hold['carrier']}: excluded, {hold['reason']}")
            options.append(RateOption(carrier=hold["carrier"], status="excluded_hold", reason=hold["reason"]))

    for flag in state["compliance_flags"]:
        warning = flag["description"]
        if flag.get("live_general_rate") is not None:
            warning += (
                f" [live check, {flag['live_source']}: general duty rate '{flag['live_general_rate']}', "
                f"non-preferential 'other' rate '{flag['live_other_rate']}' -- reconcile with the static rate above]"
            )
        warnings.append(warning)

    benchmark = state.get("market_benchmark")
    if benchmark:
        warnings.append(
            f"Market reference ({benchmark['source']}): {benchmark['trend']} at "
            f"${benchmark['rate_usd_per_40ft']:,.0f} per 40ft container -- for context only, "
            f"not used in this carrier-specific quote"
        )

    if not state["candidate_rates"] and not state["active_holds"]:
        # No carrier has ever offered a rate on this lane at all -- distinct
        # from "rates exist but none are valid right now" (plain ValueError
        # below). Still surface whatever we learned anyway (e.g. a live duty
        # check), since that's real information even without a carrier quote.
        raise NoLaneDataError(
            f"No carrier rate data available for {state['request']['origin']} -> "
            f"{state['request']['destination']}",
            warnings=warnings,
        )

    valid = [o for o in options if o.status == "valid"]
    if not valid:
        raise ValueError("No valid rate option found for this shipment")
    chosen = min(valid, key=lambda o: o.computed_cost)

    decision = QuoteDecision(
        chosen_carrier=chosen.carrier,
        applicable_rate=chosen.rate_usd_per_kg,
        weight_break_kg=chosen.weight_break_kg,
        computed_cost=chosen.computed_cost,
        alternatives_considered=options,
        warnings=warnings,
    )
    return {**state, "decision": decision.model_dump()}


def validate_output(state: AgentState) -> AgentState:
    """Re-validate the assembled decision against the Pydantic model."""
    QuoteDecision.model_validate(state["decision"])
    return state


def build_agent():
    graph = StateGraph(AgentState)

    graph.add_node("parse_request", parse_request)
    graph.add_node("query_graph", query_graph)
    graph.add_node("compute_quote", compute_quote)
    graph.add_node("validate_output", validate_output)

    graph.set_entry_point("parse_request")
    graph.add_edge("parse_request", "query_graph")
    graph.add_edge("query_graph", "compute_quote")
    graph.add_edge("compute_quote", "validate_output")
    graph.add_edge("validate_output", END)

    return graph.compile()
