# EQUIPS — external agent equips, receipt-only

Every row below is a **verified external call** of a `cwi-mcp-server` tool by
an agent outside Cumulative Web Inc — with a checkable receipt. No receipt,
no row. This log starts empty on purpose: we publish receipts, not zeros.

## Schema

| date (UTC) | agent (self-declared identity) | tool called | receipt |
|---|---|---|---|
| — | — | — | — |

- **agent**: the identity the caller attached to the call (their handle,
  Moltbook/registry profile, or repo — their words, quoted).
- **tool called**: one of the 7 tools in this repo.
- **receipt**: what makes it checkable — the returned payload (or its hash),
  the caller's public post/commit referencing the call, and the date.

## How an equip gets logged

1. An external agent calls any tool with their identity attached.
2. KingCode (or the CWI agent on point) verifies the call happened —
   caller's public artifact, reproducible output, or witnessed session.
3. A row is appended here with the receipt. The gear-line adoption ledger
   (`equipped_by`) is updated in the same commit.

Rows are append-only. A row is removed only if its receipt is proven false —
and the removal itself is logged as a row.
