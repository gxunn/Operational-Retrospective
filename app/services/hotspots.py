import json
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HotspotReport
from .runtime import get_json_setting, runtime_settings


EMPTY_PAYLOAD = {"summary": "", "realtime": [], "weekly": [], "topics": [], "sources": []}

HOTSPOT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "realtime": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "heat_reason": {"type": "string"},
                    "usable_angle": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "platforms", "heat_reason", "usable_angle", "source_urls"],
                "additionalProperties": False,
            },
        },
        "weekly": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "trend": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "platforms", "trend", "why_it_matters", "source_urls"],
                "additionalProperties": False,
            },
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "string", "enum": ["S", "A", "B"]},
                    "direction": {"type": "string"},
                    "hotspot": {"type": "string"},
                    "fusion": {"type": "string"},
                    "reason": {"type": "string"},
                    "format": {"type": "string"},
                    "timeliness": {"type": "string"},
                    "titles": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["priority", "direction", "hotspot", "fusion", "reason", "format", "timeliness", "titles"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "realtime", "weekly", "topics"],
    "additionalProperties": False,
}


def _safe_urls(values: list) -> list[str]:
    result = []
    for value in values or []:
        url = str(value).strip()
        if urlparse(url).scheme in {"http", "https"} and url not in result:
            result.append(url)
    return result[:20]


def normalize_payload(payload: dict, response_dump: dict | None = None) -> dict:
    clean = {
        "summary": str(payload.get("summary", "")).strip(),
        "realtime": list(payload.get("realtime", []))[:8],
        "weekly": list(payload.get("weekly", []))[:10],
        "topics": list(payload.get("topics", []))[:10],
        "sources": [],
    }
    for item in clean["realtime"] + clean["weekly"]:
        item["source_urls"] = _safe_urls(item.get("source_urls", []))
        clean["sources"].extend(item["source_urls"])
    for item in clean["topics"]:
        item["priority"] = item.get("priority") if item.get("priority") in {"S", "A", "B"} else "B"
        item["titles"] = [str(title).strip() for title in item.get("titles", []) if str(title).strip()][:5]

    def collect(value):
        if isinstance(value, dict):
            if "url" in value:
                clean["sources"].extend(_safe_urls([value["url"]]))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(response_dump or {})
    clean["sources"] = list(dict.fromkeys(clean["sources"]))[:20]
    return clean


def friendly_openai_error(exc: Exception) -> str:
    code = getattr(exc, "code", "") or ""
    message = str(exc).lower()
    if code == "insufficient_quota" or "insufficient_quota" in message:
        return "OpenAI API 额度不足，请充值或检查项目消费上限。"
    if "invalid_api_key" in message or type(exc).__name__ == "AuthenticationError":
        return "OpenAI API 密钥无效，请检查服务器配置。"
    if type(exc).__name__ == "RateLimitError":
        return "热点服务请求过快，请稍后再试。"
    return f"热点检索暂时不可用（{type(exc).__name__}），已保留上一次结果。"


def generate_hotspots(db: Session, target: date | None = None) -> HotspotReport:
    settings = runtime_settings(db)
    business_keywords = get_json_setting(
        db,
        "business_keywords",
        ["无人机足球", "青少年科技体育", "科技特长生", "赛事培训", "安全科普"],
    )
    hotspot_sources = get_json_setting(db, "hotspot_sources", ["抖音", "小红书", "微信视频号"])
    target = target or datetime.now(ZoneInfo(settings.app_timezone)).date()
    report = db.scalar(select(HotspotReport).where(HotspotReport.report_date == target))
    if not report:
        report = HotspotReport(report_date=target)
        db.add(report)

    if not settings.openai_api_key:
        report.status = "failed"
        report.error_message = "未配置 OPENAI_API_KEY，暂时无法检索热点。"
        db.flush()
        return report

    prompt = f"""你是中国短视频热点研究员和青少年科技体育内容策划。
当前日期是 {target.isoformat()}，时区为中国标准时间。请使用网页搜索，研究这些平台或来源：{", ".join(hotspot_sources)}：
1. 最近 24 小时仍有时效性的“大众短视频平台热点”，最多 6 条；
2. 最近 7 天持续发酵、适合内容创作的“大众周热点”，最多 8 条；
3. 再从上述大众热点中初筛最多 8 个能与“{", ".join(business_keywords)}”自然融合的选题。

要求：
- realtime 和 weekly 是大众热点概览，不是无人机行业新闻列表；应覆盖社会文化、体育、教育、科技、生活方式等多个类别。除非无人机事件已成为大众平台热点，否则不要放进前两部分。
- 热点必须基于搜索到的近期信息，不得把陈旧事件写成实时热点；无法确认的平台不要编造热度，也不要仅凭一篇新闻声称它已在某平台爆火。
- topics 才负责把大众热点与无人机足球自然连接；关联不了就放弃，不要硬蹭。
- 选题按 S/A/B 优先级排序：S 为 24 小时内应发布，A 为本周可做，B 为可储备。
- 每个选题给出融合方式、推荐视频形式、时效窗口，以及 3 个可以直接发布使用的中文标题。
- 标题自然、具体、有短视频感，避免夸大、虚假承诺和生硬蹭热点。
- source_urls 填支持该热点的网页地址；不要包含用户隐私信息。
"""
    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=90)
        response = client.responses.create(
            model=settings.openai_model,
            tools=[{"type": "web_search_preview", "search_context_size": "medium"}],
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "daily_hotspots",
                    "strict": True,
                    "schema": HOTSPOT_SCHEMA,
                }
            },
        )
        payload = normalize_payload(json.loads(response.output_text), response.model_dump())
        report.payload_json = json.dumps(payload, ensure_ascii=False)
        report.status = "generated"
        report.error_message = ""
    except Exception as exc:
        report.status = "generated_with_warning" if report.payload_json not in {"", "{}"} else "failed"
        report.error_message = friendly_openai_error(exc)
    report.updated_at = datetime.now().replace(microsecond=0)
    db.flush()
    return report


def report_payload(report: HotspotReport | None) -> dict:
    if not report:
        return dict(EMPTY_PAYLOAD)
    try:
        return normalize_payload(json.loads(report.payload_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(EMPTY_PAYLOAD)
