from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DailyAccountMetric, PlatformAccount


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe_number(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def detect_anomalies(db: Session, target: date) -> list[dict]:
    rows = db.scalars(
        select(DailyAccountMetric).where(
            DailyAccountMetric.metric_date >= target - timedelta(days=7),
            DailyAccountMetric.metric_date <= target,
        )
    ).all()
    grouped: dict[int, list[DailyAccountMetric]] = defaultdict(list)
    for row in rows:
        grouped[row.account_id].append(row)

    accounts = {account.id: account for account in db.scalars(select(PlatformAccount)).all()}
    results: list[dict] = []
    for account_id, items in grouped.items():
        today = next((item for item in items if item.metric_date == target), None)
        history = [item for item in items if item.metric_date < target][-7:]
        if not today or len(history) < 3:
            continue
        for field in ("views", "followers_new"):
            baseline = _avg([_safe_number(getattr(item, field, 0)) for item in history])
            current = _safe_number(getattr(today, field, 0))
            if baseline <= 0:
                continue
            change = (current - baseline) / baseline * 100
            if abs(change) >= 60:
                account = accounts.get(account_id) or today.account
                reason = "内容节奏变化或选题命中度波动"
                suggestion = "复查发布时间、标题、封面和最近 3 条选题的共性"
                if change > 0:
                    suggestion = "沉淀这次爆发的选题结构，复制到同平台账号"
                results.append(
                    {
                        "account": f"{account.platform} · {account.name}",
                        "metric": field,
                        "current": current,
                        "baseline": baseline,
                        "change": change,
                        "reason": reason,
                        "suggestion": suggestion,
                    }
                )
    return sorted(results, key=lambda item: abs(item["change"]), reverse=True)


def forecast_trend(values: list[tuple[date, float]]) -> dict:
    if len(values) < 2:
        return {"next": 0, "trend": "数据不足"}
    ordered = sorted((item for item in values if item and item[0] is not None), key=lambda item: item[0])
    if len(ordered) < 2:
        return {"next": 0, "trend": "数据不足"}
    xs = list(range(len(ordered)))
    ys = [_safe_number(item[1]) for item in ordered]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs) or 1
    slope = numerator / denominator
    next_value = max(0.0, ys[-1] + slope)
    trend = "上升" if slope > 0 else ("下降" if slope < 0 else "平稳")
    return {"next": next_value, "trend": trend, "slope": slope}


def explain_forecast(field: str, forecast: dict) -> str:
    if forecast["trend"] == "数据不足":
        return "历史数据不足，暂时无法给出可靠预测。"
    label = "播放量" if field == "views" else "涨粉"
    return f"{label}趋势{forecast['trend']}，按当前斜率推算下一期约为 {forecast['next']:,.0f}。"
