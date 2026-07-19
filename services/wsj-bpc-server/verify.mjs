// Minimal end-to-end verification:
//   node verify.mjs [URL ...]        -> extension loaded, health checks, optional fetch
//   BPC_DISABLE=1 node verify.mjs U  -> control run without the extension
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { launchBpc, waitForBpcReady, fetchArticle, EXTENSION_ID, BPC_DIR } from './browser.mjs';

const DISABLED = process.env.BPC_DISABLE === '1';
const urls = process.argv.slice(2);
let failed = false;

console.log(`mode: ${DISABLED ? 'CONTROL (no extension)' : 'extension loaded'}`);
console.log(`extension dir: ${BPC_DIR}`);

// Fresh profile every run -> exercises the onInstalled path of background.js.
const profile = mkdtempSync(path.join(os.tmpdir(), 'bpc-verify-'));
const { context, sw } = await launchBpc(profile, { loadExtension: !DISABLED });

if (!DISABLED) {
  const swUrl = sw.url();
  const idOk = swUrl.startsWith(`chrome-extension://${EXTENSION_ID}/`);
  console.log(`${idOk ? 'PASS' : 'FAIL'} service worker: ${swUrl}`);
  if (!idOk) failed = true;

  const offscreenApi = await sw.evaluate(() => typeof chrome.offscreen === 'object');
  const health = await waitForBpcReady(sw);
  const storageOk = health.sitesInStorage > 0;
  const rulesOk = health.dnrSessionRules > 0;
  console.log(`${storageOk ? 'PASS' : 'FAIL'} sites in storage: ${health.sitesInStorage}`);
  console.log(`${rulesOk ? 'PASS' : 'FAIL'} DNR session rules: ${health.dnrSessionRules}`);
  console.log(`info offscreen API present: ${offscreenApi}`);
  if (!storageOk || !rulesOk) failed = true;
}

for (const url of urls) {
  try {
    const r = await fetchArticle(context, url);
    console.log(`\n=== ${url}`);
    console.log(`status: ${r.status} | title: ${r.title}`);
    console.log(`text length: ${r.textLength}`);
    console.log('--- first 400 chars ---');
    console.log(r.text.slice(0, 400));
    console.log('--- last 300 chars ---');
    console.log(r.text.slice(-300));
  } catch (e) {
    failed = true;
    console.log(`\n=== ${url}\nERROR: ${e.message}`);
  }
}

await context.close();
console.log(`\n${failed ? 'FAILED' : 'OK'}`);
process.exit(failed ? 1 : 0);
