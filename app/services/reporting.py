import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import markdown as md
from openai import OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import BASE_DIR, settings
from ..models import ContentDailyMetric, DailyAccountMetric, PlatformAccount, Report


METRICS = ["followers_new", "views", "likes", "comments", "favorites", "shares", "leads"]
LABELS = {
    "followers_new": "新增粉丝",
    "views": "播放量",
    "likes": "点赞",
    "comments": "评论",
    "favorites": "收藏",
    "shares": "转发",
    "leads": "私信/线索",
}


def _totals(db: Session, target: date) -> dict[str, float]:
    rows = db.scalars(select(DailyAccountMetric).where(DailyAccountMetric.metric_date == target)).all()
    return {field: sum(getattr(row, field) for row in rows) for field in METRICS}


def latest_data_date(db: Session) -> date | None:
    return db.scalar(select(func.max(DailyAccountMetric.metric_date)))


def report_stats(db: Session, target: date) -> dict:
    current = _totals(db, target)
    previous = _totals(db, target - timedelta(days=1))
    history_dates = [target - timedelta(days=offset) for offset in range(1, 8)]
    history = [_totals(db, day) for day in history_dates]
    available = [row for row in history if any(row.values())]
    averages = {
        field: sum(row[field] for row in available) / len(available) if available else 0
        for field in METRICS
    }

    active_accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.is_active.is_(True))).all()
    present_ids = set(
        db.scalars(select(DailyAccountMetric.account_id).where(DailyAccountMetric.metric_date == target)).all()
    )
    missing = [f"{account.platform} · {account.name}" for account in active_accounts if account.id not in present_ids]

    contents = db.scalars(
        select(ContentDailyMetric).where(ContentDailyMetric.metric_date == target).order_by(ContentDailyMetric.views.desc())
    ).all()
    by_account: dict[int, list] = defaultdict(list)
    for content in contents:
        by_account[content.account_id].append(content)

    rankings = []
    accounts = {account.id: account for account in active_accounts}
    for account_id, rows in by_account.items():
        valid = [row for row in rows if row.views > 0]
        if not valid:
            continue

        def rate(row):
            return (row.likes + row.comments + row.favorites + row.shares) / row.views

        highest_views = max(valid, key=lambda row: row.views)
        best = max(valid, key=rate)
        worst = min(valid, key=rate)
        account = accounts.get(account_id) or rows[0].account
        rankings.append(
            {
                "account": f"{account.platform} · {account.name}",
                "highest_views": {"title": highest_views.title, "views": highest_views.views},
                "best": {"title": best.title, "rate": rate(best)},
                "worst": {"title": worst.title, "rate": rate(worst)},
            }
        )

    return {
        "date": target.isoformat(),
        "current": current,
        "previous": previous,
        "averages": averages,
        "average_sample_days": len(available),
        "missing_accounts": missing,
        "rankings": rankings,
    }


def percent_change(current: float, baseline: float) -> str:
    if baseline == 0:
        return "—" if current == 0 else "新增"
    return f"{(current - baseline) / baseline * 100:+.1f}%"


def base_markdown(stats: dict) -> str:
    target = stats["date"]
    lines = [f"# {target} 自媒体每日复盘", "", "## 数据概览", ""]
    if stats["missing_accounts"]:
        lines += [f"> 数据提醒：以下账号当日缺少数据：{'、'.join(stats['missing_accounts'])}", ""]
    lines += ["| 指标 | 当日 | 较前日 | 较前7日平均 |", "|---|---:|---:|---:|"]
    for field in METRICS:
        current = stats["current"][field]
        lines.append(
            f"| {LABELS[field]} | {current:,.0f} | {percent_change(current, stats['previous'][field])} | "
            f"{percent_change(current, stats['averages'][field])} |"
        )
    lines += ["", f"近 7 日平均使用了 {stats['average_sample_days']} 个有数据的日期。", ""]
    if stats["rankings"]:
        lines += ["## 内容表现", ""]
        for item in stats["rankings"]:
            lines += [
                f"### {item['account']}",
                f"- 最高播放：{item['highest_views']['title']}（{item['highest_views']['views']:,.0f}）",
                f"- 最高互动率：{item['best']['title']}（{item['best']['rate']:.2%}）",
                f"- 最低互动率：{item['worst']['title']}（{item['worst']['rate']:.2%}）",
                "",
            ]
    return "\n".join(lines)


def ai_analysis(stats: dict) -> tuple[str, str]:
    if not settings.openai_api_key:
        return "", "未配置 OPENAI_API_KEY，已生成纯数据版报告"
    prompt = f"""你是资深中文自媒体运营顾问。根据下面的汇总数据写一份简洁、具体、可执行的中文日报。
只分析给出的汇总数据和标题，不推测用户隐私。必须严格使用以下二级标题：
## 今日亮点
## 需要关注
## 内容分析
## 明日行动建议
## 风险提示
每节 2-4 条，避免空话。数据不足时明确说明。不要重复输出一级标题或数据表。

数据：{json.dumps(stats, ensure_ascii=False)}
"""
    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=45)
        response = client.responses.create(model=settings.openai_model, input=prompt)
        return response.output_text.strip(), ""
    except Exception as exc:  # 保证 AI 故障不影响数据报告
        code = getattr(exc, "code", "") or ""
        message = str(exc).lower()
        if code == "insufficient_quota" or "insufficient_quota" in message:
            return "", "OpenAI API 额度不足，请充值或检查项目消费上限；已生成纯数据版报告"
        if "invalid_api_key" in message or type(exc).__name__ == "AuthenticationError":
            return "", "OpenAI API 密钥无效；已生成纯数据版报告"
        if type(exc).__name__ == "RateLimitError":
            return "", "OpenAI API 请求过快，请稍后重试；已生成纯数据版报告"
        return "", f"AI 暂时不可用（{type(exc).__name__}）；已生成纯数据版报告"


def render_pdf(report: Report) -> str:
    output_dir = BASE_DIR / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"daily-{report.report_date.isoformat()}.pdf"
    try:
        from weasyprint import HTML

        page = f"""<!doctype html><meta charset='utf-8'><style>
        @page {{ size: A4; margin: 18mm; }} body {{ font-family: 'Noto Sans CJK SC','PingFang SC',sans-serif; color:#18181b; line-height:1.7; }}
        h1 {{ border-left:5px solid #e62828; padding-left:14px; }} h2 {{ margin-top:26px; }}
        table {{ width:100%; border-collapse:collapse; }} th,td {{ border-bottom:1px solid #ddd; padding:8px; text-align:left; }}
        blockquote {{ background:#fff3f3; margin:0; padding:10px 14px; border-left:3px solid #e62828; }}
        </style>{report.html_content}"""
        HTML(string=page, base_url=str(BASE_DIR)).write_pdf(target)
        return str(target)
    except Exception:
        return ""


def generate_report(db: Session, target: date | None = None, use_latest: bool = True) -> Report:
    requested = target or (date.today() - timedelta(days=1))
    if use_latest and not any(_totals(db, requested).values()):
        requested = latest_data_date(db) or requested
    stats = report_stats(db, requested)
    markdown_content = base_markdown(stats)
    analysis, error = ai_analysis(stats)
    if analysis:
        markdown_content += "\n\n" + analysis
    elif error:
        markdown_content += "\n\n## AI 分析状态\n\n" + error
    html_content = md.markdown(markdown_content, extensions=["tables", "fenced_code"])
    report = db.scalar(select(Report).where(Report.report_date == requested))
    if not report:
        report = Report(report_date=requested)
        db.add(report)
    report.status = "generated_with_warning" if error else "generated"
    report.markdown_content = markdown_content
    report.html_content = html_content
    report.stats_json = json.dumps(stats, ensure_ascii=False)
    report.ai_error = error
    report.updated_at = datetime.now().replace(microsecond=0)
    db.flush()
    report.pdf_path = render_pdf(report)
    return report
