import {
  findSite,
  parseDocumentCookie,
  replaceSiteCookie,
  saveStore,
  upsertSite,
} from './cookies.mjs';

const WSJ_COOKIE_URL = 'https://cn.wsj.com/';
const WSJ_COOKIE_SITE = 'wsj.com';

function notifyPersistenceFailure(onPersistenceFailure) {
  if (typeof onPersistenceFailure !== 'function') return;
  try {
    onPersistenceFailure();
  } catch {
    // The stable persistence error below must not be replaced by a hook error.
  }
}

function persistenceError(cause, taskError = undefined) {
  const error = new Error('WSJ session cookie could not be persisted', { cause });
  error.name = 'WsjSessionPersistenceError';
  if (taskError) error.taskError = taskError;
  return error;
}

function trustedDatadomeCookie(cookie) {
  if (!cookie || cookie.name !== 'datadome' || typeof cookie.value !== 'string') return false;
  if (!cookie.value || cookie.value.length > 4096 || /[;\r\n]/.test(cookie.value)) return false;
  const domain = String(cookie.domain || '').toLowerCase().replace(/^\./, '');
  return domain === 'wsj.com' || domain === 'cn.wsj.com';
}

/** Return the cookie seed that is loaded into Chromium exactly once at startup. */
export function wsjStartupSession(store) {
  const entry = findSite(store, 'cn.wsj.com');
  return {
    cookies: entry && Array.isArray(entry.cookies) ? entry.cookies : [],
    ua: entry?.ua || null,
  };
}

/**
 * Read Chromium's current rotated DataDome token and atomically save it.
 * Cookie values are deliberately never returned, logged, or placed in errors.
 */
export async function persistWsjSession(context, store, cookiesFile) {
  const browserCookies = await context.cookies(WSJ_COOKIE_URL);
  const existing = findSite(store, 'cn.wsj.com');
  const storedDatadome = existing?.cookies?.find((cookie) => cookie?.name === 'datadome');
  const current = browserCookies.find((cookie) =>
    trustedDatadomeCookie(cookie) &&
    String(cookie.domain || '').toLowerCase().replace(/^\./, '') === WSJ_COOKIE_SITE
  ) || browserCookies.find(trustedDatadomeCookie);
  if (!current) {
    // If Chromium started with a durable seed and later loses it, keeping the
    // old value on disk would make the next restart silently reuse a stale
    // session. Treat disappearance exactly like a failed durable write.
    if (storedDatadome?.value) throw new Error('current WSJ session cookie is missing');
    return { persisted: false };
  }

  if (storedDatadome?.value === current.value &&
    (storedDatadome.domain || '') === (current.domain || '') &&
    (storedDatadome.path || '/') === (current.path || '/')) {
    return { persisted: true, changed: false };
  }
  replaceSiteCookie(store, existing?.site || WSJ_COOKIE_SITE, {
    name: 'datadome',
    value: current.value,
    // Normalize host-only cn.wsj.com cookies to the narrowest domain that
    // still survives a restart and works for every allowed listing/article.
    domain: current.domain || `.${WSJ_COOKIE_SITE}`,
    path: current.path || '/',
  });
  saveStore(cookiesFile, store);
  return { persisted: true, changed: true };
}

/** Apply an explicit operator-provided WSJ seed inside the shared queue. */
export async function applyWsjSessionUpdate(
  context,
  store,
  cookiesFile,
  site,
  rawCookie,
  ua,
  { onPersistenceFailure } = {}
) {
  const normalizedSite = String(site).replace(/^\./, '').toLowerCase();
  const cookies = parseDocumentCookie(rawCookie, normalizedSite);
  // Update Chromium first. If it rejects the cookie, the durable session stays
  // untouched instead of advertising a value the live context never received.
  await context.addCookies(cookies);
  const entry = upsertSite(store, normalizedSite, rawCookie, ua);
  try {
    saveStore(cookiesFile, store);
  } catch (error) {
    // Chromium has already accepted the new value, so failure to save it has
    // created the same live/durable split as a failed rotation persistence.
    notifyPersistenceFailure(onPersistenceFailure);
    throw persistenceError(error);
  }
  return entry;
}

/** Run one already-serialized WSJ job and persist rotation before queue release. */
export async function runWsjSessionTask(
  context,
  store,
  cookiesFile,
  task,
  { onPersistenceFailure } = {}
) {
  let result;
  let taskError;
  try {
    result = await task();
  } catch (error) {
    taskError = error;
  }

  try {
    await persistWsjSession(context, store, cookiesFile);
  } catch (error) {
    // Persistence failure takes precedence: allowing the next queued request
    // to run with an unrecorded token would make a future restart reuse an old
    // DataDome value. The original task error remains available as the cause.
    // Mark the owning service unhealthy and close its queue before this task
    // unwinds, so BoundedQueue cannot start the next waiter in its finally.
    notifyPersistenceFailure(onPersistenceFailure);
    throw persistenceError(error, taskError);
  }

  if (taskError) throw taskError;
  return result;
}
