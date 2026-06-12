"""
================================================================================
modules/procurement/checks.py — per-client BOM cross-checks (Stage 2 §8)
================================================================================

Validation rules live in config/bom_checks.yaml (loaded + cached here), extending
the Stage-1 config-driven posture: onboarding a client's checks is a YAML entry,
not a code branch (acceptance §11.6/§11.8). Each rule yields a non-blocking FLAG on
the draft (surfaced to the cutting manager) unless `severity: error`, which blocks
finalization.

Rule KINDS are generic engine primitives; the which/where is config:
    range                      a numeric field within [lo, hi]
    pom_pitch_monotonic        per-POM grading is consistent (non-decreasing by size)
    size_qty_sum_equals_total  Σ per-size order qty == order total
    pattern_reference_exists   the spec's pattern_reference resolved

This is PURE + SYNC; the service builds the `context` from DB rows and runs it.
================================================================================
"""
from __future__ import annotations

import os
from functools import lru_cache

import yaml

_CHECKS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config", "bom_checks.yaml",
)


@lru_cache(maxsize=1)
def load_checks(path: str | None = None) -> tuple:
    with open(path or _CHECKS_YAML, encoding="utf-8") as fh:
        return tuple(yaml.safe_load(fh) or [])


def _flag(rule: dict, ok: bool, message: str) -> dict:
    return {
        "id": rule.get("id"),
        "kind": rule.get("kind", "range" if "range" in rule else None),
        "severity": rule.get("severity", "warn"),
        "ok": ok,
        "message": message,
    }


# ── rule-kind primitives ─────────────────────────────────────────────────────
def _check_range(rule: dict, ctx: dict) -> dict:
    field = rule.get("field")
    lo, hi = rule.get("range", [None, None])
    val = (ctx.get("attributes") or {}).get(field)
    if val is None:
        return _flag(rule, True, f"{field}: not present (skipped)")
    values = val if isinstance(val, (list, tuple)) else [val]
    try:
        nums = [float(v) for v in values]
    except (TypeError, ValueError):
        return _flag(rule, True, f"{field}: non-numeric (skipped)")
    out = [v for v in nums if (lo is not None and v < lo - 1e-9) or (hi is not None and v > hi + 1e-9)]
    if out:
        return _flag(rule, False, f"{field}={nums} outside [{lo}, {hi}]")
    return _flag(rule, True, f"{field}={nums} within [{lo}, {hi}]")


def _check_pitch_monotonic(rule: dict, ctx: dict) -> dict:
    """Per POM, values across sizes (in size order) must be non-decreasing — a
    grading sanity check (a pitch should not reverse)."""
    poms = ctx.get("poms") or []
    sizes = ctx.get("sizes") or []
    bad = []
    for p in poms:
        by = p.get("by_size") or {}
        seq = [by[s] for s in sizes if s in by]
        if any(b < a - 1e-9 for a, b in zip(seq, seq[1:])):
            bad.append(p.get("pom_code"))
    if bad:
        return _flag(rule, False, f"non-monotonic grading on {bad}")
    return _flag(rule, True, "grading pitch consistent")


def _check_qty_sum(rule: dict, ctx: dict) -> dict:
    per_size = ctx.get("per_size_qty") or {}
    total = ctx.get("order_qty")
    if total is None:
        return _flag(rule, True, "order_qty unknown (skipped)")
    s = sum(per_size.values())
    if int(s) != int(total):
        return _flag(rule, False, f"Σ per-size qty ({s}) != order total ({total})")
    return _flag(rule, True, f"Σ per-size qty == order total ({total})")


def _check_pattern_ref(rule: dict, ctx: dict) -> dict:
    pr = ctx.get("pattern_reference")
    if not pr or not pr.get("pattern_code"):
        return _flag(rule, False, "no pattern_reference found on the spec")
    if not pr.get("resolved"):
        return _flag(rule, False,
                     f"pattern_reference {pr.get('pattern_code')!r} did not resolve")
    return _flag(rule, True, f"pattern_reference {pr.get('pattern_code')!r} resolved")


_KINDS = {
    "range": _check_range,
    "pom_pitch_monotonic": _check_pitch_monotonic,
    "size_qty_sum_equals_total": _check_qty_sum,
    "pattern_reference_exists": _check_pattern_ref,
}


def run_checks(client_code: str | None, ctx: dict, checks_cfg: tuple | None = None) -> list[dict]:
    """Run the configured checks for `client_code` against `ctx`. Returns a list of
    flags; the service blocks finalization iff any flag has severity 'error' and ok
    is False. Unknown client → no checks (empty list)."""
    cfg = checks_cfg if checks_cfg is not None else load_checks()
    flags: list[dict] = []
    for entry in cfg:
        if entry.get("client_code") != client_code:
            continue
        for rule in entry.get("checks", []):
            kind = rule.get("kind", "range")
            fn = _KINDS.get(kind)
            if fn is None:
                continue
            flags.append(fn(rule, ctx))
    return flags


def has_blocking_error(flags: list[dict]) -> bool:
    return any(f["severity"] == "error" and not f["ok"] for f in flags)
