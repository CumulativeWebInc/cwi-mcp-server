#!/usr/bin/env python3
"""
The Error Bar v1.0.0 — a claim validator any LLM can run.

    claim in  ->  the same claim stamped with a machine-readable confidence
                  interval, a provenance check, and a reproducibility record.

"Fake precision dies on contact."

Stdlib only. Deterministic given (input, seed): no hidden randomness.
Every stamped output carries its method_id, seed, evidence-tier inputs, and
a reproducibility statement — an output that cannot be re-run is void.

Usage:
    python3 errorbar.py stamp   --in claim.json [--seed 1337] [--at ISO] [--out stamped.json]
    python3 errorbar.py verify  --in claim.json --stamped stamped.json
    python3 errorbar.py schema

Exit codes: 0 ok | 1 verify ran, reproduced=false | 2 invalid input/usage.
"""

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone

TOOL_VERSION = "1.0.0"

METHOD_NORMAL = "eb-mc-normal/1.0"
METHOD_FORECAST = "eb-mc-forecast/1.0"
METHOD_WILSON = "eb-wilson/1.0"

DRAWS = 4096
NOMINAL_COVERAGE = 0.90
Z90 = 1.6448536269514722  # 95th percentile of standard normal (Wilson)

# Evidence tier -> implied relative std-dev for the seeded interval method,
# minimum sources the tier demands, and the public confidence label.
# These are published heuristics, honestly labeled as such — not frequentist
# coverage guarantees. The method_id says exactly which rule produced the
# interval, and the seed makes it re-runnable.
TIERS = {
    "verified":      {"rel_sd": 0.05, "min_sources": 2, "confidence": "strong"},
    "corroborated":  {"rel_sd": 0.12, "min_sources": 1, "confidence": "moderate"},
    "single-source": {"rel_sd": 0.25, "min_sources": 1, "confidence": "weak"},
    "anecdotal":     {"rel_sd": 0.50, "min_sources": 1, "confidence": "weak"},
    "none":          {"rel_sd": None, "min_sources": 0, "confidence": "insufficient-data"},
}

CLAIM_TYPES = ("stat", "forecast", "proportion")
METADATA_KEYS = ("stamped_at", "output_sha256")


def canonical(obj):
    """Canonical JSON for hashing/comparison: sorted keys, compact, ascii."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def sha256_hex(b):
    return hashlib.sha256(b).hexdigest()


def _mc_interval(point, sd, seed, coverage=NOMINAL_COVERAGE):
    """Deterministic seeded Monte Carlo interval.

    4096 draws from Normal(point, sd) via Box-Muller on MT19937 uniform
    draws (random.Random(seed); uniform draws are stable across CPython
    versions, unlike Random.gauss). Returns empirical 5th/95th percentiles.
    Same (point, sd, seed) -> byte-identical bounds, always.
    """
    rng = random.Random(seed)
    draws = []
    for _ in range(DRAWS):
        u1 = rng.random()
        u2 = rng.random()
        if u1 == 0.0:
            u1 = 2.0 ** -53
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        draws.append(point + sd * z)
    draws.sort()
    lo_q = (1.0 - coverage) / 2.0
    hi = draws[min(DRAWS - 1, int((1.0 - lo_q) * DRAWS))]
    lo = draws[min(DRAWS - 1, int(lo_q * DRAWS))]
    return lo, hi


def _wilson(p, n, coverage=NOMINAL_COVERAGE):
    """Wilson score interval for a proportion. Deterministic, seed-free."""
    z = Z90
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def provenance(claim):
    """Grade the claim's checkability BEFORE any interval is computed.

    Returns (status, checks): status is pass | flagged | insufficient-data.
    Missing sources is *flagged*, never *passed*. A tier of "none" (or an
    ungradeable claim) is insufficient-data: no interval is emitted.
    """
    checks = []
    sources = claim.get("sources") or []
    tier = claim.get("evidence_tier")
    claim_type = claim.get("claim_type")

    checks.append(_check(
        "claim_type_known", claim_type in CLAIM_TYPES,
        "claim_type '%s' routed to an interval method" % claim_type
        if claim_type in CLAIM_TYPES else
        "unknown claim_type '%s' (expected one of %s)" % (claim_type, ", ".join(CLAIM_TYPES))))

    checks.append(_check(
        "evidence_tier_known", tier in TIERS,
        "evidence tier '%s'" % tier if tier in TIERS else
        "unknown evidence_tier '%s'" % tier))

    checks.append(_check(
        "sources_present", len(sources) > 0,
        "%d source(s) listed" % len(sources) if sources else "no sources listed"))

    if tier in TIERS:
        need = TIERS[tier]["min_sources"]
        checks.append(_check(
            "tier_source_minimum", len(sources) >= need,
            "tier '%s' needs >=%d source(s), has %d" % (tier, need, len(sources))))

    bad_urls = [s for s in sources
                if not isinstance(s, dict) or not s.get("url") and not s.get("ref")]
    checks.append(_check(
        "sources_identifiable", len(bad_urls) == 0,
        "all sources have a url or ref" if not bad_urls else
        "%d source(s) lack both url and ref" % len(bad_urls)))

    obs = claim.get("observed_at")
    obs_ok, obs_detail = True, "observed_at not given (optional)"
    if obs:
        try:
            dt = datetime.fromisoformat(str(obs).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            obs_ok = dt <= now
            obs_detail = "observed_at %s" % obs if obs_ok else "observed_at %s is in the future" % obs
        except ValueError:
            obs_ok, obs_detail = False, "observed_at '%s' is not ISO-8601" % obs
    checks.append(_check("observed_at_sane", obs_ok, obs_detail))

    if tier == "none" or tier not in TIERS or claim_type not in CLAIM_TYPES:
        status = "insufficient-data"
    elif all(c["ok"] for c in checks):
        status = "pass"
    else:
        status = "flagged"
    return status, checks


def stamp(claim, seed, at=None):
    """Stamp one claim. Returns the stamped output dict."""
    if not isinstance(claim, dict):
        raise ValueError("claim must be a JSON object")
    for f in ("claim", "point_estimate", "claim_type", "evidence_tier"):
        if f not in claim:
            raise ValueError("missing required field '%s'" % f)
    point = claim["point_estimate"]
    if not isinstance(point, (int, float)) or isinstance(point, bool):
        raise ValueError("point_estimate must be a number")
    tier = claim["evidence_tier"]
    claim_type = claim["claim_type"]

    status, checks = provenance(claim)

    interval = None
    method_id = None
    method_note = None
    if status != "insufficient-data":
        if claim_type == "stat":
            sd = abs(point) * TIERS[tier]["rel_sd"]
            lo, hi = _mc_interval(point, sd, seed)
            method_id = METHOD_NORMAL
            method_note = ("4096 seeded draws from Normal(mean=point_estimate, "
                           "sd=|point|*tier_rel_sd); 5th/95th percentiles. "
                           "tier_rel_sd=%s." % TIERS[tier]["rel_sd"])
        elif claim_type == "forecast":
            horizon = claim.get("horizon_days", 30)
            if not isinstance(horizon, (int, float)) or horizon < 0:
                raise ValueError("horizon_days must be a non-negative number")
            sd = abs(point) * TIERS[tier]["rel_sd"] * (1.0 + horizon / 30.0)
            lo, hi = _mc_interval(point, sd, seed)
            method_id = METHOD_FORECAST
            method_note = ("4096 seeded draws from Normal(mean=point_estimate, "
                           "sd=|point|*tier_rel_sd*(1+horizon_days/30)); "
                           "5th/95th percentiles. horizon_days=%s." % horizon)
        elif claim_type == "proportion":
            n = claim.get("sample_n")
            if not isinstance(n, int) or n <= 0:
                raise ValueError("proportion claims require a positive integer sample_n")
            if not 0.0 <= point <= 1.0:
                raise ValueError("proportion point_estimate must be in [0,1]")
            lo, hi = _wilson(point, n)
            method_id = METHOD_WILSON
            method_note = "Wilson score interval, z=1.6449 (90%%), n=%d. Deterministic; seed carried but unused." % n
        interval = {
            "low": lo,
            "high": hi,
            "nominal_coverage": NOMINAL_COVERAGE,
            "method_id": method_id,
        }

    confidence = (TIERS[tier]["confidence"] if tier in TIERS
                  else "insufficient-data")
    if status == "insufficient-data":
        confidence = "insufficient-data"

    stamped_at = at or datetime.now(timezone.utc).isoformat()
    out = {
        "tool": "error-bar",
        "tool_version": TOOL_VERSION,
        "label": claim.get("label", "SAMPLE"),
        "claim": claim["claim"],
        "claim_type": claim_type,
        "point_estimate": point,
        "unit": claim.get("unit"),
        "interval": interval,
        "confidence_tier": confidence,
        "provenance": {
            "status": status,
            "checks": checks,
            "source_count": len(claim.get("sources") or []),
        },
        "seed": seed,
        "input_sha256": sha256_hex(canonical(claim)),
        "reproducibility": (
            "Re-run: python3 errorbar.py stamp --in <claim.json> "
            "--seed %d --at %s  ->  byte-identical output expected. "
            "Method %s: %s "
            "Fields stamped_at and output_sha256 are run metadata and are "
            "excluded from the recompute comparison performed by "
            "`errorbar.py verify`. An output that cannot be re-run is void."
            % (seed, stamped_at, method_id, method_note)),
        "stamped_at": stamped_at,
    }
    body = {k: v for k, v in out.items() if k not in METADATA_KEYS}
    out["output_sha256"] = sha256_hex(canonical(body))
    return out


def verify(claim, stamped):
    """Recompute the stamp and compare. Returns a verdict dict."""
    seed = stamped.get("seed")
    if not isinstance(seed, int):
        return {"reproduced": False, "reason": "stamped output has no integer seed"}
    at = stamped.get("stamped_at")
    try:
        recomputed = stamp(claim, seed, at=at)
    except ValueError as e:
        return {"reproduced": False, "reason": "recompute failed: %s" % e}
    a = {k: v for k, v in stamped.items() if k not in METADATA_KEYS}
    b = {k: v for k, v in recomputed.items() if k not in METADATA_KEYS}
    if canonical(a) == canonical(b):
        return {"reproduced": True, "method_id":
                (stamped.get("interval") or {}).get("method_id"),
                "input_sha256": stamped.get("input_sha256")}
    diffs = [k for k in set(a) | set(b)
             if canonical(a.get(k)) != canonical(b.get(k))]
    return {"reproduced": False, "reason": "fields differ",
            "differing_fields": sorted(diffs)}


def _load_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="errorbar.py",
                                 description="The Error Bar: stamp claims with reproducible confidence intervals.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_stamp = sub.add_parser("stamp", help="stamp a claim JSON file (or stdin with -)")
    p_stamp.add_argument("--in", dest="inp", required=True)
    p_stamp.add_argument("--seed", type=int, default=1337)
    p_stamp.add_argument("--at", dest="at", default=None,
                         help="ISO timestamp for stamped_at (default: now)")
    p_stamp.add_argument("--out", default=None)

    p_verify = sub.add_parser("verify", help="re-run a stamp and check byte-equivalence")
    p_verify.add_argument("--in", dest="inp", required=True)
    p_verify.add_argument("--stamped", required=True)

    sub.add_parser("schema", help="print the input/output JSON schema")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "schema":
            schema = json.loads(SCHEMA_JSON)
            text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
            sys.stdout.write(text)
            return 0
        claim = _load_json(args.inp)
        if args.cmd == "stamp":
            out = stamp(claim, args.seed, at=args.at)
            text = json.dumps(out, indent=2, sort_keys=True,
                              ensure_ascii=True) + "\n"
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(text)
            else:
                sys.stdout.write(text)
            return 0
        if args.cmd == "verify":
            with open(args.stamped, "r", encoding="utf-8") as f:
                stamped = json.load(f)
            verdict = verify(claim, stamped)
            sys.stdout.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
            return 0 if verdict["reproduced"] else 1
    except (ValueError, json.JSONDecodeError, OSError) as e:
        sys.stderr.write("error: %s\n" % e)
        return 2
    return 2


SCHEMA_JSON = r"""
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "error-bar/1.0.0",
  "definitions": {
    "claim_input": {
      "type": "object",
      "required": ["claim", "point_estimate", "claim_type", "evidence_tier"],
      "properties": {
        "claim": {"type": "string", "description": "the claim text, verbatim"},
        "point_estimate": {"type": "number"},
        "unit": {"type": ["string", "null"]},
        "claim_type": {"type": "string", "enum": ["stat", "forecast", "proportion"]},
        "evidence_tier": {"type": "string", "enum": ["verified", "corroborated", "single-source", "anecdotal", "none"]},
        "sources": {
          "type": "array",
          "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "ref": {"type": "string"},
            "kind": {"type": "string", "enum": ["api", "document", "human", "internal-record", "other"]}
          }}
        },
        "observed_at": {"type": "string", "description": "ISO-8601, must not be in the future"},
        "horizon_days": {"type": "number", "description": "required meaningfully for forecast claims"},
        "sample_n": {"type": "integer", "description": "required for proportion claims"},
        "seed": {"type": "integer"},
        "label": {"type": "string", "enum": ["SAMPLE", "LIVE"], "default": "SAMPLE",
                   "description": "LIVE only for CWI-verified facts; everything else is SAMPLE"}
      }
    },
    "stamped_output": {
      "type": "object",
      "required": ["tool", "tool_version", "claim", "claim_type", "point_estimate",
                   "interval", "confidence_tier", "provenance", "seed",
                   "input_sha256", "output_sha256", "reproducibility", "stamped_at"],
      "properties": {
        "tool": {"const": "error-bar"},
        "tool_version": {"type": "string"},
        "label": {"type": "string", "enum": ["SAMPLE", "LIVE"]},
        "claim": {"type": "string"},
        "claim_type": {"type": "string"},
        "point_estimate": {"type": "number"},
        "unit": {"type": ["string", "null"]},
        "interval": {"type": ["object", "null"], "properties": {
          "low": {"type": "number"}, "high": {"type": "number"},
          "nominal_coverage": {"type": "number"}, "method_id": {"type": "string"}
        }},
        "confidence_tier": {"type": "string", "enum": ["strong", "moderate", "weak", "insufficient-data"]},
        "provenance": {"type": "object", "properties": {
          "status": {"type": "string", "enum": ["pass", "flagged", "insufficient-data"]},
          "checks": {"type": "array"},
          "source_count": {"type": "integer"}
        }},
        "seed": {"type": "integer"},
        "input_sha256": {"type": "string"},
        "output_sha256": {"type": "string"},
        "reproducibility": {"type": "string"},
        "stamped_at": {"type": "string"}
      }
    }
  }
}
"""

if __name__ == "__main__":
    # Entry-point guard: the module must stay importable (tests import
    # stamp()/verify() directly), so main() runs only when this file is the
    # invoked entry point.
    from os.path import realpath
    if sys.argv and realpath(sys.argv[0]) == realpath(__file__):
        sys.exit(main())
