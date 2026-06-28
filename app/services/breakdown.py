import html
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import OpenAI

BREAKDOWN_STATUS_LABELS = {
    "未开始": "未开始",
    "分析中": "分析中",
    "已完成": "已完成",
    "失败": "失败",
}

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "object",
            "properties": {
                "explosive_potential": {"type": "integer"},
                "reuse_value": {"type": "integer"},
                "difficulty": {"type": "integer"},
                "overall_suggestion": {"type": "string"},
            },
            "required": ["explosive_potential", "reuse_value", "difficulty", "overall_suggestion"],
            "additionalProperties": False,
        },
        "video_info": {
            "type": "object",
            "properties": {
                "video_url": {"type": "string"},
                "platform": {"type": "string"},
                "video_title": {"type": "string"},
                "author_name": {"type": "string"},
                "publish_time": {"type": "string"},
                "duration": {"type": "string"},
                "play_count": {"type": "string"},
                "like_count": {"type": "string"},
                "comment_count": {"type": "string"},
                "collect_count": {"type": "string"},
                "share_count": {"type": "string"},
                "cover_info": {"type": "string"},
                "video_text": {"type": "string"},
                "transcript": {"type": "string"},
            },
            "required": [
                "video_url",
                "platform",
                "video_title",
                "author_name",
                "publish_time",
                "duration",
                "play_count",
                "like_count",
                "comment_count",
                "collect_count",
                "share_count",
                "cover_info",
                "video_text",
                "transcript",
            ],
            "additionalProperties": False,
        },
        "core_judgment": {
            "type": "object",
            "properties": {
                "why": {"type": "string"},
                "core_attraction": {"type": "string"},
                "retention_reason": {"type": "string"},
                "interaction_reason": {"type": "string"},
            },
            "required": ["why", "core_attraction", "retention_reason", "interaction_reason"],
            "additionalProperties": False,
        },
        "title_analysis": {
            "type": "object",
            "properties": {
                "structure": {"type": "string"},
                "keywords": {"type": "string"},
                "emotional_hook": {"type": "string"},
                "templates": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["structure", "keywords", "emotional_hook", "templates"],
            "additionalProperties": False,
        },
        "opening_analysis": {
            "type": "object",
            "properties": {
                "hook": {"type": "string"},
                "visual_impact": {"type": "string"},
                "information_density": {"type": "string"},
                "reuse_for_drone_football": {"type": "string"},
            },
            "required": ["hook", "visual_impact", "information_density", "reuse_for_drone_football"],
            "additionalProperties": False,
        },
        "content_structure": {
            "type": "object",
            "properties": {
                "opening": {"type": "string"},
                "conflict": {"type": "string"},
                "process": {"type": "string"},
                "result_feedback": {"type": "string"},
                "cta": {"type": "string"},
            },
            "required": ["opening", "conflict", "process", "result_feedback", "cta"],
            "additionalProperties": False,
        },
        "camera_rhythm": {
            "type": "object",
            "properties": {
                "rhythm": {"type": "string"},
                "focus": {"type": "string"},
                "transition": {"type": "string"},
                "music": {"type": "string"},
            },
            "required": ["rhythm", "focus", "transition", "music"],
            "additionalProperties": False,
        },
        "interaction": {
            "type": "object",
            "properties": {
                "likely_comments": {"type": "array", "items": {"type": "string"}},
                "discussion_topics": {"type": "array", "items": {"type": "string"}},
                "pinned_comment": {"type": "string"},
            },
            "required": ["likely_comments", "discussion_topics", "pinned_comment"],
            "additionalProperties": False,
        },
        "reuse_plan": {
            "type": "object",
            "properties": {
                "adaptation_direction": {"type": "string"},
                "script": {"type": "string"},
                "recommended_titles": {"type": "array", "items": {"type": "string"}},
                "recommended_cover_lines": {"type": "array", "items": {"type": "string"}},
                "recommended_publish_time": {"type": "string"},
            },
            "required": [
                "adaptation_direction",
                "script",
                "recommended_titles",
                "recommended_cover_lines",
                "recommended_publish_time",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "score",
        "video_info",
        "core_judgment",
        "title_analysis",
        "opening_analysis",
        "content_structure",
        "camera_rhythm",
        "interaction",
        "reuse_plan",
    ],
    "additionalProperties": False,
}

_META_ATTR_RE = re.compile(r"([a-zA-Z0-9_:.-]+)\s*=\s*['\"]([^'\"]*)['\"]", re.I)
_JSON_LD_RE = re.compile(r"<script[^>]+type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>", re.I | re.S)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SCRIPT_KEY_RE = re.compile(r'["\']?(playCount|likeCount|commentCount|collectCount|favoriteCount|shareCount|repostCount|viewCount)["\']?\s*[:=]\s*["\']?([\d,\.]+)(?:[万亿])?["\']?', re.I)
_DATE_RE = re.compile(r'(?:20\d{2}[/-]\d{1,2}[/-]\d{1,2}(?:[ T]\d{1,2}:\d{1,2}(?::\d{1,2})?)?|20\d{2}年\d{1,2}月\d{1,2}日(?:\d{1,2}[:时]\d{1,2}(?:[:分]\d{1,2})?)?)')


def normalize_source_url(value: str) -> str:
    text = clean_text(value)
    if text and "://" not in text and "." in text:
        text = f"https://{text}"
    return text


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32).strip()


def clean_number(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "数据未填写"
    if number <= 0:
        return "数据未填写"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def clean_list(values: Any, limit: int = 5) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = clean_text(value)
        if text:
            result.append(text)
    return result[:limit]


def friendly_openai_error(exc: Exception) -> str:
    code = getattr(exc, "code", "") or ""
    status_code = getattr(exc, "status_code", None)
    message = str(exc).strip()
    lowered = message.lower()
    if code == "insufficient_quota" or "insufficient_quota" in lowered:
        return "OpenAI 额度不足，请检查余额或项目配额。"
    if status_code == 401 or "invalid_api_key" in lowered or type(exc).__name__ == "AuthenticationError":
        return "OpenAI API Key 无效，请检查 OPENAI_API_KEY。"
    if status_code == 429 or type(exc).__name__ == "RateLimitError":
        return "OpenAI 请求过于频繁，请稍后重试。"
    if status_code == 404 or "model_not_found" in lowered or "not found" in lowered:
        return "OpenAI 模型不可用，请检查 OPENAI_MODEL。"
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"} or "timed out" in lowered or "timeout" in lowered:
        return "OpenAI 网络连接超时，请稍后重试。"
    if status_code in {400, 403}:
        return f"OpenAI 请求被拒绝：{message}"
    return f"OpenAI API 调用失败（{type(exc).__name__}）：{message}"


def detect_platform_from_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if any(token in host for token in ("douyin.com", "iesdouyin.com", "snssdk.com", "tiktok.com")):
        return "抖音"
    if any(token in host for token in ("xiaohongshu.com", "xhslink.com", "xhscdn.com")):
        return "小红书"
    if any(token in host for token in ("channels.weixin.qq.com", "weixin.qq.com", "wechat.com")):
        return "视频号"
    if any(token in host for token in ("mp.weixin.qq.com", "weixin.qq.com")):
        return "公众号"
    return "其他"


def _normalize_duration(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.isdigit():
        seconds = int(text)
        if seconds >= 60:
            minutes, rest = divmod(seconds, 60)
            return f"{minutes}分{rest}秒" if rest else f"{minutes}分"
        return f"{seconds}秒"
    return text


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def _extract_meta_tags(html_text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for match in _META_RE.finditer(html_text):
        attrs = {key.lower(): html.unescape(value) for key, value in _META_ATTR_RE.findall(match.group(0))}
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content") or attrs.get("value")
        if key and content and key.lower() not in data:
            data[key.lower()] = clean_text(content)
    return data


def _extract_json_ld_objects(html_text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for match in _JSON_LD_RE.finditer(html_text):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(html.unescape(raw))
        except Exception:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
        elif isinstance(payload, list):
            objects.extend(item for item in payload if isinstance(item, dict))
    return objects


def _extract_script_counts(html_text: str) -> dict[str, str]:
    counts = {
        "play_count": "数据未填写",
        "like_count": "数据未填写",
        "comment_count": "数据未填写",
        "collect_count": "数据未填写",
        "share_count": "数据未填写",
    }
    for key, value in _SCRIPT_KEY_RE.findall(html_text):
        normalized = clean_number(value)
        if key.lower() in {"playcount", "viewcount"} and counts["play_count"] == "数据未填写":
            counts["play_count"] = normalized
        elif key.lower() == "likecount" and counts["like_count"] == "数据未填写":
            counts["like_count"] = normalized
        elif key.lower() == "commentcount" and counts["comment_count"] == "数据未填写":
            counts["comment_count"] = normalized
        elif key.lower() in {"collectcount", "favoritecount"} and counts["collect_count"] == "数据未填写":
            counts["collect_count"] = normalized
        elif key.lower() in {"sharecount", "repostcount"} and counts["share_count"] == "数据未填写":
            counts["share_count"] = normalized
    return counts


def _read_ld_value(objects: list[dict[str, Any]], *keys: str) -> str:
    for obj in objects:
        values = [obj]
        while values:
            current = values.pop(0)
            if not isinstance(current, dict):
                continue
            for key in keys:
                if key in current and current[key]:
                    value = current[key]
                    if isinstance(value, dict):
                        name = value.get("name") or value.get("text") or value.get("title")
                        if name:
                            return clean_text(name)
                    elif isinstance(value, list):
                        item = next((clean_text(item) for item in value if clean_text(item)), "")
                        if item:
                            return item
                    else:
                        return clean_text(value)
            values.extend(current.values())
    return ""


def _parse_publish_time(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text


def fetch_video_info(url: str) -> dict[str, Any]:
    source_url = normalize_source_url(url)
    if not source_url:
        return {"ok": False, "error": "视频链接格式不正确，请填写常见平台的 http 或 https 链接。"}
    platform = detect_platform_from_url(source_url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers=headers) as client:
            response = client.get(source_url)
    except httpx.TimeoutException:
        return {"ok": False, "platform": platform, "error": "抓取超时，该平台可能限制自动抓取，请手动补充视频信息后继续拆解。"}
    except httpx.HTTPError as exc:
        return {"ok": False, "platform": platform, "error": f"抓取失败：{clean_text(str(exc)) or '网络错误'}，请手动补充视频信息后继续拆解。"}

    if response.status_code >= 400:
        return {
            "ok": False,
            "platform": platform,
            "error": f"抓取失败，平台返回 {response.status_code}。该平台可能限制自动抓取，请手动补充视频信息后继续拆解。",
        }

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type and "json" not in content_type:
        return {
            "ok": False,
            "platform": platform,
            "error": "抓取失败，返回内容不是网页页面。该平台可能限制自动抓取，请手动补充视频信息后继续拆解。",
        }

    body = response.text or ""
    if len(body.strip()) < 120:
        return {
            "ok": False,
            "platform": platform,
            "error": "抓取失败，页面内容过少。该平台可能限制自动抓取，请手动补充视频信息后继续拆解。",
        }

    meta = _extract_meta_tags(body)
    ld_objects = _extract_json_ld_objects(body)
    counts = _extract_script_counts(body)
    title = _first_nonempty(
        meta.get("og:title"),
        meta.get("twitter:title"),
        _read_ld_value(ld_objects, "name", "headline", "title"),
        re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S).group(1) if _TITLE_RE.search(body) else "",
    )
    author_name = _first_nonempty(
        meta.get("article:author"),
        meta.get("author"),
        _read_ld_value(ld_objects, "author"),
    )
    publish_time = _first_nonempty(
        _parse_publish_time(meta.get("article:published_time", "")),
        _parse_publish_time(meta.get("article:modified_time", "")),
        _parse_publish_time(_read_ld_value(ld_objects, "uploadDate", "datePublished")),
        _DATE_RE.search(body).group(0) if _DATE_RE.search(body) else "",
    )
    duration = _normalize_duration(
        _first_nonempty(meta.get("video:duration"), _read_ld_value(ld_objects, "duration"))
    )
    cover_url = _first_nonempty(
        meta.get("og:image"),
        meta.get("twitter:image"),
        _read_ld_value(ld_objects, "thumbnailUrl", "thumbnail")
    )
    description = _first_nonempty(
        meta.get("og:description"),
        meta.get("description"),
        _read_ld_value(ld_objects, "description"),
    )
    video_text = description or clean_text(re.sub(r"\s+", " ", body))[:1000]
    transcript = _first_nonempty(meta.get("keywords"), _read_ld_value(ld_objects, "transcript"))
    if transcript and len(transcript) > 1200:
        transcript = transcript[:1200]
    if not title:
        title = "未填写"
    return {
        "ok": True,
        "platform": platform,
        "fetch_status": "抓取成功",
        "data": {
            "platform": platform,
            "source_url": source_url,
            "title": title,
            "cover_url": cover_url,
            "author_name": author_name,
            "publish_time": publish_time,
            "duration": duration,
            "views": counts["play_count"],
            "likes": counts["like_count"],
            "comments": counts["comment_count"],
            "collect_count": counts["collect_count"],
            "share_count": counts["share_count"],
            "video_text": video_text,
            "transcript": transcript,
            "cover_description": description,
            "fetch_status": "抓取成功",
            "fetch_error": "",
        },
    }


def _script_placeholder(payload: dict[str, Any]) -> str:
    script = clean_text(payload.get("script_content"))
    transcript = clean_text(payload.get("transcript"))
    video_text = clean_text(payload.get("video_text"))
    return script or transcript or video_text


def build_breakdown_markdown(payload: dict[str, Any], analysis: dict[str, Any]) -> str:
    video = analysis["video_info"]
    score = analysis["score"]
    core = analysis["core_judgment"]
    title = analysis["title_analysis"]
    opening = analysis["opening_analysis"]
    content = analysis["content_structure"]
    rhythm = analysis["camera_rhythm"]
    interaction = analysis["interaction"]
    reuse = analysis["reuse_plan"]
    script_value = _script_placeholder(payload)
    script_line = script_value if script_value else "数据未填写，缺少完整脚本，分析结果仅供参考。"
    lines = [
        "# 爆款视频拆解报告",
        "",
        "## 1. 视频基础信息",
        f"- 视频链接：{video['video_url']}",
        f"- 视频标题：{video['video_title']}",
        f"- 作者昵称：{video['author_name']}",
        f"- 发布时间：{video['publish_time']}",
        f"- 视频时长：{video['duration']}",
        f"- 点赞数：{video['like_count']}",
        f"- 评论数：{video['comment_count']}",
        f"- 收藏数：{video['collect_count']}",
        f"- 转发数：{video['share_count']}",
        f"- 封面信息：{video['cover_info']}",
        f"- 视频文案/脚本：{script_line}",
        "",
        "## 2. 爆款核心判断",
        f"- 这个视频为什么可能成为爆款：{core['why']}",
        f"- 核心吸引点：{core['core_attraction']}",
        f"- 用户停留理由：{core['retention_reason']}",
        f"- 用户互动理由：{core['interaction_reason']}",
        "",
        "## 3. 标题拆解",
        f"- 标题结构：{title['structure']}",
        f"- 关键词：{title['keywords']}",
        f"- 情绪钩子：{title['emotional_hook']}",
        f"- 可复用标题模板：{'; '.join(title['templates'])}",
        "",
        "## 4. 开头3秒分析",
        f"- 开头钩子：{opening['hook']}",
        f"- 视觉冲击点：{opening['visual_impact']}",
        f"- 信息密度：{opening['information_density']}",
        f"- 是否适合无人机足球账号复用：{opening['reuse_for_drone_football']}",
        "",
        "## 5. 内容结构拆解",
        f"- 开场：{content['opening']}",
        f"- 冲突/痛点：{content['conflict']}",
        f"- 展示过程：{content['process']}",
        f"- 结果反馈：{content['result_feedback']}",
        f"- 行动引导：{content['cta']}",
        "",
        "## 6. 镜头与节奏分析",
        f"- 镜头节奏：{rhythm['rhythm']}",
        f"- 画面重点：{rhythm['focus']}",
        f"- 转场方式：{rhythm['transition']}",
        f"- 音乐/音效建议：{rhythm['music']}",
        "",
        "## 7. 评论区与互动点",
        f"- 用户可能评论什么：{'; '.join(interaction['likely_comments'])}",
        f"- 可引导互动的话题：{'; '.join(interaction['discussion_topics'])}",
        f"- 可置顶评论建议：{interaction['pinned_comment']}",
        "",
        "## 8. 无人机足球账号可复用方案",
        f"- 适合改编的方向：{reuse['adaptation_direction']}",
        f"- 可拍摄脚本：{reuse['script']}",
        f"- 推荐标题：{'; '.join(reuse['recommended_titles'])}",
        f"- 推荐封面文案：{'; '.join(reuse['recommended_cover_lines'])}",
        f"- 推荐发布时间：{reuse['recommended_publish_time']}",
        "",
        "## 9. 最终评分",
        f"- 爆款潜力评分：{score['explosive_potential']}/100",
        f"- 复用价值评分：{score['reuse_value']}/100",
        f"- 执行难度评分：{score['difficulty']}/100",
        f"- 综合建议：{score['overall_suggestion']}",
    ]
    return "\n".join(lines).strip()


def build_breakdown_prompt(payload: dict[str, Any]) -> str:
    return f"""你是资深中文短视频拆解分析师，同时懂无人机足球账号的内容复用。
请根据输入内容，输出严格符合 JSON 结构的数据，不要输出多余说明。
要求：
- 只能依据输入信息分析，不要编造不存在的播放量、点赞、评论。
- 如果某项数据缺失或为 0，请在对应分析中明确写“数据未填写”或说明无法判断。
- 语言简洁、具体、可执行。
- 所有数组字段请输出 3 到 5 条中文短句。
- 如果缺少完整脚本，请在 analysis 中体现“缺少完整脚本，分析结果仅供参考”。
- 不要输出 Markdown，后端会把 JSON 转成 Markdown。

输入数据：
{json.dumps(payload, ensure_ascii=False)}
"""


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def generate_breakdown_analysis(settings, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not settings.openai_api_key:
        raise RuntimeError("未配置 OpenAI API Key，请在环境变量中配置 OPENAI_API_KEY。")
    client = OpenAI(api_key=settings.openai_api_key, timeout=90)
    response = client.responses.create(
        model=settings.openai_model,
        input=build_breakdown_prompt(payload),
        text={
            "format": {
                "type": "json_schema",
                "name": "breakdown_report",
                "strict": True,
                "schema": BREAKDOWN_SCHEMA,
            }
        },
    )
    raw_text = (response.output_text or "").strip()
    if not raw_text:
        raise RuntimeError("OpenAI 未返回有效内容，未生成拆解结果。")
    try:
        analysis = json.loads(raw_text)
    except Exception as exc:
        raise RuntimeError("OpenAI 返回格式异常，未生成拆解结果。") from exc
    if not isinstance(analysis, dict):
        raise RuntimeError("OpenAI 返回格式异常，未生成拆解结果。")

    video_info = analysis.get("video_info") if isinstance(analysis.get("video_info"), dict) else {}
    raw_cover_url = clean_text(payload.get("cover_url"))
    raw_cover_description = clean_text(payload.get("cover_description"))
    raw_script = _script_placeholder(payload)
    analysis["video_info"] = {
        "video_url": clean_text(payload.get("source_url")),
        "platform": clean_text(payload.get("platform")) or detect_platform_from_url(clean_text(payload.get("source_url"))),
        "video_title": clean_text(payload.get("title")) or clean_text(video_info.get("video_title")) or "未填写",
        "author_name": clean_text(payload.get("author_name")) or clean_text(video_info.get("author_name")) or "数据未填写",
        "publish_time": clean_text(payload.get("publish_time")) or clean_text(video_info.get("publish_time")) or "数据未填写",
        "duration": clean_text(payload.get("duration")) or clean_text(video_info.get("duration")) or "数据未填写",
        "play_count": clean_number(payload.get("views")) if payload.get("views") not in {None, ""} else clean_text(video_info.get("play_count")) or "数据未填写",
        "like_count": clean_number(payload.get("likes")) if payload.get("likes") not in {None, ""} else clean_text(video_info.get("like_count")) or "数据未填写",
        "comment_count": clean_number(payload.get("comments")) if payload.get("comments") not in {None, ""} else clean_text(video_info.get("comment_count")) or "数据未填写",
        "collect_count": clean_number(payload.get("collect_count")) if payload.get("collect_count") not in {None, ""} else clean_text(video_info.get("collect_count")) or "数据未填写",
        "share_count": clean_number(payload.get("share_count")) if payload.get("share_count") not in {None, ""} else clean_text(video_info.get("share_count")) or "数据未填写",
        "cover_info": "；".join(part for part in [
            raw_cover_url and f"封面图 {raw_cover_url}",
            raw_cover_description and f"封面描述 {raw_cover_description}",
            clean_text(video_info.get("cover_info")),
        ] if part) or "数据未填写",
        "video_text": clean_text(payload.get("video_text")) or clean_text(video_info.get("video_text")) or "数据未填写",
        "transcript": clean_text(payload.get("transcript")) or clean_text(video_info.get("transcript")) or raw_script or "数据未填写",
    }
    for key, value in list(analysis.items()):
        if isinstance(value, dict):
            for sub_key, sub_value in list(value.items()):
                if isinstance(sub_value, list):
                    value[sub_key] = clean_list(sub_value)
                elif isinstance(sub_value, str):
                    value[sub_key] = clean_text(sub_value) or "数据未填写"
        elif isinstance(value, list):
            analysis[key] = clean_list(value)
    analysis["score"]["explosive_potential"] = max(0, min(100, _safe_int(analysis["score"].get("explosive_potential"))))
    analysis["score"]["reuse_value"] = max(0, min(100, _safe_int(analysis["score"].get("reuse_value"))))
    analysis["score"]["difficulty"] = max(0, min(100, _safe_int(analysis["score"].get("difficulty"))))
    markdown = build_breakdown_markdown(payload, analysis)
    if not markdown.strip():
        raise RuntimeError("未生成拆解结果，请重试。")
    return analysis, markdown


def normalize_breakdown_url(value: str) -> str:
    text = clean_text(value)
    if text and "://" not in text and "." in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text

