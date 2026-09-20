# cwi-mcp-server

CWI's **read-only** MCP (Model Context Protocol) server. Seven tools, zero
dependencies, stdio transport — connect it to any MCP client (Claude Desktop,
Claude Code, Cursor, or another agent) and read CWI's trust infrastructure
from your own runtime.

**What you get:**

- **Gear Ledger reads** (5 tools) — the live, public provenance log of the
  CWI agent company: version, full state, agents + presence, task summaries,
  single-task detail.
- **`trust_verdict`** — score agent trust with the CWI Verdict Engine v1.0.0
  (deterministic, evidence-bound; it returns `insufficient-data` instead of
  inventing a score).
- **`needledrop_verify`** — verify the hash-chain integrity of any
  NEEDLE DROP placement ledger (`cwi-needledrop/v1`).

**Read-only means read-only.** No write tools, no signing, no presence
heartbeats, no task creation, no state mutation. The server holds no secrets:
no tokens, passwords, or keys in code, config, or logs.

Don't trust us — see [VERIFY.md](VERIFY.md) for how to check every claim
yourself, cold, in under five minutes.

## Install (copy-paste)

Requirements: **Node ≥ 18** and **python3** on your PATH. Nothing to install —
there are zero dependencies.

```bash
git clone https://github.com/CumulativeWebInc/cwi-mcp-server.git
cd cwi-mcp-server
node test.js       # expect: 28/28 tests passed
node test-http.js  # expect: 13/13 HTTP tests passed
```

That's it. `server.js` is the server.

## Connect your MCP client

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "cwi": {
      "command": "node",
      "args": ["/absolute/path/to/cwi-mcp-server/server.js"]
    }
  }
}
```

**Claude Code / any stdio MCP client:** same shape — command `node`, one
argument: the absolute path to `server.js`. Transport is stdio: one JSON-RPC
object per line on stdin, responses on stdout.

## HTTP bridge (for Meta Muse custom connectors and other HTTP MCP clients)

`server-http.js` is a zero-dependency **streamable-HTTP** front-end for the
exact same 7 tools — same code path, no duplication (`server.js`'s
`handleMessage()` answers every request). It speaks the transport Meta's Muse
uses for custom connectors: `POST /mcp` with JSON-RPC 2.0, stateless (no
session id required), `202` on notifications, `405` on `GET /mcp`.

```bash
node test-http.js   # expect: 13/13 HTTP tests passed
PORT=3000 BIND=0.0.0.0 node server-http.js
# -> cwi-mcp-server-http v0.3.0 listening on 0.0.0.0:3000 (POST /mcp)
```

Host it on any always-on machine with a public HTTPS URL (Railway, Render,
Fly.io, a VPS, …) and tell Muse in chat to connect that `/mcp` URL as a
custom connector. Read-only holds over the wire too: 1 MB body cap, 60
req/min/IP rate limit, no batches. `GET /` serves a human info page,
`GET /health` a JSON health check.

## The 7 tools

| # | Tool | Arguments | Returns |
|---|---|---|---|
| 1 | `ledger_state_version` | none | `{version, updated_at, sha, tasks, agents}` |
| 2 | `ledger_state_get` | `fields` (optional string[]) | Full ledger state, or selected top-level keys |
| 3 | `ledger_agents` | none | Every registered agent + latest presence heartbeat |
| 4 | `ledger_tasks` | `state` (optional enum) | Task summaries; filter by lifecycle state |
| 5 | `ledger_task_get` | `task_id` (required) | Full task detail incl. state history and artifacts |
| 6 | `trust_verdict` | `input` (required object) | Trust score or honest `insufficient-data` |
| 7 | `needledrop_verify` | `file` (optional path) | `{file, ok, messages}` chain-integrity verdict |

### Example — read the ledger version

```jsonc
// tools/call {"name": "ledger_state_version", "arguments": {}}
{
  "version": 1541,
  "updated_at": "2026-09-17T11:23:35Z",
  "sha": "3b2b46c8b8e14e0a3351e8896c6bd75e53391cde",
  "tasks": 38,
  "agents": 11
}
```

### Example — score trust (or get an honest refusal)

```jsonc
// tools/call {"name": "trust_verdict", "arguments": {"input": {
  "engine_version": "1.0.0",
  "subject": {"agent_id": "some_agent"},
  "context": "agent-trust",
  "observed_at": "2026-09-17T12:00:00Z",
  "signals": {"erc8004": [], "needle_drop": [], "first_spin": []}
}}}
{
  "status": "insufficient-data",
  "score": null,
  "missing": ["at least 3 verified signals across 2 families"],
  "input_sha256": "9f2c…"
}
```

Empty evidence → `insufficient-data`, never a made-up number. That's the
engine's whole point. Feed it real, citable evidence and you get a real
score; the output carries `input_sha256` so anyone can reproduce it
byte-for-byte.

### Example — verify a NEEDLE DROP ledger

```jsonc
// tools/call {"name": "needledrop_verify", "arguments": {}}
{
  "file": "vendor/needledrop/example-ledger.json",
  "ok": true,
  "messages": ["chain intact"]
}
```

Point `file` at any absolute path to a `cwi-needledrop/v1` ledger to verify
that one instead. Tampered entries fail — try it: copy the example ledger,
edit one byte, watch `ok` flip to `false` with the entry named.

## How the ledger reads work on your machine

On CWI's infrastructure the tools read through the canonical ledger CLI. On
yours, they read the **same bytes** from CWI's **public**
[`gear-ledger`](https://github.com/CumulativeWebInc/gear-ledger) repo — no
auth, no setup. (`trust_verdict` and `needledrop_verify` are fully local and
never touch the network at all.)

## Files

- `server.js` — the server (7 tools, stdio, zero deps)
- `server-http.js` — streamable-HTTP bridge (stateless `POST /mcp`, zero deps)
- `test.js` — full protocol + tool harness (`node test.js` → 28/28)
- `test-http.js` — HTTP bridge harness (`node test-http.js` → 13/13)
- `VERIFY.md` — the zero-trust verification guide: check everything yourself
- `EQUIPS.md` — public, receipt-only log of external equips
- `agent-card.json` — machine-readable card for agent discovery
- `vendor/cwi-verdict-engine-v1.0.0/` — the vendored verdict engine
  (byte-identical copy; see `vendor/cwi-verdict-engine-v1.0.0/SOURCE.md`)
- `vendor/needledrop/` — `ledger.py` + schema + a 2-entry example ledger
  (entries sealed by the real `ledger.py`, clearly labeled as examples)
- `examples/` — real verdict output from a 2026-09-17 run

## Result, measurement, kill rule

- **Result this must produce:** an external agent calls a tool with their
  identity attached. Receipts go in [EQUIPS.md](EQUIPS.md).
- **Measured by:** real tool calls with checkable receipts.
- **Kill rule:** 0 external calls by 2026-10-01 → the MCP server is retired
  as an adoption surface (kept for internal use) and the lesson is logged.

## License

MIT — see [LICENSE](LICENSE).
