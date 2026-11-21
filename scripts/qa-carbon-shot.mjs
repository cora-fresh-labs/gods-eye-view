#!/usr/bin/env node
/**
 * Capture the carbon-flows layer: globe arcs, one buyer country, Pacific.
 * Usage: node scripts/qa-carbon-shot.mjs [--url http://localhost:4173]
 */
import { mkdirSync } from 'node:fs';
import puppeteer from 'puppeteer';

const argv = process.argv;
const url = argv.includes('--url') ? argv[argv.indexOf('--url') + 1] : 'http://localhost:4173';
const outDir = new URL('../docs/media/carbon/', import.meta.url);
mkdirSync(outDir, { recursive: true });

const browser = await puppeteer.launch({
  headless: 'new',
  protocolTimeout: 180_000,
  args: [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--window-size=1440,900',
    '--use-gl=angle',
    '--use-angle=swiftshader',
    '--ignore-gpu-blocklist',
  ],
});

async function settle(page, frames = 40) {
  await page.evaluate((count) => new Promise((resolve) => {
    const viewer = window.__godsEyeView.viewer;
    let ticks = 0;
    const tick = () => {
      viewer.scene.requestRender?.();
      if (++ticks < count) requestAnimationFrame(tick);
      else resolve();
    };
    requestAnimationFrame(tick);
  }), frames);
  await new Promise((r) => setTimeout(r, 400));
}

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', (error) => console.log('PAGEERROR', error.message));
  await page.goto(`${url}/?welcome=0`, { waitUntil: 'domcontentloaded', timeout: 120_000 });
  await page.waitForFunction(() => window.__godsEyeView?.dataManager, { timeout: 120_000 });
  await page.waitForFunction(
    () => document.getElementById('loading-screen')?.classList.contains('hidden'),
    { timeout: 120_000 },
  );

  const ready = await page.evaluate(async () => {
    const gev = window.__godsEyeView;
    gev.viewer.camera.cancelFlight();
    for (const [id, entry] of gev.dataManager.layers) {
      if (entry.enabled && id !== 'carbon-flows') {
        try { await gev.dataManager.setEnabled(id, false, { origin: 'user' }); } catch { /* shot */ }
      }
    }
    await gev.dataManager.setEnabled('carbon-flows', true, { origin: 'user' });
    const layer = gev.dataManager.layers.get('carbon-flows').module;
    const started = Date.now();
    while (Date.now() - started < 30000) {
      const stats = layer.getStats();
      if (stats.error) return { ok: false, error: stats.error };
      if (stats.count > 0) return { ok: true, count: stats.count, filters: layer.getFilters() };
      await new Promise((r) => setTimeout(r, 200));
    }
    return { ok: false, error: 'arcs did not render', stats: layer.getStats() };
  });
  console.log('ready', JSON.stringify(ready));
  if (!ready.ok) process.exitCode = 1;

  const clockA = await page.evaluate(() => (
    window.__godsEyeView.dataManager.layers.get('carbon-flows').module.getAnimationClock()
  ));
  await new Promise((r) => setTimeout(r, 700));
  const clockB = await page.evaluate(() => (
    window.__godsEyeView.dataManager.layers.get('carbon-flows').module.getAnimationClock()
  ));
  console.log('animation clock', clockA, '->', clockB);
  if (!(clockB > clockA)) {
    console.log('ANIMATION DID NOT ADVANCE');
    process.exitCode = 1;
  }

  const shots = [
    {
      name: 'globe-arcs',
      file: 'globe-arcs.png',
      setup: () => ({ buyerCountry: '', buyerIndex: null, projectIndex: null, pacific: false, group: '', registry: '', year: null }),
      camera: { lon: 8, lat: 18, height: 18_000_000 },
    },
    {
      name: 'switzerland',
      file: 'switzerland.png',
      setup: () => ({ buyerCountry: 'CH', buyerIndex: null, projectIndex: null, pacific: false, group: '', registry: '', year: null }),
      camera: null,
    },
    {
      name: 'shell',
      file: 'shell.png',
      phrase: 'show me all Shell credits',
    },
    {
      name: 'april-salumei',
      file: 'april-salumei.png',
      phrase: 'who bought from April Salumei',
    },
    {
      name: 'qantas',
      file: 'qantas.png',
      phrase: 'Qantas',
    },
    {
      name: 'pacific',
      file: 'pacific.png',
      setup: () => ({ buyerCountry: '', buyerIndex: null, projectIndex: null, pacific: true, group: '', registry: '', year: null }),
      camera: null,
    },
  ];

  for (const shot of shots) {
    const summary = await page.evaluate((shot) => {
      const layer = window.__godsEyeView.dataManager.layers.get('carbon-flows').module;
      let lookup = null;
      if (shot.phrase) {
        lookup = layer.lookup(shot.phrase);
      } else {
        layer.setFilters(shot.filters);
        if (shot.globe) {
          window.__godsEyeView.viewer.camera.setView({
            destination: window.__godsEyeView.viewer.scene.globe.ellipsoid.cartographicToCartesian({
              longitude: 8 * Math.PI / 180,
              latitude: 18 * Math.PI / 180,
              height: 18_000_000,
            }),
            orientation: { heading: 0, pitch: -Math.PI / 2, roll: 0 },
          });
        } else {
          layer.flyToView();
        }
      }
      return {
        caption: document.querySelector('.carbon-caption')?.textContent || '',
        detail: document.querySelector('.carbon-detail')?.innerText?.slice(0, 700) || '',
        stats: layer.getStats(),
        filters: layer.getFilters(),
        lookup,
      };
    }, {
      phrase: shot.phrase || null,
      filters: shot.setup ? shot.setup() : null,
      globe: Boolean(shot.camera),
    });
    console.log(shot.name, JSON.stringify(summary));
    await new Promise((r) => setTimeout(r, shot.camera ? 600 : 1800));
    await settle(page, 30);
    const path = new URL(shot.file, outDir).pathname;
    await page.screenshot({ path });
    console.log('saved', path);
    if (shot.name === 'switzerland') {
      const clicked = await page.evaluate(() => {
        const row = [...document.querySelectorAll('.carbon-row')]
          .find((button) => button.textContent.includes('Nespresso'));
        row?.click();
        const title = document.querySelector('.carbon-detail-title')?.textContent || '';
        const rows = document.querySelectorAll('.carbon-list .carbon-row').length;
        return { title, rows };
      });
      console.log('click buyer', JSON.stringify(clicked));
      if (!clicked.title.includes('Nespresso') || clicked.rows < 1) {
        console.log('CLICK THROUGH FAILED');
        process.exitCode = 1;
      }
      await settle(page, 20);
      const detailPath = new URL('switzerland-nespresso.png', outDir).pathname;
      await page.screenshot({ path: detailPath });
      console.log('saved', detailPath);
    }
  }
} finally {
  await browser.close();
}
