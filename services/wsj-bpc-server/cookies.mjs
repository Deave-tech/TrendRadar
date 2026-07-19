// Per-site cookie store for the fetch API.
// Format on disk (COOKIES_FILE, default server/.cookies.json, mode 0600):
//   { "wsj.com": { "cookies": [{name, value, domain, path}...], "ua": "...",
//                  "updatedAt": "ISO" }, ... }
// Sites are matched by hostname suffix, so one "wsj.com" entry covers
// cn.wsj.com, www.wsj.com, ... — extend to any site by pushing a new entry
// through the API, no code change needed.
import {
  chmodSync,
  closeSync,
  fsyncSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export function loadStore(file) {
  try {
    const data = JSON.parse(readFileSync(file, 'utf8'));
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

export function saveStore(file, store) {
  const directory = path.dirname(file);
  const temp = path.join(directory, `.${path.basename(file)}.${process.pid}.${randomUUID()}.tmp`);
  let fileDescriptor = null;
  try {
    fileDescriptor = openSync(temp, 'wx', 0o600);
    writeFileSync(fileDescriptor, JSON.stringify(store, null, 2));
    fsyncSync(fileDescriptor);
    closeSync(fileDescriptor);
    fileDescriptor = null;
    chmodSync(temp, 0o600);
    renameSync(temp, file);

    // Best effort: the rename is already atomic, but syncing the containing
    // directory makes the new name durable across a sudden host restart.
    let directoryDescriptor = null;
    try {
      directoryDescriptor = openSync(directory, 'r');
      fsyncSync(directoryDescriptor);
    } catch {
      // Some filesystems do not permit fsync on a directory.
    } finally {
      if (directoryDescriptor !== null) closeSync(directoryDescriptor);
    }
  } finally {
    if (fileDescriptor !== null) closeSync(fileDescriptor);
    try {
      unlinkSync(temp);
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
  }
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

/** Replace one stored cookie without disturbing the other cookies for a site. */
export function replaceSiteCookie(store, site, cookie, ua = undefined) {
  const key = String(site).replace(/^\./, '').toLowerCase();
  if (!cookie || typeof cookie !== 'object' || typeof cookie.name !== 'string' ||
    typeof cookie.value !== 'string') throw new TypeError('cookie name and value are required');
  const prev = store[key] || {};
  const normalized = {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain || `.${key}`,
    path: cookie.path || '/',
  };
  store[key] = {
    cookies: [
      ...(Array.isArray(prev.cookies) ? prev.cookies : [])
        .filter((item) => item && item.name !== normalized.name),
      normalized,
    ],
    ua: ua === undefined ? (prev.ua || null) : (ua || null),
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
