import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EmailRecipient, Report
from .runtime import runtime_settings


def send_report_email(db: Session, report: Report) -> int:
    recipients = db.scalars(select(EmailRecipient).where(EmailRecipient.is_active.is_(True))).all()
    if not recipients:
        raise ValueError("还没有配置启用的收件邮箱")
    settings = runtime_settings(db)
    if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password:
        raise ValueError("SMTP 尚未在系统设置中配置完整")
    sender = settings.mail_from or settings.smtp_username
    message = EmailMessage()
    message["Subject"] = f"{report.report_date} 自媒体每日复盘"
    message["From"] = f"{settings.mail_from_name} <{sender}>"
    message["To"] = ", ".join(recipient.email for recipient in recipients)
    message.set_content(report.markdown_content)
    message.add_alternative(report.html_content, subtype="html")
    if report.pdf_path and Path(report.pdf_path).exists():
        data = Path(report.pdf_path).read_bytes()
        message.add_attachment(data, maintype="application", subtype="pdf", filename=Path(report.pdf_path).name)
    smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
    with smtp_class(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if not settings.smtp_use_ssl:
            server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
    report.emailed_at = datetime.now().replace(microsecond=0)
    return len(recipients)
