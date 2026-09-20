#!/usr/bin/env node
/**
 * test-http.js — conformance tests for server-http.js (streamable HTTP bridge).
 * Zero dependencies. Spawns the bridge on 127.0.0.1:0, runs JSON-RPC over
 * HTTP, asserts handshake/list/call/notification/edge behavior, kills it.
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const PORT = 18731;
const BASE = `http://127.0.0.1:${PORT}`;

let passed = 0;
let failed = 0;
function check(name, cond, extra = '') {
  if (cond) {
    passed++;
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    console.log(`  FAIL ${name} ${extra}`);
  }
}

async function post(path, body, accept = 'application/json') {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: accept },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try {
    json = text.startsWith('data:') ? JSON.parse(text.replace(/^data:\s*/, '')) : JSON.parse(text);
  } catch { /* non-JSON body */ }
  return { status: res.status, text, json, headers: res.headers };
}

async function main() {
  const child = spawn('node', [join(HERE, 'server-http.js')], {
    env: { ...process.env, PORT: String(PORT), BIND: '127.0.0.1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stderr.on('data', () => {});
  await new Promise((r) => setTimeout(r, 1200));

  try {
    // health + landing page
    const h = await fetch(`${BASE}/health`);
    check('GET /health -> 200 ok', h.status === 200 && (await h.json()).ok === true);
    const idx = await fetch(`${BASE}/`);
    check('GET / -> 200 html mentions /mcp', idx.status === 200 && (await idx.text()).includes('/mcp'));

    // initialize handshake
    const init = await post('/mcp', {
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 't', version: '0' } },
    });
    check('initialize -> 200 + serverInfo', init.status === 200 && init.json?.result?.serverInfo?.name === 'cwi-mcp-server', init.text.slice(0, 120));

    // notification -> 202 empty
    const notif = await post('/mcp', { jsonrpc: '2.0', method: 'notifications/initialized' });
    check('notification -> 202 empty', notif.status === 202 && notif.text === '');

    // tools/list -> 7 tools
    const list = await post('/mcp', { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
    const names = (list.json?.result?.tools || []).map((t) => t.name);
    check('tools/list -> 7 tools', list.status === 200 && names.length === 7, names.join(','));
    check('tools include trust_verdict + needledrop_verify',
      names.includes('trust_verdict') && names.includes('needledrop_verify'));

    // live tool call over HTTP
    const call = await post('/mcp', {
      jsonrpc: '2.0', id: 3, method: 'tools/call',
      params: { name: 'ledger_state_version', arguments: {} },
    });
    const txt = call.json?.result?.content?.[0]?.text || '';
    check('tools/call ledger_state_version -> live ledger JSON', call.status === 200 && txt.includes('"version"'), txt.slice(0, 80));

    // unknown tool -> isError, not transport failure
    const bad = await post('/mcp', {
      jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'nope', arguments: {} },
    });
    check('unknown tool -> isError', bad.status === 200 && bad.json?.result?.isError === true);

    // malformed body -> 400 JSON-RPC parse error
    const mal = await post('/mcp', '{not json');
    check('malformed JSON -> 400 parse error', mal.status === 400 && mal.json?.error?.code === -32700);

    // batch array -> 400
    const batch = await post('/mcp', [{ jsonrpc: '2.0', id: 1, method: 'ping' }]);
    check('batch array -> 400 rejected', batch.status === 400);

    // SSE-only client gets framed event
    const sse = await post('/mcp', { jsonrpc: '2.0', id: 5, method: 'ping', params: {} }, 'text/event-stream');
    check('SSE accept -> data: framed', sse.status === 200 && sse.text.startsWith('data: ') && sse.headers.get('content-type')?.includes('text/event-stream'));

    // GET /mcp -> 405 stateless
    const g = await fetch(`${BASE}/mcp`);
    check('GET /mcp -> 405', g.status === 405);

    // unknown route -> 404
    const nf = await fetch(`${BASE}/nope`);
    check('unknown route -> 404', nf.status === 404);
  } finally {
    child.kill();
  }

  console.log(`\n${passed}/${passed + failed} HTTP tests passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error('test harness error:', e.message);
  process.exit(1);
});
