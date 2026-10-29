"""
Parallel.ai calls, held to the cheap tiers.

Cost ceiling: the Task API "base" price — $10 per 1,000 runs (Parallel
pricing, October 2026: Task lite $5, base $10, core $25 per 1,000; Search
turbo/fast $1, basic/advanced $5 per 1,000 requests).

  Task API     "base" by default (the user raised research from lite to the
               next tier, Oct 2026); "lite" allowed. Anything higher set in
               the environment is refused and logged, so a dashboard typo
               can't multiply the bill.
  Search API   "turbo" first ($1/1,000); "advanced" ($5/1,000 — the lite
               price) only for what turbo couldn't settle. Measured on ten
               NVIDIA leaders' LinkedIn profiles: advanced 8/10, turbo 7/10,
               fast and basic 3/10 — so fast/basic are never used.

Search uses /v1/search. The /v1beta endpoint now performs like the weakest
modes (3/10 on the same test) and Parallel's docs direct new code to /v1.
"""
import logging
import os

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.parallel.ai/v1/search"
TASKS_URL = "https://api.parallel.ai/v1/tasks/runs"

# $ per request, from Parallel's pricing page; env-overridable if it changes.
SEARCH_COST = {
    "turbo":    float(os.environ.get("PARALLEL_SEARCH_TURBO_COST_USD", "0.001")),
    "advanced": float(os.environ.get("PARALLEL_SEARCH_COST_USD", "0.005")),
}
TASK_COST = {
    "lite": float(os.environ.get("PARALLEL_TASK_LITE_COST_USD", "0.005")),
    "base": float(os.environ.get("PARALLEL_TASK_COST_USD", "0.010")),
}
TASK_COST_LITE = TASK_COST["lite"]

_ALLOWED_PROCESSORS = ("lite", "base")   # nothing above the base price
_ALLOWED_SEARCH_MODES = ("turbo", "advanced")


def task_processor() -> str:
    """The Task API processor to use — never above the base price ceiling."""
    asked = os.environ.get("ORGANOGRAM_PARALLEL_PROCESSOR", "base").strip().lower() or "base"
    if asked not in _ALLOWED_PROCESSORS:
        logger.warning("ORGANOGRAM_PARALLEL_PROCESSOR=%r is above the base cost ceiling; "
                       "using 'base'", asked)
        return "base"
    return asked


def api_key() -> str:
    return os.environ.get("PARALLEL_API_KEY", "")


def search(queries: list[str], objective: str = "", mode: str = "turbo",
           max_results: int = 10, max_chars_per_result: int = 600,
           timeout: float = 30) -> list[dict]:
    """
    One Parallel search. Returns results [{url, title, excerpts, publish_date}],
    or [] on any failure. Records its cost against the current job.
    """
    import httpx
    from usage_tracker import record_cost

    if mode not in _ALLOWED_SEARCH_MODES:
        mode = "turbo"
    key = api_key()
    if not key:
        return []
    body = {
        "search_queries": queries,
        "mode": mode,
        "advanced_settings": {"max_results": max_results,
                              "excerpt_settings": {"max_chars_per_result": max_chars_per_result}},
    }
    if objective:
        body["objective"] = objective
    resp = httpx.post(SEARCH_URL, json=body, timeout=timeout, headers={"x-api-key": key})
    record_cost("parallel", f"parallel-search-{mode}", SEARCH_COST[mode])
    if not resp.is_success:
        logger.warning("Parallel search (%s) HTTP %s: %s", mode, resp.status_code, resp.text[:200])
        return []
    return resp.json().get("results", []) or []
