from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ContentDailyMetric, DailyAccountMetric, ImportBatch, PlatformAccount, User
from app.services.importer import file_sha256, import_batch, parse_date, parse_number, read_table, suggest_mapping
from app.services.metrics import comparison_groups, summarize_metrics
from app.services.hotspots import normalize_payload
from app.security import hash_password, verify_password
from app.services.reporting import report_stats


def test_parse_chinese_numbers():
    assert parse_number("1.2万") == 12000
    assert parse_number("2亿") == 200000000
    assert parse_number("1,234") == 1234
    assert parse_number("12.5%") == 0.125
    assert parse_number("--") == 0


def test_header_mapping():
    mapping = suggest_mapping(["统计日期", "作品名称", "播放量", "点赞量", "私信数"])
    assert mapping["date"] == "统计日期"
    assert mapping["title"] == "作品名称"
    assert mapping["views"] == "播放量"
    assert mapping["leads"] == "私信数"


def test_xiaohongshu_banner_row_and_chinese_date(tmp_path: Path):
    path = tmp_path / "xiaohongshu.xlsx"
    pd.DataFrame(
        [
            ["最多导出排序后前1000条笔记"] * 9,
            ["笔记标题", "首次发布时间", "曝光", "观看量", "点赞", "评论", "收藏", "涨粉", "分享"],
            ["测试笔记", "2026年06月22日18时01分32秒", 117, 22, 6, 1, 4, 2, 3],
        ]
    ).to_excel(path, index=False, header=False)
    frame = read_table(path, "小红书")
    mapping = suggest_mapping(list(frame.columns), platform="小红书")
    assert mapping == {
        "date": "首次发布时间",
        "title": "笔记标题",
        "followers_new": "涨粉",
        "views": "观看量",
        "likes": "点赞",
        "comments": "评论",
        "favorites": "收藏",
        "shares": "分享",
    }
    assert parse_date(frame.iloc[0][mapping["date"]]) == date(2026, 6, 22)


def test_platform_specific_common_headers():
    cases = {
        "视频号": (["创建时间", "视频描述", "播放次数", "点赞次数", "评论次数", "转发次数", "新增关注"], "播放次数"),
        "B站": (["投稿时间", "稿件标题", "bvid", "播放", "点赞", "评论", "收藏", "分享", "涨粉数"], "播放"),
        "公众号": (["发表日期", "文章标题", "阅读人数", "分享人数", "微信收藏人数"], "阅读人数"),
    }
    for platform, (columns, views_column) in cases.items():
        mapping = suggest_mapping(columns, platform=platform)
        assert mapping["date"] in columns
        assert mapping["title"] in columns
        assert mapping["views"] == views_column


def test_official_account_composite_export_is_merged_by_date(tmp_path: Path):
    path = tmp_path / "official.xlsx"
    pd.DataFrame(
        [
            [None] * 8,
            ["数据趋势概况"] * 8,
            ["日期", "渠道", "阅读人数", "日期", "分享人数", "微信收藏人数", "发表篇数", "备注"],
            ["2026-06-21", "公众号消息", 10, "2026-06-21", 3, 4, 1, None],
            ["2026-06-21", "朋友圈", 5, "2026-06-22", 2, 1, 1, None],
        ]
    ).to_excel(path, index=False, header=False)
    frame = read_table(path, "公众号")
    assert list(frame.columns) == ["日期", "阅读人数", "分享人数", "微信收藏人数"]
    first = frame.loc[frame["日期"] == "2026-06-21"].iloc[0]
    assert first["阅读人数"] == 15
    assert first["分享人数"] == 3
    assert first["微信收藏人数"] == 4


def test_password_hash_is_not_plaintext():
    hashed = hash_password("a-safe-password")
    assert hashed != "a-safe-password"
    assert verify_password("a-safe-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_csv_read_utf8(tmp_path: Path):
    path = tmp_path / "sample.csv"
    pd.DataFrame([{"日期": "2026-06-21", "播放量": "1.2万"}]).to_csv(path, index=False, encoding="utf-8-sig")
    frame = read_table(path)
    assert frame.iloc[0]["日期"] == "2026-06-21"


def test_report_comparison_ignores_missing_days():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    target = date(2026, 6, 21)
    with Session() as db:
        account = PlatformAccount(platform="抖音", name="测试账号")
        db.add(account)
        db.flush()
        db.add_all(
            [
                DailyAccountMetric(account_id=account.id, metric_date=target, views=300, likes=30),
                DailyAccountMetric(account_id=account.id, metric_date=target - timedelta(days=1), views=200, likes=20),
                DailyAccountMetric(account_id=account.id, metric_date=target - timedelta(days=3), views=100, likes=10),
            ]
        )
        db.commit()
        stats = report_stats(db, target)
        assert stats["current"]["views"] == 300
        assert stats["previous"]["views"] == 200
        assert stats["average_sample_days"] == 2
        assert stats["averages"]["views"] == 150


def test_content_import_and_daily_aggregation(tmp_path: Path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    path = tmp_path / "content.csv"
    pd.DataFrame(
        [
            {"发布时间": "2026-06-21 10:00:00", "作品名称": "第一条", "播放量": "1万", "点赞": 500, "评论": 20, "收藏": 30, "转发": 10},
            {"发布时间": "2026-06-21 18:00:00", "作品名称": "第二条", "播放量": 5000, "点赞": 200, "评论": 10, "收藏": 20, "转发": 5},
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")
    with Session() as db:
        user = User(username="tester", password_hash="unused", role="admin")
        account = PlatformAccount(platform="小红书", name="测试账号")
        db.add_all([user, account])
        db.flush()
        batch = ImportBatch(
            account_id=account.id,
            uploaded_by=user.id,
            original_filename=path.name,
            stored_path=str(path),
            file_hash=file_sha256(path),
        )
        db.add(batch)
        db.flush()
        mapping = suggest_mapping(list(read_table(path).columns))
        count = import_batch(db, batch, mapping)
        db.commit()
        assert count == 2
        assert db.query(ContentDailyMetric).count() == 2
        daily = db.query(DailyAccountMetric).one()
        assert daily.views == 15000
        assert daily.likes == 700


def test_metric_range_summary_and_comparison_groups():
    account_a = PlatformAccount(platform="抖音", name="同名账号")
    account_b = PlatformAccount(platform="小红书", name="同名账号")
    rows = [
        DailyAccountMetric(account=account_a, metric_date=date(2026, 6, 21), views=100, likes=10),
        DailyAccountMetric(account=account_b, metric_date=date(2026, 6, 21), views=200, likes=20),
    ]
    totals = summarize_metrics(rows, ["views", "likes"])
    groups = comparison_groups(rows, "account")
    assert totals == {"views": 300.0, "likes": 30.0}
    assert len(groups) == 1
    assert groups[0]["name"] == "同名账号"
    assert [row.account.platform for row in groups[0]["rows"]] == ["小红书", "抖音"]


def test_hotspot_payload_normalization_filters_unsafe_urls():
    payload = normalize_payload(
        {
            "summary": "今日概览",
            "realtime": [{"topic": "热点", "source_urls": ["https://example.com/a", "javascript:alert(1)"]}],
            "weekly": [],
            "topics": [{"priority": "X", "titles": ["标题一", "标题二"]}],
        },
        {"annotations": [{"url": "https://example.com/source"}]},
    )
    assert payload["realtime"][0]["source_urls"] == ["https://example.com/a"]
    assert payload["topics"][0]["priority"] == "B"
    assert payload["sources"] == ["https://example.com/a", "https://example.com/source"]
