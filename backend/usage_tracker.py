"""
Per-request token/cost tracking for the Claude + Gemini calls made while
generating an org chart.

Scoped via a contextvar rather than threaded as a parameter through every
classifier/enrichment function — a request's ContextVar binding stays visible
through FastAPI's BackgroundTasks (Starlette runs them via
anyio.to_thread.run_sync, which copies the context into the worker thread),
so the same UsageTracker instance accumulates both the synchronous
industry-classification call and the later background enrichment calls for
one upload, while concurrent uploads for different orgs never share state.
"""

import os
from contextvars import ContextVar
from dataclasses import dataclass, field

# $ per million tokens (input, output).
# claude-haiku-4-5-20251001 / claude-haiku-4-5: Anthropic's own pricing table.
# gemini-2.5-flash: unverified estimate (Google's 2.5 Flash launch pricing) —
# override via env if wrong; surfaced to the frontend as an estimate flag.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5":          (1.00, 5.00),   # same model, alias
    "gemini-2.5-flash": (
        float(os.environ.get("GEMINI_FLASH_INPUT_PER_M", "0.30")),
        float(os.environ.get("GEMINI_FLASH_OUTPUT_PER_M", "2.50")),
    ),
}
GEMINI_PRICING_IS_ESTIMATE = True


@dataclass
class UsageTracker:
    calls: list[dict] = field(default_factory=list)

    def record(self, service: str, model: str, input_tokens: int, output_tokens: int) -> None:
        in_rate, out_rate = PRICING.get(model, (0.0, 0.0))
        cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
        self.calls.append({
            "service": service, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost,
        })

    def summary(self) -> dict:
        has_gemini = any(c["service"] == "gemini" for c in self.calls)
        return {
            "input_tokens":  sum(c["input_tokens"]  for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "cost_usd":      sum(c["cost_usd"]       for c in self.calls),
            "gemini_pricing_is_estimate": has_gemini and GEMINI_PRICING_IS_ESTIMATE,
        }


_current: ContextVar["UsageTracker | None"] = ContextVar("current_usage_tracker", default=None)


def start_tracking() -> UsageTracker:
    """Bind a fresh tracker to this request's context — call once per upload/demo-load."""
    tracker = UsageTracker()
    _current.set(tracker)
    return tracker


def record_usage(service: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """No-op if no tracker is bound (e.g. a debug endpoint outside the upload flow)."""
    tracker = _current.get()
    if tracker is not None:
        tracker.record(service, model, input_tokens, output_tokens)
