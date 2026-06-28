import json
from typing import Any
from urllib.parse import urlparse

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
                "video_title": {"type": "string"},
                "play_count": {"type": "string"},
                "like_count": {"type": "string"},
                "comment_count": {"type": "string"},
                "duration": {"type": "string"},
            },
            "required": ["video_url", "video_title", "play_count", "like_count", "comment_count", "duration"],
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
    lines = [
        "# 爆款视频拆解报告",
        "",
        "## 1. 视频基础信息",
        f"- 视频链接：{video['video_url']}",
        f"- 视频标题：{video['video_title']}",
        f"- 播放量：{video['play_count']}",
        f"- 点赞数：{video['like_count']}",
        f"- 评论数：{video['comment_count']}",
        f"- 视频时长：{video['duration']}",
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
- 不要输出 Markdown，后端会把 JSON 转成 Markdown。

输入数据：
{json.dumps(payload, ensure_ascii=False)}
"""


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
    analysis["video_info"] = {
        "video_url": clean_text(payload.get("source_url")),
        "video_title": clean_text(payload.get("title")) or "未填写",
        "play_count": clean_number(payload.get("views")),
        "like_count": clean_number(payload.get("likes")),
        "comment_count": clean_number(payload.get("comments")),
        "duration": clean_text(payload.get("duration")) or "数据未填写",
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
    analysis["score"]["explosive_potential"] = max(0, min(100, int(analysis["score"].get("explosive_potential") or 0)))
    analysis["score"]["reuse_value"] = max(0, min(100, int(analysis["score"].get("reuse_value") or 0)))
    analysis["score"]["difficulty"] = max(0, min(100, int(analysis["score"].get("difficulty") or 0)))
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
