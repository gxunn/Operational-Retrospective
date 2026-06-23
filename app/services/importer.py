import hashlib
import json
import math
import re
import csv
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ContentDailyMetric,
    DailyAccountMetric,
    ImportBatch,
    MappingProfile,
)


FIELDS = {
    "date": "数据日期",
    "title": "内容标题（内容明细表才需要）",
    "content_id": "内容ID（可选）",
    "followers_new": "新增粉丝",
    "views": "播放量/阅读量",
    "likes": "点赞",
    "comments": "评论",
    "favorites": "收藏",
    "shares": "转发/分享",
    "leads": "私信/线索",
}

ALIASES = {
    "date": ["日期", "数据日期", "统计日期", "首次发布时间", "发布时间", "发表时间", "发表日期", "投稿时间", "创建时间", "时间", "date", "day"],
    "title": ["内容标题", "笔记标题", "作品标题", "作品名称", "视频标题", "文章标题", "稿件标题", "视频描述", "内容", "标题", "title"],
    "content_id": ["内容id", "作品id", "笔记id", "视频id", "文章id", "稿件id", "稿件avid", "avid", "bvid", "item_id", "content_id"],
    "followers_new": ["新增粉丝", "新增关注", "净增粉丝", "粉丝增量", "涨粉数", "涨粉", "新关注人数", "new followers", "followers_new"],
    "views": ["观看量", "播放量", "阅读量", "阅读人数", "观看次数", "播放次数", "视频播放次数", "浏览量", "曝光量", "曝光", "views", "播放"],
    "likes": ["点赞", "点赞量", "点赞数", "获赞", "点赞次数", "likes"],
    "comments": ["评论", "评论量", "评论数", "评论次数", "comments"],
    "favorites": ["收藏", "收藏量", "收藏数", "收藏次数", "微信收藏人数", "favorites", "saves"],
    "shares": ["分享", "分享量", "分享数", "分享次数", "分享人数", "转发", "转发量", "转发数", "转发次数", "shares"],
    "leads": ["私信", "私信数", "私信咨询数", "线索", "线索数", "咨询", "咨询数", "leads"],
}

PLATFORM_ALIASES = {
    "小红书": {"views": ["观看量", "浏览量", "曝光量", "曝光"]},
    "抖音": {"views": ["播放量", "视频播放次数"]},
    "视频号": {"views": ["播放次数", "观看次数", "播放量"]},
    "公众号": {"views": ["阅读人数", "阅读量", "阅读次数"]},
    "B站": {"views": ["播放", "播放量", "观看量"]},
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_header(value: object) -> str:
    return re.sub(r"[\s_\-()/（）]", "", str(value)).lower()


def _header_score(values: list[object]) -> int:
    """Score a possible header row by the number of distinct import fields it names."""
    normalized = {normalize_header(value) for value in values if pd.notna(value)}
    matched = 0
    for aliases in ALIASES.values():
        if any(normalize_header(alias) in normalized for alias in aliases):
            matched += 1
    return matched


def _unique_headers(values: list[object]) -> list[str]:
    """Create stable unique column names for exports containing repeated headings."""
    result: list[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(values):
        base = str(value).strip() if pd.notna(value) and str(value).strip() else f"未命名列{index + 1}"
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base}.{counts[base] - 1}")
    return result


def _official_account_daily_table(raw: pd.DataFrame, header_position: int) -> pd.DataFrame | None:
    """Combine the side-by-side daily trend blocks used by 公众号 exports."""
    headers = [normalize_header(value) if pd.notna(value) else "" for value in raw.iloc[header_position].tolist()]
    date_indexes = [index for index, value in enumerate(headers) if value == normalize_header("日期")]
    if len(date_indexes) < 2:
        return None
    first_date, second_date = date_indexes[:2]

    def find(names: list[str], start: int, end: int) -> int | None:
        targets = {normalize_header(name) for name in names}
        return next((index for index in range(start, end) if headers[index] in targets), None)

    views_index = find(["阅读人数", "阅读量", "阅读次数"], first_date + 1, second_date)
    shares_index = find(["分享人数", "分享量", "转发人数"], second_date + 1, len(headers))
    favorites_index = find(["微信收藏人数", "收藏人数", "收藏量"], second_date + 1, len(headers))
    if views_index is None or (shares_index is None and favorites_index is None):
        return None

    body = raw.iloc[header_position + 1 :]
    views = pd.DataFrame({"日期": body.iloc[:, first_date], "阅读人数": body.iloc[:, views_index]}).dropna(subset=["日期"])
    views["阅读人数"] = views["阅读人数"].map(parse_number)
    views = views.groupby("日期", as_index=False, sort=False)["阅读人数"].sum()

    daily_data: dict[str, object] = {"日期": body.iloc[:, second_date]}
    if shares_index is not None:
        daily_data["分享人数"] = body.iloc[:, shares_index]
    if favorites_index is not None:
        daily_data["微信收藏人数"] = body.iloc[:, favorites_index]
    engagement = pd.DataFrame(daily_data).dropna(subset=["日期"])
    for column in engagement.columns[1:]:
        engagement[column] = engagement[column].map(parse_number)
    engagement = engagement.groupby("日期", as_index=False, sort=False).sum()
    return views.merge(engagement, on="日期", how="outer").fillna(0)


def _table_from_raw(raw: pd.DataFrame, platform: str = "") -> pd.DataFrame:
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        raise ValueError("表格没有可导入的数据")
    scan_count = min(20, len(raw))
    scores = [_header_score(raw.iloc[index].tolist()) for index in range(scan_count)]
    header_position = max(range(scan_count), key=lambda index: scores[index])
    # A real import header must identify a date plus at least one other useful field.
    if scores[header_position] < 2:
        header_position = 0
    if platform == "公众号":
        daily = _official_account_daily_table(raw, header_position)
        if daily is not None:
            return daily
    frame = raw.iloc[header_position + 1 :].copy()
    frame.columns = _unique_headers(raw.iloc[header_position].tolist())
    return frame.dropna(how="all").reset_index(drop=True)


def read_table(path: Path, platform: str = "") -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                with path.open("r", encoding=encoding, newline="") as source:
                    rows = list(csv.reader(source))
                width = max((len(row) for row in rows), default=0)
                raw = pd.DataFrame([row + [None] * (width - len(row)) for row in rows])
                return _table_from_raw(raw, platform)
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 编码无法识别，请另存为 UTF-8 CSV 后重试")
    if suffix in {".xlsx", ".xls"}:
        return _table_from_raw(pd.read_excel(path, header=None), platform)
    raise ValueError("仅支持 CSV、XLSX、XLS 文件")


def suggest_mapping(columns: list[str], saved: dict | None = None, platform: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    saved = saved or {}
    for field, saved_column in saved.items():
        if saved_column in columns:
            result[field] = saved_column
    normalized = {column: normalize_header(column) for column in columns}
    platform_rules = PLATFORM_ALIASES.get(platform, {})
    for field, aliases in ALIASES.items():
        if field in result:
            continue
        preferred_aliases = platform_rules.get(field, []) + aliases
        normalized_aliases = list(dict.fromkeys(normalize_header(alias) for alias in preferred_aliases))
        # Alias order expresses preference (for example 小红书观看量 before 曝光量).
        exact = next((c for alias in normalized_aliases for c, value in normalized.items() if value == alias), None)
        fuzzy = next(
            (
                c
                for alias in normalized_aliases
                for c, value in normalized.items()
                if len(alias) >= 2 and (alias in value or value in alias) and not any(word in value for word in ("率", "占比", "人均", "平均"))
            ),
            None,
        )
        if exact or fuzzy:
            result[field] = exact or fuzzy
    return result


def preview(path: Path, platform: str, db: Session) -> tuple[list[str], list[dict], dict[str, str]]:
    frame = read_table(path, platform)
    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.dropna(how="all")
    if frame.empty:
        raise ValueError("表格没有可导入的数据")
    profile = db.scalar(select(MappingProfile).where(MappingProfile.platform == platform))
    saved = json.loads(profile.mapping_json) if profile else {}
    mapping = suggest_mapping(list(frame.columns), saved, platform)
    rows = frame.head(8).fillna("").astype(str).to_dict(orient="records")
    return list(frame.columns), rows, mapping


def parse_number(value: object) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "").replace("+", "")
    if not text or text.lower() in {"nan", "none", "-", "--"}:
        return 0.0
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    elif text.endswith("%"):
        multiplier, text = 0.01, text[:-1]
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) * multiplier if match else 0.0


def parse_date(value: object) -> date:
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    if isinstance(value, (int, float)) and not pd.isna(value):
        number = int(value)
        if 19_000_101 <= number <= 21_001_231:
            parsed = pd.to_datetime(str(number), format="%Y%m%d", errors="coerce")
        elif 20_000 <= float(value) <= 80_000:
            parsed = pd.to_datetime(float(value), unit="D", origin="1899-12-30", errors="coerce")
        else:
            parsed = pd.NaT
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{8}", text):
            parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        else:
            text = re.sub(r"年|月", "-", text)
            text = re.sub(r"日", " ", text)
            text = re.sub(r"时|分", ":", text)
            text = re.sub(r"秒", "", text)
            parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"无法识别日期：{value}")
    return parsed.date()


def import_batch(db: Session, batch: ImportBatch, mapping: dict[str, str]) -> int:
    if not mapping.get("date"):
        raise ValueError("必须选择“数据日期”字段")
    if not any(mapping.get(field) for field in ("views", "likes", "comments", "favorites", "shares", "followers_new", "leads")):
        raise ValueError("至少映射一个数据指标")

    duplicated = db.scalar(
        select(ImportBatch).where(
            ImportBatch.file_hash == batch.file_hash,
            ImportBatch.status == "imported",
            ImportBatch.id != batch.id,
        )
    )
    if duplicated:
        raise ValueError(f"这个文件已经在导入记录 #{duplicated.id} 中成功导入")

    frame = read_table(Path(batch.stored_path), batch.account.platform).dropna(how="all").drop_duplicates()
    metrics = ["followers_new", "views", "likes", "comments", "favorites", "shares", "leads"]
    imported = 0
    dates: list[date] = []
    has_content = bool(mapping.get("title"))
    content_cache: dict[tuple[date, str], ContentDailyMetric] = {}
    daily_cache: dict[date, DailyAccountMetric] = {}

    for _, row in frame.iterrows():
        metric_date = parse_date(row[mapping["date"]])
        dates.append(metric_date)
        values = {field: parse_number(row[mapping[field]]) if mapping.get(field) else 0.0 for field in metrics}

        if has_content:
            title = str(row[mapping["title"]]).strip()
            if not title or title.lower() == "nan":
                continue
            raw_key = str(row[mapping["content_id"]]).strip() if mapping.get("content_id") else title
            content_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:64]
            cache_key = (metric_date, content_key)
            content = content_cache.get(cache_key)
            if not content:
                content = db.scalar(
                    select(ContentDailyMetric).where(
                        ContentDailyMetric.account_id == batch.account_id,
                        ContentDailyMetric.metric_date == metric_date,
                        ContentDailyMetric.content_key == content_key,
                    )
                )
            if not content:
                content = ContentDailyMetric(
                    account_id=batch.account_id,
                    metric_date=metric_date,
                    content_key=content_key,
                    title=title[:500],
                )
                db.add(content)
            content_cache[cache_key] = content
            content.title = title[:500]
            content.source_batch_id = batch.id
            for field in metrics[1:]:
                setattr(content, field, values[field])
        else:
            daily = daily_cache.get(metric_date)
            if not daily:
                daily = db.scalar(
                    select(DailyAccountMetric).where(
                        DailyAccountMetric.account_id == batch.account_id,
                        DailyAccountMetric.metric_date == metric_date,
                    )
                )
            if not daily:
                daily = DailyAccountMetric(account_id=batch.account_id, metric_date=metric_date)
                db.add(daily)
            daily_cache[metric_date] = daily
            daily.source_batch_id = batch.id
            for field in metrics:
                setattr(daily, field, values[field])
        imported += 1

    if has_content:
        db.flush()
        for metric_date in sorted(set(dates)):
            rows = db.scalars(
                select(ContentDailyMetric).where(
                    ContentDailyMetric.account_id == batch.account_id,
                    ContentDailyMetric.metric_date == metric_date,
                )
            ).all()
            daily = db.scalar(
                select(DailyAccountMetric).where(
                    DailyAccountMetric.account_id == batch.account_id,
                    DailyAccountMetric.metric_date == metric_date,
                )
            ) or DailyAccountMetric(account_id=batch.account_id, metric_date=metric_date)
            if daily.id is None:
                db.add(daily)
            daily.source_batch_id = batch.id
            for field in metrics[1:]:
                setattr(daily, field, sum(getattr(item, field) for item in rows))

    profile = db.scalar(select(MappingProfile).where(MappingProfile.platform == batch.account.platform))
    if not profile:
        profile = MappingProfile(platform=batch.account.platform, mapping_json="{}")
        db.add(profile)
    profile.mapping_json = json.dumps(mapping, ensure_ascii=False)
    batch.status = "imported"
    batch.mapping_json = profile.mapping_json
    batch.row_count = imported
    batch.start_date = min(dates) if dates else None
    batch.end_date = max(dates) if dates else None
    batch.imported_at = datetime.now().replace(microsecond=0)
    return imported
