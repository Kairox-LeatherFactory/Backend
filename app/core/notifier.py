"""
================================================================================
core/notifier.py — Pluggable email transport (Stage 3 §2c)
================================================================================

WHY AN ABSTRACTION (mirrors storage.py)
    The 2-hour escalation sends an email when an in-app BOM-review notice goes
    unseen. The repo must keep NO hard SMTP dependency and the SQLite/in-memory
    test path must run with zero external services. A thin `EmailBackend` is
    selected by `settings.email_backend`:

        log     (default, dev + tests)  -> writes to stdout, no network
        noop                            -> discards (silent)
        smtp                            -> real send via stdlib smtplib (lazy)

    `send()` is blocking for the smtp driver; callers invoke it from a threadpool
    (the house async rule, CLAUDE.md §3.3) — the sweeper does exactly that.

FUNCTION GUIDE  (mirrors storage.py: an interface + drivers + a singleton factory)
  EmailBackend   the interface: send(*, to, subject, body, html?, attachments?) -> bool.
      NEVER raises — a transport failure marks the notification FAILED, not a crash.
  LogEmailBackend   default — prints (observable offline). NoopEmailBackend — discards.
  SmtpEmailBackend  real send via stdlib smtplib (lazy). SesEmailBackend — Amazon SES (lazy boto3).
  get_notifier() -> EmailBackend   the configured singleton (defaults to log).
      CALLED FROM: bom.notification_service (the 2-hour BOM escalation email) and
      supplier_po.po_service (the PO dispatch email with the PDF attached).
  reset_notifier_cache()   test hook — drop the cached backend.
================================================================================
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.config import settings


class EmailBackend(ABC):
    @abstractmethod
    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None, attachments: list[dict] | None = None) -> bool:
        """Send one email; return True on success. Never raise to the caller — a
        transport failure marks the notification FAILED, it does not crash the sweeper.

        Stage 5 (§5c) extends the contract with an optional HTML alternative (carries
        the open-pixel + wrapped links) and `attachments` (the PO PDF): each attachment
        is `{filename, content: bytes, mime}`. Drivers that ignore them still send the
        plain-text body — the dev/test path stays simple."""


class LogEmailBackend(EmailBackend):
    """Default. Prints the email so the escalation path is observable in dev/tests
    without an SMTP server."""

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None, attachments: list[dict] | None = None) -> bool:
        atts = ", ".join(a.get("filename", "?") for a in (attachments or []))
        print(f"📧 [email:log] to={to!r} subject={subject!r}"
              f"{(' attachments=[' + atts + ']') if atts else ''}\n{body}")
        return True


class NoopEmailBackend(EmailBackend):
    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None, attachments: list[dict] | None = None) -> bool:
        return True


class SmtpEmailBackend(EmailBackend):
    """Real send via the stdlib smtplib (lazy import). Blocking — call from a
    threadpool. Supports the HTML alternative + attachments (§5c)."""

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None, attachments: list[dict] | None = None) -> bool:
        import smtplib
        from email.message import EmailMessage

        if not settings.smtp_host:
            print("⚠️  EMAIL_BACKEND=smtp but SMTP_HOST is unset — email not sent.")
            return False
        msg = EmailMessage()
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        for a in attachments or []:
            maintype, _, subtype = (a.get("mime") or "application/octet-stream").partition("/")
            msg.add_attachment(a["content"], maintype=maintype,
                               subtype=subtype or "octet-stream", filename=a.get("filename"))
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                if settings.smtp_use_tls:
                    s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
            return True
        except Exception as exc:                       # never propagate to the sweeper
            print(f"⚠️  SMTP send failed to {to!r}: {exc}")
            return False


class SesEmailBackend(EmailBackend):
    """Amazon SES (§5a, the recommended real driver) via boto3 send_raw_email (lazy
    import) so attachments + the HTML alternative ride a single raw MIME message.
    Bounce/complaint/delivery come back over SNS → the §5b webhook, not here."""

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None, attachments: list[dict] | None = None) -> bool:
        from email.message import EmailMessage

        if not settings.aws_ses_region:
            print("⚠️  EMAIL_BACKEND=ses but AWS_SES_REGION is unset — email not sent.")
            return False
        msg = EmailMessage()
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        for a in attachments or []:
            maintype, _, subtype = (a.get("mime") or "application/octet-stream").partition("/")
            msg.add_attachment(a["content"], maintype=maintype,
                               subtype=subtype or "octet-stream", filename=a.get("filename"))
        try:
            import boto3
            client = boto3.client(
                "ses", region_name=settings.aws_ses_region,
                aws_access_key_id=settings.aws_ses_access_key_id or None,
                aws_secret_access_key=settings.aws_ses_secret_access_key or None,
            )
            client.send_raw_email(RawMessage={"Data": msg.as_bytes()})
            return True
        except Exception as exc:
            print(f"⚠️  SES send failed to {to!r}: {exc}")
            return False


_BACKENDS = {"log": LogEmailBackend, "noop": NoopEmailBackend,
             "smtp": SmtpEmailBackend, "ses": SesEmailBackend}

_instance: EmailBackend | None = None


def get_notifier() -> EmailBackend:
    """Return the configured email backend (singleton). Defaults to `log`."""
    global _instance
    if _instance is None:
        cls = _BACKENDS.get(settings.email_backend.lower(), LogEmailBackend)
        _instance = cls()
    return _instance


def reset_notifier_cache() -> None:
    """Test hook: drop the cached backend so a changed setting takes effect."""
    global _instance
    _instance = None
