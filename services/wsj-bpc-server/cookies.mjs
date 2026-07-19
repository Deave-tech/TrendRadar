// Per-site cookie store for the fetch API.
// Format on disk (COOKIES_FILE, default server/.cookies.json, mode 0600):
//   { "wsj.com": { "cookies": [{name, value, domain, path}...], "ua": "...",
//                  "updatedAt": "ISO" }, ... }
// Sites are matched by hostname suffix, so one "wsj.com" entry covers
// cn.wsj.com, www.wsj.com, ... — extend to any site by pushing a new entry
// through the API, no code change needed.
import { readFileSync, writeFileSync } from 'node:fs';

export function loadStore(file) {
  try {
    const data = JSON.parse(readFileSync(file, 'utf8'));
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

export function saveStore(file, store) {
  writeFileSync(file, JSON.stringify(store, null, 2), { mode: 0o600 });
}

/** Parse a raw `document.cookie` string into Playwright cookie objects. */
export function parseDocumentCookie(raw, site) {
  const domain = '.' + site.replace(/^\./, '');
  return String(raw)
    .split(';')
    .map((p) => p.trim())
    .filter(Boolean)
    .map((pair) => {
      const i = pair.indexOf('=');
      if (i < 1) return null;
      return { name: pair.slice(0, i).trim(), value: pair.slice(i + 1).trim(), domain, path: '/' };
    })
    .filter(Boolean);
}

export function upsertSite(store, site, rawCookie, ua) {
  const key = String(site).replace(/^\./, '').toLowerCase();
  const prev = store[key] || {};
  store[key] = {
    cookies: parseDocumentCookie(rawCookie, key),
    ua: ua || prev.ua || null,
    updatedAt: new Date().toISOString(),
  };
  return store[key];
}

export function deleteSite(store, site) {
  const key = String(site).replace(/^\./, '').toLowerCase();
  const existed = key in store;
  delete store[key];
  return existed;
}

/** Longest-suffix match: entry for "wsj.com" serves "cn.wsj.com". */
export function findSite(store, hostname) {
  let best = null;
  for (const site of Object.keys(store)) {
    if (hostname === site || hostname.endsWith('.' + site)) {
      if (!best || site.length > best.length) best = site;
    }
  }
  return best ? { site: best, ...store[best] } : null;
}

/** Store view with cookie values masked (for GET /cookies). */
export function maskedView(store) {
  return Object.fromEntries(
    Object.entries(store).map(([site, e]) => [
      site,
      {
        cookieNames: (e.cookies || []).map((c) => c.name),
        cookieCount: (e.cookies || []).length,
        ua: e.ua || null,
        updatedAt: e.updatedAt || null,
      },
    ])
  );
}
