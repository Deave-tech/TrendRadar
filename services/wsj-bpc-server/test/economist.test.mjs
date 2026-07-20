import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { fetchEconomistArticle } from '../browser.mjs';
import {
  economistArticleSha256,
  EconomistFetchError,
  isEconomistChallengeUrl,
  normalizeEconomistImageUrl,
  sanitizeEconomistImages,
  validateEconomistArticleUrl,
} from '../economist.mjs';

const ARTICLE = 'https://www.economist.com/business/2026/07/19/chinas-mysterious-new-billionaires-are-conquering-the-world';
const image = (name, width = 1200, extension = 'jpg') =>
  `https://www.economist.com/cdn-cgi/image/width=${width},quality=80,format=${extension === 'jpeg' ? 'jpg' : extension}/content-assets/images/2026/07/${name}.${extension}`;
const autoImage = (name, width = 1200, extension = 'jpg') =>
  `https://www.economist.com/cdn-cgi/image/width=${width},quality=80,format=auto/content-assets/images/2026/07/${name}.${extension}`;

test('Economist URL allowlist accepts only dated HTTPS prose articles', () => {
  const checked = validateEconomistArticleUrl(`${ARTICLE}/?utm_source=rss#chart`);
  assert.equal(checked.ok, true);
  assert.equal(checked.url.href, ARTICLE);

  for (const value of [
    ARTICLE.replace('https:', 'http:'),
    ARTICLE.replace('www.economist.com', 'economist.com'),
    ARTICLE.replace('www.economist.com', 'www.economist.com.evil.test'),
    ARTICLE.replace('www.economist.com', 'user:pass@www.economist.com'),
    ARTICLE.replace('www.economist.com', 'www.economist.com:8443'),
    'https://www.economist.com/podcasts/2026/07/19/the-week-ahead',
    'https://www.economist.com/video/2026/07/19/a-film',
    'https://www.economist.com/audio/2026/07/19/an-audio-edition',
    'https://www.economist.com/films/2026/07/19/a-film',
    'https://www.economist.com/interactive/2026/07/19/a-graphic',
    'https://www.economist.com/business/2026/02/30/not-a-date',
    'https://www.economist.com/business/latest-story',
    'https://www.economist.com/business/2026/07/19/story/extra',
    'not a URL',
  ]) assert.equal(validateEconomistArticleUrl(value).ok, false, value);
});

test('Cloudflare challenges are distinguished from ordinary redirects', () => {
  assert.equal(isEconomistChallengeUrl('https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x'), true);
  assert.equal(isEconomistChallengeUrl('https://www.economist.com/cdn-cgi/challenge-platform/x'), true);
  assert.equal(isEconomistChallengeUrl('https://example.com/cdn-cgi/challenge-platform/x'), false);
  assert.equal(isEconomistChallengeUrl('not a URL'), false);
});

test('Economist images are restricted to exact publisher editorial paths', () => {
  assert.equal(normalizeEconomistImageUrl(image('hero'), ARTICLE), image('hero'));
  assert.equal(normalizeEconomistImageUrl(autoImage('hero'), ARTICLE), image('hero'));
  assert.equal(
    normalizeEconomistImageUrl(autoImage('chart', 1424, 'png'), ARTICLE),
    image('chart', 1424, 'png')
  );
  assert.equal(
    normalizeEconomistImageUrl(
      'https://www.economist.com/cdn-cgi/image/format=webp,width=1424,quality=80/content-assets/images/photo.jpeg?tracking=1#x',
      ARTICLE
    ),
    'https://www.economist.com/cdn-cgi/image/width=1424,quality=80,format=jpg/content-assets/images/photo.jpeg'
  );
  assert.equal(
    normalizeEconomistImageUrl('/content-assets/images/photo.jpg#x', ARTICLE),
    'https://www.economist.com/content-assets/images/photo.jpg'
  );
  assert.equal(
    normalizeEconomistImageUrl('/cdn-cgi/image/width=1424,quality=80/content-assets/images/chart.png#x', ARTICLE),
    'https://www.economist.com/cdn-cgi/image/width=1424,quality=80,format=png/content-assets/images/chart.png'
  );
  for (const value of [
    image('hero').replace('https:', 'http:'),
    image('hero').replace('www.economist.com', 'cdn.economist.com'),
    image('hero').replace('www.economist.com', 'www.economist.com.evil.test'),
    'https://www.economist.com/assets/images/photo.jpg',
    'https://www.economist.com/cdn-cgi/image/width=1200/assets/logo.png',
    'https://www.economist.com/cdn-cgi/image/width=1200/content-assets/images/site-logo.png',
    'https://www.economist.com/cdn-cgi/image/width=1200,format=auto/content-assets/images/photo.avif',
    'https://www.economist.com/content-assets/images/vector.svg',
    'data:image/png;base64,abc',
  ]) assert.equal(normalizeEconomistImageUrl(value, ARTICLE), null, value);
});

test('image sanitizer selects one metadata-matched hero, largest srcset and ordered body figures', () => {
  const candidates = [
    {
      scope: 'hero', semantic: 'figure', sources: [{ url: image('wrong-hero', 800), widthHint: 800 }],
      alt: 'wrong hero', caption: '', afterParagraph: -1, width: 800, height: 450,
      visible: true, domIndex: 0,
    },
    {
      scope: 'hero', semantic: 'figure', sources: [
        { url: image('hero', 480), widthHint: 480 },
        { url: image('hero', 1424), widthHint: 1424 },
      ],
      alt: 'Main photograph', caption: 'illustration: artist', afterParagraph: -1,
      width: 712, height: 400, visible: true, domIndex: 1,
    },
    {
      scope: 'body', semantic: 'figure', sources: [
        { url: image('chart', 640), widthHint: 640 },
        { url: image('chart', 1424), widthHint: 1424 },
      ],
      alt: 'A chart', caption: 'chart: the economist', afterParagraph: 0,
      width: 712, height: 356, visible: true, domIndex: 2,
    },
    // Same editorial asset at another transform must not be emitted twice.
    {
      scope: 'body', semantic: 'figure', sources: [{ url: image('chart', 800), widthHint: 800 }],
      alt: 'duplicate chart', afterParagraph: 1, width: 800, height: 400,
      visible: true, domIndex: 3,
    },
    // A figure after the final paragraph is a recommendation boundary.
    {
      scope: 'body', semantic: 'figure', sources: [{ url: image('recommended'), widthHint: 1200 }],
      alt: 'recommended', afterParagraph: 2, width: 1200, height: 675,
      visible: true, domIndex: 4,
    },
  ];
  const result = sanitizeEconomistImages(candidates, ARTICLE, 3, [image('hero', 1200)]);
  assert.deepEqual(result, [
    {
      url: image('hero', 1424),
      alt: 'Main photograph',
      caption: 'illustration: artist',
      afterParagraph: -1,
      width: 1424,
      height: 800,
    },
    {
      url: image('chart', 1424),
      alt: 'A chart',
      caption: 'chart: the economist',
      afterParagraph: 0,
      width: 1424,
      height: 712,
    },
  ]);
});

test('image sanitizer fails closed for recommendation contexts and unrelated assets', () => {
  const base = {
    scope: 'body', semantic: 'figure', sources: [{ url: image('body'), widthHint: 1000 }],
    alt: 'body', caption: 'caption', afterParagraph: 0, width: 1000, height: 600,
    visible: true,
  };
  for (const candidate of [
    { ...base, badContext: true },
    { ...base, linkedArticle: true },
    { ...base, visible: false },
    { ...base, semantic: 'img' },
    { ...base, scope: 'page' },
    { ...base, afterParagraph: -2 },
    { ...base, width: 200 },
    { ...base, sources: [{ url: 'https://example.com/photo.jpg', widthHint: 1000 }] },
  ]) assert.deepEqual(sanitizeEconomistImages([candidate], ARTICLE, 4), []);
});

test('image sanitizer uses only trusted editorial metadata as an immersive hero fallback', () => {
  const trusted = 'https://www.economist.com/content-assets/images/immersive-hero.jpg';
  assert.deepEqual(sanitizeEconomistImages([], ARTICLE, 4, [
    'https://www.economist.com/assets/recommendation.jpg',
    trusted,
  ]), [{
    url: trusted,
    alt: null,
    caption: null,
    afterParagraph: -1,
    width: null,
    height: null,
  }]);
  assert.deepEqual(sanitizeEconomistImages([], ARTICLE, 4, [
    'https://evil.test/content-assets/images/hero.jpg',
  ]), []);
});

test('trusted metadata hero beats an unrelated DOM hero and captioned article tails survive', () => {
  const decorative = {
    scope: 'hero', semantic: 'figure', sources: [{ url: image('decorative'), widthHint: 1200 }],
    alt: 'decorative', afterParagraph: -1, width: 1200, height: 600, visible: true,
  };
  const tail = {
    scope: 'body', semantic: 'figure', sources: [{ url: image('tail'), widthHint: 1200 }],
    alt: 'final article image', caption: 'Photograph: The Economist', afterParagraph: 2,
    width: 1200, height: 600, visible: true,
  };
  const result = sanitizeEconomistImages(
    [decorative, tail], ARTICLE, 3, [image('metadata-hero', 1424)]
  );
  assert.equal(result[0].url, image('metadata-hero', 1424));
  assert.equal(result[1].url, image('tail'));
  assert.equal(result[1].articleTail, true);
});

test('metadata fallback promotes the same DOM tail asset without losing its caption', () => {
  const transformed = image('immersive-tail', 1424);
  const direct = 'https://www.economist.com/content-assets/images/2026/07/immersive-tail.jpg';
  const result = sanitizeEconomistImages([{
    scope: 'body', semantic: 'figure', sources: [{ url: transformed, widthHint: 1424 }],
    alt: 'Immersive illustration', caption: 'Illustration: The Economist',
    afterParagraph: 2, width: 712, height: 400, visible: true,
  }], ARTICLE, 3, [direct]);
  assert.equal(result.length, 1);
  assert.equal(result[0].url, transformed);
  assert.equal(result[0].afterParagraph, -1);
  assert.equal(result[0].caption, 'Illustration: The Economist');
  assert.equal(result[0].width, 1424);
});

test('article hash and structured fetch error are stable', () => {
  const paragraphs = ['one', 'two', 'three'];
  assert.equal(
    economistArticleSha256(paragraphs),
    createHash('sha256').update(paragraphs.join('\n\n')).digest('hex')
  );
  const error = new EconomistFetchError('ARTICLE_NOT_READY', 503, true, 'not ready', { sample: 1 });
  assert.equal(error.name, 'EconomistFetchError');
  assert.equal(error.code, 'ARTICLE_NOT_READY');
  assert.equal(error.httpStatus, 503);
  assert.equal(error.retryable, true);
  assert.deepEqual(error.details, { sample: 1 });
});

function fakeFetchContext({ finalUrl = ARTICLE, status = 200, headers = {} } = {}) {
  const page = {
    route: async () => {},
    goto: async () => ({ status: () => status, headers: () => headers }),
    url: () => finalUrl,
    close: async () => {},
  };
  return {
    browser: () => ({ isConnected: () => true }),
    newPage: async () => page,
  };
}

test('fetch classifies Cloudflare 403 before the generic upstream gate', async () => {
  await assert.rejects(
    fetchEconomistArticle(fakeFetchContext({
      status: 403,
      headers: { 'cf-mitigated': 'challenge' },
    }), ARTICLE, { pageTimeoutMs: 1000, settleMs: 0 }),
    (error) => error instanceof EconomistFetchError &&
      error.code === 'CHALLENGE_DETECTED' && error.httpStatus === 422
  );
});

test('fetch refuses a redirect to another valid Economist article', async () => {
  await assert.rejects(
    fetchEconomistArticle(fakeFetchContext({
      finalUrl: ARTICLE.replace('chinas-mysterious-new-billionaires-are-conquering-the-world', 'another-story'),
    }), ARTICLE, { pageTimeoutMs: 1000, settleMs: 0 }),
    (error) => error instanceof EconomistFetchError && error.code === 'URL_NOT_ALLOWED'
  );
});
