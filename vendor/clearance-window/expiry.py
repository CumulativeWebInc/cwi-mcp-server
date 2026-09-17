#!/usr/bin/env python3
"""
THE CLEARANCE WINDOW — expiry engine (the whole product).

A pure, deterministic function: grant JSON + reference time ->
one verdict + one machine-readable reason code. No daemon, no network,
no trust in the holder's clock beyond the explicit leeway rule.

Verdict precedence (first match wins):
  1. INVALID        - grant is malformed, unsigned, or tampered (hash mismatch)
  2. REVOKED        - status == "revoked" (revocation wins over expiry)
  3. NOT_YET_VALID  - reference time is before issued_at (with leeway)
  4. EXPIRED        - reference time is at or after expires_at (with leeway)
  5. SCOPE_MISMATCH - required_scope given and not inside grant scope
  6. VALID          - all checks pass

Time semantics (Kerberos/OAuth2-style, applied concretely):
  - expires_at is EXCLUSIVE: at == expires_at  -> EXPIRED
    (a grant lives for the half-open interval [issued_at, expires_at))
  - leeway_seconds models clock skew only: the verifier's own clock is
    authoritative, but a grant that lapsed <= leeway ago still reports
    VALID so honest skew doesn't strand agents. Leeway is explicit,
    named in the output, and never silently extended.

Design-masters lineage (studied, not endorsed):
  - Macaroons (Google, 2014): expiry as a first-class caveat; caveats
    attenuate only. Here the expiry caveat is the lead artifact.
  - Biscuit tokens: verification is a local, logic-based check over
    facts + rules. Here verify_grant() needs no daemon or issuer call.
  - OAuth2/JWT: exp/nbf/iat as machine-readable time bounds checked by
    the resource server against ITS clock. Here the verifier passes its
    own reference time in.
  - Kerberos: tickets are time-boxed by design; expiry is the default
    state, not an exception. Here grants die by default; nothing renews.

This module has no side effects and imports only the stdlib.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

VALID = "VALID"
EXPIRED = "EXPIRED"
NOT_YET_VALID = "NOT_YET_VALID"
REVOKED = "REVOKED"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
INVALID = "INVALID"

REQUIRED_FIELDS = (
    "grant_id",
    "grantor_urn",
    "grantee_urn",
    "scope",
    "issued_at",
    "expires_at",
    "terms_ref",
    "status",
    "hash",
)


def canonical(grant: dict) -> str:
    """Canonical JSON of the grant WITHOUT its hash field (macaroon-caveat style)."""
    body = {k: v for k, v in grant.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_hash(grant: dict) -> str:
    """SHA-256 hex of the canonical grant body."""
    return hashlib.sha256(canonical(grant).encode("utf-8")).hexdigest()


def parse_time(value) -> datetime:
    """Parse an ISO-8601 timestamp; bare datetimes pass through. Always UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def verify_grant(
    grant: dict,
    at=None,
    required_scope: str | None = None,
    leeway_seconds: int = 60,
) -> dict:
    """
    The expiry engine. Pure function.

    grant: dict shaped like grant-schema.json.
    at: reference time (datetime or ISO-8601 str). Defaults to now (UTC).
       The VERIFIER supplies this, never the holder.
    required_scope: a single scope string the caller needs (macaroon-style
       attenuation check: the need must be inside the grant).
    leeway_seconds: explicit clock-skew leeway, reported in the output.

    Returns {"verdict": ..., "reason": ..., "checked_at": ..., "leeway_seconds": ...}.
    """
    checked_at = parse_time(at) if at is not None else datetime.now(timezone.utc)
    leeway = timedelta(seconds=max(0, int(leeway_seconds)))

    def out(verdict, reason):
        return {
            "verdict": verdict,
            "reason": reason,
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "leeway_seconds": int(leeway_seconds),
        }

    # 1. Structure: all required fields present.
    if not isinstance(grant, dict):
        return out(INVALID, "grant is not a JSON object")
    missing = [f for f in REQUIRED_FIELDS if f not in grant]
    if missing:
        return out(INVALID, "missing fields: " + ",".join(missing))
    if not isinstance(grant["scope"], list) or not all(
        isinstance(s, str) for s in grant["scope"]
    ):
        return out(INVALID, "scope must be a list of strings")

    # 2. Tamper check: recompute the hash over the canonical body.
    #    (macaroon principle: any change to a caveat changes the chain.)
    try:
        expected = compute_hash(grant)
    except Exception as exc:  # pragma: no cover - defensive
        return out(INVALID, f"unhashable grant body: {exc}")
    if not isinstance(grant["hash"], str) or grant["hash"].lower() != expected:
        return out(INVALID, "hash mismatch: grant body was altered after issuance")

    # 3. Timestamps must parse.
    try:
        issued = parse_time(grant["issued_at"])
        expires = parse_time(grant["expires_at"])
    except Exception as exc:
        return out(INVALID, f"unparseable timestamp: {exc}")
    if expires <= issued:
        return out(INVALID, "expires_at must be after issued_at")

    # 4. REVOKED wins over everything else (revocation precedence).
    if str(grant["status"]).lower() == "revoked":
        return out(REVOKED, "grant was revoked by the grantor")

    # 5. NOT_YET_VALID: window has not opened (leeway forgives early reads).
    if checked_at < issued - leeway:
        return out(NOT_YET_VALID, "window has not opened yet")

    # 6. EXPIRED: window has closed. expires_at is EXCLUSIVE (boundary rule).
    #    Leeway EXTENDS the window: a grant that lapsed <= leeway ago still
    #    reads VALID, so honest clock skew never strands an agent.
    if checked_at >= expires + leeway:
        return out(EXPIRED, "window closed: reference time at or after expires_at")

    # 7. SCOPE_MISMATCH: the need must sit inside the grant (attenuation only).
    if required_scope is not None and required_scope not in grant["scope"]:
        return out(
            SCOPE_MISMATCH,
            f"required scope '{required_scope}' not granted (granted: {grant['scope']})",
        )

    return out(VALID, "window open and terms satisfied")


def mint_grant(
    grantor_urn: str,
    grantee_urn: str,
    scope: list,
    issued_at: datetime,
    expires_at: datetime,
    terms_ref: str,
    grant_id: str | None = None,
) -> dict:
    """Issue a grant dict and seal it with its hash. Used by window.py."""
    import uuid as _uuid

    grant = {
        "grant_id": grant_id or ("cw_" + _uuid.uuid4().hex[:12]),
        "grantor_urn": grantor_urn,
        "grantee_urn": grantee_urn,
        "scope": list(scope),
        "issued_at": parse_time(issued_at).isoformat().replace("+00:00", "Z"),
        "expires_at": parse_time(expires_at).isoformat().replace("+00:00", "Z"),
        "terms_ref": terms_ref,
        "status": "active",
    }
    grant["hash"] = compute_hash(grant)
    return grant


if __name__ == "__main__":
    # Read-only verify mode for shell-out callers (e.g. the CWI MCP server).
    # Reads ONE JSON object from stdin:
    #   {"grant": {...}, "at": "ISO-8601|null", "required_scope": "str|null",
    #    "leeway_seconds": 60}
    # Prints the verdict JSON to stdout. No side effects.
    import sys as _sys

    payload = json.load(_sys.stdin)
    result = verify_grant(
        payload.get("grant"),
        at=payload.get("at"),
        required_scope=payload.get("required_scope"),
        leeway_seconds=payload.get("leeway_seconds", 60),
    )
    print(json.dumps(result))
