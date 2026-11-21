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

test('Qantas, Shell, and April Salumei resolve after the name merge', () => {
  const qantas = dataset.buyers.filter((buyer) => buyer.name === 'Qantas');
  assert.equal(qantas.length, 1);
  assert.ok(qantas[0].variants.includes('Qantas Airways Limited'));
  assert.equal(dataset.buyers.filter((buyer) => buyer.name === 'Qantas Airways Limited').length, 0);
  const shell = dataset.buyers.find((buyer) => buyer.name === 'Shell');
  assert.ok(shell);
  assert.equal(shell.cc, 'GB');
  assert.ok((shell.variants || []).some((name) => name.toLowerCase() === 'shell international ltd'));
  const shellIndex = dataset.buyers.indexOf(shell);
  const shellFlows = dataset.flows.filter((flow) => flow[0] === shellIndex);
  assert.ok(shellFlows.length > 50);
  const salumei = dataset.projects.find((project) => project.id === 'VCS1122');
  assert.match(salumei.name, /April Salumei/);
  assert.equal(salumei.cc, 'PG');
  assert.ok(salumei.developer);
  assert.ok(salumei.issued > salumei.retiredRegistry);
  assert.ok(salumei.undisclosed > 0);
  const nespresso = dataset.buyers.find((buyer) => buyer.name === 'Nespresso');
  assert.ok(nespresso.variants.some((name) => /on behalf of nespresso/i.test(name)));
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
