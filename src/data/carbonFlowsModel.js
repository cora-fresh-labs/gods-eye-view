/**
 * Pure filters, captions, and click-through records for the carbon-flows layer.
 * No Cesium and no network. The dataset is the JSON written by
 * scripts/carbon/build_carbon_flows.py.
 */

export const YEAR0 = 1996;
export const YEAR1 = 2026;

export const NATURE_GROUPS = Object.freeze(['redd', 'arr', 'ifm', 'blue', 'soil', 'biochar']);

const REGISTRY_LABELS = Object.freeze({
  VCS: 'Verra',
  GOLD: 'Gold Standard',
  ACR: 'ACR',
  CAR: 'CAR',
  ART: 'ART',
  ISO: 'Isometric',
});

/** @returns {object} Filter state. Empty strings and nulls mean "all". */
export function defaultFilters() {
  return {
    buyerCountry: '',
    buyerIndex: null,
    group: '',
    registry: '',
    year: null,
    pacific: false,
    hideLowConfidence: false,
    projectIndex: null,
  };
}

/** @param {number} mask Bit field, bit 0 = YEAR0. */
export function yearsFromMask(mask) {
  const value = Number(mask) || 0;
  const years = [];
  for (let year = YEAR0; year <= YEAR1; year += 1) {
    if ((value & (1 << (year - YEAR0))) !== 0) years.push(year);
  }
  return years;
}

/** @param {number} mask @param {number} year */
export function maskHasYear(mask, year) {
  const bit = Number(year) - YEAR0;
  if (!Number.isInteger(bit) || bit < 0 || bit > (YEAR1 - YEAR0)) return false;
  return ((Number(mask) || 0) & (1 << bit)) !== 0;
}

export function registryLabel(code) {
  return REGISTRY_LABELS[code] || code || '';
}

export function groupLabel(dataset, group) {
  return dataset?.meta?.typeGroups?.[group] || group || '';
}

export function isPacificCountry(dataset, code) {
  return Array.isArray(dataset?.meta?.pacificIso) && dataset.meta.pacificIso.includes(code);
}

export function countryName(dataset, code) {
  return dataset?.countries?.[code]?.name || code || '';
}

/** Tonnes of CO₂e, grouped so a caption stays short. */
export function formatTonnes(value) {
  const tonnes = Number(value) || 0;
  const abs = Math.abs(tonnes);
  if (abs >= 1e9) return `${(tonnes / 1e9).toFixed(2)} Gt`;
  if (abs >= 1e6) return `${(tonnes / 1e6).toFixed(2)} Mt`;
  if (abs >= 1e3) return `${(tonnes / 1e3).toFixed(1)} kt`;
  return `${Math.round(tonnes).toLocaleString('en-US')} t`;
}

function buyerOf(dataset, index) {
  return dataset?.buyers?.[index] || null;
}

function projectOf(dataset, index) {
  return dataset?.projects?.[index] || null;
}

/**
 * @param {object} dataset
 * @param {number[]} flow [buyerIndex, projectIndex, tonnes, yearMask]
 * @param {object} filters
 */
export function flowMatches(dataset, flow, filters) {
  if (!Array.isArray(flow) || flow.length < 4) return false;
  const buyer = buyerOf(dataset, flow[0]);
  const project = projectOf(dataset, flow[1]);
  if (!buyer || !project) return false;
  if (filters?.buyerIndex != null && flow[0] !== filters.buyerIndex) return false;
  if (filters?.projectIndex != null && flow[1] !== filters.projectIndex) return false;
  if (filters?.buyerCountry && buyer.cc !== filters.buyerCountry) return false;
  if (filters?.hideLowConfidence && (buyer.conf === 'low' || buyer.conf === 'unplaced')) return false;
  if (filters?.pacific && !isPacificCountry(dataset, project.cc)) return false;
  if (filters?.registry && project.registry !== filters.registry) return false;
  if (filters?.group === 'nature' && !NATURE_GROUPS.includes(project.group)) return false;
  if (filters?.group && filters.group !== 'nature' && project.group !== filters.group) return false;
  if (filters?.year != null && !maskHasYear(flow[3], filters.year)) return false;
  return true;
}

function matchingFlows(dataset, filters) {
  const flows = Array.isArray(dataset?.flows) ? dataset.flows : [];
  return flows.filter((flow) => flowMatches(dataset, flow, filters));
}

/**
 * Buyer countries that have at least one placed buyer under the filters,
 * ranked by retired tonnes. The country filter itself is ignored so the
 * globe can cycle through countries.
 * @returns {{cc:string,name:string,tonnes:number,buyers:number}[]}
 */
export function buyerCountryRanking(dataset, filters) {
  const open = { ...defaultFilters(), ...filters, buyerCountry: '', buyerIndex: null };
  const tonnes = new Map();
  const buyers = new Map();
  for (const flow of matchingFlows(dataset, open)) {
    const buyer = buyerOf(dataset, flow[0]);
    if (!buyer?.cc || buyer.lon == null || buyer.lat == null) continue;
    tonnes.set(buyer.cc, (tonnes.get(buyer.cc) || 0) + flow[2]);
    if (!buyers.has(buyer.cc)) buyers.set(buyer.cc, new Set());
    buyers.get(buyer.cc).add(flow[0]);
  }
  return [...tonnes.entries()]
    .map(([cc, retired]) => ({
      cc,
      name: countryName(dataset, cc),
      tonnes: retired,
      buyers: buyers.get(cc).size,
    }))
    .sort((a, b) => b.tonnes - a.tonnes || a.name.localeCompare(b.name));
}

/**
 * @returns {object} Counts plus the caption string shown over the globe.
 */
export function summarize(dataset, filters) {
  const active = { ...defaultFilters(), ...filters };
  const buyerIds = new Set();
  const projectIds = new Set();
  const projectCountries = new Set();
  let tonnes = 0;
  for (const flow of matchingFlows(dataset, active)) {
    buyerIds.add(flow[0]);
    projectIds.add(flow[1]);
    const project = projectOf(dataset, flow[1]);
    if (project?.cc) projectCountries.add(project.cc);
    tonnes += flow[2];
  }
  const summary = {
    buyers: buyerIds.size,
    projects: projectIds.size,
    projectCountries: projectCountries.size,
    tonnes,
    buyerCountryName: active.buyerCountry ? countryName(dataset, active.buyerCountry) : '',
    pacific: active.pacific === true,
  };
  summary.caption = captionFor(summary, active, dataset);
  return summary;
}

function plural(count, singular, pluralForm) {
  const shown = count.toLocaleString('en-US');
  return `${shown} ${count === 1 ? singular : pluralForm}`;
}

function captionFor(summary, filters, dataset) {
  const buyers = plural(summary.buyers, 'buyer', 'buyers');
  const countries = plural(summary.projectCountries, 'country', 'countries');
  if (filters.pacific) {
    return `Pacific projects: ${buyers} → ${countries}`;
  }
  if (filters.buyerIndex != null) {
    const name = buyerOf(dataset, filters.buyerIndex)?.name || 'Buyer';
    const projects = plural(summary.projects, 'project', 'projects');
    return `${name}: ${projects} → ${countries}`;
  }
  if (filters.buyerCountry) {
    return `${summary.buyerCountryName}: ${buyers} → ${countries}`;
  }
  return `${buyers} → ${plural(summary.projectCountries, 'project country', 'project countries')}`;
}

/**
 * One arc per placed buyer headquarters to a project-country label point.
 * Unplaced buyers are omitted from the globe and remain available in the panel.
 * @returns {object[]}
 */
export function arcsFor(dataset, filters, limit = 500) {
  const active = { ...defaultFilters(), ...filters };
  const grouped = new Map();
  for (const flow of matchingFlows(dataset, active)) {
    const buyer = buyerOf(dataset, flow[0]);
    const project = projectOf(dataset, flow[1]);
    if (buyer?.lon == null || buyer?.lat == null || !project?.cc) continue;
    const country = dataset.countries?.[project.cc];
    if (!country) continue;
    const key = `${flow[0]}|${project.cc}`;
    let arc = grouped.get(key);
    if (!arc) {
      arc = {
        buyerIndex: flow[0],
        cc: project.cc,
        tonnes: 0,
        natureTonnes: 0,
        projects: 0,
        lon1: buyer.lon,
        lat1: buyer.lat,
        lon2: country.lon,
        lat2: country.lat,
        conf: buyer.conf || '',
      };
      grouped.set(key, arc);
    }
    arc.tonnes += flow[2];
    arc.projects += 1;
    if (NATURE_GROUPS.includes(project.group)) arc.natureTonnes += flow[2];
  }
  const arcs = [...grouped.values()].sort((a, b) => b.tonnes - a.tonnes || a.buyerIndex - b.buyerIndex);
  const cap = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : arcs.length;
  return arcs.slice(0, cap);
}

function yearsForFlows(flows) {
  const mask = flows.reduce((bits, flow) => bits | (Number(flow[3]) || 0), 0);
  return yearsFromMask(mask);
}

/** Projects a buyer retired from, under the other active filters. */
export function buyerDetail(dataset, buyerIndex, filters) {
  const buyer = buyerOf(dataset, buyerIndex);
  if (!buyer) return null;
  const active = { ...defaultFilters(), ...filters, buyerIndex };
  const byProject = new Map();
  for (const flow of matchingFlows(dataset, active)) {
    const current = byProject.get(flow[1]) || { tonnes: 0, flows: [] };
    current.tonnes += flow[2];
    current.flows.push(flow);
    byProject.set(flow[1], current);
  }
  const projects = [...byProject.entries()]
    .map(([projectIndex, row]) => {
      const project = projectOf(dataset, projectIndex);
      return {
        projectIndex,
        id: project.id,
        name: project.name,
        country: countryName(dataset, project.cc),
        cc: project.cc,
        type: project.type,
        group: project.group,
        registry: project.registry,
        tonnes: row.tonnes,
        years: yearsForFlows(row.flows),
      };
    })
    .sort((a, b) => b.tonnes - a.tonnes || a.name.localeCompare(b.name));
  const tonnes = projects.reduce((sum, project) => sum + project.tonnes, 0);
  const years = [...new Set(projects.flatMap((project) => project.years))].sort((a, b) => a - b);
  return {
    buyerIndex,
    name: buyer.name,
    hq: buyer.hq || '',
    country: countryName(dataset, buyer.cc),
    cc: buyer.cc || '',
    conf: buyer.conf || 'unplaced',
    method: buyer.method || '',
    domain: buyer.domain || '',
    tonnes,
    years,
    variants: Array.isArray(buyer.variants) ? buyer.variants : [],
    projects,
  };
}

/** Buyers of one project, under the other active filters. */
export function projectDetail(dataset, projectIndex, filters) {
  const project = projectOf(dataset, projectIndex);
  if (!project) return null;
  const active = { ...defaultFilters(), ...filters, buyerIndex: null };
  const byBuyer = new Map();
  for (const flow of matchingFlows(dataset, active)) {
    if (flow[1] !== projectIndex) continue;
    const current = byBuyer.get(flow[0]) || { tonnes: 0, flows: [] };
    current.tonnes += flow[2];
    current.flows.push(flow);
    byBuyer.set(flow[0], current);
  }
  const buyers = [...byBuyer.entries()]
    .map(([buyerIndex, row]) => {
      const buyer = buyerOf(dataset, buyerIndex);
      return {
        buyerIndex,
        name: buyer?.name || '',
        country: countryName(dataset, buyer?.cc),
        cc: buyer?.cc || '',
        conf: buyer?.conf || 'unplaced',
        tonnes: row.tonnes,
        years: yearsForFlows(row.flows),
      };
    })
    .sort((a, b) => b.tonnes - a.tonnes || a.name.localeCompare(b.name));
  const namedTonnes = buyers.reduce((sum, buyer) => sum + buyer.tonnes, 0);
  const undisclosed = Number(project.undisclosed) || 0;
  return {
    projectIndex,
    id: project.id,
    name: project.name,
    country: countryName(dataset, project.cc),
    cc: project.cc,
    type: project.type,
    group: project.group,
    registry: project.registry,
    site: project.site || '',
    developer: project.developer || '',
    owner: project.owner || '',
    operator: project.operator || '',
    issued: Number(project.issued) || 0,
    retiredRegistry: Number(project.retiredRegistry) || 0,
    vintages: Array.isArray(project.vintages) ? project.vintages : [],
    undisclosed,
    undisclosedYears: yearsFromMask(project.undisclosedMask),
    tonnes: namedTonnes,
    accountedTonnes: namedTonnes + undisclosed,
    buyers,
  };
}

/**
 * Projects hosted in one country (the arc destination), under the filters.
 * @returns {object[]}
 */
export function countryProjects(dataset, countryCode, filters) {
  const active = { ...defaultFilters(), ...filters };
  const byProject = new Map();
  for (const flow of matchingFlows(dataset, active)) {
    const project = projectOf(dataset, flow[1]);
    if (!project || project.cc !== countryCode) continue;
    const current = byProject.get(flow[1]) || { tonnes: 0, buyers: new Set() };
    current.tonnes += flow[2];
    current.buyers.add(flow[0]);
    byProject.set(flow[1], current);
  }
  return [...byProject.entries()]
    .map(([projectIndex, row]) => {
      const project = projectOf(dataset, projectIndex);
      return {
        projectIndex,
        id: project.id,
        name: project.name,
        type: project.type,
        group: project.group,
        registry: project.registry,
        tonnes: row.tonnes,
        buyers: row.buyers.size,
      };
    })
    .sort((a, b) => b.tonnes - a.tonnes || a.name.localeCompare(b.name));
}

/**
 * Buyers in the current filter, largest retirements first.
 * Includes buyers with no headquarters so a Pacific or country view can
 * still name them.
 * @returns {{buyerIndex:number,name:string,cc:string,conf:string,tonnes:number,placed:boolean}[]}
 */
export function topBuyers(dataset, filters, limit = 20) {
  const active = { ...defaultFilters(), ...filters };
  const byBuyer = new Map();
  for (const flow of matchingFlows(dataset, active)) {
    const current = byBuyer.get(flow[0]) || { tonnes: 0 };
    current.tonnes += flow[2];
    byBuyer.set(flow[0], current);
  }
  const cap = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : byBuyer.size;
  return [...byBuyer.entries()]
    .map(([buyerIndex, row]) => {
      const buyer = buyerOf(dataset, buyerIndex);
      return {
        buyerIndex,
        name: buyer?.name || '',
        cc: buyer?.cc || '',
        conf: buyer?.conf || 'unplaced',
        tonnes: row.tonnes,
        placed: buyer?.lon != null && buyer?.lat != null,
      };
    })
    .sort((a, b) => b.tonnes - a.tonnes || a.name.localeCompare(b.name))
    .slice(0, cap);
}

/** Distinct values for the filter controls, from the whole dataset. */
export function filterChoices(dataset) {
  const buyers = Array.isArray(dataset?.buyers) ? dataset.buyers : [];
  const projects = Array.isArray(dataset?.projects) ? dataset.projects : [];
  const countries = new Map();
  buyers.forEach((buyer, index) => {
    if (!buyer?.cc || buyer.lon == null) return;
    const current = countries.get(buyer.cc) || { cc: buyer.cc, name: countryName(dataset, buyer.cc), buyers: 0 };
    current.buyers += 1;
    countries.set(buyer.cc, current);
  });
  const registries = [...new Set(projects.map((project) => project.registry).filter(Boolean))]
    .sort((a, b) => registryLabel(a).localeCompare(registryLabel(b)));
  const groups = [];
  const present = new Set(projects.map((project) => project.group));
  for (const group of [...NATURE_GROUPS, 'other']) {
    if (present.has(group)) groups.push({ id: group, label: groupLabel(dataset, group) });
  }
  return {
    buyerCountries: [...countries.values()].sort((a, b) => a.name.localeCompare(b.name)),
    registries,
    groups,
    buyers: buyers.map((buyer, index) => ({
      index,
      name: buyer.name,
      variants: Array.isArray(buyer.variants) ? buyer.variants : [],
      cc: buyer.cc || '',
      conf: buyer.conf || '',
    })),
  };
}

function foldQuery(value) {
  return String(value || '').toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim();
}

/**
 * Turn a typed or spoken phrase into a buyer or project search.
 * "show me all Shell credits" and "who bought from April Salumei" are the
 * phrasings the voice tool and the search boxes both accept.
 * @returns {{kind:'buyer'|'project'|'auto', query:string}}
 */
export function interpretCarbonQuery(text) {
  const raw = String(text || '').trim().replace(/[?.!]+$/g, '').replace(/\s+/g, ' ');
  const who = raw.match(/^(?:who|which buyers?)\s+(?:bought|retired|purchased)\s+from\s+(.+)$/i);
  if (who) return { kind: 'project', query: who[1].trim() };
  const buyersOf = raw.match(/^(?:buyers?|retirers?|sellers?)\s+of\s+(.+)$/i);
  if (buyersOf) return { kind: 'project', query: buyersOf[1].trim() };
  const show = raw.match(/^(?:show me|list|find)\s+(?:all\s+)?(.+?)\s+credits?$/i);
  if (show) return { kind: 'buyer', query: show[1].trim() };
  const credits = raw.match(/^(?:all\s+)?(.+?)\s+credits?$/i);
  if (credits && !/^(who|what|which)\b/i.test(credits[1])) {
    return { kind: 'buyer', query: credits[1].replace(/^all\s+/i, '').trim() };
  }
  return { kind: 'auto', query: raw };
}

/**
 * Case-insensitive company search over buyer names. Short queries return
 * nothing so a single letter does not dump the whole registry.
 * @returns {{index:number,name:string,cc:string,conf:string}[]}
 */
export function searchBuyers(dataset, query, limit = 12) {
  const text = foldQuery(query);
  if (text.length < 2) return [];
  const choices = filterChoices(dataset).buyers;
  const hits = [];
  for (const buyer of choices) {
    const names = [buyer.name, ...(buyer.variants || [])].map(foldQuery).filter(Boolean);
    let score = 0;
    for (const name of names) {
      if (name === text) score = Math.max(score, 100);
      else if (name.startsWith(text)) score = Math.max(score, 80);
      else if (name.includes(text)) score = Math.max(score, 50);
    }
    if (score) hits.push({ ...buyer, score });
  }
  hits.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
  const cap = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : hits.length;
  return hits.slice(0, cap);
}

/**
 * Find a project by registry id (VCS1122, GS566) or by name.
 * @returns {{index:number,id:string,name:string,cc:string,registry:string,score:number}[]}
 */
export function searchProjects(dataset, query, limit = 8) {
  const text = foldQuery(query);
  const compact = text.replace(/ /g, '');
  if (compact.length < 2) return [];
  const projects = Array.isArray(dataset?.projects) ? dataset.projects : [];
  const hits = [];
  projects.forEach((project, index) => {
    const id = foldQuery(project?.id).replace(/ /g, '');
    const name = foldQuery(project?.name);
    let score = 0;
    if (id && id === compact) score = 100;
    else if (id && compact.length >= 3 && (id.endsWith(compact) || id.includes(compact))) score = 85;
    else if (name && name === text) score = 95;
    else if (name && name.startsWith(text)) score = 75;
    else if (name && text.length >= 3 && name.includes(text)) score = 55;
    if (!score) return;
    hits.push({
      index,
      id: project.id,
      name: project.name,
      cc: project.cc || '',
      registry: project.registry || '',
      score,
    });
  });
  hits.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
  const cap = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : hits.length;
  return hits.slice(0, cap);
}

/**
 * Resolve a typed or spoken carbon question to the best buyer or project.
 * @returns {{kind:string,query:string,hits:object[]}}
 */
export function resolveCarbonQuery(dataset, text) {
  const parsed = interpretCarbonQuery(text);
  if (!parsed.query) return { kind: 'none', query: '', hits: [] };
  if (parsed.kind === 'buyer') {
    return { kind: 'buyer', query: parsed.query, hits: searchBuyers(dataset, parsed.query, 5) };
  }
  if (parsed.kind === 'project') {
    return { kind: 'project', query: parsed.query, hits: searchProjects(dataset, parsed.query, 5) };
  }
  const projects = searchProjects(dataset, parsed.query, 5);
  const buyers = searchBuyers(dataset, parsed.query, 5);
  const projectScore = projects[0]?.score || 0;
  const buyerScore = buyers[0]?.score || 0;
  if (projectScore >= 90 && projectScore >= buyerScore) {
    return { kind: 'project', query: parsed.query, hits: projects };
  }
  if (buyerScore) return { kind: 'buyer', query: parsed.query, hits: buyers };
  if (projectScore) return { kind: 'project', query: parsed.query, hits: projects };
  return { kind: 'none', query: parsed.query, hits: [] };
}
