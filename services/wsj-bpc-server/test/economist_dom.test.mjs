import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright';
import { readEconomistSnapshot } from '../browser.mjs';
import { sanitizeEconomistImages } from '../economist.mjs';

const articleUrl = 'https://www.economist.com/business/2026/07/19/test-article';
const cdn = (name, width) =>
  `https://www.economist.com/cdn-cgi/image/width=${width},quality=80,format=auto/content-assets/images/2026/07/${name}.jpg`;

test('Economist DOM extraction keeps exact prose, hero, body figures and captions only', { timeout: 20000 }, async () => {
  const browser = await chromium.launch({ channel: 'chromium', headless: true });
  try {
    const page = await browser.newPage();
    await page.route('https://www.economist.com/**', (route) => route.abort('blockedbyclient'));
    await page.setContent(`
      <!doctype html><html><head>
        <base href="${articleUrl}">
        <link rel="canonical" href="${articleUrl}?utm_source=test">
        <meta property="og:image" content="${cdn('hero', 1200)}">
        <script type="application/ld+json">${JSON.stringify({
          '@type': 'NewsArticle',
          headline: 'A test Economist article',
          datePublished: '2026-07-19T10:00:00Z',
          author: [{ '@type': 'Person', name: 'Jane Writer' }],
          image: { url: cdn('hero', 1200) },
        })}</script>
      </head><body>
        <img src="${cdn('page-logo', 600)}" width="600" height="300" alt="page chrome">
        <article>
          <h1>A test Economist article</h1>
          <figure class="illustration">
            <picture><source srcset="${cdn('wrong-hero', 600)} 600w, ${cdn('wrong-hero', 1000)} 1000w">
              <img src="${cdn('wrong-hero', 600)}" width="600" height="400" alt="wrong hero">
            </picture>
          </figure>
          <figure class="illustration">
            <picture><source srcset="${cdn('hero', 480)} 480w, ${cdn('hero', 1424)} 1424w">
              <img src="${cdn('hero', 480)}" width="712" height="475" alt="Hero scene">
            </picture>
            <figcaption style="text-transform:lowercase">Illustration: Test Artist</figcaption>
          </figure>
          <p>Your browser does not support the audio element.</p>
          <section data-component="article-body">
            <p data-component="paragraph"><span style="display:inline-block">F</span>${'irst accepted body paragraph. '.repeat(10)}</p>
            <figure data-component="chart">
              <picture><source srcset="${cdn('chart-one', 384)} 384w, ${cdn('chart-one', 1424)} 1424w">
                <img src="${cdn('chart-one', 384)}" width="712" height="356" alt="Chart one">
              </picture>
              <figcaption>chart: the economist</figcaption>
            </figure>
            <div data-component="RecommendedArticles">
              <p>Recommendation text must not be extracted.</p>
              <figure><a href="https://www.economist.com/finance-and-economics/2026/07/18/another-story">
                <img src="${cdn('inline-recommendation', 1000)}" width="1000" height="600" alt="recommendation">
              </a></figure>
            </div>
            <p data-component="paragraph">${'Second accepted body paragraph. '.repeat(10)}</p>
            <figure data-testid="article-image">
              <img src="${cdn('photo-two', 800)}"
                srcset="${cdn('photo-two', 480)} 480w, ${cdn('photo-two', 1200)} 1200w"
                width="600" height="400" alt="Second body photograph">
              <div data-component="image-caption">A full second caption</div>
            </figure>
            <p data-component="paragraph">${'Third accepted body paragraph. '.repeat(10)}</p>
            <figure><img src="${cdn('bottom-recommendation', 1200)}" width="1200" height="700" alt="bottom card"></figure>
          </section>
          <section data-component="onward-journey">
            <figure><img src="${cdn('outside-section', 1000)}" width="1000" height="600" alt="outside"></figure>
            <p>Latest and recommended stories</p>
          </section>
        </article>
      </body></html>
    `, { waitUntil: 'domcontentloaded' });

    const snapshot = await readEconomistSnapshot(page);
    assert.equal(snapshot.articlePresent, true);
    assert.equal(snapshot.bodySectionPresent, true);
    assert.equal(snapshot.challenge, false);
    assert.equal(snapshot.paywall, false);
    assert.equal(snapshot.title, 'A test Economist article');
    assert.equal(snapshot.author, 'Jane Writer');
    assert.equal(snapshot.publishedAt, '2026-07-19T10:00:00Z');
    assert.equal(snapshot.paragraphs.length, 3);
    assert.equal(snapshot.paragraphs[0].startsWith('First accepted'), true);
    assert.equal(snapshot.paragraphs.some((value) => value.includes('audio element')), false);
    assert.equal(snapshot.paragraphs.some((value) => value.includes('Recommendation')), false);
    assert.equal(snapshot.imageCandidates.some((candidate) => candidate.sources.some((source) =>
      String(source.url).includes('inline-recommendation'))), false);
    assert.equal(snapshot.imageCandidates.some((candidate) => candidate.sources.some((source) =>
      String(source.url).includes('outside-section'))), false);

    const images = sanitizeEconomistImages(
      snapshot.imageCandidates,
      articleUrl,
      snapshot.paragraphs.length,
      snapshot.trustedHeroUrls
    );
    assert.deepEqual(images.map((item) => ({
      asset: item.url.match(/\/([^/]+)\.jpg$/)?.[1],
      afterParagraph: item.afterParagraph,
      caption: item.caption,
      width: item.width,
    })), [
      { asset: 'hero', afterParagraph: -1, caption: 'Illustration: Test Artist', width: 1424 },
      { asset: 'chart-one', afterParagraph: 0, caption: 'chart: the economist', width: 1424 },
      { asset: 'photo-two', afterParagraph: 1, caption: 'A full second caption', width: 1200 },
    ]);
  } finally {
    await browser.close();
  }
});

test('Economist DOM snapshot exposes Cloudflare, paywall and BPC failure gates', { timeout: 20000 }, async () => {
  const browser = await chromium.launch({ channel: 'chromium', headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`<!doctype html><html><head><title>Just a moment...</title></head><body>
      <div id="challenge-running">Checking your browser before accessing the site</div>
      <div id="bpc_fail">BPC failed</div>
      <div data-component="paywall">Subscribe to continue</div>
    </body></html>`);
    const snapshot = await readEconomistSnapshot(page);
    assert.equal(snapshot.challenge, true);
    assert.equal(snapshot.bpcFailure, true);
    assert.equal(snapshot.paywall, true);
    assert.equal(snapshot.articlePresent, false);
    assert.deepEqual(snapshot.paragraphs, []);
  } finally {
    await browser.close();
  }
});

test('Economist newsletter BR prose is split safely and multi-image captions are not repeated', { timeout: 20000 }, async () => {
  const browser = await chromium.launch({ channel: 'chromium', headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`<!doctype html><html><head>
      <base href="${articleUrl}"><meta property="og:image" content="${cdn('newsletter', 1200)}">
    </head><body><article><h1>Newsletter article</h1>
      <section data-component="article-body">
        <div data-component="newsletter-signup"><p data-component="paragraph">Sign up teaser</p></div>
        <p data-component="paragraph"><span style="display:inline-block">E</span>arly first section ${'body '.repeat(30)}<br>
          Second section ${'body '.repeat(30)}<br>Third section ${'body '.repeat(30)}</p>
        <figure><img src="${cdn('gallery-one', 1200)}" width="1200" height="700">
          <img src="${cdn('gallery-two', 1200)}" width="1200" height="700">
          <figcaption style="text-transform:lowercase">Photographs: The Economist</figcaption>
        </figure>
      </section></article></body></html>`, { waitUntil: 'domcontentloaded' });
    const snapshot = await readEconomistSnapshot(page);
    assert.equal(snapshot.paragraphs.length, 3);
    assert.equal(snapshot.paragraphs[0].startsWith('Early first section'), true);
    assert.equal(snapshot.paragraphs.some((value) => value.includes('Sign up teaser')), false);
    const gallery = snapshot.imageCandidates.filter((candidate) =>
      candidate.sources.some((source) => String(source.url).includes('gallery-'))
    );
    assert.equal(gallery.length, 2);
    assert.deepEqual(gallery.map((candidate) => candidate.caption), [
      '', 'Photographs: The Economist',
    ]);
    const images = sanitizeEconomistImages(
      gallery, articleUrl, snapshot.paragraphs.length, []
    );
    assert.equal(images.length, 2);
    assert.deepEqual(images.map((item) => item.caption), [
      null, 'Photographs: The Economist',
    ]);
  } finally {
    await browser.close();
  }
});
