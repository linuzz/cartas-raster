"""Decide what to publish or which pull requests to open.

Rejected fingerprints stay rejected in both modes until a different file
(a new sha256) shows up. Review mode opens at most ``max_new`` pull requests
per run and does not open a second PR for a fingerprint that already has one.
"""

from __future__ import annotations

from cartas_raster.models import OpenPullRequest, PreparedChart, ReviewDecision
from cartas_raster.store import is_rejected


def select_for_publish(prepared: list[PreparedChart], rejected: dict) -> tuple[list[PreparedChart], list[str]]:
    accepted: list[PreparedChart] = []
    skipped: list[str] = []
    for chart in prepared:
        if is_rejected(rejected, chart.remote.chart_id, chart.sha256):
            skipped.append(chart.remote.chart_id)
            continue
        accepted.append(chart)
    return accepted, skipped


def select_for_review(
    prepared: list[PreparedChart],
    rejected: dict,
    open_prs: list[OpenPullRequest],
    max_new: int,
) -> ReviewDecision:
    by_chart: dict[str, list[OpenPullRequest]] = {}
    for pr in open_prs:
        by_chart.setdefault(pr.chart_id, []).append(pr)
    decision = ReviewDecision()
    pending: list[PreparedChart] = []
    for chart in sorted(prepared, key=lambda item: item.remote.chart_id):
        chart_id = chart.remote.chart_id
        if is_rejected(rejected, chart_id, chart.sha256):
            decision.skipped_rejected.append(chart_id)
            continue
        existing = by_chart.get(chart_id, [])
        if any(pr.sha256 == chart.sha256 for pr in existing):
            decision.skipped_duplicate.append(chart_id)
            continue
        pending.append((chart, [pr for pr in existing if pr.sha256 != chart.sha256]))
    decision.opens = [chart for chart, _prs in pending[:max_new]]
    opened = {chart.remote.chart_id for chart in decision.opens}
    for chart, stale in pending:
        if chart.remote.chart_id in opened:
            decision.supersede.extend(stale)
    decision.skipped_cap = [chart.remote.chart_id for chart, _prs in pending[max_new:]]
    return decision
