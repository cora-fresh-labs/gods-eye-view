import assert from 'node:assert/strict';
import test from 'node:test';

import {
  arcsFor,
  buyerDetail,
  defaultFilters,
  flowMatches,
  formatCredit,
  formatTonnes,
  maskHasYear,
  projectDetail,
  interpretCarbonQuery,
  resolveCarbonQuery,
  searchBuyers,
  searchProjects,
  summarize,
  topBuyers,
  yearsFromMask,
} from './carbonFlowsModel.js';

const dataset = {
  meta: {
    pacificIso: ['PG', 'FJ'],
    typeGroups: { redd: 'REDD+', other: 'Other' },
  },
  countries: {
    CH: { name: 'Switzerland', lon: 8.2, lat: 46.8 },
    US: { name: 'United States', lon: -98.5, lat: 39.8 },
    PG: { name: 'Papua New Guinea', lon: 145.2, lat: -6.3 },
    FJ: { name: 'Fiji', lon: 177.9, lat: -17.8 },
  },
  buyers: [
    { name: 'Alpine Carbon AG', cc: 'CH', lon: 8.54, lat: 47.37, hq: 'Zurich', conf: 'high', method: 'wikidata-hq' },
    { name: 'Low Confidence Co', cc: 'US', lon: -98.5, lat: 39.8, conf: 'low', method: 'registry-country-centroid' },
    { name: 'Unplaced Buyer', cc: '', conf: 'unplaced', method: '' },
  ],
  projects: [
    { id: 'VCS1', name: 'Highlands', cc: 'PG', type: 'REDD+', group: 'redd', registry: 'VCS' },
    { id: 'GS9', name: 'Cookstove', cc: 'FJ', type: 'Cookstoves', group: 'other', registry: 'GOLD' },
  ],
  // year mask bit 0 = 1996, so 2020 is bit 24.
  flows: [
    [0, 0, 1000, 1 << (2020 - 1996)],
    [0, 1, 250, 1 << (2021 - 1996)],
    [1, 0, 50, 1 << (2020 - 1996)],
    [2, 0, 80, 1 << (2019 - 1996)],
  ],
};

test('year masks round-trip the retirement years', () => {
  assert.deepEqual(yearsFromMask((1 << 24) | (1 << 25)), [2020, 2021]);
  assert.equal(maskHasYear(1 << 24, 2020), true);
  assert.equal(maskHasYear(1 << 24, 2021), false);
  assert.equal(maskHasYear(1 << 24, 1800), false);
});

test('a country caption counts buyers and the project countries they reach', () => {
  const summary = summarize(dataset, { ...defaultFilters(), buyerCountry: 'CH' });
  assert.equal(summary.buyers, 1);
  assert.equal(summary.projectCountries, 2);
  assert.equal(summary.tonnes, 1250);
  assert.equal(summary.caption, 'Switzerland: 1 buyer → 2 countries');
});

test('the pacific preset keeps only pacific host countries', () => {
  const summary = summarize(dataset, { ...defaultFilters(), pacific: true });
  assert.equal(summary.projects, 2);
  assert.equal(summary.caption, 'Pacific projects: 3 buyers → 2 countries');
  const redd = summarize(dataset, { ...defaultFilters(), pacific: true, group: 'redd' });
  assert.equal(redd.projects, 1);
  assert.equal(redd.tonnes, 1130);
});

test('low-confidence headquarters drop out when that filter is on', () => {
  const arcs = arcsFor(dataset, { hideLowConfidence: true });
  assert.ok(arcs.every((arc) => arc.conf !== 'low'));
  assert.equal(arcs.some((arc) => arc.buyerIndex === 1), false);
  assert.equal(arcs.some((arc) => arc.buyerIndex === 2), false);
});

test('unplaced buyers are not drawn and still open in the detail record', () => {
  const arcs = arcsFor(dataset, defaultFilters());
  assert.equal(arcs.some((arc) => arc.buyerIndex === 2), false);
  const detail = buyerDetail(dataset, 2, defaultFilters());
  assert.equal(detail.conf, 'unplaced');
  assert.equal(detail.projects.length, 1);
  assert.equal(detail.projects[0].id, 'VCS1');
  assert.deepEqual(detail.projects[0].years, [2019]);
});

test('clicking a buyer lists projects and clicking a project lists buyers', () => {
  const buyer = buyerDetail(dataset, 0, defaultFilters());
  assert.deepEqual(buyer.projects.map((project) => project.id), ['VCS1', 'GS9']);
  assert.equal(buyer.hq, 'Zurich');
  const project = projectDetail(dataset, 0, { buyerCountry: 'CH' });
  assert.deepEqual(project.buyers.map((entry) => entry.name), ['Alpine Carbon AG']);
  assert.equal(flowMatches(dataset, dataset.flows[0], { year: 2020 }), true);
  assert.equal(flowMatches(dataset, dataset.flows[0], { year: 2021 }), false);
});

test('the largest-buyer list keeps an unplaced name in the same view', () => {
  const leaders = topBuyers(dataset, { ...defaultFilters(), pacific: true }, 5);
  assert.equal(leaders[0].name, 'Alpine Carbon AG');
  assert.equal(leaders.some((buyer) => buyer.name === 'Unplaced Buyer' && buyer.placed === false), true);
});

test('spoken phrasings route to a buyer or a project', () => {
  assert.deepEqual(interpretCarbonQuery('show me all Shell credits'), { kind: 'buyer', query: 'Shell' });
  assert.deepEqual(interpretCarbonQuery('who bought from April Salumei?'), { kind: 'project', query: 'April Salumei' });
  const project = searchProjects(dataset, 'VCS1', 3);
  assert.equal(project[0].id, 'VCS1');
  const spoken = resolveCarbonQuery(dataset, 'who bought from Highlands');
  assert.equal(spoken.kind, 'project');
  assert.equal(spoken.hits[0].name, 'Highlands');
});

test('a removal filter and a biochar project keep their own credit unit', () => {
  const local = {
    ...dataset,
    projects: [
      { ...dataset.projects[0], removal: 'avoidance' },
      dataset.projects[1],
      {
        id: 'PURO:1',
        name: 'Kiln',
        cc: 'PG',
        type: 'Biochar',
        group: 'biochar',
        registry: 'PURO',
        removal: 'removal',
        unit: 'corc',
        lat: -6.1,
        lon: 145.4,
      },
    ],
    flows: [...dataset.flows, [0, 2, 40, 1 << (2024 - 1996)]],
  };
  const removal = summarize(local, { ...defaultFilters(), removal: 'removal' });
  assert.equal(removal.projects, 1);
  assert.equal(removal.tonnes, 40);
  const biochar = summarize(local, { ...defaultFilters(), group: 'biochar' });
  assert.equal(biochar.projects, 1);
  const arcs = arcsFor(local, { ...defaultFilters(), projectIndex: 2 });
  assert.equal(arcs[0].lon2, 145.4);
  assert.equal(arcs[0].lat2, -6.1);
  assert.equal(formatCredit(40, 'corc'), '40 CORC');
  assert.equal(formatCredit(1250000, 'c-sink'), '1.25 Mt C-sink');
});

test('company search needs two characters and prefers a prefix', () => {
  assert.deepEqual(searchBuyers(dataset, 'a'), []);
  const hits = searchBuyers(dataset, 'alp');
  assert.equal(hits[0].name, 'Alpine Carbon AG');
  assert.equal(formatTonnes(1250000), '1.25 Mt');
});
