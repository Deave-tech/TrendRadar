import { createHash } from 'node:crypto';

export const WSJ_HOST = 'cn.wsj.com';

// The Chinese article pages observed in production serve正文 photography from
// this exact CDN host. Do not widen this to publisher domain suffixes: delivery
// workers may later download the returned URLs, and unrelated WSJ/Dow Jones
// hosts are not part of the image-fetch trust boundary. Callers must still
// re-check every redirect when they fetch an image.
export const WSJ_IMAGE_HOSTS = Object.freeze([
  'images.wsj.net',
]);
export const MAX_WSJ_ARTICLE_IMAGES = 20;

const IMAGE_TRACKING_PARAMS = new Set([
  'mod',
  'ref',
  'source',
  'campaign',
  'cid',
  'cmpid',
]);

const IMAGE_TRANSFORM_PARAMS = new Set([
  'w',
  'h',
  'width',
  'height',
  'size',
  'quality',
  'q',
  'fit',
  'crop',
  'dpr',
]);

function boundedImageText(value) {
  const text = String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t\f\v]+/g, ' ')
    .replace(/\s*\n\s*/g, ' ')
    .trim();
  return text ? text.slice(0, 1000) : null;
}

function safeImageDimension(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0 || number > 20000) return null;
  return Math.round(number);
}

export function isAllowedWsjImageHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  return WSJ_IMAGE_HOSTS.includes(host);
}

/** Resolve and validate one publisher-hosted HTTPS image URL. */
export function normalizeWsjImageUrl(value, baseUrl) {
  if (typeof value !== 'string' || !value.trim() || value.length > 8192) return null;
  let url;
  try {
    url = new URL(value.trim(), baseUrl);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || !isAllowedWsjImageHost(url.hostname)) return null;
  if (url.port && url.port !== '443') return null;
  if (url.username || url.password) return null;
  url.hostname = url.hostname.replace(/\.$/, '');

  // Preserve the complete query string: CDN resize/signature parameters can be
  // required to retrieve the bytes. Tracking/transform keys are removed only
  // from the private deduplication key below, never from the download URL.
  url.hash = '';

  // Refuse common non-article assets even when they happen to be hosted on an
  // allowed CDN. Dimensions provide a second independent tracking-pixel gate.
  let path;
  try {
    path = decodeURIComponent(url.pathname).toLowerCase();
  } catch {
    return null;
  }
  if (/(^|[\/_.-])(logo|icon|avatar|badge|sprite|spacer|pixel|tracking|tracker|beacon|favicon|emoji|loading)([\/_.-]|$)/i.test(path))
    return null;
  if (/\.svg(?:$|[?#])/i.test(url.href)) return null;
  return url.href;
}

function wsjImageDedupeKey(value) {
  const url = new URL(value);
  // The same WSJ image asset is frequently exposed twice: the OpenGraph hero
  // uses `/im-123/social`, while the in-article picture uses `/im-123?...`.
  // Treat every rendition of one immutable `im-*` asset as the same image.
  const assetMatch = url.pathname.match(/^\/(im-[a-z0-9_-]+)(?:\/|$)/i);
  if (assetMatch) return `${url.origin}/${assetMatch[1].toLowerCase()}`;
  for (const key of Array.from(url.searchParams.keys())) {
    const normalized = key.toLowerCase();
    if (normalized.startsWith('utm_') || IMAGE_TRACKING_PARAMS.has(normalized) ||
      IMAGE_TRANSFORM_PARAMS.has(normalized)) url.searchParams.delete(key);
  }
  url.searchParams.sort();
  return url.href;
}

function chooseImageSource(sources, baseUrl) {
  const valid = [];
  for (const [index, source] of Array.from(sources || []).slice(0, 100).entries()) {
    const rawUrl = typeof source === 'string' ? source : source?.url;
    const url = normalizeWsjImageUrl(rawUrl, baseUrl);
    if (!url) continue;
    const widthHint = safeImageDimension(typeof source === 'object' ? source?.widthHint : null);
    const density = Number(typeof source === 'object' ? source?.density : 0);
    valid.push({
      url,
      widthHint,
      density: Number.isFinite(density) && density > 0 && density <= 10 ? density : 0,
      // Earlier entries win exact ties; browser extraction puts current/lazy
      // sources before the plain src placeholder.
      index,
    });
  }
  valid.sort((left, right) =>
    (right.widthHint || 0) - (left.widthHint || 0) ||
    right.density - left.density ||
    left.index - right.index
  );
  return valid[0] || null;
}

/**
 * Turn DOM-derived image candidates into the stable public image schema.
 *
 * `afterParagraph` is a 0-based text anchor. -1 means before the first
 * paragraph. Candidates after the final paragraph are intentionally rejected:
 * that conservative boundary prevents footer recirculation cards from leaking
 * into the document even if WSJ nests them inside the article section.
 */
export function sanitizeWsjImages(candidates, baseUrl, paragraphCount, trustedMetadata = null) {
  if (!Number.isInteger(paragraphCount) || paragraphCount < 1) return [];
  const images = [];
  const seen = new Set();

  // Page-wide images are never accepted as ordinary candidates. The sole
  // metadata exception is the exact og:image value extracted into this
  // separate trusted slot by readWsjSnapshot(), and it needs independently
  // declared large-image dimensions. This recovers WSJ hero photographs that
  // are absent from article<section> without admitting sidebar/social assets.
  const ogImage = trustedMetadata && typeof trustedMetadata === 'object' ? trustedMetadata.ogImage : null;
  if (ogImage && typeof ogImage === 'object') {
    const width = safeImageDimension(ogImage.width);
    const height = safeImageDimension(ogImage.height);
    const url = normalizeWsjImageUrl(ogImage.url, baseUrl);
    if (url && width >= 600 && height >= 300) {
      const dedupeKey = wsjImageDedupeKey(url);
      seen.add(dedupeKey);
      images.push({
        url,
        alt: null,
        caption: null,
        afterParagraph: -1,
        width,
        height,
      });
    }
  }

  for (const candidate of Array.from(candidates || []).slice(0, 200)) {
    if (!candidate || typeof candidate !== 'object') continue;
    if ((!candidate.inProseFlow && !candidate.articleContent) || candidate.badContext || candidate.linkedArticle || candidate.visible === false) continue;
    if (!['figure', 'picture', 'img'].includes(candidate.semantic)) continue;

    const afterParagraph = Number(candidate.afterParagraph);
    const articleTail = candidate.articleContent && afterParagraph === paragraphCount - 1;
    if (!Number.isInteger(afterParagraph) || afterParagraph < -1 ||
      afterParagraph >= paragraphCount || (afterParagraph === paragraphCount - 1 && !articleTail))
      continue;

    const alt = boundedImageText(candidate.alt);
    const caption = boundedImageText(candidate.caption);
    if (candidate.semantic === 'img' && !candidate.mediaHint && !alt && !caption) continue;

    const source = chooseImageSource(candidate.sources, baseUrl);
    if (!source) continue;
    const width = safeImageDimension(candidate.width) || source.widthHint;
    const height = safeImageDimension(candidate.height);

    // Reject icons, avatars and tracking pixels by rendered/intrinsic size.
    // If dimensions are unavailable, semantic figure/picture markup still
    // needs meaningful alt/caption text before it is trusted.
    if ((width && width < 200) || (height && height < 100)) continue;
    if (!width && !height && !alt && !caption) continue;

    const dedupeKey = wsjImageDedupeKey(source.url);
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    const image = {
      url: source.url,
      alt,
      caption,
      afterParagraph,
      width: width || null,
      height: height || null,
    };
    if (articleTail) image.articleTail = true;
    images.push(image);
    if (images.length >= MAX_WSJ_ARTICLE_IMAGES) break;
  }
  return images;
}

export function isAntiBotChallengeUrl(value) {
  try {
    const host = new URL(value).hostname;
    return host === 'captcha-delivery.com' || host.endsWith('.captcha-delivery.com') ||
      host === 'datadome.co' || host.endsWith('.datadome.co');
  } catch {
    return false;
  }
}

/**
 * Validate the deliberately narrow URL surface exposed by POST /v1/fetch.
 * Keeping this separate from the HTTP handler makes it hard for a redirect or
 * a future caller to accidentally turn the browser into a general-purpose
 * SSRF endpoint.
 */
export function validateWsjArticleUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, reason: 'URL must be an absolute URL' };
  }

  if (url.protocol !== 'https:')
    return { ok: false, reason: 'URL must use HTTPS' };
  if (url.hostname !== WSJ_HOST)
    return { ok: false, reason: `URL host must be ${WSJ_HOST}` };
  if (url.port && url.port !== '443')
    return { ok: false, reason: 'URL must use the default HTTPS port' };
  if (url.username || url.password)
    return { ok: false, reason: 'URL credentials are not allowed' };
  if (!url.pathname.startsWith('/articles/') || url.pathname === '/articles/')
    return { ok: false, reason: 'URL path must start with /articles/' };

  // Fragments never reach the server and only create multiple forms of the
  // same input. Drop them while preserving WSJ query parameters.
  url.hash = '';
  return { ok: true, url };
}

export function validRequestId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(value);
}

export function articleSha256(paragraphs) {
  return createHash('sha256').update(paragraphs.join('\n\n'), 'utf8').digest('hex');
}
