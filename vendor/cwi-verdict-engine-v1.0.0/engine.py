#!/usr/bin/env python3
"""CWI Verdict Engine v1.0.0 — deterministic, evidence-bound trust scoring.

Implements trust/spec.md. Stdlib only. Same inputs -> byte-identical outputs.
Never invents a score: insufficient or disputed evidence yields a refusal
status, never a number.

Usage:
    python3 engine.py < input.json > output.json
    python3 engine.py input.json > output.json

Exit codes: 0 = scored or clean refusal (insufficient-data, evidence-disputed,
unknown-context); 2 = invalid-input (refusal JSON still emitted on stdout).
"""

import hashlib
import json
import sys

ENGINE_NAME = "cwi-verdict-engine"
ENGINE_VERSION = "1.0.0"
SPEC_VERSION = "1.0.0"

FAMILIES = ("erc8004", "needle_drop", "first_spin")

WEIGHT_CLASSES = {1: "self-asserted", 2: "third-party-attested",
                  3: "protocol-or-ledger-sealed"}

SELF_ASSERTION_FACTOR = 0.5
ISSUER_CAP_PER_FAMILY = 2
SATURATION_K = 3.0

CONTEXTS = {
    "agent-trust": {
        "description": "General trust in an agent as an ecosystem participant.",
        "min_verified": 3,
        "min_families": 2,
        "required_families": ["erc8004"],
        "required_any_of": [],
        "rationale": ("General trust requires an anchored identity (ERC-8004) "
                      "plus corroboration from at least one more family."),
    },
    "music-review": {
        "description": "Trust in an agent as a music evaluator/curator.",
        "min_verified": 2,
        "min_families": 1,
        "required_families": [],
        "required_any_of": ["first_spin", "needle_drop"],
        "rationale": ("Music-domain trust requires music-domain evidence: "
                      "published First Spin verdicts or verified Needle Drop history."),
    },
    "payments": {
        "description": "Trust in an agent as a payment counterparty.",
        "min_verified": 2,
        "min_families": 2,
        "required_families": ["erc8004", "needle_drop"],
        "required_any_of": [],
        "rationale": ("Payments require a verifiable identity anchor plus "
                      "completed commercial history."),
    },
}

BANDS = [
    (0.80, "established"),
    (0.60, "emerging"),
    (0.40, "thin"),
    (0.20, "weak"),
    (0.00, "negligible"),
]

VALID_STATUSES = ("verified", "claimed", "pending", "disputed", "refuted")
VALID_ISSUER_TYPES = ("self", "third_party", "protocol")


def _fail(message):
    return {"_invalid": message}


def _validate(doc):
    """Return None if valid, else an error string. Deterministic."""
    if not isinstance(doc, dict):
        return "input must be a JSON object"
    if doc.get("engine_version") != ENGINE_VERSION:
        return ("engine_version must be %r, got %r"
                % (ENGINE_VERSION, doc.get("engine_version")))
    subject = doc.get("subject")
    if not isinstance(subject, dict) or not subject.get("agent_id") \
            or not isinstance(subject["agent_id"], str):
        return "subject.agent_id must be a non-empty string"
    if not isinstance(doc.get("context"), str):
        return "context must be a string"
    if not isinstance(doc.get("observed_at"), str) or not doc["observed_at"]:
        return "observed_at must be a non-empty string (ISO-8601)"
    signals = doc.get("signals")
    if not isinstance(signals, dict):
        return "signals must be an object"
    if sorted(signals.keys()) != sorted(FAMILIES):
        return ("signals must contain exactly the families %s, got %s"
                % (list(FAMILIES), sorted(signals.keys())))
    for fam in FAMILIES:
        items = signals[fam]
        if not isinstance(items, list):
            return "signals.%s must be a list" % fam
        seen = set()
        for i, e in enumerate(items):
            where = "signals.%s[%d]" % (fam, i)
            if not isinstance(e, dict):
                return "%s must be an object" % where
            eid = e.get("evidence_id")
            if not isinstance(eid, str) or not eid:
                return "%s.evidence_id must be a non-empty string" % where
            if eid in seen:
                return "%s: duplicate evidence_id %r" % (where, eid)
            seen.add(eid)
            for f in ("kind", "issuer", "description"):
                if not isinstance(e.get(f), str) or not e[f]:
                    return "%s.%s must be a non-empty string" % (where, f)
            if e.get("issuer_type") not in VALID_ISSUER_TYPES:
                return ("%s.issuer_type must be one of %s"
                        % (where, list(VALID_ISSUER_TYPES)))
            if e.get("status") not in VALID_STATUSES:
                return ("%s.status must be one of %s"
                        % (where, list(VALID_STATUSES)))
            if e.get("weight_class") not in WEIGHT_CLASSES:
                return ("%s.weight_class must be one of %s"
                        % (where, sorted(WEIGHT_CLASSES)))
            if not isinstance(e.get("observed_at"), str) or not e["observed_at"]:
                return "%s.observed_at must be a non-empty string" % where
            cluster = e.get("identity_cluster")
            if cluster is not None and (not isinstance(cluster, str) or not cluster):
                return ("%s.identity_cluster must be a non-empty string or null"
                        % where)
    notes = doc.get("evidence_notes")
    if notes is not None and not isinstance(notes, dict):
        return "evidence_notes must be an object if present"
    return None


def _canonical_hash(doc):
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _process_family(items):
    """Apply admission + Sybil damping. Returns the family trace dict."""
    not_counted = []
    verified = []
    for e in items:
        if e["status"] == "verified":
            verified.append(e)
        else:
            not_counted.append({
                "evidence_id": e["evidence_id"],
                "status": e["status"],
                "reason": "status is not verified — contributes 0",
            })
    not_counted.sort(key=lambda d: d["evidence_id"])

    # D2: group by identity cluster (Sybil: many issuers, one controller).
    groups = {}
    for e in verified:
        key = e.get("identity_cluster") or ("issuer:" + e["issuer"])
        groups.setdefault(key, []).append(e)

    kept = []
    damped = []
    for key in sorted(groups):
        members = sorted(groups[key],
                         key=lambda e: (-e["weight_class"], e["evidence_id"]))
        for e in members[:ISSUER_CAP_PER_FAMILY]:
            factor = SELF_ASSERTION_FACTOR if e["issuer_type"] == "self" else 1.0
            kept.append((e, e["weight_class"] * factor,
                         e["issuer_type"] == "self"))
        for e in members[ISSUER_CAP_PER_FAMILY:]:
            damped.append({
                "evidence_id": e["evidence_id"],
                "reason": "issuer-cap",
                "detail": ("identity cluster %r exceeds cap of %d verified "
                           "items per family" % (key, ISSUER_CAP_PER_FAMILY)),
            })
    kept.sort(key=lambda t: t[0]["evidence_id"])
    damped.sort(key=lambda d: d["evidence_id"])

    kept_weight = sum(w for _, w, _ in kept)
    self_discounted = [e["evidence_id"] for e, _, s in kept if s]
    family_score = kept_weight / (kept_weight + SATURATION_K)

    return {
        "evidence_submitted": len(items),
        "verified_count": len(verified),
        "kept_count": len(kept),
        "kept_weight": kept_weight,
        "self_assertion_discounted": self_discounted,
        "damped": damped,
        "not_counted": not_counted,
        "kept_evidence_ids": [e["evidence_id"] for e, _, _ in kept],
        "family_score": family_score,
    }


def _band_for(score):
    for threshold, band in BANDS:
        if score >= threshold:
            return band
    return "negligible"  # unreachable; kept for totality


def score(doc):
    """Score a validated input doc. Returns the output dict (insertion-ordered)."""
    context = doc["context"]
    input_sha = _canonical_hash(doc)

    base = {
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "spec_version": SPEC_VERSION,
        "subject": {
            "agent_id": doc["subject"]["agent_id"],
            "display_name": doc["subject"].get("display_name"),
        },
        "context": context,
        "observed_at": doc["observed_at"],
        "input_sha256": input_sha,
    }

    if context not in CONTEXTS:
        base["status"] = "unknown-context"
        base["score"] = None
        base["band"] = None
        base["missing"] = ["context %r is not in the context registry %s"
                           % (context, sorted(CONTEXTS))]
        base["families"] = {}
        base["notes"] = doc.get("evidence_notes") or {}
        return base

    # Dispute check precedes everything: a live dispute is a refusal.
    disputed = sorted({
        e["evidence_id"]
        for fam in FAMILIES
        for e in doc["signals"][fam]
        if e["status"] == "disputed"
    })
    families = {fam: _process_family(doc["signals"][fam]) for fam in FAMILIES}

    if disputed:
        base["status"] = "evidence-disputed"
        base["score"] = None
        base["band"] = None
        base["missing"] = ["resolve disputed evidence before scoring: %s"
                           % ", ".join(disputed)]
        base["families"] = families
        base["notes"] = doc.get("evidence_notes") or {}
        return base

    cfg = CONTEXTS[context]
    verified_total = sum(f["verified_count"] for f in families.values())
    fams_with_verified = sorted(f for f in FAMILIES
                                if families[f]["verified_count"] > 0)

    missing = []
    for fam in cfg["required_families"]:
        if families[fam]["verified_count"] == 0:
            missing.append(
                "%s: 0 verified evidence items (required by context %r)"
                % (fam, context))
    if cfg["required_any_of"] and not any(
            families[f]["verified_count"] > 0 for f in cfg["required_any_of"]):
        missing.append(
            "none of %s has verified evidence (context %r requires at least one)"
            % (cfg["required_any_of"], context))
    if verified_total < cfg["min_verified"]:
        missing.append("verified evidence items: %d found, %d required by context %r"
                       % (verified_total, cfg["min_verified"], context))
    if len(fams_with_verified) < cfg["min_families"]:
        missing.append("families with verified evidence: %d found, %d required by context %r"
                       % (len(fams_with_verified), cfg["min_families"], context))

    base["gate"] = {
        "min_verified": cfg["min_verified"],
        "verified_found": verified_total,
        "min_families": cfg["min_families"],
        "families_with_verified": fams_with_verified,
        "required_families": cfg["required_families"],
        "required_any_of": cfg["required_any_of"],
        "rationale": cfg["rationale"],
        "met": not missing,
    }

    if missing:
        base["status"] = "insufficient-data"
        base["score"] = None
        base["band"] = None
        base["missing"] = missing
        base["families"] = families
        base["notes"] = doc.get("evidence_notes") or {}
        return base

    scored = [families[f]["family_score"] for f in FAMILIES
              if families[f]["kept_count"] > 0]
    final = round(sum(scored) / len(scored), 3)
    base["status"] = "scored"
    base["score"] = final
    base["band"] = _band_for(final)
    base["missing"] = []
    base["families"] = families
    base["notes"] = doc.get("evidence_notes") or {}
    return base


def run(doc):
    err = _validate(doc)
    if err is not None:
        out = {
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "spec_version": SPEC_VERSION,
            "subject": (doc.get("subject") if isinstance(doc, dict) else None),
            "context": (doc.get("context") if isinstance(doc, dict) else None),
            "observed_at": (doc.get("observed_at") if isinstance(doc, dict) else None),
            "input_sha256": None,
            "status": "invalid-input",
            "score": None,
            "band": None,
            "missing": [err],
            "families": {},
            "notes": {},
        }
        return out, 2
    return score(doc), 0


def main(argv):
    if len(argv) > 2:
        sys.stderr.write("usage: engine.py [input.json]\n")
        return 2
    try:
        if len(argv) == 2:
            with open(argv[1], encoding="utf-8") as f:
                doc = json.load(f)
        else:
            doc = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write("could not read input JSON: %s\n" % exc)
        return 2
    out, code = run(doc)
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
