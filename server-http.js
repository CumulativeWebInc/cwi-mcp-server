#!/usr/bin/env node
/**
 * server-http.js — Streamable-HTTP (stateless) front-end for cwi-mcp-server.
 *
 * Lets any MCP client that speaks HTTP (Meta Muse custom connectors, Claude,
 * Cursor, …) reach the exact same read-only tools as the stdio server, with
 * zero code duplication: every request is dispatched through server.js's
 * handleMessage(). Still zero dependencies — node stdlib only.
 *
 * Endpoints:
 *   POST /mcp    JSON-RPC 2.0 over HTTP (stateless; no session id required).
 *                Notifications (no id) -> 202 Accepted, empty body.
 *   GET  /mcp    405 (this server is stateless; it opens no SSE streams).
 *   GET  /health -> {"ok":true}
 *   GET  /       -> human-readable info page.
 *
 * Safety: read-only tools only (inherited from server.js). 1 MB body cap,
 * 60 req/min/IP rate limit, single-message requests only (no batches).
 *
 *   PORT=3000 BIND=0.0.0.0 node server-http.js
 */

import { createServer } from 'node:http';
import { handleMessage, TOOLS, SERVER_NAME, SERVER_VERSION } from './server.js';

const PORT = parseInt(process.env.PORT || '3000', 10);
const BIND = process.env.BIND || '0.0.0.0';
const MAX_BODY = 1_000_000; // 1 MB

// --- tiny rate limiter: 60 requests / 60s per IP ---------------------------
const hits = new Map(); // ip -> [timestamps]
function rateLimited(ip) {
  const now = Date.now();
  const window = hits.get(ip) || [];
  const fresh = window.filter((t) => now - t < 60_000);
  fresh.push(now);
  hits.set(ip, fresh);
  if (hits.size > 10_000) hits.clear();
  return fresh.length > 60;
}

// --- helpers ----------------------------------------------------------------
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function jsonRpcError(id, code, message) {
  return { jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } };
}

// If the client only accepts SSE, frame the single JSON response as an event.
function sendJson(res, status, obj, sse) {
  if (sse) {
    const body = `data: ${JSON.stringify(obj)}\n\n`;
    res.writeHead(status, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    res.end(body);
  } else {
    const body = JSON.stringify(obj);
    res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
    res.end(body);
  }
}

const INFO_HTML = `<!doctype html><html><head><meta charset="utf-8"><title>${SERVER_NAME}</title></head>
<body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px">
<h1>${SERVER_NAME} <small>v${SERVER_VERSION}</small></h1>
<p>CWI's read-only MCP server over streamable HTTP. ${TOOLS.length} tools, zero secrets, nothing to write.</p>
<p><b>MCP endpoint:</b> <code>POST /mcp</code> (JSON-RPC 2.0)</p>
<p><b>Tools:</b> ${TOOLS.map((t) => `<code>${t.name}</code>`).join(' ')}</p>
<p>For Meta Muse: tell it this page's <code>/mcp</code> URL as a custom connector.</p>
</body></html>`;

// --- server -----------------------------------------------------------------
const server = createServer(async (req, res) => {
  const ip = req.socket.remoteAddress || 'unknown';
  if (rateLimited(ip)) {
    res.writeHead(429, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'rate limited: 60 req/min' }));
    return;
  }

  const url = new URL(req.url, 'http://x');

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, server: SERVER_NAME, version: SERVER_VERSION }));
    return;
  }

  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(INFO_HTML);
    return;
  }

  if (url.pathname === '/mcp') {
    if (req.method === 'GET' || req.method === 'DELETE') {
      // Stateless server: no SSE streams, no sessions to terminate.
      res.writeHead(405, { 'Content-Type': 'application/json', Allow: 'POST' });
      res.end(JSON.stringify({ error: 'method not allowed; use POST /mcp (stateless)' }));
      return;
    }
    if (req.method !== 'POST') {
      res.writeHead(405, { 'Content-Type': 'application/json', Allow: 'POST' });
      res.end(JSON.stringify({ error: 'method not allowed' }));
      return;
    }

    let msg;
    try {
      const raw = await readBody(req);
      msg = JSON.parse(raw);
    } catch {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(jsonRpcError(null, -32700, 'parse error: body must be JSON')));
      return;
    }
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(jsonRpcError(null, -32600, 'invalid request: single JSON-RPC object only (no batches)')));
      return;
    }

    const accept = req.headers.accept || '';
    const sse = !accept.includes('application/json') && accept.includes('text/event-stream');

    try {
      const out = await handleMessage(msg);
      if (out === null) {
        res.writeHead(202); // notification accepted, nothing to return
        res.end();
        return;
      }
      sendJson(res, 200, out, sse);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(jsonRpcError(msg.id, -32603, `internal error: ${e.message}`)));
    }
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'not found' }));
});

server.listen(PORT, BIND, () => {
  console.error(`${SERVER_NAME}-http v${SERVER_VERSION} listening on ${BIND}:${PORT} (POST /mcp)`);
});
