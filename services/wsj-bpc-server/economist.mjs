import { createHash } from 'node:crypto';

export const ECONOMIST_HOST = 'www.economist.com';
export const MAX_ECONOMIST_ARTICLE_IMAGES = 20;

const ECONOMIST_IMAGE_FORMATS = new Map([
  ['jpg', 'jpg'],
  ['jpeg', 'jpg'],
  ['png', 'png'],
  ['gif', 'gif'],
  ['webp', 'webp'],
]);

const NON_ARTICLE_SECTIONS = new Set([
  'audio',
  'film',
  'films',
  'interactive',
  'podcast',
  'podcasts',
  'video',
  'videos',
]);

function cleanText(value, limit = 1000) {
  const text = String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t\f\v]+/g, ' ')
    .replace(/\s*\n\s*/g, ' ')
    .trim();
  return text ? text.slice(0, limit) : null;
}

function safeDimension(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0 || number > 20000) return null;
  return Math.round(number);
}

/**
 * Accept only Economist's dated prose-article URL shape.
 *
 * Query strings and fragments are intentionally discarded. They are not part
 * of an Economist article's identity and retaining them would let one article
 * enter an outbox more than once through tracking links.
 */
export function validateEconomistArticleUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, reason: 'URL must be an absolute URL' };
  }

  if (url.protocol !== 'https:')
    return { ok: false, reason: 'URL must use HTTPS' };
  if (url.hostname !== ECONOMIST_HOST)
    return { ok: false, reason: `URL host must be ${ECONOMIST_HOST}` };
  if (url.port && url.port !== '443')
    return { ok: false, reason: 'URL must use the default HTTPS port' };
  if (url.username || url.password)
    return { ok: false, reason: 'URL credentials are not allowed' };

  const match = url.pathname.match(
    /^\/([a-z0-9][a-z0-9-]*)\/(\d{4})\/(\d{2})\/(\d{2})\/([a-z0-9][a-z0-9-]*)(?:\/)?$/
  );
  if (!match)
    return { ok: false, reason: 'URL path must be a dated Economist article' };
  if (NON_ARTICLE_SECTIONS.has(match[1]))
    return { ok: false, reason: 'audio, podcast and video URLs are not allowed' };

  const year = Number(match[2]);
  const month = Number(match[3]);
  const day = Number(match[4]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (year < 1900 || year > 2099 || date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day)
    return { ok: false, reason: 'URL contains an invalid publication date' };

  url.search = '';
  url.hash = '';
  if (url.pathname.endsWith('/')) url.pathname = url.pathname.slice(0, -1);
  return { ok: true, url };
}

export function isEconomistChallengeUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    return host === 'challenges.cloudflare.com' || host.endsWith('.challenges.cloudflare.com') ||
      (host === ECONOMIST_HOST && url.pathname.startsWith('/cdn-cgi/challenge-platform/'));
  } catch {
    return false;
  }
}

/**
 * Only the publisher's editorial-image path is downloadable.
 *
 * Economist's Cloudflare endpoint serves WebP bytes for `format=auto` when the
 * client advertises WebP, but retains the original asset's Content-Type (for
 * example image/jpeg).  That mismatch must be rejected by the downloader.  A
 * deterministic format matching the source extension keeps both the response
 * bytes and Content-Type truthful without weakening download-time validation.
 */
export function normalizeEconomistImageUrl(value, baseUrl) {
  if (typeof value !== 'string' || !value.trim() || value.length > 8192) return null;
  let url;
  try {
    url = new URL(value.trim(), baseUrl);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.hostname !== ECONOMIST_HOST) return null;
  if (url.port && url.port !== '443') return null;
  if (url.username || url.password) return null;
  url.hostname = ECONOMIST_HOST;
  url.hash = '';

  let path;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
  // Cloudflare's transform directives occupy exactly the component between
  // /cdn-cgi/image/ and /content-assets/images/. Refuse generic site assets,
  // avatars, logos and recommendation-network hosts even when they are HTTPS.
  const assetMatch = path.match(
    /^\/(?:cdn-cgi\/image\/[^/]+\/)?content-assets\/images\/.+\.([a-z0-9]+)$/i
  );
  if (!assetMatch) return null;
  const outputFormat = ECONOMIST_IMAGE_FORMATS.get(assetMatch[1].toLowerCase());
  if (!outputFormat) return null;
  if (/(^|[\/_.-])(logo|icon|avatar|badge|sprite|spacer|pixel|tracking|tracker|beacon|favicon|emoji|loading)([\/_.-]|$)/i.test(path))
    return null;
  const transformed = path.match(
    /^\/cdn-cgi\/image\/([^/]+)(\/content-assets\/images\/.+)$/i
  );
  if (transformed) {
    const directives = transformed[1]
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item && !/^format(?:=|$)/i.test(item));
    directives.push(`format=${outputFormat}`);
    url.pathname = `/cdn-cgi/image/${directives.join(',')}${transformed[2]}`;
  } else {
    url.pathname = path;
  }
  url.search = '';
  return url.href;
}

function imageAssetKey(value) {
  const url = new URL(value);
  let path = url.pathname;
  try {
    path = decodeURIComponent(path);
  } catch {
    // normalizeEconomistImageUrl already rejected malformed encodings.
  }
  const marker = '/content-assets/images/';
  const index = path.toLowerCase().indexOf(marker);
  return index >= 0 ? path.slice(index).toLowerCase() : url.href;
}

function chooseLargestSource(sources, baseUrl) {
  const valid = [];
  for (const [index, source] of Array.from(sources || []).slice(0, 100).entries()) {
    const rawUrl = typeof source === 'string' ? source : source?.url;
    const url = normalizeEconomistImageUrl(rawUrl, baseUrl);
    if (!url) continue;
    const widthHint = safeDimension(typeof source === 'object' ? source?.widthHint : null);
    const densityValue = Number(typeof source === 'object' ? source?.density : 0);
    const density = Number.isFinite(densityValue) && densityValue > 0 && densityValue <= 10
      ? densityValue
      : 0;
    valid.push({ url, widthHint, density, index });
  }
  valid.sort((left, right) =>
    (right.widthHint || 0) - (left.widthHint || 0) ||
    right.density - left.density ||
    left.index - right.index
  );
  return valid[0] || null;
}

/**
 * Convert the deliberately narrow DOM snapshot into delivery image records.
 * Only one leading hero and figures between accepted正文 paragraphs survive.
 */
export function sanitizeEconomistImages(candidates, baseUrl, paragraphCount, trustedHeroUrls = []) {
  if (!Number.isInteger(paragraphCount) || paragraphCount < 1) return [];
  const trustedAssets = new Set();
  const trustedHeroes = [];
  for (const value of Array.from(trustedHeroUrls || []).slice(0, 20)) {
    const normalized = normalizeEconomistImageUrl(value, baseUrl);
    if (!normalized) continue;
    const assetKey = imageAssetKey(normalized);
    if (trustedAssets.has(assetKey)) continue;
    trustedAssets.add(assetKey);
    trustedHeroes.push({ url: normalized, assetKey });
  }

  const prepared = [];
  for (const [inputIndex, candidate] of Array.from(candidates || []).slice(0, 200).entries()) {
    if (!candidate || typeof candidate !== 'object') continue;
    if (!['hero', 'body'].includes(candidate.scope) || candidate.semantic !== 'figure') continue;
    if (candidate.visible === false || candidate.badContext || candidate.linkedArticle) continue;
    const afterParagraph = Number(candidate.afterParagraph);
    if (!Number.isInteger(afterParagraph)) continue;
    if (candidate.scope === 'hero' && afterParagraph !== -1) continue;
    if (candidate.scope === 'body' &&
      (afterParagraph < -1 || afterParagraph >= paragraphCount)) continue;

    const source = chooseLargestSource(candidate.sources, baseUrl);
    if (!source) continue;
    const intrinsicWidth = safeDimension(candidate.width);
    const intrinsicHeight = safeDimension(candidate.height);
    if ((intrinsicWidth && intrinsicWidth < 300) || (intrinsicHeight && intrinsicHeight < 150))
      continue;
    const width = source.widthHint || intrinsicWidth;
    let height = intrinsicHeight;
    if (width && intrinsicWidth && intrinsicHeight && width !== intrinsicWidth)
      height = Math.round(intrinsicHeight * width / intrinsicWidth);
    if ((width && width < 300) || (height && height < 150)) continue;

    const alt = cleanText(candidate.alt);
    const caption = cleanText(candidate.caption);
    if (!width && !height && !alt && !caption) continue;
    const assetKey = imageAssetKey(source.url);
    // A figure after the last paragraph is accepted only with article-specific
    // evidence. Recommendation cards are normally linked and outside the body
    // section; requiring a figure caption or NewsArticle/og:image match closes
    // the remaining tail ambiguity without dropping legitimate photo essays.
    const articleTail = candidate.scope === 'body' &&
      afterParagraph === paragraphCount - 1;
    if (articleTail && !caption && !candidate.figureCaptioned &&
      !trustedAssets.has(assetKey)) continue;
    prepared.push({
      url: source.url,
      alt,
      caption,
      afterParagraph,
      width: width || null,
      height: height || null,
      scope: candidate.scope,
      trustedHero: candidate.scope === 'hero' && trustedAssets.has(assetKey),
      domIndex: Number.isInteger(candidate.domIndex) ? candidate.domIndex : inputIndex,
      assetKey,
      articleTail,
    });
  }

  // Prefer a metadata-matched DOM hero. If no DOM figure matches the article
  // metadata, the trusted metadata fallback is safer than an unrelated leading
  // decorative figure; only then fall back to the first strict DOM hero.
  const heroes = prepared.filter((item) => item.scope === 'hero');
  heroes.sort((left, right) => left.domIndex - right.domIndex);
  let selectedHero = heroes.find((item) => item.trustedHero) || null;
  let promotedAsset = null;
  // Some immersive Economist templates expose the leading illustration only
  // through the NewsArticle/og:image metadata while their DOM copies sit after
  // the final prose paragraph.  In that narrow case use the publisher-owned
  // editorial asset as a hero rather than accepting any tail/recommendation
  // figure.  Download-time image validation still supplies the true dimensions.
  for (const trusted of selectedHero ? [] : trustedHeroes) {
    const domCopy = prepared.find((item) =>
      item.scope === 'body' && item.assetKey === trusted.assetKey
    );
    if (domCopy) {
      promotedAsset = domCopy.assetKey;
      selectedHero = {
        ...domCopy,
        scope: 'hero',
        afterParagraph: -1,
        articleTail: false,
        trustedHero: true,
      };
      break;
    }
    const widthMatch = trusted.url.match(/\/cdn-cgi\/image\/[^/]*\bwidth=(\d+)/i);
    const width = widthMatch ? safeDimension(widthMatch[1]) : null;
    if (!width || width >= 600) {
      selectedHero = {
        url: trusted.url,
        alt: null,
        caption: null,
        afterParagraph: -1,
        width,
        height: null,
        scope: 'hero',
        trustedHero: true,
        domIndex: -1,
        assetKey: trusted.assetKey,
      };
      break;
    }
  }
  if (!selectedHero) selectedHero = heroes[0] || null;
  const body = prepared.filter((item) =>
    item.scope === 'body' && item.assetKey !== promotedAsset
  )
    .sort((left, right) => left.domIndex - right.domIndex);
  const ordered = selectedHero ? [selectedHero, ...body] : body;

  const images = [];
  const seen = new Set();
  for (const item of ordered) {
    if (seen.has(item.assetKey)) continue;
    seen.add(item.assetKey);
    images.push({
      url: item.url,
      alt: item.alt,
      caption: item.caption,
      afterParagraph: item.afterParagraph,
      width: item.width,
      height: item.height,
      ...(item.articleTail ? { articleTail: true } : {}),
    });
    if (images.length >= MAX_ECONOMIST_ARTICLE_IMAGES) break;
  }
  return images;
}

export function economistArticleSha256(paragraphs) {
  return createHash('sha256').update(paragraphs.join('\n\n'), 'utf8').digest('hex');
}

export class EconomistFetchError extends Error {
  constructor(code, httpStatus, retryable, message, details = undefined) {
    super(message);
    this.name = 'EconomistFetchError';
    this.code = code;
    this.httpStatus = httpStatus;
    this.retryable = retryable;
    this.details = details;
  }
}

/**
 * Browser-only snapshot function. It is self-contained so Playwright can
 * serialize it directly into the page without injecting remote code.
 */
export function extractEconomistSnapshot() {
  const clean = (value, limit = 10000) => String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t\f\v]+/g, ' ')
    .replace(/\s*\n\s*/g, ' ')
    .trim()
    .slice(0, limit);
  const visible = (elem) => {
    if (!elem || elem.hidden) return false;
    const style = window.getComputedStyle(elem);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  };
  const structuralMarker = (elem) => clean([
    elem?.id,
    typeof elem?.className === 'string' ? elem.className : '',
    elem?.getAttribute?.('data-testid'),
    elem?.getAttribute?.('data-component'),
    elem?.getAttribute?.('data-module'),
    elem?.getAttribute?.('aria-label'),
  ].filter(Boolean).join(' '), 2000)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[^a-zA-Z0-9]+/g, ' ')
    .toLowerCase()
    .trim();
  const badContext = (node, boundary) => {
    for (let elem = node; elem; elem = elem.parentElement) {
      if (['ASIDE', 'NAV', 'FOOTER'].includes(elem.tagName)) return true;
      const role = clean(elem.getAttribute?.('role')).toLowerCase();
      if (['complementary', 'navigation', 'banner'].includes(role)) return true;
      const marker = structuralMarker(elem);
      if (/(^| )(recommend|recommended|recommendation|related|recirc|recirculation|onward journey|read next|more stories|most read|most popular|popular|latest stories|newsletter|promo|promotion|sponsor|sponsored)( |$)/.test(marker))
        return true;
      if (/(^| )(advert|advertisement|ad wrapper)( |$)/.test(marker)) return true;
      if (elem === boundary) break;
    }
    return false;
  };
  const meta = (selector) => clean(document.querySelector(selector)?.getAttribute('content'));

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
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      schema = findArticleSchema(JSON.parse(script.textContent));
    } catch {
      // Ignore unrelated/malformed JSON-LD blocks.
    }
    if (schema) break;
  }

  const article = document.querySelector('article');
  const sections = article ? Array.from(article.querySelectorAll('section')) : [];
  let bodySection = null;
  let bodyScore = { count: 0, length: 0 };
  for (const section of sections) {
    if (badContext(section, article)) continue;
    const paragraphs = Array.from(section.querySelectorAll('p[data-component="paragraph"]'))
      .filter((elem) => visible(elem) && !badContext(elem, section));
    const score = {
      count: paragraphs.length,
      length: paragraphs.reduce((sum, elem) => sum + clean(elem.textContent || elem.innerText).length, 0),
    };
    const better = score.count > bodyScore.count ||
      (score.count === bodyScore.count && score.length > bodyScore.length) ||
      (score.count === bodyScore.count && score.length === bodyScore.length &&
        bodySection?.contains(section));
    if (better) {
      bodySection = section;
      bodyScore = score;
    }
  }

  const paragraphs = [];
  const paragraphElements = [];
  const seenParagraphs = new Set();
  const paragraphParts = (elem) => {
    const parts = [''];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        parts[parts.length - 1] += node.nodeValue || '';
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (node.tagName === 'BR') {
        parts.push('');
        return;
      }
      for (const child of node.childNodes) walk(child);
    };
    walk(elem);
    return parts.map((part) => clean(part)).filter((part) => part.length >= 2);
  };
  if (bodySection) {
    for (const elem of bodySection.querySelectorAll('p[data-component="paragraph"]')) {
      if (!visible(elem) || badContext(elem, bodySection)) continue;
      // A few newsletter articles put all prose in one <p> separated by <br>.
      // Walk text nodes so inline formatting and drop caps keep their exact
      // casing, while converting publisher-authored line breaks into the real
      // paragraph sequence required by the quality gate.
      for (const text of paragraphParts(elem)) {
        if (seenParagraphs.has(text)) continue;
        seenParagraphs.add(text);
        paragraphs.push(text);
        paragraphElements.push(elem);
      }
    }
  }

  const parseSrcset = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return [];
    // Economist's Cloudflare transform URLs contain commas themselves. Split
    // only where the following token starts another absolute/root URL.
    return raw.split(/,\s*(?=(?:https?:)?\/\/|\/)/i).map((part) => {
      const match = part.trim().match(/^(\S+?)(?:\s+([0-9]+(?:\.[0-9]+)?)(w|x))?$/i);
      if (!match) return null;
      return {
        url: match[1],
        widthHint: match[3]?.toLowerCase() === 'w' ? Number(match[2]) : null,
        density: match[3]?.toLowerCase() === 'x' ? Number(match[2]) : null,
      };
    }).filter(Boolean);
  };
  const addSources = (img) => {
    const sources = [];
    const add = (url, widthHint = null, density = null) => {
      if (url) sources.push({ url, widthHint, density });
    };
    add(img.currentSrc);
    for (const attr of ['data-src', 'data-lazy-src', 'data-original', 'data-image-src', 'data-url'])
      add(img.getAttribute(attr));
    for (const attr of ['srcset', 'data-srcset', 'data-lazy-srcset'])
      sources.push(...parseSrcset(img.getAttribute(attr)));
    const picture = img.closest('picture');
    if (picture) {
      for (const source of picture.querySelectorAll('source')) {
        for (const attr of ['srcset', 'data-srcset', 'data-lazy-srcset'])
          sources.push(...parseSrcset(source.getAttribute(attr)));
      }
    }
    add(img.getAttribute('src'));
    return sources;
  };
  const linkedToOtherArticle = (node) => {
    const links = [];
    if (node?.matches?.('a[href]')) links.push(node);
    if (node?.querySelectorAll) links.push(...node.querySelectorAll('a[href]'));
    for (const link of links) {
      try {
        const target = new URL(link.href, document.baseURI || location.href);
        const dated = /^\/[a-z0-9][a-z0-9-]*\/\d{4}\/\d{2}\/\d{2}\/[a-z0-9][a-z0-9-]*\/?$/i.test(target.pathname);
        if (dated && (target.hostname !== location.hostname || target.pathname.replace(/\/$/, '') !== location.pathname.replace(/\/$/, '')))
          return true;
      } catch {
        return true;
      }
    }
    return false;
  };
  const captionFor = (figure) => {
    const caption = figure.querySelector(
      'figcaption, [data-component*="caption" i], [data-testid*="caption" i], [class*="caption" i]'
    );
    return clean(caption && (caption.textContent || caption.innerText), 1000);
  };
  const afterParagraphFor = (img) => {
    let afterParagraph = -1;
    for (let index = 0; index < paragraphElements.length; index++) {
      if (paragraphElements[index].compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING)
        afterParagraph = index;
    }
    return afterParagraph;
  };

  const imageCandidates = [];
  let domIndex = 0;
  const addFigure = (figure, scope, boundary) => {
    if (!visible(figure) || badContext(figure, boundary) || linkedToOtherArticle(figure)) return;
    const images = Array.from(figure.querySelectorAll('img')).filter(visible);
    const sharedCaption = captionFor(figure);
    for (const [imageIndex, img] of images.entries()) {
      if (!visible(img)) continue;
      imageCandidates.push({
        scope,
        semantic: 'figure',
        sources: addSources(img),
        alt: clean(img.getAttribute('alt'), 1000),
        // A multi-image figure owns one shared figcaption. Attach it only to
        // the final distinct image so the document does not repeat the text.
        caption: imageIndex === images.length - 1 ? sharedCaption : '',
        figureCaptioned: Boolean(sharedCaption),
        afterParagraph: scope === 'hero' ? -1 : afterParagraphFor(img),
        width: img.naturalWidth || Number(img.getAttribute('width')) || null,
        height: img.naturalHeight || Number(img.getAttribute('height')) || null,
        visible: true,
        badContext: false,
        linkedArticle: false,
        domIndex: domIndex++,
      });
    }
  };

  if (article && bodySection && paragraphElements.length) {
    const firstParagraph = paragraphElements[0];
    // Hero ownership is deliberately strict: it must be an unlinked figure in
    // <article>, before正文 starts, and outside the selected正文 section.
    for (const figure of article.querySelectorAll('figure')) {
      if (bodySection.contains(figure)) continue;
      if (!(figure.compareDocumentPosition(firstParagraph) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      addFigure(figure, 'hero', article);
    }
    // Body media comes only from the selected section. Positional tail
    // filtering is enforced again by sanitizeEconomistImages().
    for (const figure of bodySection.querySelectorAll('figure'))
      addFigure(figure, 'body', bodySection);
  }

  const trustedHeroUrls = [];
  const addTrustedHero = (value) => {
    if (typeof value === 'string' && value.trim()) trustedHeroUrls.push(value.trim());
    else if (Array.isArray(value)) value.forEach(addTrustedHero);
    else if (value && typeof value === 'object') addTrustedHero(value.url || value.contentUrl);
  };
  addTrustedHero(meta('meta[property="og:image"]'));
  const schemaIdentity = (() => {
    const main = schema?.mainEntityOfPage;
    return schema?.url || schema?.['@id'] ||
      (typeof main === 'string' ? main : main?.['@id'] || main?.url) || '';
  })();
  let schemaMatchesPage = false;
  try {
    const identity = new URL(schemaIdentity, location.href);
    schemaMatchesPage = identity.hostname === location.hostname &&
      identity.pathname.replace(/\/$/, '') === location.pathname.replace(/\/$/, '');
  } catch {
    schemaMatchesPage = false;
  }
  if (schemaMatchesPage) addTrustedHero(schema?.image);

  const paywallSelectors = [
    '[data-component*="paywall" i]',
    '[data-testid*="paywall" i]',
    '[class*="paywall" i]',
    '[id*="paywall" i]',
    '[data-component*="registration-wall" i]',
    '[class*="subscription-proposition" i]',
  ];
  const paywall = paywallSelectors.some((selector) => {
    try {
      return Array.from(document.querySelectorAll(selector)).some(visible);
    } catch {
      return false;
    }
  });
  const bpcFailureElem = document.querySelector('#bpc_fail, #bpc_nofix');
  const bodyStart = clean(document.body?.innerText, 5000);
  const challengeElement = document.querySelector(
    '#challenge-running, #challenge-stage, .cf-challenge, iframe[src*="challenges.cloudflare.com"], script[src*="challenges.cloudflare.com"]'
  );
  const challengeWords = /just a moment|checking (?:your )?browser|verify (?:that )?you are human|enable javascript and cookies|cloudflare ray id|attention required|captcha/i;
  const challenge = Boolean(challengeElement) ||
    location.pathname.startsWith('/cdn-cgi/challenge-platform/') ||
    challengeWords.test(document.title) || (!article && challengeWords.test(bodyStart));

  const firstText = (selectors) => {
    for (const selector of selectors) {
      const elem = document.querySelector(selector);
      const value = clean(elem && (elem.innerText || elem.textContent));
      if (value) return value;
    }
    return '';
  };
  let author = schema?.author;
  if (Array.isArray(author))
    author = author.map((item) => typeof item === 'string' ? item : item?.name).filter(Boolean).join(', ');
  else if (author && typeof author === 'object') author = author.name;
  author = clean(author) || meta('meta[name="author"]') || firstText([
    '[rel="author"]',
    '[data-component*="author" i]',
    '[data-testid*="author" i]',
  ]);
  const publishedAt = clean(schema?.datePublished) ||
    meta('meta[property="article:published_time"]') ||
    document.querySelector('time[datetime]')?.getAttribute('datetime') || '';
  const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
  const title = firstText(['article h1', 'main h1', 'h1']) ||
    clean(schema?.headline) || meta('meta[property="og:title"]') || clean(document.title);

  return {
    title,
    author,
    publishedAt: clean(publishedAt),
    canonical,
    paragraphs,
    imageCandidates,
    trustedHeroUrls,
    paywall,
    bpcFailure: Boolean(bpcFailureElem),
    bpcFailureText: clean(bpcFailureElem?.innerText || bpcFailureElem?.textContent, 200),
    challenge,
    articlePresent: Boolean(article),
    bodySectionPresent: Boolean(bodySection),
  };
}
