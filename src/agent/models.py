"""Structured output schema for the quote decision."""

from typing import Literal, Optional

from pydantic import BaseModel, model_validator


class NoLaneDataError(Exception):
    """Raised when no carrier has ever offered a rate for this lane at all
    (as opposed to rates existing but none being currently valid -- that
    case still raises a plain ValueError). Carries whatever warnings were
    gathered anyway (e.g. a live duty-rate check), so that information isn't
    lost just because there's no carrier quote to attach it to."""

    def __init__(self, message: str, warnings: list[str] | None = None):
        super().__init__(message)
        self.warnings = warnings or []


class RateOption(BaseModel):
    """One candidate rate considered for the quote (chosen or alternative)."""

    carrier: str
    weight_break_kg: Optional[int] = None
    rate_usd_per_kg: Optional[float] = None
    min_charge: Optional[float] = None
    computed_cost: Optional[float] = None
    status: Literal["valid", "excluded_hold", "excluded_unpriced", "excluded_no_tier"]
    reason: Optional[str] = None


class QuoteDecision(BaseModel):
    """Final structured decision returned by the agent."""

    chosen_carrier: str
    applicable_rate: float
    weight_break_kg: int
    computed_cost: float
    alternatives_considered: list[RateOption] = []
    warnings: list[str] = []

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.applicable_rate <= 0:
            raise ValueError("applicable_rate must be positive")
        if self.computed_cost <= 0:
            raise ValueError("computed_cost must be positive")
        chosen = [
            alt for alt in self.alternatives_considered
            if alt.carrier == self.chosen_carrier and alt.status == "valid"
        ]
        if not chosen:
            raise ValueError(
                "chosen_carrier must appear in alternatives_considered with status='valid'"
            )
        return self
