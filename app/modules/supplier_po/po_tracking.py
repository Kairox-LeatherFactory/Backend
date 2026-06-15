"""
================================================================================
modules/supplier_po/po_tracking.py — self-hosted open tracking (§6)
================================================================================

PURE helpers for the self-hosted pixel + link-wrap (§6a recommends self-hosted over
Mailtrack/HubSpot/Streak/Yesware — app-owned read-state, no per-seat lock-in, no
third party sees supplier data). No DB; the service persists the events.

  - `new_token()`          a per-send opaque token embedded in the pixel + links.
  - `PIXEL_GIF`            a 1×1 transparent GIF (the open-pixel response body).
  - `pixel_url` / `wrap`   build the tracked URLs injected into the email HTML at send.
  - `sign` / `verify`      HMAC over the wrapped target so the redirect can't be turned
                           into an open redirect.
  - `inject_tracking`      rewrite an HTML body: append the pixel + wrap its links.

HONEST CAVEAT (§6b, documented): image-proxy/prefetch makes an OPEN probabilistic (a
false positive from a proxy prefetch, a false negative when images are off). A CLICK is
the reliable read signal; the ladder (§7) treats open as the soft trigger and reserves
the strong action for no-read-at-all, and any acknowledgement is the authoritative stop —
so a missed pixel only ever over-escalates by one cheap rung, never under-delivers.

FUNCTION GUIDE  (pure + sync; no DB — the service persists the resulting events)
  PIXEL_GIF        the 1×1 transparent GIF bytes (the open-pixel response body). → the /t/o route.
  new_token() -> str            a per-send opaque token. CALLED FROM: PoService.send_po.
  sign(target, secret) / verify(target, sig, secret)   HMAC tag (anti open-redirect).
  pixel_url(base, token) -> str        the open-pixel URL.
  wrap(base, token, target, secret) -> str   a click-tracking redirect URL.
  inject_tracking(html, base, token, secret) -> str
      Rewrite every link + append the pixel into the email HTML. CALLED FROM: PoService._send_email.
  unwrap_target(u) -> str       URL-decode a wrapped target (used by the /t/c redirect route).
================================================================================
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import quote, unquote

# 1×1 transparent GIF — the open-pixel body.
PIXEL_GIF: bytes = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def new_token() -> str:
    """A URL-safe per-send token (the pixel/link correlation key)."""
    return secrets.token_urlsafe(24)


def sign(target: str, secret: str) -> str:
    """Short HMAC tag over a wrapped redirect target (anti open-redirect)."""
    mac = hmac.new(secret.encode(), target.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:12]).decode().rstrip("=")


def verify(target: str, sig: str, secret: str) -> bool:
    return hmac.compare_digest(sign(target, secret), sig or "")


def pixel_url(base: str, token: str) -> str:
    return f"{base.rstrip('/')}/t/o/{token}.gif"


def wrap(base: str, token: str, target: str, secret: str) -> str:
    """Rewrite a link to the click-tracking redirect endpoint (records a `click` then
    302s to the signed target — a stronger 'read' signal than an open)."""
    return (f"{base.rstrip('/')}/t/c/{token}"
            f"?u={quote(target, safe='')}&s={sign(target, secret)}")


_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def inject_tracking(html: str, base: str, token: str, secret: str) -> str:
    """Wrap every http(s) link in the body + append the open pixel (§5c)."""
    def _sub(m: re.Match) -> str:
        url = m.group(1)
        if url.lower().startswith(("http://", "https://")):
            return f'href="{wrap(base, token, url, secret)}"'
        return m.group(0)

    wrapped = _HREF_RE.sub(_sub, html)
    pixel = f'<img src="{pixel_url(base, token)}" width="1" height="1" alt="" style="display:none">'
    if "</body>" in wrapped.lower():
        return re.sub(r"</body>", pixel + "</body>", wrapped, count=1, flags=re.IGNORECASE)
    return wrapped + pixel


def unwrap_target(u: str) -> str:
    return unquote(u or "")
