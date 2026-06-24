import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DailyAccountMetric, ContentDailyMetric, PlatformAccount


def resolve_range(range_type: str, end_date: date | None = None, start_date: date | None = None) -> tuple[date, date]:
    end = end_date or date.today() - timedelta(days=1)
    if range_type == "yesterday":
        start = end
    elif range_type == "7d":
        start = end - timedelta(days=6)
    elif range_type == "30d":
        start = end - timedelta(days=29)
    else:
        start = start_date or end - timedelta(days=13)
    if start > end:
        start, end = end, start
    return start, end


def _metric_rows(db: Session, start_date: date, end_date: date, platform: str = "", account_id: int | None = None) -> list[DailyAccountMetric]:
    query = select(DailyAccountMetric).join(PlatformAccount).where(
        DailyAccountMetric.metric_date >= start_date,
        DailyAccountMetric.metric_date <= end_date,
    )
    if platform:
        query = query.where(PlatformAccount.platform == platform)
    if account_id:
        query = query.where(DailyAccountMetric.account_id == account_id)
    return db.scalars(query.order_by(DailyAccountMetric.metric_date.desc())).all()


def _content_rows(db: Session, start_date: date, end_date: date, platform: str = "", account_id: int | None = None) -> list[ContentDailyMetric]:
    query = select(ContentDailyMetric).join(PlatformAccount).where(
        ContentDailyMetric.metric_date >= start_date,
        ContentDailyMetric.metric_date <= end_date,
    )
    if platform:
        query = query.where(PlatformAccount.platform == platform)
    if account_id:
        query = query.where(ContentDailyMetric.account_id == account_id)
    return db.scalars(query.order_by(ContentDailyMetric.views.desc())).all()


def generateAIReport(db: Session, data: dict[str, Any], promptType: str) -> dict[str, Any]:
    range_type = str(data.get("range_type", "custom"))
    start_date, end_date = resolve_range(
        range_type,
        _parse_date(data.get("end_date")),
        _parse_date(data.get("start_date")),
    )
    platform = str(data.get("platform", "")).strip()
    account_id = int(data.get("account_id") or 0) or None
    metrics = _metric_rows(db, start_date, end_date, platform, account_id)
    contents = _content_rows(db, start_date, end_date, platform, account_id)
    total_views = sum(row.views for row in metrics)
    total_followers = sum(row.followers_new for row in metrics)
    best = contents[0] if contents else None
    worst = min(contents, key=lambda item: ((item.likes + item.comments + item.favorites + item.shares) / item.views) if item.views else 0, default=None)
    hottest = max(contents, key=lambda item: item.views, default=None)
    notes = [
        "# AI复盘",
        "",
        "## 一、整体数据概览",
        f"- 时间范围：{start_date} 至 {end_date}",
        f"- 总播放/阅读：{total_views:,.0f}",
        f"- 新增粉丝：{total_followers:,.0f}",
        f"- 统计条数：{len(metrics)} 条账号数据，{len(contents)} 条内容数据",
        "",
        "## 二、爆款内容分析",
        f"- 最佳内容：{best.title if best else '暂无'}",
        f"- 最高播放内容：{hottest.title if hottest else '暂无'}",
        f"- 爆款判断：{hottest.title if hottest else '暂无'}更适合复制标题结构和开头钩子。",
        "",
        "## 三、低效内容分析",
        f"- 低效内容：{worst.title if worst else '暂无'}",
        "- 低效原因：开头吸引力不足或转化动作不够明确。",
        "",
        "## 四、账号增长判断",
        f"- 当前趋势：{('上升' if total_followers >= 0 else '下降')}，需要继续盯紧互动率和转化备注。",
        "- 当前趋势基于历史数据与内容互动率做 mock 推断。",
        "",
        "## 五、下周优化建议",
        "- 复制高播放选题结构。",
        "- 优先优化前 3 秒钩子。",
        "- 聚焦高转化平台和账号。",
        "",
        "## 六、可执行任务清单",
        "- 复盘本周前三条高表现内容。",
        "- 拆解一条低效内容。",
        "- 输出 5 个同题材延展选题。",
    ]
    copy_text = "\n".join([
        f"{promptType}复盘结论：",
        f"时间范围 {start_date} 至 {end_date}，总播放/阅读 {total_views:,.0f}，新增粉丝 {total_followers:,.0f}。",
        f"重点内容：{best.title if best else '暂无'}。",
        "建议优先复制爆款结构，调整开头钩子和转化设计。",
    ])
    sections = {
        "overall": {
            "total_views": total_views,
            "total_followers": total_followers,
            "best_content": best.title if best else "",
            "worst_content": worst.title if worst else "",
        },
        "reasoning": "mock 分析逻辑，后续可替换为真实 AI。",
    }
    return {
        "markdown": "\n".join(notes),
        "copy_text": copy_text,
        "summary_json": sections,
        "best_title": best.title if best else "",
        "worst_title": worst.title if worst else "",
        "start_date": start_date,
        "end_date": end_date,
    }


def fetchHotTopics(platform: str, keyword: str) -> list[dict[str, str]]:
    base_platform = platform or "全平台"
    base_keyword = keyword or "热门话题"
    topics: list[dict[str, str]] = []
    for index in range(1, 9):
        topics.append(
            {
                "topic": f"{base_keyword} · {base_platform} 选题 {index}",
                "trend": "近期持续升温",
                "reference": f"https://example.com/hot/{index}",
                "angle": f"从{base_keyword}的真实场景切入，结合{base_platform}表达方法",
            }
        )
    return topics


def syncPlatformData(account: PlatformAccount) -> str:
    return "当前平台暂未授权，请先配置 API 或导入数据"


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
