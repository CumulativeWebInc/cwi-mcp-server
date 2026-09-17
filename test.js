#!/usr/bin/env node
/**
 * Test harness for cwi-mcp-server: spawns the server as a child process and
 * drives initialize -> tools/list -> tools/call over stdio, asserting on the
 * JSON-RPC responses. Exits 0 on all-green, 1 with a failure list otherwise.
 */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { realpathSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER = join(HERE, 'server.js');

let passed = 0;
let failed = 0;
const failures = [];
function check(name, cond, detail = '') {
  if (cond) {
    passed++;
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    failures.push(name + (detail ? ` — ${detail}` : ''));
    console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
  }
}

// ------------------------------------------------------------- client ------
let idCounter = 0;
let pending = new Map();
let child;
let rl;

function send(method, params, expectResponse = true) {
  const id = expectResponse ? ++idCounter : undefined;
  const msg = { jsonrpc: '2.0', method };
  if (id !== undefined) msg.id = id;
  if (params !== undefined) msg.params = params;
  child.stdin.write(JSON.stringify(msg) + '\n');
  if (!expectResponse) return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`timeout waiting for response to ${method}`));
    }, 45000);
    pending.set(id, (resp) => {
      clearTimeout(timer);
      resolve(resp);
    });
  });
}

function toolText(result) {
  const t = result && result.content && result.content[0] && result.content[0].text;
  return t || '';
}
function toolPayload(result) {
  return JSON.parse(toolText(result));
}

async function main() {
  child = spawn('node', [SERVER], { stdio: ['pipe', 'pipe', 'inherit'] });
  rl = createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    const t = line.trim();
    if (!t) return;
    let resp;
    try {
      resp = JSON.parse(t);
    } catch {
      return;
    }
    const cb = pending.get(resp.id);
    if (cb) {
      pending.delete(resp.id);
      cb(resp);
    }
  });
  child.on('exit', (code) => {
    if (code !== 0 && code !== null) {
      console.error(`server exited with code ${code}`);
      process.exit(1);
    }
  });

  console.log('protocol:');
  const init = await send('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'cwi-mcp-test', version: '0.1.0' },
  });
  check('initialize returns result', !!init.result, JSON.stringify(init).slice(0, 200));
  check(
    'initialize protocolVersion',
    init.result && init.result.protocolVersion === '2024-11-05',
    init.result && init.result.protocolVersion
  );
  check(
    'initialize serverInfo',
    init.result && init.result.serverInfo && init.result.serverInfo.name === 'cwi-mcp-server',
    JSON.stringify(init.result && init.result.serverInfo)
  );

  await send('notifications/initialized', {}, false);
  check('notification accepted (no crash)', true);

  const list = await send('tools/list', {});
  const names = (list.result && list.result.tools || []).map((t) => t.name).sort();
  const expected = [
    'ledger_agents',
    'ledger_state_get',
    'ledger_state_version',
    'ledger_task_get',
    'ledger_tasks',
    'needledrop_verify',
    'trust_verdict',
  ].sort();
  check('tools/list exposes exactly 7 tools', JSON.stringify(names) === JSON.stringify(expected), names.join(','));
  check(
    'every tool has name, description, inputSchema',
    (list.result.tools || []).every((t) => t.name && t.description && t.inputSchema && t.inputSchema.type === 'object')
  );

  console.log('ledger reads:');
  const ver = await send('tools/call', { name: 'ledger_state_version', arguments: {} });
  const verP = toolPayload(ver.result);
  check('state_version: version is a number', typeof verP.version === 'number', JSON.stringify(verP).slice(0, 120));
  check(
    'state_version: sha is 40-hex when fetchable, honestly null otherwise',
    (typeof verP.sha === 'string' && /^[0-9a-f]{40}$/.test(verP.sha)) || verP.sha === null,
    JSON.stringify(verP.sha)
  );
  check('state_version: task counts present', typeof verP.tasks === 'number' && typeof verP.agents === 'number');

  const agents = await send('tools/call', { name: 'ledger_agents', arguments: {} });
  const agentsP = toolPayload(agents.result);
  check('ledger_agents: non-empty array', Array.isArray(agentsP) && agentsP.length > 0, `len=${agentsP.length}`);
  const chief = agentsP.find((a) => a.agent_id === 'agent:MUSE_CWI');
  check('ledger_agents: agent:MUSE_CWI present with presence heartbeat', !!chief && !!chief.presence && chief.presence.status === 'online', JSON.stringify(chief && chief.presence));

  const tasks = await send('tools/call', { name: 'ledger_tasks', arguments: {} });
  const tasksP = toolPayload(tasks.result);
  check('ledger_tasks: non-empty array', Array.isArray(tasksP) && tasksP.length > 0, `len=${tasksP.length}`);
  check(
    'ledger_tasks: summary fields',
    tasksP.every((t) => t.task_id && t.title && t.state),
    JSON.stringify(tasksP[0])
  );

  const filt = await send('tools/call', { name: 'ledger_tasks', arguments: { state: 'verified' } });
  const filtP = toolPayload(filt.result);
  check(
    'ledger_tasks state filter honored',
    Array.isArray(filtP) && filtP.every((t) => t.state === 'verified'),
    `len=${filtP.length}`
  );

  const oneId = tasksP[0].task_id;
  const one = await send('tools/call', { name: 'ledger_task_get', arguments: { task_id: oneId } });
  const oneP = toolPayload(one.result);
  check('ledger_task_get: returns full task', oneP.task_id === oneId && 'state_history' in oneP);

  const missing = await send('tools/call', { name: 'ledger_task_get', arguments: { task_id: 'nope_123' } });
  check('ledger_task_get unknown id -> isError', missing.result && missing.result.isError === true);

  const sliced = await send('tools/call', { name: 'ledger_state_get', arguments: { fields: ['version', 'agents'] } });
  const slicedP = toolPayload(sliced.result);
  check('ledger_state_get fields filter', Object.keys(slicedP).sort().join(',') === 'agents,version', Object.keys(slicedP).join(','));

  console.log('trust_verdict:');
  const coldInput = {
    engine_version: '1.0.0',
    subject: { agent_id: 'TEST_AGENT' },
    context: 'agent-trust',
    observed_at: '2026-09-17T07:50:00Z',
    signals: { erc8004: [], needle_drop: [], first_spin: [] },
  };
  const cold1 = await send('tools/call', { name: 'trust_verdict', arguments: { input: coldInput } });
  const cold1P = toolPayload(cold1.result);
  check('verdict: honest insufficient-data on empty evidence', cold1P.status === 'insufficient-data' && cold1P.score === null, cold1P.status);
  const cold2 = await send('tools/call', { name: 'trust_verdict', arguments: { input: coldInput } });
  check('verdict: deterministic (byte-identical x2)', toolText(cold2.result) === toolText(cold1.result));
  check('verdict: carries input_sha256', typeof cold1P.input_sha256 === 'string' && cold1P.input_sha256.length === 64);

  const bad = await send('tools/call', { name: 'trust_verdict', arguments: { input: 'not-an-object' } });
  check('verdict: non-object input -> isError', bad.result && bad.result.isError === true);

  console.log('needledrop_verify:');
  const nd = await send('tools/call', { name: 'needledrop_verify', arguments: {} });
  const ndP = toolPayload(nd.result);
  check('needledrop: canonical ledger verifies OK', ndP.ok === true, JSON.stringify(ndP).slice(0, 200));

  // Negative control: a ledger with a tampered SEALED ENTRY must FAIL.
  // verify() covers entries + chain head + revisions, so we add an entry to
  // a scratch copy via the vendored tool, then edit the entry's payload —
  // exactly the tampering verify() is built to detect.
  const tampered = join(tmpdir(), `nd-tampered-${Date.now()}.json`);
  const srcLedger = join(HERE, 'vendor', 'needledrop', 'example-ledger.json');
  writeFileSync(tampered, readFileSync(srcLedger, 'utf8'));
  const draftFile = join(tmpdir(), `nd-draft-${Date.now()}.json`);
  writeFileSync(draftFile, JSON.stringify({
    track: 'Test Track', production: 'Test Film', scene: 'closing credits',
    timestamp: '2026-09-17T00:00', terms: { fee_tier: 'undisclosed', territory: 'undisclosed', term_length: 'undisclosed' },
  }));
  const ledgerPy = join(HERE, 'vendor', 'needledrop', 'ledger.py');
  const { spawnSync } = await import('node:child_process');
  const addR = spawnSync('python3', [ledgerPy, 'add', '--file', tampered, '--draft', draftFile, '--by', 'TEST_HARNESS'], { encoding: 'utf8' });
  check('negative-control setup: add_entry succeeded on scratch copy', addR.status === 0, (addR.stderr || '').slice(0, 200));
  const doc = JSON.parse(readFileSync(tampered, 'utf8'));
  doc.placements[0].track = 'TAMPERED TITLE — edited outside add_entry/verify_entry';
  writeFileSync(tampered, JSON.stringify(doc));
  try { rmSync(draftFile); } catch {}
  const ndBad = await send('tools/call', { name: 'needledrop_verify', arguments: { file: tampered } });
  const ndBadP = toolPayload(ndBad.result);
  check('needledrop: tampered entry fails', ndBadP.ok === false, JSON.stringify(ndBadP).slice(0, 200));
  check(
    'needledrop: failure names the entry_hash mismatch',
    !ndBadP.ok && JSON.stringify(ndBadP.messages).includes('entry_hash'),
    JSON.stringify(ndBadP.messages)
  );
  try { rmSync(tampered); } catch {}

  const ndMissing = await send('tools/call', { name: 'needledrop_verify', arguments: { file: '/tmp/does-not-exist-nd.json' } });
  check('needledrop: missing file -> isError', ndMissing.result && ndMissing.result.isError === true);

  console.log('error handling:');
  const unknownMethod = await send('nope/method', {});
  check('unknown method -> -32601', unknownMethod.error && unknownMethod.error.code === -32601);
  const unknownTool = await send('tools/call', { name: 'no_such_tool', arguments: {} });
  check('unknown tool -> isError', unknownTool.result && unknownTool.result.isError === true);

  child.stdin.end();
  await new Promise((r) => setTimeout(r, 500));
  try { child.kill(); } catch {}

  console.log(`\n${passed}/${passed + failed} tests passed${failed ? `, ${failed} FAILED` : ''}`);
  if (failures.length) {
    console.log('failures:');
    for (const f of failures) console.log('  - ' + f);
  }
  process.exit(failed ? 1 : 0);
}

const invokedAsScript = (() => {
  try {
    return process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
})();
if (invokedAsScript) main().catch((e) => { console.error('harness crashed:', e); process.exit(1); });
