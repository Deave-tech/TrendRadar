import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { loadStore, saveStore } from '../cookies.mjs';
import { BoundedQueue, QueueClosedError } from '../queue.mjs';
import {
  applyWsjSessionUpdate,
  runWsjSessionTask,
  wsjStartupSession,
} from '../wsj_session.mjs';

function fixtureStore(value = 'seed-token') {
  return {
    'wsj.com': {
      cookies: [
        { name: 'other', value: 'kept', domain: '.wsj.com', path: '/' },
        { name: 'datadome', value, domain: '.wsj.com', path: '/' },
      ],
      ua: 'Fixture UA',
      updatedAt: '2026-01-01T00:00:00.000Z',
    },
  };
}

test('WSJ startup session exposes one disk seed without mutating the store', () => {
  const store = fixtureStore();
  const before = JSON.stringify(store);
  const session = wsjStartupSession(store);
  assert.equal(session.ua, 'Fixture UA');
  assert.equal(session.cookies.find((cookie) => cookie.name === 'datadome')?.value, 'seed-token');
  assert.equal(JSON.stringify(store), before);
});

test('atomic cookie store writes mode 0600 and leaves no temporary files', () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-cookie-store-'));
  const file = path.join(directory, '.cookies.json');
  try {
    saveStore(file, fixtureStore());
    assert.deepEqual(loadStore(file), fixtureStore());
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(readdirSync(directory), ['.cookies.json']);

    saveStore(file, fixtureStore('rotated-token'));
    assert.equal(loadStore(file)['wsj.com'].cookies.at(-1).value, 'rotated-token');
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(readdirSync(directory), ['.cookies.json']);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('serialized WSJ tasks persist each browser-rotated token without re-adding the seed', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-session-'));
  const file = path.join(directory, '.cookies.json');
  const store = fixtureStore();
  const context = {
    current: 'seed-token',
    cookieReads: 0,
    async cookies(url) {
      assert.equal(url, 'https://cn.wsj.com/');
      this.cookieReads++;
      return [{
        name: 'datadome',
        value: this.current,
        domain: '.wsj.com',
        path: '/',
      }];
    },
  };
  try {
    const first = await runWsjSessionTask(context, store, file, async () => {
      context.current = 'rotated-after-list';
      return 'list-result';
    });
    assert.equal(first, 'list-result');
    assert.equal(loadStore(file)['wsj.com'].cookies.at(-1).value, 'rotated-after-list');

    const second = await runWsjSessionTask(context, store, file, async () => {
      // The second task sees the live browser value; no addCookies method is
      // present on the mock, so a per-request seed reset would fail this test.
      assert.equal(context.current, 'rotated-after-list');
      context.current = 'rotated-after-article';
      return 'article-result';
    });
    assert.equal(second, 'article-result');
    assert.equal(context.cookieReads, 2);
    const persisted = loadStore(file)['wsj.com'];
    assert.equal(persisted.cookies.find((cookie) => cookie.name === 'datadome').value, 'rotated-after-article');
    assert.equal(persisted.cookies.find((cookie) => cookie.name === 'other').value, 'kept');
    assert.equal(persisted.ua, 'Fixture UA');
    assert.equal(statSync(file).mode & 0o777, 0o600);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a failed WSJ task still persists the token rotated by its response', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-failed-session-'));
  const file = path.join(directory, '.cookies.json');
  const store = fixtureStore();
  const expected = new Error('upstream status');
  const context = {
    current: 'seed-token',
    async cookies() {
      return [{ name: 'datadome', value: this.current, domain: '.wsj.com', path: '/' }];
    },
  };
  try {
    await assert.rejects(
      runWsjSessionTask(context, store, file, async () => {
        context.current = 'rotated-on-error';
        throw expected;
      }),
      (error) => error === expected
    );
    assert.equal(loadStore(file)['wsj.com'].cookies.at(-1).value, 'rotated-on-error');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('an operator cookie update is ordered after in-flight rotation persistence', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-operator-update-'));
  const file = path.join(directory, '.cookies.json');
  const store = fixtureStore();
  saveStore(file, store);
  const queue = new BoundedQueue(1, 2);
  let releaseTask;
  let taskStarted;
  const started = new Promise((resolve) => { taskStarted = resolve; });
  const context = {
    current: 'seed-token',
    addCalls: 0,
    async cookies() {
      return [{ name: 'datadome', value: this.current, domain: '.wsj.com', path: '/' }];
    },
    async addCookies(cookies) {
      this.addCalls++;
      this.current = cookies.find((cookie) => cookie.name === 'datadome')?.value || this.current;
    },
  };
  try {
    const inFlight = queue.run(() => runWsjSessionTask(context, store, file, async () => {
      context.current = 'rotated-by-in-flight-response';
      taskStarted();
      await new Promise((resolve) => { releaseTask = resolve; });
    }));
    await started;
    const operatorUpdate = queue.run(() => applyWsjSessionUpdate(
      context,
      store,
      file,
      'wsj.com',
      'datadome=operator-token',
      'Operator UA'
    ));

    releaseTask();
    await inFlight;
    const entry = await operatorUpdate;
    assert.equal(entry.cookies.find((cookie) => cookie.name === 'datadome').value, 'operator-token');
    assert.equal(context.current, 'operator-token');
    assert.equal(context.addCalls, 1);
    const durable = loadStore(file)['wsj.com'];
    assert.equal(durable.cookies.find((cookie) => cookie.name === 'datadome').value, 'operator-token');
    assert.equal(durable.ua, 'Operator UA');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('unchanged browser token does not rewrite the durable store', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-no-churn-'));
  const file = path.join(directory, '.cookies.json');
  const store = fixtureStore();
  saveStore(file, store);
  const originalUpdatedAt = loadStore(file)['wsj.com'].updatedAt;
  try {
    await runWsjSessionTask({
      async cookies() {
        return [{ name: 'datadome', value: 'seed-token', domain: '.wsj.com', path: '/' }];
      },
    }, store, file, async () => 'same-token');
    assert.equal(loadStore(file)['wsj.com'].updatedAt, originalUpdatedAt);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('session persistence errors are stable and never include cookie values', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-persist-error-'));
  const secret = 'must-not-appear-in-errors';
  const store = fixtureStore(secret);
  try {
    await assert.rejects(
      runWsjSessionTask({
        async cookies() {
          throw new Error(`read failed while cookie was ${secret}`);
        },
      }, store, path.join(directory, '.cookies.json'), async () => 'ok'),
      (error) => {
        assert.equal(error.name, 'WsjSessionPersistenceError');
        assert.equal(error.message, 'WSJ session cookie could not be persisted');
        assert.equal(error.message.includes(secret), false);
        return true;
      }
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('persistence failure closes the queue before another WSJ task can run', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-fail-closed-'));
  const missingDirectory = path.join(directory, 'missing');
  const store = fixtureStore();
  const queue = new BoundedQueue(1, 2);
  let firstStarted;
  const started = new Promise((resolve) => { firstStarted = resolve; });
  let releaseFirst;
  let secondRan = false;
  const context = {
    async cookies() {
      return [{
        name: 'datadome',
        value: 'rotated-but-not-persisted',
        domain: '.wsj.com',
        path: '/',
      }];
    },
  };
  try {
    const first = queue.run(() => runWsjSessionTask(
      context,
      store,
      path.join(missingDirectory, '.cookies.json'),
      async () => {
        firstStarted();
        await new Promise((resolve) => { releaseFirst = resolve; });
      },
      { onPersistenceFailure: () => queue.close() }
    ));
    await started;
    const second = queue.run(async () => {
      secondRan = true;
    });

    releaseFirst();
    await assert.rejects(first, { name: 'WsjSessionPersistenceError' });
    await assert.rejects(second, QueueClosedError);
    assert.equal(secondRan, false);
    assert.deepEqual(queue.snapshot(), {
      active: 0,
      queued: 0,
      concurrency: 1,
      maxQueued: 2,
      closed: true,
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('operator update fails closed when Chromium changes but durable save fails', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'bpc-wsj-update-fail-closed-'));
  const store = fixtureStore();
  const queue = new BoundedQueue(1, 1);
  let hookCalls = 0;
  const context = {
    async addCookies() {},
  };
  try {
    await assert.rejects(
      queue.run(() => applyWsjSessionUpdate(
        context,
        store,
        path.join(directory, 'missing', '.cookies.json'),
        'wsj.com',
        'datadome=operator-token',
        'Operator UA',
        {
          onPersistenceFailure: () => {
            hookCalls++;
            queue.close();
          },
        }
      )),
      { name: 'WsjSessionPersistenceError' }
    );
    assert.equal(hookCalls, 1);
    assert.equal(queue.snapshot().closed, true);
    await assert.rejects(queue.run(async () => {}), QueueClosedError);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('losing a previously persisted browser token also fails closed', async () => {
  const queue = new BoundedQueue(1, 1);
  const store = fixtureStore();
  let hookCalls = 0;
  await assert.rejects(
    queue.run(() => runWsjSessionTask(
      { async cookies() { return []; } },
      store,
      '/unused/when-cookie-is-missing',
      async () => 'response-without-cookie',
      {
        onPersistenceFailure: () => {
          hookCalls++;
          queue.close();
        },
      }
    )),
    { name: 'WsjSessionPersistenceError' }
  );
  assert.equal(hookCalls, 1);
  assert.equal(queue.snapshot().closed, true);
});
