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
                    "heat_index": {"type": "integer"},
                    "video_count": {"type": "string"},
                    "heat_reason": {"type": "string"},
                    "usable_angle": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "platforms", "heat_index", "video_count", "heat_reason", "usable_angle", "source_urls"],
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
                    "heat_index": {"type": "integer"},
                    "video_count": {"type": "string"},
                    "trend": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "platforms", "heat_index", "video_count", "trend", "why_it_matters", "source_urls"],
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
                    "cover_lines": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["priority", "direction", "hotspot", "fusion", "reason", "format", "timeliness", "titles", "cover_lines"],
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


def fallback_payload(target: date, hotspot_sources: list[str]) -> dict:
    sources = [
        "https://s.weibo.com/top/summary",
        "https://www.douyin.com/hot",
    ]
    if hotspot_sources:
        sources.extend(hotspot_sources[:3])
    realtime = [
        {
            "topic": "短视频教程拆解",
            "platforms": ["抖音", "视频号"],
            "heat_index": 92,
            "video_count": "12.4万",
            "heat_reason": "教程、清单和实操演示类内容更容易获得稳定播放。",
            "usable_angle": "把无人机足球的入门步骤拆成三段讲清楚。",
            "source_urls": sources[:2],
        },
        {
            "topic": "赛事现场感",
            "platforms": ["抖音", "小红书"],
            "heat_index": 88,
            "video_count": "8.7万",
            "heat_reason": "现场、沉浸和第一视角内容更容易带来停留。",
            "usable_angle": "用比赛现场和训练现场做强对比。",
            "source_urls": sources[:2],
        },
        {
            "topic": "挑战赛和打卡",
            "platforms": ["抖音", "视频号"],
            "heat_index": 85,
            "video_count": "6.5万",
            "heat_reason": "挑战任务天然适合复用和接力传播。",
            "usable_angle": "做一周一个挑战主题的系列内容。",
            "source_urls": sources[:2],
        },
        {
            "topic": "亲子科技体验",
            "platforms": ["小红书", "公众号"],
            "heat_index": 81,
            "video_count": "5.1万",
            "heat_reason": "家长关注成长和体验，容易形成收藏和转发。",
            "usable_angle": "强调安全、学习成果和孩子参与感。",
            "source_urls": sources[:2],
        },
        {
            "topic": "装备和场地改造",
            "platforms": ["小红书", "公众号"],
            "heat_index": 78,
            "video_count": "4.3万",
            "heat_reason": "工具、场地、设备改造类内容容易沉淀搜索流量。",
            "usable_angle": "用预算清单和前后对比展示投入产出。",
            "source_urls": sources[:2],
        },
    ]
    weekly = [
        {
            "topic": "校园科技活动",
            "platforms": ["公众号", "视频号"],
            "heat_index": 84,
            "video_count": "9.1万",
            "trend": "开学季和活动季长期有效。",
            "why_it_matters": "适合把无人机足球放进校园活动、社团和研学场景。",
            "source_urls": sources[:2],
        },
        {
            "topic": "青少年技能展示",
            "platforms": ["抖音", "小红书"],
            "heat_index": 82,
            "video_count": "7.8万",
            "trend": "展示类内容持续稳定。",
            "why_it_matters": "可自然包装成训练成果、竞赛成果和成长记录。",
            "source_urls": sources[:2],
        },
        {
            "topic": "团队协作挑战",
            "platforms": ["抖音", "视频号"],
            "heat_index": 80,
            "video_count": "6.2万",
            "trend": "团队配合、策略和协同经常有讨论热度。",
            "why_it_matters": "无人机足球天然具备团队协作和战术表达。",
            "source_urls": sources[:2],
        },
        {
            "topic": "训练前后对比",
            "platforms": ["小红书", "公众号"],
            "heat_index": 77,
            "video_count": "5.6万",
            "trend": "成长对比内容容易引发收藏。",
            "why_it_matters": "适合做技能提升、认知提升和进步轨迹。",
            "source_urls": sources[:2],
        },
        {
            "topic": "科普与安全提示",
            "platforms": ["公众号", "视频号"],
            "heat_index": 75,
            "video_count": "4.9万",
            "trend": "实用型内容更容易长期留存。",
            "why_it_matters": "适合补充规则、场地、安全和装备知识。",
            "source_urls": sources[:2],
        },
    ]
    topics = [
        {
            "priority": "S",
            "direction": "无人机足球入门课",
            "hotspot": "短视频教程拆解",
            "fusion": "把规则、装备和第一节训练拆成 3 个镜头讲清楚。",
            "reason": "最容易在一天内完成，并且适合持续做系列。",
            "format": "口播讲解 + 现场演示",
            "timeliness": "今天就能拍",
            "titles": [
                "第一次接触无人机足球，先学会这三件事",
                "无人机足球入门到底难不难，一分钟讲明白",
                "新手做无人机足球，先别急着上场",
            ],
            "cover_lines": [
                "新手先看这 3 步",
                "一条视频讲清入门",
                "先会规则再上场",
            ],
        },
        {
            "priority": "A",
            "direction": "赛事现场感记录",
            "hotspot": "赛事现场感",
            "fusion": "用比赛前中后对比讲一场完整训练或比赛。",
            "reason": "现场感强，适合积累账号辨识度。",
            "format": "第一视角 + 现场配音",
            "timeliness": "本周可拍",
            "titles": [
                "无人机足球现场到底有多燃",
                "跟着镜头看一场无人机足球比赛",
                "比赛现场和你想的不一样",
            ],
            "cover_lines": [
                "现场比想象更刺激",
                "一镜看懂比赛节奏",
                "真实赛场氛围拉满",
            ],
        },
        {
            "priority": "A",
            "direction": "亲子科技体验",
            "hotspot": "亲子科技体验",
            "fusion": "把家长关心的安全、成长和参与感说清楚。",
            "reason": "适合扩展到家长用户和研学用户。",
            "format": "家长视角 + 孩子视角",
            "timeliness": "本周可拍",
            "titles": [
                "孩子为什么适合接触无人机足球",
                "家长最关心的问题，我一次讲透",
                "科技体验课到底值不值得参加",
            ],
            "cover_lines": [
                "家长最关心这几点",
                "安全和成长都讲清楚",
                "孩子会喜欢的科技课",
            ],
        },
        {
            "priority": "B",
            "direction": "装备和场地改造",
            "hotspot": "装备和场地改造",
            "fusion": "展示训练场地、设备和预算方案。",
            "reason": "适合做搜索流量和长期收藏内容。",
            "format": "清单 + 前后对比",
            "timeliness": "可储备",
            "titles": [
                "做一个无人机足球训练场要花多少钱",
                "无人机足球场地怎么布置更实用",
                "一套基础装备能完成哪些训练",
            ],
            "cover_lines": [
                "预算和效果都给你看",
                "场地改造直接参考",
                "少走弯路的清单",
            ],
        },
        {
            "priority": "B",
            "direction": "团队协作训练",
            "hotspot": "团队协作挑战",
            "fusion": "把协同、战术和分工拍成挑战内容。",
            "reason": "适合作为系列延展，增强账号记忆点。",
            "format": "挑战赛 + 复盘",
            "timeliness": "可储备",
            "titles": [
                "无人机足球最考验团队什么能力",
                "这不是一个人能赢的项目",
                "团队配合好的时候到底有多强",
            ],
            "cover_lines": [
                "团队配合才是重点",
                "看懂战术再看比赛",
                "协作决定结果",
            ],
        },
    ]
    return {
        "summary": f"{target.isoformat()} 未配置 OpenAI，当前展示固定热点模板，仍可继续做基础选题筛选。",
        "realtime": realtime,
        "weekly": weekly,
        "topics": topics,
        "sources": sources,
    }


def normalize_payload(payload: dict, response_dump: dict | None = None) -> dict:
    def clean_text(value: str) -> str:
        return "".join(ch for ch in str(value) if ch == "\n" or ch == "\t" or ord(ch) >= 32).strip()

    clean = {
        "summary": clean_text(payload.get("summary", "")),
        "realtime": list(payload.get("realtime", []))[:8],
        "weekly": list(payload.get("weekly", []))[:10],
        "topics": list(payload.get("topics", []))[:10],
        "sources": [],
    }
    for item in clean["realtime"] + clean["weekly"]:
        item["topic"] = clean_text(item.get("topic", ""))
        item["heat_index"] = int(item.get("heat_index", 0) or 0)
        item["video_count"] = clean_text(item.get("video_count", ""))
        item["heat_reason"] = clean_text(item.get("heat_reason", ""))
        item["usable_angle"] = clean_text(item.get("usable_angle", ""))
        item["trend"] = clean_text(item.get("trend", ""))
        item["why_it_matters"] = clean_text(item.get("why_it_matters", ""))
        item["source_urls"] = _safe_urls(item.get("source_urls", []))
        clean["sources"].extend(item["source_urls"])
    for item in clean["topics"]:
        item["priority"] = item.get("priority") if item.get("priority") in {"S", "A", "B"} else "B"
        item["direction"] = clean_text(item.get("direction", ""))
        item["hotspot"] = clean_text(item.get("hotspot", ""))
        item["fusion"] = clean_text(item.get("fusion", ""))
        item["reason"] = clean_text(item.get("reason", ""))
        item["format"] = clean_text(item.get("format", ""))
        item["timeliness"] = clean_text(item.get("timeliness", ""))
        item["titles"] = [clean_text(title) for title in item.get("titles", []) if clean_text(title)][:5]
        item["cover_lines"] = [clean_text(line) for line in item.get("cover_lines", []) if clean_text(line)][:5]

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
        payload = fallback_payload(target, hotspot_sources)
        report.payload_json = json.dumps(payload, ensure_ascii=False)
        report.status = "generated_with_fallback"
        report.error_message = "未配置 OpenAI，已切换到固定热点模板。"
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
- 每个选题给出融合方式、推荐视频形式、时效窗口，以及 3 到 5 个可以直接发布使用的中文标题和 3 到 5 条可直接用于封面的中文文案。
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
        if report.payload_json in {"", "{}"}:
            report.payload_json = json.dumps(fallback_payload(target, hotspot_sources), ensure_ascii=False)
            report.status = "generated_with_fallback"
        else:
            report.status = "generated_with_warning"
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
