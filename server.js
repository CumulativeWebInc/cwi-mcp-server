#!/usr/bin/env node
/**
 * cwi-mcp-server — stdio MCP server exposing CWI's real tools.
 *
 * Zero-dependency Node implementation of the MCP JSON-RPC 2.0-over-stdio
 * profile: initialize, notifications/initialized, tools/list, tools/call.
 *
 * SAFETY CONTRACT (read-only): this server exposes NO write tools. Every
 * ledger path goes through the canonical CLI's read-only exports
 * (readState). No presence heartbeats, no task creation, no state mutation,
 * no signing. Trust-verdict runs the vendored engine locally on
 * caller-supplied evidence; needle-drop exposes verify() only.
 *
 * Dependencies: none (node stdlib only). Python3 is shell-out'd for the
 * vendored trust-verdict engine and the canonical NEEDLE DROP ledger.py
 * (both stdlib-only).
 */
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { realpathSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';

const HERE = dirname(fileURLToPath(import.meta.url));
const LEDGER_CLI = join(homedir(), 'workspace/gear-ledger/cli/ledger.js');
// Vendored: the NEEDLE DROP verifier + schema + example ledger ship with the
// repo, so needledrop_verify works on a stranger's machine with zero setup.
const NEEDLE_DROP_DIR = join(HERE, 'vendor', 'needledrop');
const NEEDLE_DROP_LEDGER_PY = join(NEEDLE_DROP_DIR, 'ledger.py');
const NEEDLE_DROP_LEDGER = join(NEEDLE_DROP_DIR, 'example-ledger.json');
const VERDICT_ENGINE = join(HERE, 'vendor/cwi-verdict-engine-v1.0.0/engine.py');

const SERVER_NAME = 'cwi-mcp-server';
const SERVER_VERSION = '0.2.0';
const PROTOCOL_VERSION = '2024-11-05';

// ---------------------------------------------------------------- ledger ----
// The Gear Ledger is read two ways:
//   1. Canonical CLI (CWI infrastructure): ~/workspace/gear-ledger/cli/ledger.js
//      exposes readState() -> {doc, sha} through the GitHub Contents API.
//   2. Public fallback: CumulativeWebInc/gear-ledger is a PUBLIC repo, so any
//      stranger reads the same state.json bytes from the raw CDN, no auth.
//      The sha comes from the public commits API. Same data, read-only,
//      zero secrets.

const LEDGER_PUBLIC_STATE_URL =
  'https://raw.githubusercontent.com/CumulativeWebInc/gear-ledger/main/state.json';
const LEDGER_PUBLIC_SHA_URL =
  'https://api.github.com/repos/CumulativeWebInc/gear-ledger/commits/main';

function fetchText(url, redirects = 3) {
  return import('node:https').then(
    ({ get }) =>
      new Promise((resolve, reject) => {
        const req = get(
          url,
          { headers: { 'User-Agent': 'cwi-mcp-server/0.2.0' } },
          (res) => {
            if (
              res.statusCode >= 300 &&
              res.statusCode < 400 &&
              res.headers.location &&
              redirects > 0
            ) {
              res.resume();
              resolve(fetchText(res.headers.location, redirects - 1));
              return;
            }
            if (res.statusCode !== 200) {
              res.resume();
              reject(new Error(`HTTP ${res.statusCode} fetching ${url}`));
              return;
            }
            let body = '';
            res.setEncoding('utf8');
            res.on('data', (c) => {
              body += c;
            });
            res.on('end', () => resolve(body));
          }
        );
        req.on('error', reject);
        req.setTimeout(20000, () => req.destroy(new Error('fetch timeout')));
      })
  );
}

let ledgerModule = null; // canonical CLI, when present

async function readStateSource() {
  // Returns {doc, sha, source}; source is 'cli' or 'public'. Read-only both ways.
  if (existsSync(LEDGER_CLI)) {
    if (!ledgerModule) ledgerModule = await import(LEDGER_CLI);
    const { readState } = ledgerModule;
    const { doc, sha } = readState();
    return { doc, sha, source: 'cli' };
  }
  const [stateText, shaText] = await Promise.all([
    fetchText(LEDGER_PUBLIC_STATE_URL),
    fetchText(LEDGER_PUBLIC_SHA_URL).catch(() => null),
  ]);
  const doc = JSON.parse(stateText);
  let sha = null;
  if (shaText) {
    try {
      sha = JSON.parse(shaText).sha || null;
    } catch {
      sha = null;
    }
  }
  return { doc, sha, source: 'public' };
}

async function readDoc() {
  return (await readStateSource()).doc; // read-only
}

// ---------------------------------------------------------------- tools -----

const TOOLS = [
  {
    name: 'ledger_state_version',
    description:
      'Read-only: Gear Ledger version summary (version number, updated_at, repo sha, task count, agent count). No arguments. On machines without the CWI ledger CLI, reads the public CWI gear-ledger repo (no auth, read-only).',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    handler: async () => {
      const { doc, sha, source } = await readStateSource();
      return {
        version: doc.version,
        updated_at: doc.updated_at,
        sha,
        sha_source:
          source === 'cli'
            ? 'canonical ledger CLI'
            : 'public commits API (null if unreachable — the state itself still came from the public repo)',
        read_source: source, // 'cli' on CWI infra, 'public' on any stranger's machine
        tasks: doc.tasks.length,
        agents: doc.agents.length,
      };
    },
  },
  {
    name: 'ledger_state_get',
    description:
      'Read-only: full Gear Ledger state.json document (agents, tasks, presence, handoffs, approvals). Optionally pass `fields` to select top-level keys. On machines without the CWI ledger CLI, reads the public CWI gear-ledger repo (no auth, read-only).',
    inputSchema: {
      type: 'object',
      properties: {
        fields: {
          type: 'array',
          items: { type: 'string' },
          description: 'Top-level keys to include (e.g. ["agents","tasks"]). Omit for the whole document.',
        },
      },
      additionalProperties: false,
    },
    handler: async (args) => {
      const doc = await readDoc();
      if (args.fields && args.fields.length) {
        const out = {};
        for (const f of args.fields) out[f] = doc[f] ?? null;
        return out;
      }
      return doc;
    },
  },
  {
    name: 'ledger_agents',
    description:
      'Read-only: Gear Ledger agents with their latest presence heartbeat merged in (status, last seen, current task, note). No arguments. On machines without the CWI ledger CLI, reads the public CWI gear-ledger repo (no auth, read-only).',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    handler: async () => {
      const doc = await readDoc();
      const presence = doc.presence || {};
      return (doc.agents || []).map((a) => {
        const p = presence[a.agent_id] || presence[`agent:${a.agent_id}`] || null;
        return {
          agent_id: a.agent_id,
          public_name: a.public_name,
          handle: a.handle,
          department: a.department,
          role: a.role,
          status: a.status,
          presence: p
            ? { status: p.status, at: p.at, current_task_id: p.current_task_id, note: p.note }
            : null,
        };
      });
    },
  },
  {
    name: 'ledger_tasks',
    description:
      'Read-only: list Gear Ledger tasks (summaries). Optionally filter by `state` (created, assigned, in_progress, delivered, verified, cancelled, failed). On machines without the CWI ledger CLI, reads the public CWI gear-ledger repo (no auth, read-only).',
    inputSchema: {
      type: 'object',
      properties: {
        state: {
          type: 'string',
          enum: ['created', 'assigned', 'in_progress', 'delivered', 'verified', 'cancelled', 'failed'],
          description: 'Filter tasks by lifecycle state.',
        },
      },
      additionalProperties: false,
    },
    handler: async (args) => {
      const doc = await readDoc();
      let tasks = doc.tasks || [];
      if (args.state) tasks = tasks.filter((t) => t.state === args.state);
      return tasks.map((t) => ({
        task_id: t.task_id,
        type: t.type,
        title: t.title,
        state: t.state,
        assigned_to: t.assigned_to,
        priority: t.priority,
        created_by: t.created_by,
        created_at: t.created_at,
      }));
    },
  },
  {
    name: 'ledger_task_get',
    description: 'Read-only: full detail of one Gear Ledger task by task_id (including state history and artifacts). On machines without the CWI ledger CLI, reads the public CWI gear-ledger repo (no auth, read-only).',
    inputSchema: {
      type: 'object',
      properties: {
        task_id: { type: 'string', description: 'The task_id to fetch.' },
      },
      required: ['task_id'],
      additionalProperties: false,
    },
    handler: async (args) => {
      const doc = await readDoc();
      const t = (doc.tasks || []).find((x) => x.task_id === args.task_id);
      if (!t) {
        const err = new Error(`task not found: ${args.task_id}`);
        err.code = 'task.not_found';
        throw err;
      }
      return t;
    },
  },
  {
    name: 'trust_verdict',
    description:
      'Score agent trust with the CWI Verdict Engine v1.0.0 (deterministic, evidence-bound). Pass `input` as the engine input object: {engine_version, subject:{agent_id,...}, context, observed_at, signals:{erc8004:[], needle_drop:[], first_spin:[]}}. The engine NEVER invents a score: insufficient evidence yields status "insufficient-data" (score null), disputed evidence yields "evidence-disputed" — report those as-is. Inputs must be real, citable evidence.',
    inputSchema: {
      type: 'object',
      properties: {
        input: {
          type: 'object',
          description: 'Engine input document per trust/spec.md section 3.',
        },
      },
      required: ['input'],
      additionalProperties: false,
    },
    handler: async (args) => {
      if (!existsSync(VERDICT_ENGINE)) {
        throw new Error(`vendored verdict engine not found at ${VERDICT_ENGINE}`);
      }
      if (!args.input || typeof args.input !== 'object' || Array.isArray(args.input)) {
        const err = new Error('input must be a JSON object per the engine schema');
        err.code = 'verdict.bad_input';
        throw err;
      }
      const r = spawnSync('python3', [VERDICT_ENGINE], {
        input: JSON.stringify(args.input),
        encoding: 'utf8',
        timeout: 30000,
      });
      if (r.error) throw new Error(`verdict engine failed to run: ${r.error.message}`);
      let parsed;
      try {
        parsed = JSON.parse(r.stdout);
      } catch {
        throw new Error(`verdict engine returned non-JSON output: ${(r.stdout || '').slice(0, 300)}`);
      }
      return parsed; // engine emits its own exit code 2 + JSON on invalid-input; pass the JSON through
    },
  },
  {
    name: 'needledrop_verify',
    description:
      'Verify-only: check the hash-chain integrity of a NEEDLE DROP placement ledger (cwi-needledrop/v1). Recomputes every entry hash, checks prev_hash linkage, chain head, and revision-note consistency. Tampered or corrupt ledgers fail. No signing, no sealing — verification only. Defaults to the vendored example ledger (vendor/needledrop/example-ledger.json); pass an absolute `file` path to verify any other cwi-needledrop/v1 ledger, e.g. CWI\'s canonical needle-drop.json.',
    inputSchema: {
      type: 'object',
      properties: {
        file: {
          type: 'string',
          description: 'Absolute path to a cwi-needledrop/v1 ledger JSON file. Defaults to the canonical CWI needle-drop.json.',
        },
      },
      additionalProperties: false,
    },
    handler: async (args) => {
      if (!existsSync(NEEDLE_DROP_LEDGER_PY)) {
        throw new Error(`canonical NEEDLE DROP ledger.py not found at ${NEEDLE_DROP_LEDGER_PY}`);
      }
      const file = args.file ? resolve(args.file) : NEEDLE_DROP_LEDGER;
      if (!file.endsWith('.json')) {
        const err = new Error('file must be a .json ledger');
        err.code = 'needledrop.bad_file';
        throw err;
      }
      if (!existsSync(file)) {
        const err = new Error(`ledger file not found: ${file}`);
        err.code = 'needledrop.not_found';
        throw err;
      }
      const r = spawnSync('python3', [NEEDLE_DROP_LEDGER_PY, 'verify', '--file', file], {
        encoding: 'utf8',
        timeout: 30000,
      });
      if (r.error) throw new Error(`NEEDLE DROP verify failed to run: ${r.error.message}`);
      const out = (r.stdout || '').trim();
      const ok = r.status === 0 && out.startsWith('OK');
      return {
        file,
        ok,
        messages: out.replace(/^(OK|FAIL):\s*/, '').split('; ').filter(Boolean),
      };
    },
  },
];

export { TOOLS, SERVER_NAME, SERVER_VERSION, PROTOCOL_VERSION };

// --------------------------------------------------------------- JSON-RPC ---

const ERR = {
  PARSE: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL: -32603,
};

function ok(id, result) {
  return JSON.stringify({ jsonrpc: '2.0', id, result });
}
function fail(id, code, message, data = undefined) {
  const e = { code, message };
  if (data !== undefined) e.data = data;
  return JSON.stringify({ jsonrpc: '2.0', id, error: e });
}

function textResult(payload) {
  return { content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] };
}
function textError(message) {
  return { content: [{ type: 'text', text: `ERROR: ${message}` }], isError: true };
}

async function handleToolsCall(params) {
  const name = params && params.name;
  const tool = TOOLS.find((t) => t.name === name);
  if (!tool) return textError(`unknown tool: ${name}`);
  let args = (params && params.arguments) || {};
  if (typeof args !== 'object' || args === null || Array.isArray(args)) {
    return textError('tool arguments must be an object');
  }
  try {
    return textResult(await tool.handler(args));
  } catch (e) {
    return textError(e.code ? `${e.code}: ${e.message}` : e.message);
  }
}

async function dispatch(msg) {
  // Returns the response line, or null for notifications.
  if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') {
    return fail(msg && msg.id !== undefined ? msg.id : null, ERR.INVALID_REQUEST, 'invalid JSON-RPC 2.0 request');
  }
  const isNotification = msg.id === undefined || msg.id === null;
  const respond = (line) => (isNotification ? null : line);

  switch (msg.method) {
    case 'initialize':
      return respond(
        ok(msg.id, {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        })
      );
    case 'tools/list':
      return respond(
        ok(
          msg.id,
          {
            tools: TOOLS.map((t) => ({
              name: t.name,
              description: t.description,
              inputSchema: t.inputSchema,
            })),
          }
        )
      );
    case 'tools/call':
      return respond(ok(msg.id, await handleToolsCall(msg.params)));
    case 'ping':
      return respond(ok(msg.id, {}));
    default:
      if (msg.method.startsWith('notifications/')) return null; // notifications: no response
      return respond(fail(msg.id, ERR.METHOD_NOT_FOUND, `method not found: ${msg.method}`));
  }
}

async function serve() {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      process.stdout.write(fail(null, ERR.PARSE, 'parse error: request must be one JSON object per line') + '\n');
      continue;
    }
    try {
      const out = await dispatch(msg);
      if (out !== null) process.stdout.write(out + '\n');
    } catch (e) {
      if (msg.id !== undefined && msg.id !== null) {
        process.stdout.write(fail(msg.id, ERR.INTERNAL, `internal error: ${e.message}`) + '\n');
      }
    }
  }
}

const invokedAsScript = (() => {
  try {
    return process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
})();
if (invokedAsScript) serve();
