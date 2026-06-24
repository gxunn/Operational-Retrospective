from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.reporting import LABELS, METRICS


TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"
DIST = ROOT / "dist"
APP_NAME = "运营效率中台 V2"


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def url_for(name: str, path: str = "") -> str:
    if name == "static":
        return f"/static/{path.lstrip('/')}"
    return path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def copy_static_assets() -> None:
    ensure_dir(DIST)
    shutil.copytree(STATIC, DIST / "static", dirs_exist_ok=True)


def base_context(path: str) -> dict:
    return {
        "app_name": APP_NAME,
        "metric_labels": LABELS,
        "request": ns(url=ns(path=path)),
        "user": ns(id=1, username="admin", display_name="运营管理员", role="superadmin", is_active=True),
        "user_role_label": "超级管理员",
        "permissions": {
            "view": True,
            "manage_accounts": True,
            "manage_members": True,
            "manage_settings": True,
            "manage_recipients": True,
            "send_email": True,
            "manage_hotspots": True,
            "manage_reports": True,
            "import_data": True,
            "use_ai_reports": True,
            "use_report_builder": True,
            "use_topic_center": True,
            "use_breakdown": True,
            "manage_materials": True,
            "delete_data": True,
        },
        "settings": ns(
            report_hour=10,
            report_minute=0,
            hotspot_hour=9,
            hotspot_minute=0,
            app_timezone="Asia/Shanghai",
            openai_model="gpt-5.4-mini",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="demo@example.com",
            smtp_password="",
            mail_from="noreply@example.com",
            mail_from_name="运营效率中台",
        ),
        "appearance": ns(theme="light", font_scale="100"),
        "csrf_token": "static-csrf-token",
        "message": "",
        "static_site": True,
    }


def metric_row(account, metric_date: date, views: int, followers: int, likes: int, comments: int, favorites: int, shares: int, leads: int, private_messages: int = 0):
    return ns(
        account=account,
        metric_date=metric_date,
        views=views,
        followers_new=followers,
        likes=likes,
        comments=comments,
        favorites=favorites,
        shares=shares,
        leads=leads,
        private_messages=private_messages,
    )


def content_row(account, metric_date: date, title: str, views: int, likes: int, comments: int, favorites: int, shares: int, leads: int, note: str = ""):
    return ns(
        account=account,
        account_id=account.id,
        metric_date=metric_date,
        title=title,
        views=views,
        likes=likes,
        comments=comments,
        favorites=favorites,
        shares=shares,
        leads=leads,
        note=note,
    )


def build_mock_data():
    today = date.today()
    target = today - timedelta(days=1)
    prev = target - timedelta(days=1)
    platforms = ["抖音", "小红书", "视频号", "公众号", "其他自定义平台"]
    accounts = [
        ns(id=1, platform="抖音", name="品牌主号", external_id="DY-001", manager_name="小林", positioning="课程转化", data_source="manual", is_active=True, last_synced_at=datetime.now().replace(microsecond=0)),
        ns(id=2, platform="小红书", name="种草号", external_id="XHS-002", manager_name="小周", positioning="活动种草", data_source="api", is_active=True, last_synced_at=datetime.now().replace(microsecond=0)),
        ns(id=3, platform="视频号", name="校园号", external_id="SPH-003", manager_name="小王", positioning="校园内容", data_source="semi_auto", is_active=True, last_synced_at=datetime.now().replace(microsecond=0)),
        ns(id=4, platform="公众号", name="服务号", external_id="GZH-004", manager_name="小陈", positioning="私域承接", data_source="manual", is_active=False, last_synced_at=None),
    ]
    account_map = {a.id: a for a in accounts}

    metrics_today = [
        metric_row(account_map[1], target, 34620, 128, 2040, 146, 312, 88, 26, 44),
        metric_row(account_map[2], target, 22510, 94, 1488, 92, 268, 61, 19, 31),
        metric_row(account_map[3], target, 18840, 76, 1012, 58, 190, 47, 17, 18),
        metric_row(account_map[4], target, 14320, 52, 760, 44, 126, 29, 13, 22),
    ]
    metrics_prev = [
        metric_row(account_map[1], prev, 29870, 96, 1832, 132, 265, 72, 22, 36),
        metric_row(account_map[2], prev, 21040, 88, 1335, 77, 230, 54, 18, 27),
        metric_row(account_map[3], prev, 17120, 61, 924, 53, 171, 39, 11, 14),
        metric_row(account_map[4], prev, 13890, 49, 702, 41, 118, 24, 10, 20),
    ]
    metrics_history = metrics_prev + metrics_today

    contents = [
        content_row(account_map[1], target, "无人机足球训练营如何三天做出转化闭环", 10820, 620, 84, 142, 31, 12, "高转化话术直接落地"),
        content_row(account_map[2], target, "张家界研学活动，家长最关心的 3 个问题", 8640, 420, 51, 97, 22, 9, "评论区互动强"),
        content_row(account_map[3], target, "无人机考证报名避坑指南，一次说透", 7420, 310, 39, 75, 16, 8, "私信咨询明显增加"),
        content_row(account_map[4], target, "成人学历提升如何选学校更稳妥", 6240, 265, 28, 61, 11, 6, "收藏率较高"),
        content_row(account_map[1], target, "一场活动如何拍出 5 条可复用视频", 5820, 210, 24, 42, 9, 4, "结构感较强"),
        content_row(account_map[2], target, "招生页封面怎么写更容易点开", 5540, 198, 19, 36, 7, 5, ""),
        content_row(account_map[3], target, "家长咨询最常见的 5 个误区", 4920, 173, 15, 29, 6, 3, ""),
        content_row(account_map[4], target, "活动复盘模板：3 分钟整理成稿", 4380, 150, 14, 25, 5, 2, ""),
    ]

    trends = [
        {"date": (target - timedelta(days=6) + timedelta(days=i)).strftime("%-m/%-d"), "views": v}
        for i, v in enumerate([21200, 26500, 24400, 29800, 27500, 32100, 34620])
    ]
    max_trend = max(item["views"] for item in trends)
    for index, item in enumerate(trends):
        item["x"] = round(7 + index * 86 / 6, 2)
        item["y"] = round(82 - item["views"] / max_trend * 58, 2)

    account_rows = []
    for account, current_metric, previous_metric in zip(accounts, metrics_today, metrics_prev):
        interactions = current_metric.likes + current_metric.comments + current_metric.favorites + current_metric.shares
        prev_interactions = previous_metric.likes + previous_metric.comments + previous_metric.favorites + previous_metric.shares
        rate = interactions / current_metric.views if current_metric.views else 0
        prev_rate = prev_interactions / previous_metric.views if previous_metric.views else 0
        follower_change = ((current_metric.followers_new - previous_metric.followers_new) / previous_metric.followers_new * 100) if previous_metric.followers_new else None
        account_rows.append(
            {
                "account": account,
                "metric": current_metric,
                "rate": rate,
                "rate_change": (rate - prev_rate) * 100,
                "follower_change": follower_change,
            }
        )

    contents_sorted = sorted(contents, key=lambda item: item.views, reverse=True)
    totals = {field: sum(getattr(row, field) for row in metrics_today) for field in METRICS}
    rows = metrics_history
    selected_platforms = ["抖音", "小红书"]
    selected_account_ids = [1, 2]

    account_groups = [
        {
            "name": "无人机足球",
            "rows": [metric_row(account_map[1], target, 34620, 128, 2040, 146, 312, 88, 26), metric_row(account_map[2], target, 22510, 94, 1488, 92, 268, 61, 19)],
            "max_views": 34620,
        },
        {
            "name": "成人学历",
            "rows": [metric_row(account_map[3], target, 18840, 76, 1012, 58, 190, 47, 17), metric_row(account_map[4], target, 14320, 52, 760, 44, 126, 29, 13)],
            "max_views": 18840,
        },
    ]
    platform_groups = [
        {
            "name": "抖音",
            "rows": [metric_row(account_map[1], target, 34620, 128, 2040, 146, 312, 88, 26)],
            "max_views": 34620,
        },
        {
            "name": "小红书",
            "rows": [metric_row(account_map[2], target, 22510, 94, 1488, 92, 268, 61, 19)],
            "max_views": 22510,
        },
    ]

    reports = [
        ns(id=101, report_date=target, title="2026-06-23 自媒体复盘", html_content="<h2>今日亮点</h2><p>抖音主号播放量突破 3.4 万。</p><h2>明日行动建议</h2><p>继续复制前 3 秒钩子与转化设计。</p>", status="generated", ai_error=""),
        ns(id=102, report_date=prev, title="2026-06-22 自媒体复盘", html_content="<h2>今日亮点</h2><p>小红书收藏率提升明显。</p>", status="generated", ai_error="AI 额度不足，已生成纯数据版"),
    ]
    summary_reports = [
        ns(id=201, start_date=target - timedelta(days=6), end_date=target, title="第 25 周周报", html_content="<h2>本周运营概况</h2><p>整体播放量和线索数同步上升。</p>", status="generated", ai_error=""),
        ns(id=202, start_date=target - timedelta(days=29), end_date=target, title="6 月月报", html_content="<h2>本月整体表现</h2><p>抖音和小红书共同贡献主要增长。</p>", status="generated", ai_error=""),
    ]
    drafts = [
        ns(id=1, title="第 25 周周报", report_kind="weekly", created_at=datetime.now().replace(microsecond=0), markdown_content="# 周报\n\n- 本周播放量提升 18%\n- 选题中心产出 20 条", text_content="本周播放量提升 18%，选题中心产出 20 条。", ppt_outline="1. 本周概况\n2. 核心数据\n3. 重点内容\n4. 下周计划"),
        ns(id=2, title="6 月月报", report_kind="monthly", created_at=datetime.now().replace(microsecond=0), markdown_content="# 月报\n\n- 月度数据对比完成\n- 转化备注整理完成", text_content="月度数据对比完成，转化备注整理完成。", ppt_outline="1. 整体表现\n2. 平台对比\n3. 策略建议"),
    ]

    topics = [
        ns(title="无人机足球训练营 7 天转化复盘", business="无人机足球", content_type="成交案例", platform="抖音", priority="S", status="待写", reference_link="https://example.com/1", note="适合招生转化", angle="案例复盘", script_direction="前 3 秒展示结果 + 再讲方法", created_at=datetime.now().replace(microsecond=0)),
        ns(title="张家界研学路线怎么选更省心", business="其他", content_type="科普", platform="小红书", priority="A", status="待拍", reference_link="https://example.com/2", note="适合家长决策", angle="痛点问答", script_direction="家长最担心的 3 个问题", created_at=datetime.now().replace(microsecond=0)),
        ns(title="无人机考证报名避坑指南", business="无人机考证", content_type="招生", platform="视频号", priority="A", status="已发布", reference_link="https://example.com/3", note="转化咨询", angle="避坑清单", script_direction="直接对比平台差异", created_at=datetime.now().replace(microsecond=0)),
        ns(title="成人学历提升，哪些人适合先报名", business="成人学历", content_type="故事", platform="公众号", priority="B", status="已复盘", reference_link="https://example.com/4", note="教育场景", angle="用户故事", script_direction="先讲场景，再讲路径", created_at=datetime.now().replace(microsecond=0)),
    ]

    breakdown_cases = [
        ns(id=1, title="无人机足球训练营如何三天做出转化闭环", platform="抖音", source_url="https://example.com/video/1", views=10820, likes=620, comments=84, duration="38s", cover_description="蓝底大字标题 + 训练现场", script_content="开场直接展示结果，再切转化动作。", created_at=datetime.now().replace(microsecond=0)),
        ns(id=2, title="张家界研学活动，家长最关心的 3 个问题", platform="小红书", source_url="https://example.com/video/2", views=8640, likes=420, comments=51, duration="51s", cover_description="亲子合影 + 行程亮点", script_content="以提问形式带入痛点。", created_at=datetime.now().replace(microsecond=0)),
    ]

    breakdown_stats = ns(progress=18, analysis_status="分析中", eta="预计 1 分钟", total_cases=len(breakdown_cases), avg_views=9460)
    hot_rankings = sorted(breakdown_cases, key=lambda item: item.views, reverse=True)

    materials = [
        ns(
            item=ns(id=1, name="张家界研学海报", asset_type="海报", project_name="研学", use_scene="活动预热", uploader_name="小林", created_at=datetime.now().replace(microsecond=0), is_favorite=True, file_path="/uploads/poster.png"),
            tags=["张家界", "研学", "海报"],
        ),
        ns(
            item=ns(id=2, name="无人机足球招生文案", asset_type="文案", project_name="无人机足球", use_scene="招生转化", uploader_name="小周", created_at=datetime.now().replace(microsecond=0), is_favorite=False, file_path="/uploads/copy.md"),
            tags=["无人机足球", "招生", "证书"],
        ),
        ns(
            item=ns(id=3, name="成人学历活动方案", asset_type="活动方案", project_name="成人学历", use_scene="活动执行", uploader_name="小王", created_at=datetime.now().replace(microsecond=0), is_favorite=True, file_path="/uploads/plan.pdf"),
            tags=["成人学历", "活动", "招生"],
        ),
    ]

    users = [
        ns(id=1, username="admin", display_name="运营总监", role="superadmin", is_active=True),
        ns(id=2, username="ops1", display_name="运营人员 A", role="member", is_active=True),
        ns(id=3, username="ops2", display_name="运营人员 B", role="member", is_active=False),
    ]
    trash = [
        ns(id=4, username="temp", display_name="测试成员", role="member", is_active=False),
    ]

    recipients = [
        ns(recipient=ns(id=1, name="运营主管", email="ops@example.com", is_active=True), tags_text="日报, 周报"),
        ns(recipient=ns(id=2, name="市场同学", email="market@example.com", is_active=False), tags_text="月报, 活动"),
    ]
    hotspot_history = [
        ns(report_date=str(target), updated_at=datetime.now().replace(microsecond=0), status="已生成"),
        ns(report_date=str(prev), updated_at=datetime.now().replace(microsecond=0), status="已生成"),
    ]

    ai_reports = [
        ns(start_date=target, end_date=target, range_type="yesterday", created_at=datetime.now().replace(microsecond=0)),
        ns(start_date=target - timedelta(days=6), end_date=target, range_type="7d", created_at=datetime.now().replace(microsecond=0)),
        ns(start_date=target - timedelta(days=29), end_date=target, range_type="30d", created_at=datetime.now().replace(microsecond=0)),
    ]
    ai_generated = ns(
        summary_json={
            "overall": {"total_views": sum(row.views for row in metrics_today), "total_followers": sum(row.followers_new for row in metrics_today)},
            "reasoning": "mock 分析逻辑，后续可替换为真实 AI。",
        },
        copy_text="AI复盘结论：内容表现整体稳步上升，建议继续复制高互动内容结构。",
        markdown="# AI复盘\n\n## 一、整体数据概览\n- 总播放/阅读：97,290\n- 新增粉丝：350\n\n## 六、可执行任务清单\n- 复盘爆款结构\n- 优化前 3 秒钩子",
    )

    report_detail = reports[0]
    summary_detail = summary_reports[0]

    saved_views = [
        ns(id=1, name="本周抖音看板", href="/metrics?platform=%E6%8A%96%E9%9F%B3", updated_at=datetime.now().replace(microsecond=0)),
        ns(id=2, name="小红书转化看板", href="/metrics?platform=%E5%B0%8F%E7%BA%A2%E4%B9%A6", updated_at=datetime.now().replace(microsecond=0)),
    ]

    return {
        "target": target,
        "prev": prev,
        "platforms": platforms,
        "accounts": accounts,
        "account_rows": account_rows,
        "contents": contents_sorted,
        "stats": {
            "current": totals,
            "previous": {field: sum(getattr(row, field) for row in metrics_prev) for field in METRICS},
        },
        "trends": trends,
        "forecast_views": {"trend": "上升", "next": 38200},
        "forecast_followers": {"trend": "上升", "next": 156},
        "report_hour": "10:00",
        "target_weekday": "星期" + "一二三四五六日"[target.weekday()],
        "anomalies": [
            ns(account="抖音 · 品牌主号", metric="views", current=34620, change=18.6, suggestion="继续放大开头钩子和活动现场画面。"),
            ns(account="小红书 · 种草号", metric="followers_new", current=94, change=-6.4, suggestion="改成更强的场景化标题。"),
        ],
        "rows": rows,
        "metrics": METRICS,
        "totals": totals,
        "selected_platforms": selected_platforms,
        "selected_account_ids": selected_account_ids,
        "saved_views": saved_views,
        "comparison_day": target.isoformat(),
        "account_groups": account_groups,
        "platform_groups": platform_groups,
        "ai_reports": ai_reports,
        "selected_range_type": "7d",
        "selected_platform": "抖音",
        "selected_account": accounts[0],
        "generated": ai_generated,
        "generated_reports": ai_reports,
        "drafts": drafts,
        "draft": drafts[0],
        "keyword": "无人机足球",
        "business": "无人机足球",
        "topics": topics,
        "breakdown_stats": breakdown_stats,
        "cases": breakdown_cases,
        "q": "",
        "selected_platform_breakdown": "",
        "recent_cases": breakdown_cases,
        "hot_rankings": hot_rankings,
        "report": ns(id=report_detail.id, title=report_detail.title, html_content=report_detail.html_content, ai_error=report_detail.ai_error, report_date=report_detail.report_date),
        "report_data": ns(
            template="爆款标题模板",
            ratio=6.2,
            title_structure=["结果先行", "数字锚点", "场景化表达"],
            hook=["前 3 秒直接给结果", "快速切入痛点"],
            rhythm=["快切镜头 + 关键字强调", "中段补充案例"],
            selling_point=["把价值点放前面", "用可验证结果增强信任"],
            conversion=["评论区引导咨询", "结尾给出行动按钮"],
            extension_topics=["同城招生", "亲子研学", "证书报名"],
        ),
        "materials": materials,
        "role_options": [("superadmin", "超级管理员"), ("member", "运营人员")],
        "can_manage_members": True,
        "trash": trash,
        "users": users,
        "config_status": ns(openai=True, smtp=False),
        "appearance": ns(theme="light", font_scale="100"),
        "report_schedule": ns(frequency="daily", weekday="mon", monthday="1"),
        "business_keywords": "无人机足球, 张家界研学, 无人机考证, 成人学历",
        "hotspot_sources": "抖音, 小红书, 微信视频号",
        "recipients": recipients,
        "hotspot_history": hotspot_history,
        "reports": reports,
        "summary_reports": summary_reports,
        "report_detail": report_detail,
        "summary_detail": summary_detail,
        "ai_reports_list": ai_reports,
    }


def render(env: Environment, template_name: str, output_path: Path, context: dict) -> None:
    template = env.get_template(template_name)
    html = template.render(context)
    write_text(output_path, html)


def write_redirects() -> None:
    routes = [
        ("/", "/index.html"),
        ("/metrics", "/metrics/index.html"),
        ("/ai-review", "/ai-review/index.html"),
        ("/report-builder", "/report-builder/index.html"),
        ("/topic-center", "/topic-center/index.html"),
        ("/breakdown", "/breakdown/index.html"),
        ("/materials", "/materials/index.html"),
        ("/accounts", "/accounts/index.html"),
        ("/users", "/users/index.html"),
        ("/settings", "/settings/index.html"),
        ("/imports", "/imports/index.html"),
        ("/imports/upload", "/imports/upload/index.html"),
        ("/reports", "/reports/index.html"),
        ("/reports/101", "/reports/101/index.html"),
        ("/reports/101/markdown", "/reports/101/markdown/index.html"),
        ("/reports/101/pdf", "/reports/101/pdf/index.html"),
        ("/summary-reports/201", "/summary-reports/201/index.html"),
        ("/summary-reports/201/markdown", "/summary-reports/201/markdown/index.html"),
        ("/summary-reports/201/pdf", "/summary-reports/201/pdf/index.html"),
        ("/auth/login", "/auth/login/index.html"),
    ]
    content = "\n".join(f"{src} {dest} 200" for src, dest in routes) + "\n"
    write_text(DIST / "_redirects", content)


def write_404() -> None:
    write_text(
        DIST / "404.html",
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>页面不存在</title><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/design-system.css"></head><body><main class="login-panel"><div class="login-brand"><span class="brand-rule"></span><div><strong>运营效率中台</strong><small>静态部署版</small></div></div><div class="login-copy"><h1>这个页面暂时没有找到</h1><p>你可以返回总览继续查看其他模块。</p></div><a class="button primary wide" href="/">返回总览</a></main></body></html>""",
    )


def write_login() -> None:
    write_text(
        DIST / "auth" / "login" / "index.html",
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 · 运营效率中台</title><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/design-system.css"></head><body class="login-page"><main class="login-panel"><div class="login-brand"><span class="brand-rule"></span><div><strong>运营效率中台</strong><small>静态部署版</small></div></div><div class="login-copy"><h1>静态演示版已部署</h1><p>Cloudflare Pages 版本以公开访问为主，当前不启用服务端登录。</p></div><div class="login-form"><a class="button primary wide" href="/">进入总览</a></div><p class="login-help">如果你需要真实登录、上传和写入能力，请继续保留本地 FastAPI 版本。</p></main></body></html>""",
    )


def main() -> None:
    copy_static_assets()
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(url_for=url_for, app_name=APP_NAME, metric_labels=LABELS)

    context = build_mock_data()

    pages = [
        ("dashboard.html", DIST / "index.html", {**base_context("/"), **context}),
        ("metrics.html", DIST / "metrics" / "index.html", {**base_context("/metrics"), **context, "start_date": context["target"].isoformat(), "end_date": context["target"].isoformat()}),
        ("ai_review.html", DIST / "ai-review" / "index.html", {**base_context("/ai-review"), **context, "reports": context["ai_reports"], "generated": context["generated"], "selected_range_type": "7d", "selected_platform": "抖音", "selected_account": context["accounts"][0]}),
        ("report_builder.html", DIST / "report-builder" / "index.html", {**base_context("/report-builder"), **context, "drafts": context["drafts"], "draft": context["draft"]}),
        ("topic_center.html", DIST / "topic-center" / "index.html", {**base_context("/topic-center"), **context}),
        ("breakdown.html", DIST / "breakdown" / "index.html", {**base_context("/breakdown"), **context, "report": context["report"], "report_data": context["report_data"], "recent_cases": context["recent_cases"], "selected_platform": "", "q": ""}),
        ("materials.html", DIST / "materials" / "index.html", {**base_context("/materials"), **context, "permissions": base_context("/materials")["permissions"]}),
        ("accounts.html", DIST / "accounts" / "index.html", {**base_context("/accounts"), **context, "can_manage_accounts": True, "in_trash": False}),
        ("users.html", DIST / "users" / "index.html", {**base_context("/users"), **context, "can_manage_members": True}),
        ("settings.html", DIST / "settings" / "index.html", {**base_context("/settings"), **context}),
        ("upload.html", DIST / "imports" / "upload" / "index.html", {**base_context("/imports/upload"), **context, "max_mb": 30}),
        ("imports.html", DIST / "imports" / "index.html", {**base_context("/imports"), **context, "batches": [
            ns(id=1, created_at=datetime.now().replace(microsecond=0), account=context["accounts"][0], original_filename="抖音日报.csv", start_date=context["target"], end_date=context["target"], row_count=12, status="imported", error_message=""),
            ns(id=2, created_at=datetime.now().replace(microsecond=0), account=context["accounts"][1], original_filename="小红书明细.xlsx", start_date=context["target"], end_date=context["target"], row_count=9, status="failed", error_message="字段识别失败"),
        ]}),
        ("reports.html", DIST / "reports" / "index.html", {**base_context("/reports"), **context}),
        ("report_detail.html", DIST / "reports" / "101" / "index.html", {**base_context("/reports/101"), **context, "report": ns(id=101, report_date=context["target"], title="2026-06-23 自媒体复盘", html_content=context["report"].html_content, ai_error="")}),
        ("report_detail.html", DIST / "reports" / "101" / "markdown" / "index.html", {**base_context("/reports/101/markdown"), **context, "report": ns(id=101, report_date=context["target"], title="2026-06-23 自媒体复盘", html_content=f"<pre>{json.dumps({'markdown': 'demo'}, ensure_ascii=False, indent=2)}</pre>", ai_error="")}),
        ("report_detail.html", DIST / "reports" / "101" / "pdf" / "index.html", {**base_context("/reports/101/pdf"), **context, "report": ns(id=101, report_date=context["target"], title="2026-06-23 自媒体复盘", html_content="<p>静态演示版不提供 PDF 下载，发布到 Cloudflare Pages 后可继续接入真实后端导出。</p>", ai_error="")}),
        ("report_detail.html", DIST / "summary-reports" / "201" / "index.html", {**base_context("/summary-reports/201"), **context, "report": ns(id=201, start_date=context["target"] - timedelta(days=6), end_date=context["target"], title="第 25 周周报", html_content=context["summary_detail"].html_content, ai_error="")}),
        ("report_detail.html", DIST / "summary-reports" / "201" / "markdown" / "index.html", {**base_context("/summary-reports/201/markdown"), **context, "report": ns(id=201, start_date=context["target"] - timedelta(days=6), end_date=context["target"], title="第 25 周周报", html_content="<p>静态演示版不提供 Markdown 下载。</p>", ai_error="")}),
        ("report_detail.html", DIST / "summary-reports" / "201" / "pdf" / "index.html", {**base_context("/summary-reports/201/pdf"), **context, "report": ns(id=201, start_date=context["target"] - timedelta(days=6), end_date=context["target"], title="第 25 周周报", html_content="<p>静态演示版不提供 PDF 下载。</p>", ai_error="")}),
    ]

    for template_name, output_path, page_context in pages:
        render(env, template_name, output_path, page_context)

    write_redirects()
    write_404()
    write_login()


if __name__ == "__main__":
    main()
