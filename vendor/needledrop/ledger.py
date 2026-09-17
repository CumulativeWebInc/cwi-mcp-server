#!/usr/bin/env python3
"""NEEDLE DROP — the agent's placement résumé.

An auditable, hash-chained sync-placement ledger. Every sync placement an
agent lands (scene/production, timestamp, terms) is logged as a sealed entry
it carries as proof of work. The moment the needle drops, on the record.

Public API:
    load(path)                      -> ledger dict
    validate_entry(entry, sealed)    -> [errors] (empty = valid)
    add_entry(path, draft, entered_by, status="pending") -> sealed entry
    verify_entry(path, placement_id, verifier, proof_url) -> sealed entry
    verify(path)                     -> (ok: bool, messages: [str])

Honesty rules, enforced in code:
  - add_entry() accepts only status "pending" or "claimed". "verified" can
    only be granted by verify_entry(), and only with a non-empty proof_url.
  - Entries are immutable once sealed; verify_entry() promotes by re-sealing
    the entry and every downstream entry, and writes an append-only revision
    note. verify() checks hashes, linkage, head, and revision consistency.
  - Any byte edited outside add_entry()/verify_entry() breaks the chain and
    fails verify(). That is the tamper-detection story.

Chain: entry_hash = sha256(canonical_json(entry minus entry_hash)),
       prev_hash = previous entry's entry_hash (or GENESIS for the first).
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

W = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(W, "needle-drop-schema.json")
FORMAT = "cwi-needledrop/v1"

try:
    import jsonschema
    from jsonschema import Draft202012Validator
    _HAS_JSONSCHEMA = True
except Exception:  # offline-safe: fall back to the built-in mirror validator
    _HAS_JSONSCHEMA = False


# ---------------------------------------------------------------- schema ----
def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _manual_validate(entry, sealed):
    """Mirror of needle-drop-schema.json $defs.draft / $defs.entry."""
    errs = []
    if not isinstance(entry, dict):
        return ["entry must be an object"]
    req = (["placement_id", "track", "production", "scene", "timestamp",
            "terms", "status", "entered_by", "entry_timestamp",
            "prev_hash", "entry_hash"] if sealed
           else ["track", "production", "scene", "timestamp",
                 "terms", "status", "entered_by"])
    for k in req:
        if k not in entry:
            errs.append(f"missing required field: {k}")
    if sealed and not re.fullmatch(r"ND-\d{4}-\d{3}", str(entry.get("placement_id", ""))):
        errs.append("placement_id must match ND-YYYY-NNN")
    for k in ("track", "production", "scene"):
        if k in entry and not str(entry[k]).strip():
            errs.append(f"{k} must be a non-empty string")
    if "timestamp" in entry and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?", str(entry.get("timestamp") or "")):
        errs.append("timestamp must be a date or datetime")
    if sealed:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z",
                             str(entry.get("entry_timestamp") or "")):
            errs.append("entry_timestamp must be UTC ISO datetime")
        if not re.fullmatch(r"(GENESIS|[0-9a-f]{64})", str(entry.get("prev_hash") or "")):
            errs.append("prev_hash must be GENESIS or a 64-hex sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("entry_hash") or "")):
            errs.append("entry_hash must be a 64-hex sha256")
        allowed_status = {"pending", "claimed", "verified"}
    else:
        allowed_status = {"pending", "claimed"}
    if entry.get("status") not in allowed_status:
        errs.append(f"status must be one of {sorted(allowed_status)}"
                    + (" at add time" if not sealed else ""))
    terms = entry.get("terms")
    if isinstance(terms, dict):
        for k in ("fee_tier", "territory", "term_length"):
            if not str(terms.get(k) or "").strip():
                errs.append(f"terms.{k} must be a non-empty string"
                            " (use \"undisclosed\" where private — never invent numbers)")
    else:
        errs.append("terms must be an object")
    pu = entry.get("proof_url")
    if pu is not None and not re.match(r"^https?://", str(pu)):
        errs.append("proof_url must be a URL or null")
    if sealed and entry.get("status") == "verified":
        if not pu:
            errs.append("verified entries require a non-empty proof_url")
        if not entry.get("verified_by"):
            errs.append("verified entries require verified_by")
    if not str(entry.get("entered_by") or "").strip():
        errs.append("entered_by must be a non-empty string")
    return errs


def validate_entry(entry, sealed=True):
    """Validate an entry against needle-drop-schema.json.

    sealed=True  -> validates a sealed ledger entry ($defs.entry)
    sealed=False -> validates a user-supplied draft ($defs.draft)
    Returns a list of error strings; empty means valid.
    """
    if _HAS_JSONSCHEMA:
        schema = load_schema()
        sub = dict(schema["$defs"]["entry" if sealed else "draft"])
        sub["$schema"] = schema.get("$schema")
        sub["$defs"] = schema["$defs"]
        return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
                for e in Draft202012Validator(sub).iter_errors(entry)]
    return _manual_validate(entry, sealed)


# ----------------------------------------------------------------- chain ----
def canonical_payload(entry):
    """The exact bytes that entry_hash covers: everything except entry_hash."""
    payload = {k: v for k, v in entry.items() if k != "entry_hash"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_hash(entry):
    return hashlib.sha256(canonical_payload(entry)).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- ledger ---
def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != FORMAT:
        raise ValueError(f"ledger format must be {FORMAT}, "
                         f"got {data.get('format')!r}")
    return data


def save(path, ledger):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _next_id(ledger):
    year = datetime.now(timezone.utc).year
    seqs = [int(e["placement_id"].split("-")[2])
            for e in ledger["placements"]
            if re.fullmatch(rf"ND-{year}-\d{{3}}", e.get("placement_id", ""))]
    return f"ND-{year}-{max(seqs, default=0) + 1:03d}"


def add_entry(path, draft, entered_by, status="pending"):
    """Validate a draft, seal it, and append it to the chain.

    Raises ValueError on invalid drafts. Never grants "verified" — that
    status is earned only through verify_entry() with proof.
    """
    if status not in ("pending", "claimed"):
        raise ValueError("add_entry() status must be 'pending' or 'claimed'; "
                         "use verify_entry() to promote to 'verified'")
    draft = copy.deepcopy(dict(draft))
    draft["status"] = status
    draft["entered_by"] = entered_by
    errs = validate_entry(draft, sealed=False)
    if errs:
        raise ValueError("draft failed schema validation: " + "; ".join(errs))

    ledger = load(path)
    entry = dict(draft)
    entry["placement_id"] = _next_id(ledger)
    entry["entry_timestamp"] = utcnow()
    entry["prev_hash"] = ledger["ledger"]["chain"]["head"]
    entry["entry_hash"] = compute_hash(entry)

    errs = validate_entry(entry, sealed=True)
    if errs:
        raise ValueError("sealed entry failed schema validation: " + "; ".join(errs))

    ledger["placements"].append(entry)
    ledger["ledger"]["chain"]["head"] = entry["entry_hash"]
    save(path, ledger)
    return entry


def verify_entry(path, placement_id, verifier, proof_url):
    """Promote a pending/claimed entry to verified, with proof.

    Re-seals the promoted entry and every downstream entry (their prev_hash
    links are re-pointed and hashes recomputed), and appends an audit note
    to ledger.revisions. verify() checks the whole thing.
    """
    if not proof_url or not str(proof_url).strip():
        raise ValueError("verify_entry() requires a non-empty proof_url — "
                         "no proof, no verified status")
    ledger = load(path)
    entries = ledger["placements"]
    idx = next((i for i, e in enumerate(entries)
                if e.get("placement_id") == placement_id), None)
    if idx is None:
        raise ValueError(f"no entry with placement_id {placement_id}")
    entry = entries[idx]
    if entry.get("status") == "verified":
        raise ValueError(f"{placement_id} is already verified")
    old_hash = entry["entry_hash"]

    entry["status"] = "verified"
    entry["proof_url"] = proof_url
    entry["verified_by"] = verifier
    entry["verified_timestamp"] = utcnow()
    errs = validate_entry(entry, sealed=True)
    if errs:
        raise ValueError("promoted entry failed schema validation: " + "; ".join(errs))

    # re-seal this entry and everything downstream of it
    for i in range(idx, len(entries)):
        e = entries[i]
        e["prev_hash"] = entries[i - 1]["entry_hash"] if i > 0 else "GENESIS"
        e["entry_hash"] = compute_hash(e)

    ledger["ledger"].setdefault("revisions", []).append({
        "placement_id": placement_id,
        "action": "promote",
        "actor": verifier,
        "timestamp": utcnow(),
        "old_hash": old_hash,
        "new_hash": entries[idx]["entry_hash"],
    })
    ledger["ledger"]["chain"]["head"] = entries[-1]["entry_hash"]
    save(path, ledger)
    return entries[idx]


def verify(path):
    """Check full chain integrity. Returns (ok, messages)."""
    msgs = []
    try:
        ledger = load(path)
    except Exception as e:
        return False, [f"load failed: {e}"]
    entries = ledger.get("placements", [])
    chain = ledger.get("ledger", {}).get("chain", {})

    prev = "GENESIS"
    for i, e in enumerate(entries):
        errs = validate_entry(e, sealed=True)
        if errs:
            msgs.append(f"{e.get('placement_id', f'entry#{i}')}: schema: {errs[0]}")
        if e.get("prev_hash") != prev:
            msgs.append(f"{e.get('placement_id', f'entry#{i}')}: prev_hash "
                        f"breaks the chain (expected link to previous entry)")
        if e.get("entry_hash") != compute_hash(e):
            msgs.append(f"{e.get('placement_id', f'entry#{i}')}: entry_hash "
                        f"does not match payload — tampered or corrupt")
        if e.get("status") == "verified" and not e.get("proof_url"):
            msgs.append(f"{e.get('placement_id', f'entry#{i}')}: verified "
                        f"without proof_url")
        prev = e.get("entry_hash", prev)

    expected_head = entries[-1]["entry_hash"] if entries else "GENESIS"
    if chain.get("head") != expected_head:
        msgs.append(f"chain head {chain.get('head')} does not match "
                    f"last entry hash {expected_head}")

    # revision audit notes must be consistent with the entries
    by_id = {e.get("placement_id"): e for e in entries}
    for r in ledger.get("ledger", {}).get("revisions", []):
        e = by_id.get(r.get("placement_id"))
        if not e:
            msgs.append(f"revision for unknown placement {r.get('placement_id')}")
            continue
        if e.get("status") != "verified":
            msgs.append(f"revision promote note for non-verified entry "
                        f"{r.get('placement_id')}")
        if r.get("new_hash") != e.get("entry_hash"):
            msgs.append(f"revision new_hash mismatch for {r.get('placement_id')}")

    ok = not msgs
    return ok, msgs or ["chain intact"]


# ------------------------------------------------------------------- CLI ----
def main(argv=None):
    ap = argparse.ArgumentParser(description="NEEDLE DROP placement ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="seal and append a draft entry")
    p.add_argument("--file", default=os.path.join(W, "needle-drop.json"))
    p.add_argument("--draft", required=True, help="JSON file with the draft fields")
    p.add_argument("--by", required=True, help="who enters it (agent id)")
    p.add_argument("--status", default="pending", choices=["pending", "claimed"])

    p = sub.add_parser("promote", help="promote an entry to verified with proof")
    p.add_argument("--file", default=os.path.join(W, "needle-drop.json"))
    p.add_argument("--id", required=True)
    p.add_argument("--by", required=True, help="who verifies it")
    p.add_argument("--proof", required=True, help="proof URL")

    p = sub.add_parser("verify", help="check chain integrity")
    p.add_argument("--file", default=os.path.join(W, "needle-drop.json"))

    p = sub.add_parser("show", help="print the ledger")
    p.add_argument("--file", default=os.path.join(W, "needle-drop.json"))

    a = ap.parse_args(argv)
    try:
        if a.cmd == "add":
            draft = json.load(open(a.draft, encoding="utf-8"))
            entry = add_entry(a.file, draft, a.by, a.status)
            print(json.dumps(entry, indent=2, ensure_ascii=False))
        elif a.cmd == "promote":
            entry = verify_entry(a.file, a.id, a.by, a.proof)
            print(json.dumps(entry, indent=2, ensure_ascii=False))
        elif a.cmd == "verify":
            ok, msgs = verify(a.file)
            print(("OK" if ok else "FAIL") + ": " + "; ".join(msgs))
            sys.exit(0 if ok else 1)
        elif a.cmd == "show":
            print(json.dumps(load(a.file), indent=2, ensure_ascii=False))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
