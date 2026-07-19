import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { v1ErrorPayload, v1SuccessPayload } from '../api_contract.mjs';
import { BoundedQueue, QueueClosedError, QueueFullError } from '../queue.mjs';
import {
  articleSha256,
  isAntiBotChallengeUrl,
  isAllowedWsjImageHost,
  normalizeWsjImageUrl,
  sanitizeWsjImages,
  validRequestId,
  validateWsjArticleUrl,
} from '../wsj.mjs';

test('WSJ URL allowlist accepts only HTTPS Chinese article URLs', () => {
  const accepted = validateWsjArticleUrl('https://cn.wsj.com/articles/example-123?mod=hp#section');
  assert.equal(accepted.ok, true);
  assert.equal(accepted.url.href, 'https://cn.wsj.com/articles/example-123?mod=hp');

  for (const value of [
    'http://cn.wsj.com/articles/example',
    'https://www.wsj.com/articles/example',
    'https://cn.wsj.com.evil.test/articles/example',
    'https://user:pass' + '@cn.wsj.com/articles/example',
    'https://cn.wsj.com/',
    'https://cn.wsj.com/articles/',
    'not a URL',
  ]) {
    assert.equal(validateWsjArticleUrl(value).ok, false, value);
  }
});

test('DataDome redirect URLs are distinguished from ordinary cross-domain URLs', () => {
  assert.equal(isAntiBotChallengeUrl('https://geo.captcha-delivery.com/captcha/?x=1'), true);
  assert.equal(isAntiBotChallengeUrl('https://api-js.datadome.co/captcha/'), true);
  assert.equal(isAntiBotChallengeUrl('https://example.com/captcha/'), false);
  assert.equal(isAntiBotChallengeUrl('not a URL'), false);
});

test('request IDs have a small log-safe alphabet and bounded size', () => {
  assert.equal(validRequestId('wsj:1234_abc-DEF.1'), true);
  assert.equal(validRequestId(''), false);
  assert.equal(validRequestId('contains a space'), false);
  assert.equal(validRequestId('x'.repeat(129)), false);
});

test('article hash is SHA-256 of the exact delivered text', () => {
  const paragraphs = ['第一段', 'Second paragraph', '第三段'];
  const expected = createHash('sha256').update(paragraphs.join('\n\n')).digest('hex');
  assert.equal(articleSha256(paragraphs), expected);
  assert.match(expected, /^[a-f0-9]{64}$/);
});

test('WSJ image URLs are resolved and limited to publisher HTTPS hosts', () => {
  const base = 'https://cn.wsj.com/articles/example-123';
  assert.equal(isAllowedWsjImageHost('images.wsj.net'), true);
  assert.equal(isAllowedWsjImageHost('cdn.dowjones.com'), false);
  assert.equal(isAllowedWsjImageHost('sub.images.wsj.net'), false);
  assert.equal(isAllowedWsjImageHost('evilwsj.net'), false);
  assert.equal(
    normalizeWsjImageUrl('//images.wsj.net/im-123?width=1200&utm_source=test&mod=article#photo', base),
    'https://images.wsj.net/im-123?width=1200&utm_source=test&mod=article'
  );
  assert.equal(normalizeWsjImageUrl('/assets/photo.jpg', base), null);
  assert.equal(
    normalizeWsjImageUrl('https://images.wsj.net./im-123', base),
    'https://images.wsj.net/im-123'
  );

  for (const value of [
    'http://images.wsj.net/im-123',
    'https://images.wsj.net:8443/im-123',
    'https://user:pass' + '@images.wsj.net/im-123',
    'https://images.wsj.net.evil.test/im-123',
    'https://example.com/photo.jpg',
    'data:image/png;base64,abc',
    'https://images.wsj.net/assets/wsj-logo.svg',
    'https://images.wsj.net/%E0%A4%A',
  ]) {
    assert.equal(normalizeWsjImageUrl(value, base), null, value);
  }
});

test('article images preserve paragraph anchors, choose large srcset and dedupe variants', () => {
  const candidates = [
    {
      semantic: 'figure',
      sources: [
        { url: 'https://images.wsj.net/im-123?width=320', widthHint: 320 },
        { url: 'https://images.wsj.net/im-123?width=1280&utm_campaign=x', widthHint: 1280 },
      ],
      alt: '  现场\n照片  ',
      caption: ' 图注  ',
      afterParagraph: -1,
      width: 0,
      height: 720,
      visible: true,
      inProseFlow: true,
    },
    {
      semantic: 'picture',
      sources: [{ url: 'https://images.wsj.net/im-456', widthHint: 900 }],
      alt: '',
      caption: '第二张图片',
      afterParagraph: 1,
      height: 600,
      visible: true,
      inProseFlow: true,
    },
    // Same image at a different resize is emitted only at its first DOM slot.
    {
      semantic: 'figure',
      sources: [{ url: 'https://images.wsj.net/im-123?width=640', widthHint: 640 }],
      alt: 'duplicate',
      afterParagraph: 2,
      width: 640,
      height: 360,
      visible: true,
      inProseFlow: true,
    },
  ];

  assert.deepEqual(sanitizeWsjImages(candidates, 'https://cn.wsj.com/articles/example', 5), [
    {
      url: 'https://images.wsj.net/im-123?width=1280&utm_campaign=x',
      alt: '现场 照片',
      caption: '图注',
      afterParagraph: -1,
      width: 1280,
      height: 720,
    },
    {
      url: 'https://images.wsj.net/im-456',
      alt: null,
      caption: '第二张图片',
      afterParagraph: 1,
      width: 900,
      height: 600,
    },
  ]);
});

test('only a large exact og:image metadata slot is accepted as the leading hero', () => {
  const base = 'https://cn.wsj.com/articles/example';
  const duplicateDomHero = {
    semantic: 'figure',
    // Same asset as og:image, but WSJ uses the root rendition in article DOM.
    sources: [{ url: 'https://images.wsj.net/im-hero?width=800', widthHint: 800 }],
    alt: 'DOM copy of hero',
    caption: 'DOM caption',
    afterParagraph: 0,
    width: 800,
    height: 400,
    visible: true,
    inProseFlow: true,
  };
  const bodyPhoto = {
    ...duplicateDomHero,
    sources: [{ url: 'https://images.wsj.net/im-body', widthHint: 900 }],
    alt: 'body photo',
    caption: '',
    afterParagraph: 1,
    width: 900,
    height: 600,
  };
  const trustedMetadata = {
    ogImage: {
      url: 'https://images.wsj.net/im-hero/social?width=1280',
      width: '1280',
      height: '640',
    },
  };

  assert.deepEqual(
    sanitizeWsjImages([duplicateDomHero, bodyPhoto], base, 4, trustedMetadata),
    [
      {
        url: 'https://images.wsj.net/im-hero/social?width=1280',
        alt: null,
        caption: null,
        afterParagraph: -1,
        width: 1280,
        height: 640,
      },
      {
        url: 'https://images.wsj.net/im-body',
        alt: 'body photo',
        caption: null,
        afterParagraph: 1,
        width: 900,
        height: 600,
      },
    ]
  );

  for (const ogImage of [
    { url: 'https://images.wsj.net/assets/wsj-logo.png', width: 1280, height: 640 },
    { url: 'https://example.com/im-hero/social', width: 1280, height: 640 },
    { url: 'https://images.wsj.net/im-hero/social', width: 599, height: 640 },
    { url: 'https://images.wsj.net/im-hero/social', width: 1280, height: 299 },
    { url: 'https://images.wsj.net/im-hero/social', width: 1280 },
    { url: 'https://images.wsj.net/im-hero/social', width: 20001, height: 640 },
  ]) {
    assert.deepEqual(sanitizeWsjImages([], base, 4, { ogImage }), [], JSON.stringify(ogImage));
  }

  // A generic metadata-looking object in the ordinary candidate stream, or a
  // non-og slot in the trusted container, must not bypass article-flow checks.
  assert.deepEqual(sanitizeWsjImages([{
    semantic: 'metadata',
    sources: [{ url: 'https://images.wsj.net/im-generic' }],
    width: 1280,
    height: 640,
    afterParagraph: -1,
    inProseFlow: true,
    visible: true,
  }], base, 4), []);
  assert.deepEqual(sanitizeWsjImages([], base, 4, {
    genericImage: { url: 'https://images.wsj.net/im-generic', width: 1280, height: 640 },
  }), []);
});

test('article image sanitizer fails closed for recommendations and unrelated assets', () => {
  const base = {
    semantic: 'figure',
    sources: [{ url: 'https://images.wsj.net/im-safe', widthHint: 800 }],
    alt: 'article photograph',
    afterParagraph: 0,
    width: 800,
    height: 450,
    visible: true,
    inProseFlow: true,
  };
  const rejected = [
    { ...base, badContext: true },
    { ...base, inProseFlow: false },
    { ...base, linkedArticle: true },
    { ...base, visible: false },
    { ...base, afterParagraph: 3 }, // after the final paragraph
    { ...base, afterParagraph: -2 },
    { ...base, width: 100 },
    { ...base, height: 50 },
    { ...base, sources: [{ url: 'https://example.com/unrelated.jpg', widthHint: 800 }] },
    { ...base, sources: [{ url: 'https://images.wsj.net/assets/icon.png', widthHint: 800 }] },
    {
      ...base,
      semantic: 'img',
      mediaHint: false,
      alt: '',
      caption: '',
    },
  ];
  for (const candidate of rejected)
    assert.deepEqual(sanitizeWsjImages([candidate], 'https://cn.wsj.com/articles/example', 4), []);

  assert.deepEqual(
    sanitizeWsjImages([{
      ...base,
      sources: [{ url: 'https://images.wsj.net/im-article-tail', widthHint: 800 }],
      inProseFlow: false,
      articleContent: true,
      afterParagraph: 3,
    }], 'https://cn.wsj.com/articles/example', 4),
    [{
      url: 'https://images.wsj.net/im-article-tail',
      alt: 'article photograph',
      caption: null,
      afterParagraph: 3,
      width: 800,
      height: 450,
      articleTail: true,
    }]
  );

  assert.deepEqual(sanitizeWsjImages(null, 'https://cn.wsj.com/articles/example', 4), []);
  assert.deepEqual(sanitizeWsjImages([base], 'https://cn.wsj.com/articles/example', 0), []);
});

test('article image response is hard-capped at 20 unique images', () => {
  const candidates = Array.from({ length: 25 }, (_, index) => ({
    semantic: 'figure',
    sources: [{ url: `https://images.wsj.net/im-${index}`, widthHint: 800 }],
    alt: `photo ${index}`,
    afterParagraph: index,
    width: 800,
    height: 450,
    visible: true,
    inProseFlow: true,
  }));
  const images = sanitizeWsjImages(candidates, 'https://cn.wsj.com/articles/example', 30);
  assert.equal(images.length, 20);
  assert.equal(images[0].url, 'https://images.wsj.net/im-0');
  assert.equal(images[19].url, 'https://images.wsj.net/im-19');
});

test('v1 payload factories keep the stable ok/code/retryable contract', () => {
  const article = { title: '标题', paragraphs: ['a', 'b', 'c'], sha256: 'abc' };
  assert.deepEqual(v1SuccessPayload('request:1', article), {
    ok: true,
    code: 'OK',
    retryable: false,
    requestId: 'request:1',
    article,
  });
  assert.deepEqual(v1ErrorPayload('QUEUE_FULL', true, 'queue is full', 'request:1', { queued: 20 }), {
    ok: false,
    code: 'QUEUE_FULL',
    retryable: true,
    message: 'queue is full',
    requestId: 'request:1',
    details: { queued: 20 },
  });
  assert.deepEqual(v1ErrorPayload('UNAUTHORIZED', false, 'no', undefined, undefined), {
    ok: false,
    code: 'UNAUTHORIZED',
    retryable: false,
    message: 'no',
  });
});

test('bounded queue serializes work and rejects overflow', async () => {
  const queue = new BoundedQueue(1, 1);
  let releaseFirst;
  const order = [];
  const first = queue.run(() => new Promise((resolve) => {
    order.push('first-start');
    releaseFirst = () => {
      order.push('first-end');
      resolve('one');
    };
  }));
  await Promise.resolve();
  const second = queue.run(async () => {
    order.push('second');
    return 'two';
  });
  await assert.rejects(queue.run(async () => 'three'), QueueFullError);
  assert.deepEqual(queue.snapshot(), {
    active: 1,
    queued: 1,
    concurrency: 1,
    maxQueued: 1,
    closed: false,
  });
  releaseFirst();
  assert.equal(await first, 'one');
  assert.equal(await second, 'two');
  assert.deepEqual(order, ['first-start', 'first-end', 'second']);
});

test('closing a queue rejects waiting and future work', async () => {
  const queue = new BoundedQueue(1, 1);
  let release;
  const active = queue.run(() => new Promise((resolve) => { release = resolve; }));
  await Promise.resolve();
  const waiting = queue.run(async () => 'waiting');
  queue.close();
  await assert.rejects(waiting, QueueClosedError);
  await assert.rejects(queue.run(async () => 'future'), QueueClosedError);
  release();
  await active;
});
