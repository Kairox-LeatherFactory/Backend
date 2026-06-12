"""
================================================================================
modules/procurement/escalation.py — WhatsApp + Voice transport (§7b)
================================================================================

A thin transport abstraction (mirrors notifier.py's EmailBackend) for the §7
escalation ladder's two outbound rungs: a WhatsApp message, then an auto-call.

ONE transport (Twilio) for BOTH (§7b): Twilio WhatsApp + Twilio Voice is one vendor +
one SDK, vs Meta WhatsApp Cloud API (cheaper per message but a second integration +
template-approval + Business-Manager setup). At factory volume the per-message delta is
negligible; Meta-direct is the documented cost option if volume grows.

A dev `log`/`noop` driver makes the ladder testable offline (no Twilio account); the
real `twilio` driver is selected by `settings.escalation_transport` (blank-defaulted
TWILIO_* keys). Sends are blocking → callers invoke from a threadpool. Outcomes post
back to the §7 webhooks (`/webhooks/twilio/*`) which stamp the PO acknowledgement.
================================================================================
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.config import settings


class EscalationTransport(ABC):
    @abstractmethod
    def send_whatsapp(self, *, to: str, body: str) -> dict:
        """Send a WhatsApp message. Returns {ok, sid?, error?}. Never raises."""

    @abstractmethod
    def place_call(self, *, to: str, message: str) -> dict:
        """Place an auto-call (TTS reads the PO ref). Returns {ok, sid?, error?}."""


class LogEscalationTransport(EscalationTransport):
    """Default — prints so the ladder is observable in dev/tests with no Twilio."""

    def send_whatsapp(self, *, to: str, body: str) -> dict:
        print(f"💬 [whatsapp:log] to={to!r}\n{body}")
        return {"ok": True, "sid": "log-whatsapp"}

    def place_call(self, *, to: str, message: str) -> dict:
        print(f"📞 [call:log] to={to!r} says: {message!r}")
        return {"ok": True, "sid": "log-call"}


class NoopEscalationTransport(EscalationTransport):
    def send_whatsapp(self, *, to: str, body: str) -> dict:
        return {"ok": True, "sid": "noop"}

    def place_call(self, *, to: str, message: str) -> dict:
        return {"ok": True, "sid": "noop"}


class TwilioEscalationTransport(EscalationTransport):
    """Real Twilio WhatsApp + Voice (lazy import). Blocking — call from a threadpool."""

    def _client(self):
        from twilio.rest import Client
        return Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def send_whatsapp(self, *, to: str, body: str) -> dict:
        if not settings.twilio_account_sid or not settings.twilio_whatsapp_from:
            return {"ok": False, "error": "twilio_not_configured"}
        try:
            dest = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
            msg = self._client().messages.create(
                from_=settings.twilio_whatsapp_from, to=dest, body=body)
            return {"ok": True, "sid": msg.sid}
        except Exception as exc:
            print(f"⚠️  Twilio WhatsApp failed to {to!r}: {exc}")
            return {"ok": False, "error": str(exc)}

    def place_call(self, *, to: str, message: str) -> dict:
        if not settings.twilio_account_sid or not settings.twilio_voice_from:
            return {"ok": False, "error": "twilio_not_configured"}
        try:
            twiml = (f'<Response><Gather numDigits="1"><Say>{message}. '
                     f'Press 1 to confirm receipt.</Say></Gather></Response>')
            call = self._client().calls.create(
                from_=settings.twilio_voice_from, to=to, twiml=twiml)
            return {"ok": True, "sid": call.sid}
        except Exception as exc:
            print(f"⚠️  Twilio call failed to {to!r}: {exc}")
            return {"ok": False, "error": str(exc)}


_TRANSPORTS = {
    "log": LogEscalationTransport, "noop": NoopEscalationTransport,
    "twilio": TwilioEscalationTransport,
}

_instance: EscalationTransport | None = None


def get_escalation_transport() -> EscalationTransport:
    global _instance
    if _instance is None:
        cls = _TRANSPORTS.get(settings.escalation_transport.lower(), LogEscalationTransport)
        _instance = cls()
    return _instance


def reset_escalation_cache() -> None:
    """Test hook: drop the cached transport so a changed setting takes effect."""
    global _instance
    _instance = None
