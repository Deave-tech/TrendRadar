# WSJ BPC fetch service

TrendRadar's local, authenticated WSJ article extractor. It launches a warm
Playwright Chromium context with an external Bypass Paywalls Clean extension,
then exposes only:

- `POST /v1/fetch` for `https://cn.wsj.com/articles/*`
- `GET /healthz`
- authenticated cookie maintenance endpoints

The extension itself is **not vendored**. Set `BPC_DIR` to a separately obtained
BPC extension checkout containing `manifest.json`.

## Security defaults

Production must set `API_TOKEN`; the systemd template also disables the legacy
general-purpose `/fetch` endpoint. Article URLs, redirects, image hosts, image
sizes, DNS results and response quality are validated. Runtime secrets, cookies,
Chromium profiles, dependencies and logs are ignored by Git and Docker.

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
