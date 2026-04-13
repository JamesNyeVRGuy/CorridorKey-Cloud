"""Email sending for CorridorKey notifications.

Uses SMTP directly (not GoTrue). Reads config from environment:
    CK_SMTP_HOST, CK_SMTP_PORT, CK_SMTP_USER, CK_SMTP_PASS,
    CK_SMTP_FROM_EMAIL, CK_SMTP_FROM_NAME

Falls back to GoTrue's SMTP vars if CK_ variants aren't set.
No-op if SMTP is not configured.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_SMTP_HOST = os.environ.get("CK_SMTP_HOST", os.environ.get("GOTRUE_SMTP_HOST", "")).strip()
_SMTP_PORT = int(os.environ.get("CK_SMTP_PORT", os.environ.get("GOTRUE_SMTP_PORT", "587")).strip())
_SMTP_USER = os.environ.get("CK_SMTP_USER", os.environ.get("GOTRUE_SMTP_USER", "")).strip()
_SMTP_PASS = os.environ.get("CK_SMTP_PASS", os.environ.get("GOTRUE_SMTP_PASS", "")).strip()
_FROM_EMAIL = os.environ.get("CK_SMTP_FROM_EMAIL", os.environ.get("GOTRUE_SMTP_FROM_EMAIL", "")).strip()
_FROM_NAME = os.environ.get("CK_SMTP_FROM_NAME", os.environ.get("GOTRUE_SMTP_FROM_NAME", "CorridorKey")).strip()

_SITE_URL = os.environ.get("CK_SITE_URL", os.environ.get("SITE_URL", "https://corridorkey.cloud")).strip()

logger.info(f"SMTP Config: host={_SMTP_HOST}, port={_SMTP_PORT}, user={_SMTP_USER}, has_pass={bool(_SMTP_PASS)}")


def is_smtp_configured() -> bool:
    return bool(_SMTP_HOST and _FROM_EMAIL)


def send_email(to: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an email. Returns True on success, False on failure. Never raises."""
    if not is_smtp_configured():
        logger.warning("SMTP not configured — skipping email (set CK_SMTP_HOST and CK_SMTP_FROM_EMAIL)")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{_FROM_NAME} <{_FROM_EMAIL}>"
        msg["To"] = to
        msg["Subject"] = subject

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=20) as server:
            server.ehlo()
            # Try STARTTLS on port 25 too (it's available)
            # Only auth if NOT on port 25 - DC
            if _SMTP_PORT != 25:
                if _SMTP_PORT == 587:
                    server.starttls()
                    server.ehlo()
                if _SMTP_USER and _SMTP_PASS:
                    server.login(_SMTP_USER, _SMTP_PASS)

            server.sendmail(_FROM_EMAIL, to, msg.as_string())

        logger.info(f"MAIL:Email sent to {to}: {subject}")
        return True
    except Exception:
        logger.warning(f"MAIL:Failed to send email to {to}", exc_info=True)
        return False

def send_approval_otp_email(to: str) -> bool:
    try:
        import json
        import urllib.request

        anon_key = os.environ.get("ANON_KEY", "").strip()
        gotrue_url = os.environ.get(
            "CK_GOTRUE_INTERNAL_URL", os.environ.get("CK_GOTRUE_URL", "http://localhost:54324")
        ).strip()

        if not anon_key or not gotrue_url:
            logger.error("GoTrue URL or ANON_KEY not configured, cannot send OTP email")
            return False

        # Trigger OTP/magic link email
        otp_body = json.dumps(
            {
                "email": to,
            }
        ).encode()

        otp_req = urllib.request.Request(
            f"{gotrue_url}/otp",
            data=otp_body,
            headers={
                "Content-Type": "application/json",
                "apikey": anon_key,
            },
            method="POST",
        )

        with urllib.request.urlopen(otp_req, timeout=10):
            logger.info(f"MAIL:Email sent to {to} (confirmation)")
            return True

        logger.error(f"MAIL: Failed to send email to {to} (confirmation)")
        return False
    except Exception as e:
        error_msg = str(e)
        logger.error(f"GoTrue registration error: {error_msg}")
        return False

def send_approval_email(to: str, name: str) -> bool:
    """Send account approval notification."""
    subject = "Your CorridorKey account has been approved"
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
        <h2 style="color: #f0efe8; margin-bottom: 16px;">Account Approved</h2>
        <p style="color: #9d9c93; line-height: 1.6;">
            Hi{(" " + name) if name else ""},
        </p>
        <p style="color: #9d9c93; line-height: 1.6;">
            Your CorridorKey account has been approved. You can now sign in and start processing your footage.
        </p>
        <a href="{_SITE_URL}/login"
           style="display: inline-block; margin-top: 16px; padding: 12px 24px;
                  background: #fff203; color: #000; text-decoration: none;
                  border-radius: 6px; font-weight: 600; font-size: 14px;">
            Sign In
        </a>
        <p style="color: #605f56; font-size: 12px; margin-top: 24px;">
            — The CorridorKey Team
        </p>
    </div>
    """
    text = f"Hi{(' ' + name) if name else ''}, your CorridorKey account has been approved. Sign in at {_SITE_URL}/login"
    return send_email(to, subject, html, text)
