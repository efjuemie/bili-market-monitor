import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from app.core.config import Settings

logger = logging.getLogger(__name__)


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, recipient: str, subject: str, text_body: str, html_body: Optional[str] = None) -> bool:
        if not self.settings.smtp_enabled:
            # Development can exercise the full outbox state machine without
            # accidentally sending mail. Production should configure SMTP.
            logger.warning("SMTP delivery skipped recipient=%s reason=not_configured", self._mask_email(recipient))
            return False
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.settings.smtp_from_name} <{self.settings.smtp_from_email}>"
        message["To"] = recipient
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        try:
            if self.settings.smtp_use_ssl:
                server_factory = smtplib.SMTP_SSL
            else:
                server_factory = smtplib.SMTP
            with server_factory(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as server:
                if self.settings.smtp_use_tls:
                    server.starttls()
                if self.settings.smtp_username:
                    server.login(self.settings.smtp_username, self.settings.smtp_password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            logger.warning(
                "SMTP delivery failed recipient=%s exception=%s",
                self._mask_email(recipient),
                type(exc).__name__,
            )
            raise
        return True

    @staticmethod
    def _mask_email(email: str) -> str:
        local, _, domain = email.partition("@")
        return f"{local[:1]}***@{domain}" if domain else "***"


def verification_email(settings: Settings, recipient: str, code: str) -> tuple[str, str, str]:
    subject = "【B站市集好价提示系统】邮箱验证码"
    text = f"你的验证码是：{code}\n\n验证码 {settings.email_verify_code_ttl_minutes} 分钟内有效。\n如果不是你本人操作，请忽略本邮件。"
    html_body = (
        "<p>你的验证码是：</p>"
        f"<p style='font-size:24px;font-weight:bold'>{html.escape(code)}</p>"
        f"<p>验证码 {settings.email_verify_code_ttl_minutes} 分钟内有效。<br>如果不是你本人操作，请忽略本邮件。</p>"
    )
    return subject, text, html_body


def reset_email(settings: Settings, recipient: str, link: str) -> tuple[str, str, str]:
    subject = "【B站市集好价提示系统】重置密码"
    text = f"我们收到了一次密码重置请求。请在 {settings.password_reset_ttl_minutes} 分钟内打开以下链接：\n{link}\n\n如果不是你本人操作，请忽略本邮件。"
    html_body = f"<p>我们收到了一次密码重置请求。</p><p><a href='{html.escape(link)}'>重置密码</a></p><p>链接 {settings.password_reset_ttl_minutes} 分钟内有效。</p>"
    return subject, text, html_body


def price_alert_email(product, target_price: str, checked_at: str) -> tuple[str, str, str]:
    title = product.title
    current = product.current_price
    detail_url = product.detail_url
    subject = f"【B站市集低价提醒】{title[:32]} ¥{current}"
    text = (
        "你关注的 B 站市集商品已达到目标价格。\n\n"
        f"商品：{title}\n当前最低价：¥{current}\n目标价格：¥{target_price}\n"
        f"状态：{'可购买' if product.available else '已售罄'}\n检查时间：{checked_at}\n\n"
        f"前往 B 站市集：\n{detail_url}"
    )
    image = f"<p><img src='{html.escape(product.cover_url)}' alt='商品封面' style='max-width:320px'></p>" if product.cover_url else ""
    html_body = (
        f"<h2>你关注的 B 站市集商品已达到目标价格</h2>{image}"
        f"<p>商品：{html.escape(title)}</p>"
        f"<p>当前最低价：¥{html.escape(str(current))}<br>目标价格：¥{html.escape(target_price)}<br>状态：{'可购买' if product.available else '已售罄'}<br>检查时间：{html.escape(checked_at)}</p>"
        f"<p><a href='{html.escape(detail_url)}' style='display:inline-block;padding:10px 16px;background:#00aeec;color:white;text-decoration:none;border-radius:6px'>前往 B 站市集</a></p>"
        f"<p>{html.escape(detail_url)}</p>"
    )
    return subject, text, html_body
