from collections.abc import Iterable

from ..models import DailyAccountMetric


def _safe_number(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def summarize_metrics(rows: Iterable[DailyAccountMetric], metrics: list[str]) -> dict[str, float]:
    totals = {metric: 0.0 for metric in metrics}
    for row in rows:
        for metric in metrics:
            totals[metric] += _safe_number(getattr(row, metric, 0))
    return totals


def comparison_groups(rows: Iterable[DailyAccountMetric], mode: str) -> list[dict]:
    grouped: dict[str, list[DailyAccountMetric]] = {}
    for row in rows:
        account = getattr(row, "account", None)
        if mode == "account":
            key = getattr(account, "name", "") or "未填写"
        else:
            key = getattr(account, "platform", "") or "未填写"
        grouped.setdefault(key, []).append(row)

    result = []
    for name, items in sorted(grouped.items()):
        items.sort(key=lambda item: _safe_number(getattr(item, "views", 0)), reverse=True)
        result.append({"name": name, "rows": items, "max_views": max((_safe_number(getattr(item, "views", 0)) for item in items), default=0)})
    return result
