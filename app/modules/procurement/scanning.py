"""
================================================================================
modules/procurement/scanning.py — Virus scan (ClamAV, env-gated) (Stage 1 §7)
================================================================================

POLICY
    - Flag VIRUS_SCAN_ENABLED (config). ON in staging/prod, OFF in dev.
    - ON  : stream the bytes to clamd over its INSTREAM protocol.
              clean    -> ScanStatus.CLEAN  (caller promotes quarantine→submissions)
              hit      -> ScanStatus.INFECTED (reject 422, delete quarantined object)
              clamd unreachable -> FAIL CLOSED: raise ScannerUnavailable (503), nothing
                         is ever promoted. There is no "scan later".
    - OFF : skip; ScanStatus.SKIPPED. The completeness gate treats skipped as
            acceptable so dev can exercise the full flow.

NO LIBRARY DEPENDENCY
    We speak clamd's INSTREAM wire protocol directly over a TCP socket rather than
    add a `clamd` dependency: send `zINSTREAM\\0`, then length-prefixed chunks, then
    a zero-length terminator; clamd replies `stream: OK` or `stream: <sig> FOUND`.
    This keeps requirements.txt unchanged and the dev/test path dependency-free.

TESTABILITY
    `scan_bytes` takes an optional `scanner` callable so tests can inject a fake
    (e.g. detect the EICAR string) without a running clamd — mirroring how the
    intelligence module injects a fake chat model. The EICAR test string must be
    reported INFECTED when the flag is ON (acceptance §9.7).

FUNCTION GUIDE  (pure-ish + sync; called by pipeline.process_upload)
  EICAR   the standard harmless test signature — the fake-scanner trigger + test fixture.
  ScannerUnavailable   raised when clamd is unreachable while required → the pipeline maps to 503.
  Scanner   the callable type: bytes → (is_infected, signature_or_None).
  clamd_instream_scanner(host, port, timeout?) -> Scanner
      Build a scanner that speaks clamd's INSTREAM wire protocol over TCP (no `clamd` dep).
  _default_scanner() -> Scanner   [private] the configured real clamd scanner.
  scan_bytes(data, *, scanner?) -> (ScanStatus, signature)
      THE ENTRY POINT. Disabled → (SKIPPED, None); enabled → run the scanner, fail CLOSED on
      unreachable. CALLED FROM: pipeline.process_upload (step 1).
================================================================================
"""
from __future__ import annotations

import socket
from typing import Callable

from app.core.config import settings
from app.modules.procurement.enums import ScanStatus

# The standard EICAR anti-malware test signature. Harmless; every real scanner
# flags it. Used as the deterministic fake-scanner trigger and in tests.
EICAR = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


class ScannerUnavailable(RuntimeError):
    """clamd could not be reached while scanning was required (fail-closed → 503)."""


# A scanner is a callable: bytes -> (is_infected, signature_or_None).
Scanner = Callable[[bytes], "tuple[bool, str | None]"]


def clamd_instream_scanner(host: str, port: int, timeout: float = 30.0) -> Scanner:
    """Build a scanner that talks clamd's INSTREAM protocol over TCP. Raises
    ScannerUnavailable on any socket error (fail-closed)."""

    def _scan(data: bytes) -> tuple[bool, str | None]:
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall(b"zINSTREAM\0")
                # length-prefixed chunks, big-endian uint32, then a 0-length end.
                for i in range(0, len(data), 8192):
                    chunk = data[i : i + 8192]
                    s.sendall(len(chunk).to_bytes(4, "big") + chunk)
                s.sendall((0).to_bytes(4, "big"))
                resp = b""
                while True:
                    buf = s.recv(4096)
                    if not buf:
                        break
                    resp += buf
                    if b"\0" in buf or resp.endswith(b"\n"):
                        break
        except OSError as exc:  # connection refused / timeout / DNS / reset
            raise ScannerUnavailable(f"clamd unreachable at {host}:{port}: {exc}") from exc

        text = resp.decode("utf-8", "replace").strip().strip("\x00")
        if "FOUND" in text:
            sig = text.split(":", 1)[-1].replace("FOUND", "").strip() or "unknown"
            return True, sig
        return False, None

    return _scan


def _default_scanner() -> Scanner:
    return clamd_instream_scanner(settings.clamd_host, settings.clamd_port)


def scan_bytes(data: bytes, *, scanner: Scanner | None = None) -> tuple[ScanStatus, str | None]:
    """Scan `data` per the VIRUS_SCAN_ENABLED policy.

    Returns (status, signature). When disabled → (SKIPPED, None) without any
    socket call. When enabled → uses `scanner` (default: real clamd), failing
    CLOSED with ScannerUnavailable if the scanner cannot be reached.
    """
    print("=" * 50)
    print("ENTERED scan_bytes")
    print("virus_scan_enabled =", settings.virus_scan_enabled)
    print("type =", type(settings.virus_scan_enabled))
    print("=" * 50)

    if not settings.virus_scan_enabled:
        print("SKIPPING VIRUS SCAN")
        return ScanStatus.SKIPPED, None

    print("RUNNING VIRUS SCAN")

    scan = scanner or _default_scanner()
    infected, sig = scan(data)

    if infected:
        return ScanStatus.INFECTED, sig

    return ScanStatus.CLEAN, None
