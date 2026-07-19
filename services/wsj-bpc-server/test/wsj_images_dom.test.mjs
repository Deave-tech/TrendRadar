import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright';
import { readWsjSnapshot } from '../browser.mjs';
import { sanitizeWsjImages } from '../wsj.mjs';

test('DOM extraction keeps only ordered images associated with article paragraphs', { timeout: 20000 }, async () => {
  const browser = await chromium.launch({ channel: 'chromium', headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <!doctype html>
      <html><head>
        <base href="https://cn.wsj.com/articles/current">
        <meta property="og:image" content="https://images.wsj.net/im-og-hero/social">
        <meta property="og:image:width" content="1280">
        <meta property="og:image:height" content="640">
        <meta name="image" content="https://images.wsj.net/im-generic-meta">
      </head><body>
        <img class="article-image" data-src="https://images.wsj.net/im-outside" width="900" height="600" alt="outside">
        <article><section>
          <figure>
            <img data-src="https://images.wsj.net/im-hero?utm_source=test" width="1200" height="800" alt="hero">
            <figcaption>Hero caption</figcaption>
          </figure>
          <div class="body">
            <p>${'First body paragraph. '.repeat(12)}</p>
            <div class="media"><figure><img data-src="https://images.wsj.net/im-wrapped" width="1000" height="650" alt="wrapped body photo"></figure></div>
            <figure>
              <picture>
                <source data-srcset="https://images.wsj.net/im-body?width=320 320w, https://images.wsj.net/im-body?width=1280 1280w">
                <img width="1280" height="720" alt="body photo">
              </picture>
              <figcaption>Body caption</figcaption>
            </figure>
            <p>${'Second body paragraph. '.repeat(12)}</p>
            <img class="article-image" data-lazy-src="https://images.wsj.net/im-body?width=640" width="640" height="360" alt="duplicate body photo">
            <img class="article-image" data-lazy-src="https://images.wsj.net/im-lazy" width="960" height="540" alt="lazy body photo">
            <div data-component="RecommendedStories">
              <a href="https://cn.wsj.com/articles/another-article">
                <img src="https://images.wsj.net/im-recommended" width="900" height="600" alt="recommended">
              </a>
            </div>
            <aside><img src="https://images.wsj.net/im-sidebar" width="900" height="600" alt="sidebar"></aside>
            <div class="unmarked-sidebar"><figure><img src="https://images.wsj.net/im-unmarked-side" width="900" height="600" alt="unmarked side module"></figure></div>
            <div>
              <figure><img src="https://images.wsj.net/im-unmarked-bottom" width="900" height="600" alt="unmarked bottom recommendation"></figure>
              <h3><a href="https://cn.wsj.com/articles/bottom-recommendation">Another story</a></h3>
            </div>
            <figure><img src="https://images.wsj.net/im-direct-bottom" width="900" height="600" alt="split-card recommendation"></figure>
            <h3><a href="https://cn.wsj.com/articles/direct-bottom-recommendation">A neighboring story</a></h3>
            <p>${'Third body paragraph. '.repeat(12)}</p>
            <div class="paywall"><figure>
              <img src="https://images.wsj.net/im-article-tail" width="900" height="600" alt="article tail gallery">
            </figure></div>
            <figure><img src="https://images.wsj.net/im-after-body" width="900" height="600" alt="footer recommendation"></figure>
          </div>
        </section></article>
        <img class="article-image" data-src="https://images.wsj.net/im-outside-after" width="900" height="600" alt="outside after">
      </body></html>
    `);

    const snapshot = await readWsjSnapshot(page);
    assert.equal(snapshot.paragraphs.length, 3);
    assert.equal(snapshot.imageCandidates.some((item) =>
      item.sources.some((source) => String(source.url).includes('im-outside'))
    ), false);

    const recommended = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-recommended'))
    );
    assert.equal(recommended.badContext, true);
    assert.equal(recommended.linkedArticle, true);
    const hero = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-hero'))
    );
    assert.equal(hero.inProseFlow, true);
    const wrapped = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-wrapped'))
    );
    assert.equal(wrapped.inProseFlow, true);
    assert.equal(wrapped.linkedArticle, false);
    const unmarkedSide = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-unmarked-side'))
    );
    assert.equal(unmarkedSide.badContext, false);
    assert.equal(unmarkedSide.inProseFlow, false);
    const unmarkedBottom = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-unmarked-bottom'))
    );
    assert.equal(unmarkedBottom.badContext, false);
    assert.equal(unmarkedBottom.inProseFlow, false);
    assert.equal(unmarkedBottom.linkedArticle, true);
    const directBottom = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-direct-bottom'))
    );
    assert.equal(directBottom.inProseFlow, true);
    assert.equal(directBottom.linkedArticle, true);
    const trailing = snapshot.imageCandidates.find((item) =>
      item.sources.some((source) => String(source.url).includes('im-after-body'))
    );
    assert.equal(trailing.afterParagraph, 2);

    const images = sanitizeWsjImages(
      snapshot.imageCandidates,
      'https://cn.wsj.com/articles/current',
      snapshot.paragraphs.length,
      snapshot.trustedImageMetadata
    );
    assert.deepEqual(images.map((item) => ({ url: item.url, afterParagraph: item.afterParagraph })), [
      { url: 'https://images.wsj.net/im-og-hero/social', afterParagraph: -1 },
      { url: 'https://images.wsj.net/im-hero?utm_source=test', afterParagraph: -1 },
      { url: 'https://images.wsj.net/im-wrapped', afterParagraph: 0 },
      { url: 'https://images.wsj.net/im-body?width=1280', afterParagraph: 0 },
      { url: 'https://images.wsj.net/im-lazy', afterParagraph: 1 },
      { url: 'https://images.wsj.net/im-article-tail', afterParagraph: 2 },
    ]);
    assert.equal(images[0].caption, null);
    assert.equal(images[1].caption, 'Hero caption');
    assert.equal(images[3].caption, 'Body caption');
    assert.equal(images[5].articleTail, true);

    // With fewer than three trusted paragraphs, recommendation lines must not
    // be pulled back in through an article.innerText fallback.
    await page.setContent(`
      <!doctype html><html><body><article><section>
        <p>Trusted paragraph one.</p>
        <div data-component="RecommendedStories">
          <p>Recommendation line one.</p><p>Recommendation line two.</p>
        </div>
        <p>Trusted paragraph two.</p>
      </section></article></body></html>
    `);
    const shortSnapshot = await readWsjSnapshot(page);
    assert.deepEqual(shortSnapshot.paragraphs, [
      'Trusted paragraph one.',
      'Trusted paragraph two.',
    ]);

    // Topic words are not structural recommendation signals: a market story's
    // ordinary prose and direct figure remain eligible.
    await page.setContent(`
      <!doctype html><html><head><base href="https://cn.wsj.com/articles/market-story"></head><body>
        <article><section><div class="market-analysis">
          <p>${'Market analysis paragraph one. '.repeat(5)}</p>
          <figure><picture><img data-src="https://images.wsj.net/im-market" width="900" height="600" alt="market chart"></picture></figure>
          <p>${'Market analysis paragraph two. '.repeat(5)}</p>
          <p>${'Market analysis paragraph three. '.repeat(5)}</p>
        </div></section></article>
      </body></html>
    `);
    const marketSnapshot = await readWsjSnapshot(page);
    assert.equal(marketSnapshot.paragraphs.length, 3);
    const marketImages = sanitizeWsjImages(
      marketSnapshot.imageCandidates,
      'https://cn.wsj.com/articles/market-story',
      marketSnapshot.paragraphs.length
    );
    assert.deepEqual(marketImages.map((item) => item.url), ['https://images.wsj.net/im-market']);
  } finally {
    await browser.close();
  }
});
