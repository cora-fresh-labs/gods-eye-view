import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { arcsFor, defaultFilters, summarize } from './carbonFlowsModel.js';

const dataset = JSON.parse(readFileSync(
  new URL('./local_data/carbon/carbon-flows.json', import.meta.url),
  'utf8',
));

const METHODS = new Set([
  '',
  'geonames-city',
  'wikidata-hq',
  'wikidata-coord',
  'wikidata-search',
  'registry-country-centroid',
]);

test('the shipped carbon file cites Berkeley VROD v2026-08 and has real rows', () => {
  assert.equal(dataset.meta.version, 'v2026-08');
  assert.equal(dataset.meta.license, 'CC BY 4.0');
  assert.match(dataset.meta.citation, /Barbara K Haya/);
  assert.match(dataset.meta.citation, /Voluntary Registry Offsets Database v2026-08/);
  assert.ok(dataset.buyers.length > 100);
  assert.ok(dataset.projects.length > 100);
  assert.ok(dataset.flows.length > 100);
  assert.ok(dataset.meta.counts.verraBlankBeneficiaryRows > 0);
});

test('every coordinate is a flagged geocode, and flows point at real rows', () => {
  dataset.buyers.forEach((buyer, index) => {
    assert.equal(typeof buyer.name, 'string');
    assert.ok(buyer.name.trim(), `buyer ${index} has an empty name`);
    assert.ok(!buyer.name.includes('√'), `buyer ${index} still has Mac-Roman mojibake`);
    assert.ok(METHODS.has(buyer.method || ''), buyer.method);
    if (buyer.conf === 'unplaced') {
      assert.equal(buyer.lon, undefined);
      assert.equal(buyer.lat, undefined);
      return;
    }
    assert.ok(Number.isFinite(buyer.lon) && buyer.lon >= -180 && buyer.lon <= 180);
    assert.ok(Number.isFinite(buyer.lat) && buyer.lat >= -90 && buyer.lat <= 90);
    assert.notEqual(buyer.lon === 0 && buyer.lat === 0, true);
  });
  for (const flow of dataset.flows) {
    assert.ok(dataset.buyers[flow[0]], 'flow buyer');
    assert.ok(dataset.projects[flow[1]], 'flow project');
    assert.ok(flow[2] > 0);
  }
});

test('Papua New Guinea REDD+ projects are in the file and the Pacific caption counts buyers', () => {
  const png = dataset.projects.filter((project) => project.cc === 'PG' && project.group === 'redd');
  assert.ok(png.some((project) => project.id === 'VCS1122'));
  assert.ok(png.some((project) => project.id === 'VCS2293'));
  const summary = summarize(dataset, { ...defaultFilters(), pacific: true });
  assert.ok(summary.buyers > 0);
  assert.ok(summary.projectCountries >= 1);
  assert.match(summary.caption, /^Pacific projects: /);
  const arcs = arcsFor(dataset, { ...defaultFilters(), pacific: true });
  assert.ok(arcs.length > 0);
  assert.ok(arcs.every((arc) => dataset.meta.pacificIso.includes(arc.cc)));
});
