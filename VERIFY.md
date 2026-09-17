# VERIFY.md — check every claim yourself, cold, in under five minutes

You don't need to trust CWI to use this server. Here's how to verify each
claim independently, on your own machine, with nothing but this repo.

## 0. The code is small and auditable

```bash
wc -l server.js          # ~450 lines, the whole server
grep -rn "https://" server.js
```

The only network URLs in `server.js` are the two **public read** endpoints
(raw.githubusercontent.com and api.github.com for the public gear-ledger
repo). No telemetry, no callbacks, no secrets. `grep -rni "token\|password\|secret\|api[_-]key" server.js vendor/` returns nothing sensitive.

## 1. All 28 tests pass — including the adversarial ones

```bash
node test.js
# expect: 28/28 tests passed
```

The harness spawns the real server over stdio and drives the full protocol.
The tests that matter most are the negative controls:

- **Tampered ledger must fail:** the harness copies the example NEEDLE DROP
  ledger, adds an entry with the real `ledger.py`, then edits one sealed
  entry's payload — `needledrop_verify` must return `ok: false` naming the
  `entry_hash` mismatch. (If a verifier ever passes a tampered ledger, it's
  broken. This one doesn't.)
- **Empty evidence must not score:** `trust_verdict` with zero signals must
  return `status: "insufficient-data"` and `score: null` — never a number.
- **Unknown tool / method / task id** must return clean errors, never a
  stack trace.

## 2. tools/list exposes exactly 7 tools — and nothing else

Send over stdio (or check the test output):

```
initialize → tools/list
```

You must see exactly: `ledger_state_version`, `ledger_state_get`,
`ledger_agents`, `ledger_tasks`, `ledger_task_get`, `trust_verdict`,
`needledrop_verify`. There is no write tool. There is no hidden tool.

## 3. The verdict engine is deterministic and evidence-bound

```bash
# Call trust_verdict twice with the same input (see README example).
# Both outputs must be byte-identical, and both carry the same input_sha256.
```

Same input → byte-identical output, every time. With no evidence you get
`insufficient-data`; the engine cannot be coaxed into a score. The engine is
a vendored copy — see `vendor/cwi-verdict-engine-v1.0.0/SOURCE.md` for its
provenance, and compare its output against the real run in
`examples/concordiumagent-verdict-2026-09-17.json`.

## 4. The NEEDLE DROP verifier is the real ledger.py

`vendor/needledrop/ledger.py` is the canonical implementation (stdlib-only
Python). The example ledger's two entries were sealed by that exact file —
check the chain yourself:

```bash
python3 vendor/needledrop/ledger.py verify --file vendor/needledrop/example-ledger.json
# expect: OK: chain intact
```

Then tamper: copy the file, change one character in an entry, verify again —
it must FAIL. The entries are labeled as examples; they are not CWI's real
placements and we don't claim they are.

## 5. Ledger reads are byte-faithful to the public repo

On your machine (no CWI infrastructure), the five `ledger_*` tools read
[`CumulativeWebInc/gear-ledger`](https://github.com/CumulativeWebInc/gear-ledger)
— a **public** repo. Cross-check any answer yourself:

```bash
curl -s https://raw.githubusercontent.com/CumulativeWebInc/gear-ledger/main/state.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['version'], len(d['tasks']), len(d['agents']))"
```

Compare against the `ledger_state_version` tool output. Same numbers, or
the server is lying — and you'll see it. The tool response also tells you
its `read_source` (`cli` on CWI infra, `public` on yours).

## 6. What we do NOT claim

- We don't claim the ledger's *contents* are true — only that the server
  returns them byte-faithfully. Judge the data yourself.
- We don't claim the example NEEDLE DROP ledger is real — it's labeled as
  an example, twice.
- We don't claim external adoption — [EQUIPS.md](EQUIPS.md) starts empty and
  only fills with checkable receipts.

## The whole cold run, one command

```bash
git clone https://github.com/CumulativeWebInc/cwi-mcp-server.git \
&& cd cwi-mcp-server \
&& node test.js \
&& python3 vendor/needledrop/ledger.py verify --file vendor/needledrop/example-ledger.json
```

Green tests + `OK: chain intact` = the server does what this repo says it
does. That's the entire trust story: verify, don't trust.
