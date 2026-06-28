from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(120), default="")
    manager_name: Mapped[str] = mapped_column(String(80), default="")
    business_type: Mapped[str] = mapped_column(String(80), default="")
    positioning: Mapped[str] = mapped_column(String(255), default="")
    data_source: Mapped[str] = mapped_column(String(30), default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("platform", "name", name="uq_platform_account"),)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="preview")
    mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    account: Mapped[PlatformAccount] = relationship()
    uploader: Mapped[User] = relationship()


class MappingProfile(Base):
    __tablename__ = "mapping_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), unique=True)
    mapping_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DailyAccountMetric(Base):
    __tablename__ = "daily_account_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    followers_new: Mapped[float] = mapped_column(Float, default=0)
    views: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[float] = mapped_column(Float, default=0)
    comments: Mapped[float] = mapped_column(Float, default=0)
    favorites: Mapped[float] = mapped_column(Float, default=0)
    shares: Mapped[float] = mapped_column(Float, default=0)
    leads: Mapped[float] = mapped_column(Float, default=0)
    source_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    account: Mapped[PlatformAccount] = relationship()
    __table_args__ = (UniqueConstraint("account_id", "metric_date", name="uq_daily_account_date"),)


class ContentDailyMetric(Base):
    __tablename__ = "content_daily_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    content_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    views: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[float] = mapped_column(Float, default=0)
    comments: Mapped[float] = mapped_column(Float, default=0)
    favorites: Mapped[float] = mapped_column(Float, default=0)
    shares: Mapped[float] = mapped_column(Float, default=0)
    leads: Mapped[float] = mapped_column(Float, default=0)
    private_messages: Mapped[float] = mapped_column(Float, default=0)
    conversion_note: Mapped[str] = mapped_column(Text, default="")
    source_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    account: Mapped[PlatformAccount] = relationship()
    __table_args__ = (UniqueConstraint("account_id", "metric_date", "content_key", name="uq_content_date"),)


class AiReview(Base):
    __tablename__ = "ai_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(30), default="ai_review", index=True)
    range_type: Mapped[str] = mapped_column(String(20), default="custom")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    platform: Mapped[str] = mapped_column(String(30), default="")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_type: Mapped[str] = mapped_column(String(40), default="review")
    markdown_content: Mapped[str] = mapped_column(Text, default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    copy_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GeneratedReportDraft(Base):
    __tablename__ = "generated_report_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_kind: Mapped[str] = mapped_column(String(30), default="weekly", index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    platform: Mapped[str] = mapped_column(String(30), default="")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    markdown_content: Mapped[str] = mapped_column(Text, default="")
    text_content: Mapped[str] = mapped_column(Text, default="")
    ppt_outline: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TopicIdea(Base):
    __tablename__ = "topic_ideas"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    business: Mapped[str] = mapped_column(String(50), default="其他")
    content_type: Mapped[str] = mapped_column(String(30), default="科普")
    platform: Mapped[str] = mapped_column(String(120), default="")
    owner_name: Mapped[str] = mapped_column(String(80), default="")
    priority: Mapped[str] = mapped_column(String(10), default="B")
    status: Mapped[str] = mapped_column(String(20), default="待拍摄")
    reference_link: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    angle: Mapped[str] = mapped_column(Text, default="")
    script_direction: Mapped[str] = mapped_column(Text, default="")
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)


class VideoBreakdown(Base):
    __tablename__ = "video_breakdowns"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    platform: Mapped[str] = mapped_column(String(30), default="")
    views: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[float] = mapped_column(Float, default=0)
    comments: Mapped[float] = mapped_column(Float, default=0)
    duration: Mapped[str] = mapped_column(String(30), default="")
    cover_description: Mapped[str] = mapped_column(Text, default="")
    script_content: Mapped[str] = mapped_column(Text, default="")
    analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    analysis_markdown: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="未开始")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MaterialAsset(Base):
    __tablename__ = "material_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(30), default="图片")
    project_name: Mapped[str] = mapped_column(String(80), default="")
    use_scene: Mapped[str] = mapped_column(String(120), default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    uploader_name: Mapped[str] = mapped_column(String(80), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    report_type: Mapped[str] = mapped_column(String(20), default="daily")
    status: Mapped[str] = mapped_column(String(30), default="generated")
    markdown_content: Mapped[str] = mapped_column(Text, default="")
    html_content: Mapped[str] = mapped_column(Text, default="")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    ai_error: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(500), default="")
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class SummaryReport(Base):
    __tablename__ = "summary_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_type: Mapped[str] = mapped_column(String(20), default="weekly", index=True)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(30), default="generated")
    markdown_content: Mapped[str] = mapped_column(Text, default="")
    html_content: Mapped[str] = mapped_column(Text, default="")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    ai_error: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(500), default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class OperationLog(Base):
    __tablename__ = "operation_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    operator_name: Mapped[str] = mapped_column(String(120), default="")
    operation_type: Mapped[str] = mapped_column(String(40), index=True)
    object_type: Mapped[str] = mapped_column(String(40), index=True)
    object_name: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class HotspotReport(Base):
    __tablename__ = "hotspot_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="generated")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class EmailRecipient(Base):
    __tablename__ = "email_recipients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(255), unique=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SavedView(Base):
    __tablename__ = "saved_views"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(30), default="metrics")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
