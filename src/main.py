"""CLI entrypoint: run the agent against the sample quote request."""

import json
import sys

from src.agent.graph_agent import build_agent
from src.agent.models import NoLaneDataError

SAMPLE_REQUEST = (
    "Quote 250 kg of consumer electronics (HS 8517), Shanghai to New York, "
    "shipping August 2026. Cheapest valid option, and flag anything we should know."
)


def main():
    raw_text = " ".join(sys.argv[1:]) or SAMPLE_REQUEST
    agent = build_agent()

    print(f"Request: {raw_text}\n")
    try:
        result = agent.invoke({"raw_text": raw_text})
    except NoLaneDataError as e:
        print(str(e))
        if e.warnings:
            print("\nWhat we do know:")
            for warning in e.warnings:
                print(f"  - {warning}")
        return
    except ValueError as e:
        # A lane can exist (carriers do serve it) while still having no
        # quotable option for this specific request -- e.g. a weight below
        # every carrier's lowest tier, or every candidate held/unpriced.
        print(f"No quote available: {e}")
        return

    decision = result["decision"]
    print(f"Chosen carrier: {decision['chosen_carrier']}")
    print(f"Rate: ${decision['applicable_rate']}/kg at the {decision['weight_break_kg']}kg+ break")
    print(f"Computed cost: ${decision['computed_cost']:,.2f}")
    if decision["warnings"]:
        print("\nWarnings:")
        for warning in decision["warnings"]:
            print(f"  - {warning}")
    print("\nFull structured decision:")
    print(json.dumps(decision, indent=2, default=str))


if __name__ == "__main__":
    main()
