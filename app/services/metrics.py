from collections.abc import Iterable

from ..models import DailyAccountMetric


def summarize_metrics(rows: Iterable[DailyAccountMetric], metrics: list[str]) -> dict[str, float]:
    totals = {metric: 0.0 for metric in metrics}
    for row in rows:
        for metric in metrics:
            totals[metric] += getattr(row, metric)
    return totals


def comparison_groups(rows: Iterable[DailyAccountMetric], mode: str) -> list[dict]:
    grouped: dict[str, list[DailyAccountMetric]] = {}
    for row in rows:
        key = row.account.name if mode == "account" else row.account.platform
        grouped.setdefault(key, []).append(row)

    result = []
    for name, items in sorted(grouped.items()):
        items.sort(key=lambda item: item.views, reverse=True)
        result.append({"name": name, "rows": items, "max_views": max((item.views for item in items), default=0)})
    return result
