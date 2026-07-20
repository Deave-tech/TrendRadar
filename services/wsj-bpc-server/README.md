# WSJ BPC fetch service

TrendRadar's local, authenticated WSJ article extractor. It launches a warm
Playwright Chromium context with an external Bypass Paywalls Clean extension,
then exposes only:

- `POST /v1/fetch` for `https://cn.wsj.com/articles/*`
- `POST /v1/economist/fetch` for dated `https://www.economist.com/*` articles
- `POST /v1/list` for the exact homepage and eight listing URLs used by the bridge
- `GET /healthz`
- authenticated cookie maintenance endpoints

The extension itself is **not vendored**. Set `BPC_DIR` to a separately obtained
BPC extension checkout containing `manifest.json`.

## Security defaults

Production must set `API_TOKEN`; the systemd template also disables the legacy
general-purpose `/fetch` endpoint. Article URLs, redirects, image hosts, image
sizes, DNS results and response quality are validated. Runtime secrets, cookies,
Chromium profiles, dependencies and logs are ignored by Git and Docker.

WSJ list and article jobs share one serialized Chromium session. A DataDome
cookie is seeded once, then any value rotated by a response is atomically saved
with mode `0600` before the queue admits the next job. The bridge therefore
calls `/v1/list` instead of sending a second copy of the cookie directly to WSJ.
If that durable save fails, or a previously persisted browser token disappears,
the service closes the WSJ queue before releasing the current job and keeps
`/healthz`, `/v1/list`, `/v1/fetch` and WSJ cookie updates fail-closed with
`SESSION_PERSIST_FAILED`. Recovery requires fixing the store and restarting the
service; an in-process queue is never reopened.
Deleting a live WSJ session through `/cookies` is rejected because it cannot be
made atomic with an in-flight browser rotation; stop the service before
removing the durable WSJ session, or replace it through the serialized update.

Economist article jobs use a separate serialized queue. The endpoint accepts
only one-section dated prose-article URLs, rejects cross-article redirects and
challenge/paywall responses, and extracts only article paragraphs plus
publisher-owned hero/body figures. Image URLs are normalized to a deterministic
supported format before the delivery service downloads them.

## Development

```bash
npm install
npx playwright install chromium
npm test

BPC_DIR=/path/to/bpc-extension \
API_TOKEN=replace-me REQUIRE_API_TOKEN=1 ENABLE_LEGACY_FETCH=0 \
node index.mjs
```

The WSJ response contains stable metadata, paragraphs and article-scoped images.
It accepts ordinary prose-flow media plus WSJ's publisher-owned tail gallery,
while rejecting ads, sidebars, recirculation cards, linked article thumbnails,
non-WSJ CDN hosts, icons and duplicate `im-*` renditions.

The Economist response uses the same stable contract. It excludes podcast,
video, interactive and recommendation content, and retains article figure
captions and intrinsic dimensions for Feishu rendering.
