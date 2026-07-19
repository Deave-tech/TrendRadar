// HTTP API around a warm Chromium context with BPC loaded.
//   POST /v1/fetch       -> strict cn.wsj.com article extraction
//   GET  /fetch?url=...  -> legacy general-page extraction (configurable)
//   GET  /healthz        -> browser, BPC and queue health
//   GET/PUT/DELETE /cookies -> per-site cookie store
// If API_TOKEN is set, all routes except /healthz require a bearer token.
import http from 'node:http';
import path from 'node:path';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import {
  browserConnected,
  fetchArticle,
  fetchWsjArticle,
  launchBpc,
  waitForBpcReady,
  WsjFetchError,
} from './browser.mjs';
import { v1ErrorPayload, v1SuccessPayload } from './api_contract.mjs';
import { loadStore, saveStore, upsertSite, deleteSite, findSite, maskedView } from './cookies.mjs';
import { BoundedQueue, QueueClosedError, QueueFullError } from './queue.mjs';
import { validRequestId, validateWsjArticleUrl } from './wsj.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function integerEnv(name, fallback, min = 0, max = Number.MAX_SAFE_INTEGER) {
  const raw = process.env[name];
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isInteger(value) || value < min || value > max)
    throw new Error(`${name} must be an integer from ${min} to ${max}`);
  return value;
}

const PORT = integerEnv('PORT', 8080, 1, 65535);
// Default binds localhost only. Set HOST=0.0.0.0 plus API_TOKEN and a
// firewall/reverse proxy to expose it.
const HOST = process.env.HOST || '127.0.0.1';
const API_TOKEN = process.env.API_TOKEN || '';
const PROFILE_DIR = process.env.PROFILE_DIR || path.join(__dirname, '.profile');
const COOKIES_FILE = process.env.COOKIES_FILE || path.join(__dirname, '.cookies.json');
const MAX_CONCURRENCY = integerEnv('MAX_CONCURRENCY', 2, 1, 32);
const MAX_QUEUE = integerEnv('MAX_QUEUE', 20, 0, 1000);
const WSJ_MAX_QUEUE = integerEnv('WSJ_MAX_QUEUE', 20, 0, 1000);
const PAGE_TIMEOUT_MS = integerEnv('PAGE_TIMEOUT_MS', 45000, 1000, 300000);
const SETTLE_MS = integerEnv('SETTLE_MS', 6000, 0, PAGE_TIMEOUT_MS - 1);
const ENABLE_LEGACY_FETCH = process.env.ENABLE_LEGACY_FETCH !== '0';
const PRODUCTION = process.env.NODE_ENV === 'production' || process.env.BPC_PRODUCTION === '1';
const EXTERNAL_BIND = !['127.0.0.1', '::1', 'localhost'].includes(HOST);
const REQUIRE_API_TOKEN = process.env.REQUIRE_API_TOKEN === '1' || PRODUCTION || EXTERNAL_BIND;

// Fail before Chromium is launched. Production and externally-bound instances
// must never silently expose an unauthenticated browser.
if (REQUIRE_API_TOKEN && !API_TOKEN)
  throw new Error('API_TOKEN is required in production, when REQUIRE_API_TOKEN=1, or for a non-loopback HOST');

const { context, sw } = await launchBpc(PROFILE_DIR);
const ready = await waitForBpcReady(sw);
const bpcReady = {
  sitesInStorage: ready.sitesInStorage,
  dnrSessionRules: ready.dnrSessionRules,
  checkedAt: new Date().toISOString(),
};
console.log(`BPC ready: ${bpcReady.sitesInStorage} sites, ${bpcReady.dnrSessionRules} DNR rules`);

const store = loadStore(COOKIES_FILE);
// Backwards-compatible seed: DD_COOKIE/BPC_UA define the wsj.com entry if absent.
if (process.env.DD_COOKIE && !store['wsj.com']) {
  upsertSite(store, 'wsj.com', `datadome=${process.env.DD_COOKIE}`, process.env.BPC_UA);
  saveStore(COOKIES_FILE, store);
  console.log('seeded wsj.com cookie entry from DD_COOKIE');
}
console.log(`cookie sites: ${Object.keys(store).join(', ') || '(none)'}`);

// The WSJ delivery path is intentionally serialized. Legacy fetches use a
// separate bounded queue and should be disabled on the Tokyo production unit.
const wsjQueue = new BoundedQueue(1, WSJ_MAX_QUEUE);
const legacyQueue = new BoundedQueue(MAX_CONCURRENCY, MAX_QUEUE);
let shuttingDown = false;

function sendJson(res, status, obj, extraHeaders = {}) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  res.end(body);
}

function sendV1Error(res, status, code, retryable, message, requestId, details) {
  sendJson(res, status, v1ErrorPayload(code, retryable, message, requestId, details));
}

function authorized(req) {
  if (!API_TOKEN) return true;
  const actual = req.headers.authorization;
  if (typeof actual !== 'string') return false;
  const expectedBuffer = Buffer.from(`Bearer ${API_TOKEN}`);
  const actualBuffer = Buffer.from(actual);
  return expectedBuffer.length === actualBuffer.length && timingSafeEqual(expectedBuffer, actualBuffer);
}

function readBody(req, limit = 1 << 20) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let tooLarge = false;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        tooLarge = true;
        chunks.length = 0;
      } else if (!tooLarge) {
        chunks.push(chunk);
      }
    });
    req.on('end', () => {
      if (tooLarge) reject(new Error('body too large'));
      else resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', reject);
  });
}

async function handleCookies(req, res, url) {
  if (req.method === 'GET')
    return sendJson(res, 200, { sites: maskedView(store) });
  if (req.method === 'PUT' || req.method === 'POST') {
    let body;
    try {
      body = JSON.parse(await readBody(req));
    } catch {
      return sendJson(res, 400, { error: 'invalid JSON body' });
    }
    const { site, cookie, ua } = body || {};
    if (!site || !cookie || typeof site !== 'string' || typeof cookie !== 'string') {
      return sendJson(res, 400, { error: 'need { "site": "wsj.com", "cookie": "<document.cookie>", "ua?": "..." }' });
    }
    const entry = upsertSite(store, site, cookie, ua);
    saveStore(COOKIES_FILE, store);
    console.log(`cookie updated for ${site} (${entry.cookies.length} cookies)`);
    return sendJson(res, 200, {
      ok: true,
      site: site.toLowerCase(),
      cookieCount: entry.cookies.length,
      updatedAt: entry.updatedAt,
    });
  }
  if (req.method === 'DELETE') {
    const site = url.searchParams.get('site');
    if (!site) return sendJson(res, 400, { error: 'need ?site=...' });
    const existed = deleteSite(store, site);
    if (existed) saveStore(COOKIES_FILE, store);
    return sendJson(res, existed ? 200 : 404, { ok: existed, site: site.toLowerCase() });
  }
  sendJson(res, 405, { error: 'method not allowed' }, { allow: 'GET, PUT, POST, DELETE' });
}

function serviceReady() {
  return !shuttingDown && browserConnected(context) &&
    bpcReady.sitesInStorage > 0 && bpcReady.dnrSessionRules > 0;
}

async function handleV1Fetch(req, res) {
  if (req.method !== 'POST')
    return sendV1Error(res, 405, 'METHOD_NOT_ALLOWED', false, 'POST is required', undefined, undefined);

  let body;
  try {
    body = JSON.parse(await readBody(req, 16 * 1024));
  } catch (error) {
    const message = error?.message === 'body too large' ? 'request body is too large' : 'request body must be valid JSON';
    return sendV1Error(res, 400, 'INVALID_REQUEST', false, message, undefined, undefined);
  }
  if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.url !== 'string')
    return sendV1Error(res, 400, 'INVALID_REQUEST', false, 'body must contain string fields url and requestId', undefined, undefined);
  if (!validRequestId(body.requestId))
    return sendV1Error(res, 400, 'INVALID_REQUEST', false, 'requestId must be 1-128 safe ASCII characters', undefined, undefined);
  const requestId = body.requestId;

  // A malformed URL is a parameter error; a syntactically valid URL outside
  // the allowlist is a scope/authorization error.
  try {
    new URL(body.url);
  } catch {
    return sendV1Error(res, 400, 'INVALID_REQUEST', false, 'url must be an absolute URL', requestId, undefined);
  }
  const checked = validateWsjArticleUrl(body.url);
  if (!checked.ok)
    return sendV1Error(res, 403, 'URL_NOT_ALLOWED', false, checked.reason, requestId, undefined);
  if (!serviceReady())
    return sendV1Error(res, 503, 'SERVICE_NOT_READY', true, 'browser or BPC rules are not ready', requestId, undefined);

  const entry = findSite(store, checked.url.hostname);
  try {
    const article = await wsjQueue.run(() => fetchWsjArticle(context, checked.url.href, {
      pageTimeoutMs: PAGE_TIMEOUT_MS,
      settleMs: SETTLE_MS,
      cookies: entry ? entry.cookies : null,
    }));
    return sendJson(res, 200, v1SuccessPayload(requestId, article));
  } catch (error) {
    if (error instanceof QueueFullError)
      return sendV1Error(res, 429, 'QUEUE_FULL', true, 'WSJ fetch queue is full', requestId, undefined);
    if (error instanceof QueueClosedError)
      return sendV1Error(res, 503, 'SERVICE_NOT_READY', true, 'service is shutting down', requestId, undefined);
    if (error instanceof WsjFetchError)
      return sendV1Error(
        res,
        error.httpStatus,
        error.code,
        error.retryable,
        error.message,
        requestId,
        error.details
      );
    console.error(`v1 fetch failed (requestId=${requestId}): ${error?.name || 'Error'}`);
    return sendV1Error(res, 502, 'UPSTREAM_ERROR', true, 'unexpected browser failure', requestId, undefined);
  }
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    if (url.pathname === '/healthz') {
      const wsj = wsjQueue.snapshot();
      const legacy = legacyQueue.snapshot();
      const ok = serviceReady();
      return sendJson(res, ok ? 200 : 503, {
        ok,
        code: ok ? 'OK' : 'SERVICE_NOT_READY',
        browser: { connected: browserConnected(context) },
        bpc: bpcReady,
        queue: wsj,
        legacy: { enabled: ENABLE_LEGACY_FETCH, queue: legacy },
        // Retain the original top-level counters for old probes.
        active: wsj.active + legacy.active,
        queued: wsj.queued + legacy.queued,
      });
    }

    if (!authorized(req)) {
      if (url.pathname === '/v1/fetch')
        return sendV1Error(res, 401, 'UNAUTHORIZED', false, 'valid bearer token required', undefined, undefined);
      return sendJson(res, 401, { error: 'unauthorized: pass "Authorization: Bearer <API_TOKEN>"' });
    }
    if (url.pathname === '/v1/fetch')
      return await handleV1Fetch(req, res);
    if (url.pathname === '/cookies')
      return await handleCookies(req, res, url);
    if (url.pathname !== '/fetch')
      return sendJson(res, 404, { error: 'routes: POST /v1/fetch, /fetch?url=..., /cookies, /healthz' });
    if (!ENABLE_LEGACY_FETCH)
      return sendJson(res, 404, { error: 'legacy /fetch route is disabled' });

    const target = url.searchParams.get('url');
    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return sendJson(res, 400, { error: 'missing or invalid url parameter' });
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
      return sendJson(res, 400, { error: 'only http/https urls are allowed' });
    const entry = findSite(store, parsed.hostname);
    const data = await legacyQueue.run(() => fetchArticle(context, target, {
      pageTimeoutMs: PAGE_TIMEOUT_MS,
      settleMs: SETTLE_MS,
      cookies: entry ? entry.cookies : null,
    }));
    if (entry) data.cookieSite = entry.site;
    sendJson(res, 200, data);
  } catch (error) {
    if (error instanceof QueueFullError)
      return sendJson(res, 429, { error: 'fetch queue is full' });
    if (error instanceof QueueClosedError)
      return sendJson(res, 503, { error: 'service is shutting down' });
    sendJson(res, 502, { error: String((error && error.message) || error) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`listening on ${HOST}:${PORT} (auth: ${API_TOKEN ? 'token' : 'OFF'}, legacy fetch: ${ENABLE_LEGACY_FETCH ? 'ON' : 'OFF'})`);
});

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log('shutting down...');
  wsjQueue.close();
  legacyQueue.close();
  server.close();
  await context.close().catch(() => {});
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
