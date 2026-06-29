import json
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import markdown as md
from openai import OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import BASE_DIR
from ..models import ContentDailyMetric, DailyAccountMetric, PlatformAccount, Report, SummaryReport
from .runtime import runtime_settings


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


def _safe_number(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_text(value, fallback: str = "") -> str:
    text = "" if value is None else str(value).strip()
    return text or fallback


def _totals(db: Session, target: date) -> dict[str, float]:
    try:
        rows = db.scalars(select(DailyAccountMetric).where(DailyAccountMetric.metric_date == target)).all()
    except Exception:
        return {field: 0.0 for field in METRICS}
    return {field: sum(_safe_number(getattr(row, field, 0)) for row in rows) for field in METRICS}


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

    try:
        active_accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.is_active.is_(True))).all()
        present_ids = set(
            db.scalars(select(DailyAccountMetric.account_id).where(DailyAccountMetric.metric_date == target)).all()
        )
        missing = [f"{_safe_text(account.platform, '未填写')} · {_safe_text(account.name, '未命名账号')}" for account in active_accounts if account.id not in present_ids]
    except Exception:
        active_accounts = []
        missing = []

    try:
        contents = db.scalars(
            select(ContentDailyMetric).where(ContentDailyMetric.metric_date == target).order_by(ContentDailyMetric.views.desc())
        ).all()
    except Exception:
        contents = []
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
            views = _safe_number(getattr(row, "views", 0))
            if views <= 0:
                return 0.0
            return (
                _safe_number(getattr(row, "likes", 0))
                + _safe_number(getattr(row, "comments", 0))
                + _safe_number(getattr(row, "favorites", 0))
                + _safe_number(getattr(row, "shares", 0))
            ) / views

        highest_views = max(valid, key=lambda row: row.views)
        best = max(valid, key=rate)
        worst = min(valid, key=rate)
        account = accounts.get(account_id) or rows[0].account
        rankings.append(
            {
                "account": f"{_safe_text(getattr(account, 'platform', ''), '未填写')} · {_safe_text(getattr(account, 'name', ''), '未命名账号')}",
                "highest_views": {"title": _safe_text(highest_views.title, "未命名内容"), "views": _safe_number(highest_views.views)},
                "best": {"title": _safe_text(best.title, "未命名内容"), "rate": rate(best)},
                "worst": {"title": _safe_text(worst.title, "未命名内容"), "rate": rate(worst)},
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


def summary_stats(db: Session, start_date: date, end_date: date) -> dict:
    try:
        rows = db.scalars(
            select(DailyAccountMetric).where(
                DailyAccountMetric.metric_date >= start_date,
                DailyAccountMetric.metric_date <= end_date,
            )
        ).all()
    except Exception:
        rows = []
    current = {field: sum(_safe_number(getattr(row, field, 0)) for row in rows) for field in METRICS}
    days = max((end_date - start_date).days + 1, 1)
    averages = {field: current[field] / days for field in METRICS}
    try:
        contents = db.scalars(
            select(ContentDailyMetric)
            .where(ContentDailyMetric.metric_date >= start_date, ContentDailyMetric.metric_date <= end_date)
            .order_by(ContentDailyMetric.views.desc())
            .limit(10)
        ).all()
    except Exception:
        contents = []
    rankings = [
        {
            "account": f"{_safe_text(getattr(getattr(item, 'account', None), 'platform', ''), '未填写')} · {_safe_text(getattr(getattr(item, 'account', None), 'name', ''), '未命名账号')}",
            "highest_views": {"title": _safe_text(item.title, '未命名内容'), "views": _safe_number(item.views)},
            "best": {"title": _safe_text(item.title, '未命名内容'), "rate": ((_safe_number(item.likes) + _safe_number(item.comments) + _safe_number(item.favorites) + _safe_number(item.shares)) / _safe_number(item.views)) if _safe_number(item.views) else 0},
            "worst": {"title": _safe_text(item.title, '未命名内容'), "rate": ((_safe_number(item.likes) + _safe_number(item.comments) + _safe_number(item.favorites) + _safe_number(item.shares)) / _safe_number(item.views)) if _safe_number(item.views) else 0},
        }
        for item in contents
    ]
    return {
        "date": f"{start_date} 至 {end_date}",
        "current": current,
        "previous": {field: 0 for field in METRICS},
        "averages": averages,
        "average_sample_days": days,
        "missing_accounts": [],
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


def ai_analysis(stats: dict, settings) -> tuple[str, str]:
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


def render_pdf(report: Report | SummaryReport) -> str:
    from ..config import settings

    output_dir = settings.storage_dir / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = getattr(report, "report_date", None) or getattr(report, "end_date", None)
    prefix = getattr(report, "report_type", None) or getattr(report, "period_type", None) or "report"
    target = output_dir / f"{prefix}-{stamp.isoformat()}.pdf"

    def fallback_pdf() -> str:
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

            # 回退到纯文本版，确保缺少系统图形库时仍可导出 PDF。
            text = re.sub(r"<[^>]+>", "", report.html_content or "")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            styles = getSampleStyleSheet()
            story = [Paragraph((getattr(report, "title", "") or "复盘报告").replace("\n", " "), styles["Title"]), Spacer(1, 16)]
            for line in lines:
                story.append(Paragraph(line.replace("\n", " "), styles["BodyText"]))
                story.append(Spacer(1, 8))
            SimpleDocTemplate(str(target), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=42, bottomMargin=42).build(story)
            return str(target)
        except Exception:
            return ""

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
        return fallback_pdf()


def render_docx(report: Report | SummaryReport) -> str:
    from ..config import settings

    output_dir = settings.storage_dir / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = getattr(report, "report_date", None) or getattr(report, "end_date", None)
    prefix = getattr(report, "report_type", None) or getattr(report, "period_type", None) or "report"
    target = output_dir / f"{prefix}-{stamp.isoformat()}.docx"
    markdown_content = getattr(report, "markdown_content", "") or ""

    def paragraph(text: str) -> str:
        return (
            "<w:p>"
            "<w:r><w:t xml:space='preserve'>"
            f"{escape(text)}"
            "</w:t></w:r>"
            "</w:p>"
        )

    title = (getattr(report, "title", "") or "复盘报告").replace("\n", " ")
    lines = [line.strip() for line in markdown_content.splitlines() if line.strip()]
    body = [paragraph(title)]
    for line in lines:
        plain = re.sub(r"^[#>\-\*\d\.\s]+", "", line).strip()
        if not plain:
            continue
        body.append(paragraph(plain))
    if len(body) == 1:
        body.append(paragraph("暂无正文内容"))

    document_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        "<w:body>"
        + "".join(body)
        + "<w:sectPr><w:pgSz w:w='11906' w:h='16838'/><w:pgMar w:top='1440' w:right='1440' w:bottom='1440' w:left='1440'/></w:sectPr>"
        + "</w:body></w:document>"
    )
    content_types = """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>
  <Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>
  <Default Extension='xml' ContentType='application/xml'/>
  <Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>
</Types>"""
    rels = """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
  <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/>
</Relationships>"""
    doc_rels = """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>"""

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", doc_rels)
    return str(target)


def generate_report(db: Session, target: date | None = None, use_latest: bool = True) -> Report:
    settings = runtime_settings(db)
    requested = target or (date.today() - timedelta(days=1))
    if use_latest and not any(_totals(db, requested).values()):
        requested = latest_data_date(db) or requested
    stats = report_stats(db, requested)
    markdown_content = base_markdown(stats)
    analysis, error = ai_analysis(stats, settings)
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
    report.title = f"{requested} {settings.app_name} 日报"
    report.report_type = "daily"
    report.markdown_content = markdown_content
    report.html_content = html_content
    report.stats_json = json.dumps(stats, ensure_ascii=False)
    report.ai_error = error
    report.updated_at = datetime.now().replace(microsecond=0)
    db.flush()
    report.pdf_path = render_pdf(report)
    return report


def generate_summary_report(db: Session, period_type: str, target: date | None = None) -> SummaryReport:
    settings = runtime_settings(db)
    anchor = target or latest_data_date(db) or (date.today() - timedelta(days=1))
    if period_type == "monthly":
        start_date = anchor.replace(day=1)
        if anchor.month == 12:
            next_month = anchor.replace(year=anchor.year + 1, month=1, day=1)
        else:
            next_month = anchor.replace(month=anchor.month + 1, day=1)
        end_date = min(anchor, next_month - timedelta(days=1))
        title = f"{anchor.year}年{anchor.month}月复盘"
    else:
        start_date = anchor - timedelta(days=anchor.weekday())
        end_date = min(anchor, start_date + timedelta(days=6))
        title = f"{start_date} 至 {end_date} 周报"
    stats = summary_stats(db, start_date, end_date)
    markdown_content = f"# {title}\n\n" + base_markdown(stats).removeprefix(f"# {stats['date']} 自媒体每日复盘\n\n")
    analysis, error = ai_analysis(stats, settings)
    if analysis:
        markdown_content += "\n\n" + analysis
    elif error:
        markdown_content += "\n\n## AI 分析状态\n\n" + error
    html_content = md.markdown(markdown_content, extensions=["tables", "fenced_code"])
    report = db.scalar(
        select(SummaryReport).where(
            SummaryReport.period_type == period_type,
            SummaryReport.start_date == start_date,
            SummaryReport.end_date == end_date,
        )
    )
    if not report:
        report = SummaryReport(period_type=period_type, start_date=start_date, end_date=end_date)
        db.add(report)
    report.title = title
    report.status = "generated_with_warning" if error else "generated"
    report.markdown_content = markdown_content
    report.html_content = html_content
    report.stats_json = json.dumps(stats, ensure_ascii=False)
    report.ai_error = error
    report.updated_at = datetime.now().replace(microsecond=0)
    db.flush()
    report.pdf_path = render_pdf(report)
    return report
