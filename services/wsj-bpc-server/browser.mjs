import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  articleSha256,
  isAntiBotChallengeUrl,
  sanitizeWsjImages,
  validateWsjArticleUrl,
} from './wsj.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Repo root (the extension directory). Override with BPC_DIR when the
// extension is mounted elsewhere (e.g. /bpc in Docker).
export const BPC_DIR = process.env.BPC_DIR || path.resolve(__dirname, '..');

// Fixed extension id, pinned by the "key" field in manifest.json.
export const EXTENSION_ID = 'lkbebcjgcmobigpeffafkodonchffocl';

/**
 * Launch a persistent Chromium context with the BPC extension side-loaded.
 * Uses channel 'chromium' (Playwright's bundled Chromium): branded Chrome has
 * removed the side-load flags, and only the new headless mode runs extensions.
 *
 * @param {string} profileDir user-data-dir (keeps extension storage across runs)
 * @param {{ loadExtension?: boolean }} opts
 */
export async function launchBpc(profileDir, { loadExtension = true } = {}) {
  const args = ['--disable-dev-shm-usage'];
  if (loadExtension) {
    args.push(`--disable-extensions-except=${BPC_DIR}`, `--load-extension=${BPC_DIR}`);
  }
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: 'chromium',
    // BPC_HEADFUL=1 runs a headed browser (use under xvfb-run on servers):
    // some anti-bot systems (DataDome etc.) are stricter with headless mode.
    headless: process.env.BPC_HEADFUL !== '1',
    ...(process.env.PROXY_SERVER ? { proxy: { server: process.env.PROXY_SERVER } } : {}),
    // BPC_UA overrides the UA (match the browser a harvested cookie belongs to).
    ...(process.env.BPC_UA ? { userAgent: process.env.BPC_UA } : {}),
    args,
  });
  // DD_COOKIE carries a DataDome trust cookie harvested from a real browser.
  if (process.env.DD_COOKIE) {
    await context.addCookies([
      { name: 'datadome', value: process.env.DD_COOKIE, domain: '.wsj.com', path: '/' },
    ]);
  }
  if (!loadExtension) return { context, sw: null };
  let sw = context.serviceWorkers()[0];
  if (!sw) sw = await context.waitForEvent('serviceworker', { timeout: 30000 });
  return { context, sw };
}

/**
 * Wait until the extension has built its site rules (fresh profiles go through
 * onInstalled -> storage write -> storage.onChanged -> DNR registration).
 * Returns the last observed counts; zeros mean the wait timed out.
 */
export async function waitForBpcReady(sw, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  let last = { sitesInStorage: 0, dnrSessionRules: 0 };
  while (Date.now() < deadline) {
    last = await sw.evaluate(async () => {
      const stored = await chrome.storage.local.get('sites');
      return {
        sitesInStorage: stored.sites ? Object.keys(stored.sites).length : 0,
        dnrSessionRules: (await chrome.declarativeNetRequest.getSessionRules()).length,
      };
    });
    if (last.sitesInStorage > 0 && last.dnrSessionRules > 0) return last;
    await new Promise((r) => setTimeout(r, 500));
  }
  return last;
}

/** Fetch one URL through the context and return extracted text. */
export async function fetchArticle(context, url, { pageTimeoutMs = 45000, settleMs = 6000, cookies = null } = {}) {
  const page = await context.newPage();
  try {
    if (cookies && cookies.length) await context.addCookies(cookies);
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: pageTimeoutMs });
    await page.waitForTimeout(settleMs); // let BPC content scripts do their work
    const title = await page.title();
    const text = await page.evaluate(() => (document.body ? document.body.innerText.trim() : ''));
    return {
      url: page.url(),
      status: resp ? resp.status() : null,
      title,
      text,
      textLength: text.length,
      fetchedAt: new Date().toISOString(),
    };
  } finally {
    await page.close().catch(() => {});
  }
}

export class WsjFetchError extends Error {
  constructor(code, httpStatus, retryable, message, details = undefined) {
    super(message);
    this.name = 'WsjFetchError';
    this.code = code;
    this.httpStatus = httpStatus;
    this.retryable = retryable;
    this.details = details;
  }
}

export function browserConnected(context) {
  try {
    const browser = context.browser();
    return Boolean(browser && browser.isConnected());
  } catch {
    return false;
  }
}

async function primeWsjLazyImages(page, deadline) {
  let plan;
  try {
    plan = await page.evaluate(() => {
      const article = document.querySelector('article section');
      if (!article) return null;
      const originalY = window.scrollY;
      const rect = article.getBoundingClientRect();
      const top = Math.max(0, rect.top + window.scrollY);
      const bottom = Math.max(top, rect.bottom + window.scrollY);
      const viewport = Math.max(1, window.innerHeight || 800);
      const steps = Math.min(6, Math.max(1, Math.ceil((bottom - top) / viewport)));
      const positions = [];
      for (let index = 0; index < steps; index++) {
        const ratio = steps === 1 ? 0 : index / (steps - 1);
        positions.push(Math.round(top + Math.max(0, bottom - top - viewport) * ratio));
      }
      return { originalY, positions: Array.from(new Set(positions)) };
    });
  } catch {
    return;
  }
  if (!plan) return;
  try {
    for (const position of plan.positions) {
      if (Date.now() + 100 >= deadline) break;
      await page.evaluate((y) => window.scrollTo(0, y), position);
      await page.waitForTimeout(100);
    }
  } finally {
    await page.evaluate((y) => window.scrollTo(0, y), plan.originalY).catch(() => {});
  }
}

export async function readWsjSnapshot(page) {
  return page.evaluate(() => {
    const clean = (value) => String(value || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t\f\v]+/g, ' ')
      .replace(/\s*\n\s*/g, ' ')
      .trim();
    const visible = (elem) => {
      if (!elem || elem.hidden) return false;
      const style = window.getComputedStyle(elem);
      return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    };
    const firstText = (selectors) => {
      for (const selector of selectors) {
        const elem = document.querySelector(selector);
        const value = clean(elem && (elem.innerText || elem.textContent));
        if (value) return value;
      }
      return '';
    };
    const meta = (selector) => clean(document.querySelector(selector)?.getAttribute('content'));
    // Keep page-wide metadata outside the DOM candidate list. Only this exact
    // Open Graph property (with explicit dimensions) is eligible for the
    // conservative hero fallback in sanitizeWsjImages(); arbitrary meta or
    // page images must still pass the article-flow checks below.
    const trustedImageMetadata = {
      ogImage: {
        url: meta('meta[property="og:image"]'),
        width: meta('meta[property="og:image:width"]'),
        height: meta('meta[property="og:image:height"]'),
      },
    };

    let schema = null;
    const articleTypes = new Set(['Article', 'NewsArticle', 'ReportageNewsArticle', 'AnalysisNewsArticle']);
    const findArticleSchema = (value) => {
      if (!value || typeof value !== 'object') return null;
      if (Array.isArray(value)) {
        for (const item of value) {
          const found = findArticleSchema(item);
          if (found) return found;
        }
        return null;
      }
      const types = Array.isArray(value['@type']) ? value['@type'] : [value['@type']];
      if (types.some((type) => articleTypes.has(type))) return value;
      if (value['@graph']) return findArticleSchema(value['@graph']);
      return null;
    };
    for (const script of document.querySelectorAll('script[type="application/ld+json"], script#articleschema')) {
      try {
        schema = findArticleSchema(JSON.parse(script.textContent));
      } catch {
        // Ignore unrelated or malformed JSON-LD blocks.
      }
      if (schema) break;
    }

    const article = document.querySelector('article section');
    const structuralMarker = (elem) => clean([
      elem?.id,
      typeof elem?.className === 'string' ? elem.className : '',
      elem?.getAttribute?.('data-testid'),
      elem?.getAttribute?.('data-component'),
      elem?.getAttribute?.('data-module'),
      elem?.getAttribute?.('aria-label'),
    ].filter(Boolean).join(' '))
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/[^a-zA-Z0-9\u4e00-\u9fff]+/g, ' ')
      .toLowerCase()
      .trim();
    const excludedContentContext = (node) => {
      for (let elem = node; elem && elem !== article; elem = elem.parentElement) {
        if (['ASIDE', 'NAV', 'FOOTER'].includes(elem.tagName)) return true;
        const role = clean(elem.getAttribute('role')).toLowerCase();
        if (['complementary', 'navigation', 'banner'].includes(role)) return true;
        const marker = structuralMarker(elem);
        if (/(^| )(recommend|recommended|recommendation|recommendations|related|recirc|recirculation|read next|more story|more stories|more article|more articles|most popular|popular|trending|newsletter|promo|promotion|sponsor|sponsored)( |$)/i.test(marker)) return true;
        if (/(推荐|相关|热门|排行|更多文章|继续阅读|延伸阅读|广告|赞助)/.test(marker)) return true;
        if (/(^| )(ad|ads|advert|advertisement|adwrapper|wsj ad)( |$)/i.test(marker)) return true;
      }
      return false;
    };
    const paragraphSelector = 'p, [data-type="paragraph"], div[style*="font-family"]';
    const articleHeadingSelector = 'h3, h4';
    const photoEssayHeadings = new Set();
    if (article) {
      for (const container of article.querySelectorAll(
        '.paywall, [class*="PaywalledContentContainer"]'
      )) {
        const children = Array.from(container.children);
        for (let index = 1; index + 2 < children.length; index++) {
          const media = children[index - 1];
          const group = children.slice(index, index + 3);
          if (!group[0].matches('h3') || !group[1].matches('h4') || !group[2].matches('h4')) continue;
          if (media.querySelectorAll('img').length !== 1 ||
            !media.querySelector('figure img, picture img') ||
            !/(^| )(media|image|photo|photograph|photography|figure)( |$)/.test(structuralMarker(media)) ||
            media.querySelector('a[href], h1, h2, h3, h4, h5, h6, button, [role="button"]') ||
            excludedContentContext(media)) continue;
          if (group.some((elem) => !visible(elem) || excludedContentContext(elem) ||
            elem.querySelector('a[href], img, picture, figure, button, [role="button"]'))) continue;
          // WSJ photo essays render the text belonging to a photograph as a
          // strict H3/H4/H4 trio immediately after its single-image media
          // block. Trust only that narrow publisher-owned sequence so generic
          // article/recommendation headings never enter正文 extraction.
          group.forEach((elem) => photoEssayHeadings.add(elem));
        }
      }
    }
    const paragraphs = [];
    const paragraphElements = [];
    const seen = new Set();
    if (article) {
      const addParagraph = (value, elem = null) => {
        const text = clean(value);
        if (text.length < 2 || seen.has(text)) return;
        seen.add(text);
        paragraphs.push(text);
        if (elem) paragraphElements.push(elem);
      };
      const candidates = Array.from(article.querySelectorAll(
        `${paragraphSelector}, ${articleHeadingSelector}`
      ));
      for (const elem of candidates) {
        if (elem.matches(articleHeadingSelector) && !photoEssayHeadings.has(elem)) continue;
        // Keep the deepest matching element to avoid duplicating nested text.
        if (elem.querySelector(paragraphSelector)) continue;
        if (elem.closest('aside, figure, nav, footer, [role="complementary"], [role="navigation"], .wsj-ad, .adWrapper')) continue;
        if (excludedContentContext(elem)) continue;
        if (!visible(elem)) continue;
        addParagraph(elem.innerText || elem.textContent, elem);
      }

      // Deliberately fail closed when the trusted paragraph selectors find too
      // little text. Splitting article.innerText here would re-introduce lines
      // from recommendation/recirculation modules that were rejected above.
    }

    const imageCandidates = [];
    if (article && paragraphElements.length === paragraphs.length) {
      const parseSrcset = (value) => String(value || '').split(',').map((part) => {
        const match = part.trim().match(/^(\S+)(?:\s+([0-9]+(?:\.[0-9]+)?)(w|x))?$/i);
        if (!match) return null;
        return {
          url: match[1],
          widthHint: match[3]?.toLowerCase() === 'w' ? Number(match[2]) : null,
          density: match[3]?.toLowerCase() === 'x' ? Number(match[2]) : null,
        };
      }).filter(Boolean);
      const branchUnder = (root, node) => {
        if (!root || !node || root === node || !root.contains(node)) return null;
        let branch = node;
        while (branch.parentElement && branch.parentElement !== root) branch = branch.parentElement;
        return branch.parentElement === root ? branch : null;
      };
      let currentArticleUrl = null;
      try {
        currentArticleUrl = new URL(document.baseURI || location.href);
      } catch {
        // Any unit containing an article-looking link fails closed below.
      }
      const unitLinksAnotherArticle = (unit) => {
        const links = [];
        if (unit?.matches?.('a[href]')) links.push(unit);
        if (unit?.querySelectorAll) links.push(...unit.querySelectorAll('a[href]'));
        for (const link of links) {
          try {
            const linked = new URL(link.href, document.baseURI || location.href);
            if (linked.pathname.startsWith('/articles/') && (!currentArticleUrl ||
              linked.hostname !== currentArticleUrl.hostname || linked.pathname !== currentArticleUrl.pathname))
              return true;
          } catch {
            return true;
          }
        }
        return false;
      };
      const trustedSingleMediaWrapper = (wrapper, mediaBlock) => {
        if (!wrapper || mediaBlock.parentElement !== wrapper) return false;
        const marker = structuralMarker(wrapper);
        if (!/(^| )(media|image|photo|photograph|photography|figure)( |$)/.test(marker)) return false;
        if (wrapper.matches(paragraphSelector) || wrapper.querySelector(paragraphSelector)) return false;
        if (wrapper.querySelectorAll('img').length !== 1) return false;
        if (wrapper.querySelector('h1, h2, h3, h4, h5, h6, button, [role="button"]')) return false;
        return !unitLinksAnotherArticle(wrapper);
      };
      const flowOwned = (mediaBlock) => {
        // A media block owns a place in正文 flow only when it is a direct branch
        // of some ancestor that itself owns at least two accepted paragraphs.
        // Thus a direct <figure>/<picture>/<img> remains valid, including a
        // leading hero outside the body wrapper, while media hidden in an
        // unlabelled sidebar/card branch fails closed.
        for (let owner = mediaBlock.parentElement; owner; owner = owner.parentElement) {
          if (owner !== article && !article.contains(owner)) break;
          if (paragraphElements.includes(owner)) return true;
          const ownedParagraphs = paragraphElements.filter((paragraph) => owner.contains(paragraph));
          if (ownedParagraphs.length >= 2) {
            const branch = branchUnder(owner, mediaBlock);
            if (branch === mediaBlock || trustedSingleMediaWrapper(branch, mediaBlock)) return true;
          }
          if (owner === article) break;
        }
        return false;
      };
      const mediaFlowUnit = (mediaBlock) => {
        // Scan the largest surrounding unit that owns no正文 paragraph. This
        // catches article-card links that are siblings of, rather than wrappers
        // around, the thumbnail image.
        let unit = mediaBlock;
        while (unit.parentElement && unit.parentElement !== article &&
          !paragraphElements.some((paragraph) => unit.parentElement.contains(paragraph))) {
          unit = unit.parentElement;
        }
        return unit;
      };
      const linkedToAnotherArticle = (flowUnit) => {
        const units = [flowUnit];
        // Recommendation cards sometimes render a direct <figure> and put the
        // headline link in an adjacent sibling instead of wrapping the image.
        // Inspect only immediate siblings that own no accepted正文 paragraph;
        // crossing a正文 paragraph would conflate unrelated inline links.
        for (const sibling of [flowUnit.previousElementSibling, flowUnit.nextElementSibling]) {
          if (!sibling || paragraphElements.some((paragraph) =>
            sibling === paragraph || sibling.contains(paragraph))) continue;
          // Only headline-like, media-free siblings can belong to a split card.
          // A complete neighboring media card must not taint a legitimate
          // inline image merely because it also links to another article.
          const headlineLike = sibling.matches('a[href], h1, h2, h3, h4, h5, h6') ||
            Boolean(sibling.querySelector('h1, h2, h3, h4, h5, h6')) ||
            (sibling.childElementCount <= 2 && Boolean(sibling.querySelector('a[href]')));
          if (headlineLike && !sibling.querySelector('img, picture, figure')) units.push(sibling);
        }
        return units.some(unitLinksAnotherArticle);
      };
      const captionFor = (img) => {
        const figure = img.closest('figure');
        const container = figure || img.closest('picture')?.parentElement;
        if (!container) return '';
        const caption = container.querySelector(
          'figcaption, [data-testid*="caption" i], [class*="caption" i]'
        );
        return clean(caption && (caption.innerText || caption.textContent));
      };
      const addSource = (sources, url, widthHint = null, density = null) => {
        if (url) sources.push({ url, widthHint, density });
      };
      for (const img of article.querySelectorAll('img')) {
        const figure = img.closest('figure');
        const picture = img.closest('picture');
        const mediaBlock = figure || picture || img;
        const flowUnit = mediaFlowUnit(mediaBlock);
        const semantic = figure ? 'figure' : picture ? 'picture' : 'img';
        let afterParagraph = -1;
        for (let index = 0; index < paragraphElements.length; index++) {
          if (paragraphElements[index].compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING)
            afterParagraph = index;
        }

        const sources = [];
        addSource(sources, img.currentSrc);
        for (const attr of ['data-src', 'data-lazy-src', 'data-original', 'data-image-src', 'data-url'])
          addSource(sources, img.getAttribute(attr));
        for (const attr of ['srcset', 'data-srcset', 'data-lazy-srcset'])
          sources.push(...parseSrcset(img.getAttribute(attr)));
        if (picture) {
          for (const source of picture.querySelectorAll('source')) {
            for (const attr of ['srcset', 'data-srcset', 'data-lazy-srcset'])
              sources.push(...parseSrcset(source.getAttribute(attr)));
          }
        }
        // Plain src is last because it is frequently a small lazy-load
        // placeholder; source selection prefers explicit srcset widths.
        addSource(sources, img.getAttribute('src'));

        const marker = clean([
          img.id,
          typeof img.className === 'string' ? img.className : '',
          img.getAttribute('data-testid'),
          img.parentElement?.getAttribute('data-testid'),
        ].filter(Boolean).join(' '));
        imageCandidates.push({
          sources,
          alt: clean(img.getAttribute('alt')),
          caption: captionFor(img),
          afterParagraph,
          width: img.naturalWidth || Number(img.getAttribute('width')) || null,
          height: img.naturalHeight || Number(img.getAttribute('height')) || null,
          semantic,
          mediaHint: /(image|photo|media|picture)/i.test(marker),
          inProseFlow: flowOwned(mediaBlock),
          // WSJ sometimes keeps a legitimate end-of-article photo gallery in
          // its paywalled-content container after the final prose paragraph.
          // This narrow publisher-owned marker lets the sanitizer distinguish
          // that gallery from recommendation modules elsewhere in <article>.
          articleContent: Boolean(img.closest(
            '.paywall, [class*="PaywalledContentContainer"]'
          )),
          badContext: excludedContentContext(img) || Boolean(img.closest('video, [aria-hidden="true"]')),
          linkedArticle: linkedToAnotherArticle(flowUnit),
          visible: visible(img),
        });
      }
    }

    const paywallSelectors = [
      '.snippet-promotion',
      'div[id*="-snippet-overlay"]',
      '#paywall',
      '[data-testid*="paywall" i]',
      '[aria-label*="paywall" i]',
    ];
    const paywall = paywallSelectors.some((selector) => {
      try {
        return Array.from(document.querySelectorAll(selector)).some(visible);
      } catch {
        return false;
      }
    });
    const bpcFailureElem = document.querySelector('#bpc_fail, #bpc_nofix');
    const bpcFailure = Boolean(bpcFailureElem);

    const bodyStart = clean(document.body?.innerText).slice(0, 4000);
    const title = firstText(['article h1', 'main h1', 'h1']) ||
      clean(schema?.headline) || meta('meta[property="og:title"]') || clean(document.title);
    const challengeElement = document.querySelector(
      '#datadome-captcha, iframe[src*="captcha-delivery"], script[src*="captcha-delivery"], [class*="captcha" i]'
    );
    const challengeWords = /verify (that )?you are human|unusual activity|press and hold|captcha|请.{0,8}(验证|完成验证)|访问.{0,8}(受限|异常)/i;
    const challenge = Boolean(challengeElement) ||
      /captcha-delivery\.com|geo\.captcha-delivery\.com/i.test(location.href) ||
      challengeWords.test(document.title) || (!article && challengeWords.test(bodyStart));

    let author = schema?.author;
    if (Array.isArray(author)) author = author.map((item) => typeof item === 'string' ? item : item?.name).filter(Boolean).join('、');
    else if (author && typeof author === 'object') author = author.name;
    author = clean(author) || meta('meta[name="author"]') || firstText([
      '[rel="author"]',
      '[data-testid*="author" i]',
      '[class*="author" i]',
    ]);

    const publishedAt = clean(schema?.datePublished) ||
      meta('meta[property="article:published_time"]') ||
      document.querySelector('time[datetime]')?.getAttribute('datetime') || '';
    const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;

    return {
      title,
      author,
      publishedAt: clean(publishedAt),
      canonical,
      paragraphs,
      imageCandidates,
      trustedImageMetadata,
      paywall,
      bpcFailure,
      bpcFailureText: clean(bpcFailureElem?.innerText || bpcFailureElem?.textContent).slice(0, 200),
      challenge,
      articlePresent: Boolean(article),
    };
  });
}

/**
 * Fetch and extract a WSJ Chinese article. Unlike fetchArticle(), this never
 * returns the whole page and enforces the quality contract consumed by the
 * TrendRadar delivery outbox.
 */
export async function fetchWsjArticle(
  context,
  url,
  { pageTimeoutMs = 45000, settleMs = 6000, cookies = null } = {}
) {
  const input = validateWsjArticleUrl(url);
  if (!input.ok)
    throw new WsjFetchError('URL_NOT_ALLOWED', 403, false, input.reason);
  if (!browserConnected(context))
    throw new WsjFetchError('SERVICE_NOT_READY', 503, true, 'browser is not connected');

  const page = await context.newPage();
  const deadline = Date.now() + pageTimeoutMs;
  let blockedNavigation = null;
  try {
    if (cookies && cookies.length) await context.addCookies(cookies);

    // Abort a main-frame redirect before it reaches a different host/path.
    // Subresources and iframes remain untouched so BPC's DNR rules still see
    // the normal WSJ page load.
    await page.route('**/*', async (route) => {
      const request = route.request();
      if (request.resourceType() === 'document' && request.frame() === page.mainFrame()) {
        const checked = validateWsjArticleUrl(request.url());
        if (!checked.ok) {
          blockedNavigation = isAntiBotChallengeUrl(request.url()) ? 'challenge' : 'scope';
          return route.abort('blockedbyclient');
        }
      }
      // `fallback` behaves like continue when no other route is registered,
      // while still allowing a context-level route in isolated tests.
      return route.fallback();
    });

    let response;
    try {
      response = await page.goto(input.url.href, {
        waitUntil: 'domcontentloaded',
        timeout: Math.max(1, deadline - Date.now()),
      });
    } catch (error) {
      if (blockedNavigation === 'challenge')
        throw new WsjFetchError('CHALLENGE_DETECTED', 422, true, 'anti-bot challenge redirect was blocked');
      if (blockedNavigation === 'scope')
        throw new WsjFetchError('URL_NOT_ALLOWED', 403, false, 'cross-domain or non-article redirect was blocked');
      throw new WsjFetchError('UPSTREAM_ERROR', 502, true, 'upstream navigation failed', {
        reason: error?.name === 'TimeoutError' ? 'timeout' : 'navigation',
      });
    }

    const finalUrl = validateWsjArticleUrl(page.url());
    if (!finalUrl.ok && isAntiBotChallengeUrl(page.url()))
      throw new WsjFetchError('CHALLENGE_DETECTED', 422, true, 'anti-bot challenge redirect detected');
    if (!finalUrl.ok)
      throw new WsjFetchError('URL_NOT_ALLOWED', 403, false, 'upstream redirected outside the allowed URL scope');
    if (!response)
      throw new WsjFetchError('UPSTREAM_ERROR', 502, true, 'upstream returned no document response');
    if (response.status() !== 200)
      throw new WsjFetchError('UPSTREAM_STATUS', 502, true, 'upstream did not return HTTP 200', {
        upstreamStatus: response.status(),
      });

    const initialWait = Math.min(settleMs, Math.max(0, deadline - Date.now()));
    if (initialWait) await page.waitForTimeout(initialWait);
    await primeWsjLazyImages(page, deadline);

    let lastSnapshot = null;
    let lastText = '';
    let lastImageManifest = '';
    let lastImages = [];
    let stableSamples = 0;
    while (Date.now() < deadline) {
      let snapshot;
      try {
        snapshot = await readWsjSnapshot(page);
      } catch {
        throw new WsjFetchError('UPSTREAM_ERROR', 502, true, 'article DOM could not be read');
      }
      lastSnapshot = snapshot;
      if (snapshot.challenge || snapshot.bpcFailure) break;

      const text = snapshot.paragraphs.join('\n\n');
      const images = sanitizeWsjImages(
        snapshot.imageCandidates,
        finalUrl.url.href,
        snapshot.paragraphs.length,
        snapshot.trustedImageMetadata
      );
      const imageManifest = JSON.stringify(images);
      const usable = snapshot.articlePresent && !snapshot.paywall && text.length > 0;
      if (usable && text === lastText && imageManifest === lastImageManifest) stableSamples++;
      else stableSamples = usable ? 1 : 0;
      lastText = usable ? text : '';
      lastImageManifest = usable ? imageManifest : '';
      lastImages = usable ? images : [];
      if (stableSamples >= 3) break;
      await page.waitForTimeout(Math.min(500, Math.max(1, deadline - Date.now())));
    }

    if (lastSnapshot?.challenge)
      throw new WsjFetchError('CHALLENGE_DETECTED', 422, true, 'anti-bot challenge detected');
    if (lastSnapshot?.bpcFailure)
      throw new WsjFetchError('BPC_FAILURE', 422, true, 'BPC reported that full text could not be recovered');
    if (lastSnapshot?.paywall)
      throw new WsjFetchError('PAYWALL_PRESENT', 422, true, 'paywall remained after BPC processing');
    if (!lastSnapshot?.articlePresent)
      throw new WsjFetchError('ARTICLE_NOT_READY', 503, true, 'article section was not available before timeout');
    if (stableSamples < 3)
      throw new WsjFetchError('ARTICLE_NOT_READY', 503, true, 'article text and image manifest did not become stable before timeout');

    const paragraphs = lastSnapshot.paragraphs;
    const text = paragraphs.join('\n\n');
    // A few legitimate short Chinese WSJ pieces land just under 500 Unicode
    // code points (for example 498) while still containing three stable prose
    // paragraphs. Keep the gate narrow but avoid rejecting those full pages.
    if (paragraphs.length < 3 || text.length < 480)
      throw new WsjFetchError('ARTICLE_QUALITY_FAILED', 422, false, 'article did not meet the minimum text quality gate', {
        textLength: text.length,
        paragraphCount: paragraphs.length,
      });

    const canonicalResult = validateWsjArticleUrl(lastSnapshot.canonical);
    const canonicalUrl = canonicalResult.ok ? canonicalResult.url.href : finalUrl.url.href;
    const images = lastImages;
    let publishedAt = lastSnapshot.publishedAt || null;
    if (publishedAt && Number.isNaN(Date.parse(publishedAt))) publishedAt = null;

    return {
      url: finalUrl.url.href,
      canonicalUrl,
      status: response.status(),
      title: lastSnapshot.title || '',
      author: lastSnapshot.author || null,
      publishedAt,
      paragraphs,
      images,
      text,
      textLength: text.length,
      paragraphCount: paragraphs.length,
      imageCount: images.length,
      sha256: articleSha256(paragraphs),
      fetchedAt: new Date().toISOString(),
    };
  } finally {
    await page.close().catch(() => {});
  }
}
