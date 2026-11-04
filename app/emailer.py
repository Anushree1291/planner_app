"""Sends email through whichever provider is configured.

- acs:     Azure Communication Services Email (connection string + verified sender)
- smtp:    any SMTP server: Gmail (app password), Outlook, Brevo, Mailgun, SendGrid...
- console: logs the message and keeps it in OUTBOX (local dev and tests)
- none:    email is switched off

send_email() never raises: a mail failure must not break planning. It returns
"sent", "throttled" (the provider said slow down: try again later), "failed" or "off".
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config

log = logging.getLogger("planner.email")
OUTBOX: list[dict] = []   # console provider keeps sent messages here


def email_enabled() -> bool:
    if config.EMAIL_PROVIDER == "acs":
        return bool(config.ACS_CONNECTION_STRING and config.EMAIL_FROM)
    if config.EMAIL_PROVIDER == "smtp":
        return bool(config.SMTP_HOST and (config.EMAIL_FROM or config.SMTP_USERNAME))
    return config.EMAIL_PROVIDER == "console"


def _sender() -> str:
    return config.EMAIL_FROM or config.SMTP_USERNAME


_acs_client = None


def _acs():
    global _acs_client
    if _acs_client is None:   # one client, reused (Azure SDK clients are thread-safe)
        from azure.communication.email import EmailClient  # imported lazily: optional dependency
        _acs_client = EmailClient.from_connection_string(config.ACS_CONNECTION_STRING)
    return _acs_client


def _is_throttle(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    if isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code in (421, 450, 451, 452):
        return True
    return False


def _send_acs(to: str, subject: str, html: str, text: str, unsubscribe_url: str | None):
    client = _acs()
    message = {
        "senderAddress": config.EMAIL_FROM,
        "recipients": {"to": [{"address": to}]},
        "content": {"subject": subject, "plainText": text, "html": html},
    }
    # begin_send returns once Azure has accepted the message; delivery continues on
    # Azure's side, so we don't block the request waiting for the poller.
    client.begin_send(message)


def _send_smtp(to: str, subject: str, html: str, text: str, unsubscribe_url: str | None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.EMAIL_FROM_NAME, _sender()))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=_sender().split("@")[-1])
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        server.starttls(context=ctx)
    with server:
        if config.SMTP_USERNAME:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg)


def send_email(to: str | None, subject: str, html: str, text: str,
               unsubscribe_url: str | None = None) -> str:
    if not to or not email_enabled():
        return "off"
    try:
        if config.EMAIL_PROVIDER == "acs":
            _send_acs(to, subject, html, text, unsubscribe_url)
        elif config.EMAIL_PROVIDER == "smtp":
            _send_smtp(to, subject, html, text, unsubscribe_url)
        else:
            OUTBOX.append({"to": to, "subject": subject, "html": html, "text": text,
                           "unsubscribe_url": unsubscribe_url})
            log.info("[console email] to=%s subject=%s\n%s", to, subject, text)
        return "sent"
    except Exception as e:
        if _is_throttle(e):
            log.warning("email provider is throttling us; will retry later (%s)", e)
            return "throttled"
        log.exception("sending email to %s failed", to)
        return "failed"
