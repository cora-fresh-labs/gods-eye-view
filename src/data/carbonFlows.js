import * as Cesium from 'cesium';
import { governorRequestRender, holdContinuousRender, releaseContinuousRender } from '../renderGovernor.js';
import {
  arcsFor,
  buyerCountryRanking,
  buyerDetail,
  countryName,
  countryProjects,
  defaultFilters,
  filterChoices,
  formatTonnes,
  groupLabel,
  projectDetail,
  registryLabel,
  resolveCarbonQuery,
  searchBuyers,
  searchProjects,
  summarize,
  topBuyers,
} from './carbonFlowsModel.js';

const DATA_URL = new URL('./local_data/carbon/carbon-flows.json', import.meta.url).href;
const RENDER_HOLD = 'carbon-flows';
const ARC_LIMIT = 480;
const CYCLE_MS = 5600;
const FLOW_TYPE = 'CarbonArcFlow';

const FLOW_SOURCE = `
czm_material czm_getMaterial(czm_materialInput materialInput) {
  czm_material m = czm_getDefaultMaterial(materialInput);
  float along = fract(materialInput.st.s * 4.0 - time);
  float head = smoothstep(0.0, 0.06, along) * (1.0 - smoothstep(0.2, 0.62, along));
  m.diffuse = color.rgb;
  m.alpha = color.a * (0.22 + 0.78 * head);
  return m;
}
`;

let _flowTypeReady = false;

function ensureFlowMaterialType() {
  if (_flowTypeReady) return;
  _flowTypeReady = true;
  try {
    Cesium.Material.fromType(FLOW_TYPE);
    return;
  } catch {
    // First registration for this page.
  }
  Cesium.Material._materialCache.addMaterial(FLOW_TYPE, {
    fabric: {
      type: FLOW_TYPE,
      uniforms: {
        color: new Cesium.Color(0.12, 0.55, 0.32, 0.95),
        time: 0,
      },
      source: FLOW_SOURCE,
    },
    translucent: true,
  });
}

function flowMaterial(css, alpha) {
  ensureFlowMaterialType();
  const color = Cesium.Color.fromCssColorString(css).withAlpha(alpha);
  return new Cesium.Material({
    fabric: {
      type: FLOW_TYPE,
      uniforms: { color, time: 0 },
    },
  });
}

function arcPositions(lon1, lat1, lon2, lat2) {
  const start = Cesium.Cartographic.fromDegrees(lon1, lat1);
  const end = Cesium.Cartographic.fromDegrees(lon2, lat2);
  const geodesic = new Cesium.EllipsoidGeodesic(start, end);
  const distance = geodesic.surfaceDistance;
  if (distance < 120000) {
    // Same-country flows share one label point. A short loop keeps the
    // retirement visible without pretending the project sits offshore.
    const steps = 28;
    const positions = [];
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      const lift = Math.sin(t * Math.PI) * 180000;
      const east = Math.sin(t * Math.PI) * 1.4;
      positions.push(Cesium.Cartesian3.fromDegrees(lon1 + east, lat1, lift));
    }
    return positions;
  }
  const steps = Math.max(28, Math.min(72, Math.round(distance / 280000)));
  const peak = Math.min(1400000, Math.max(120000, distance * 0.2));
  const positions = [];
  for (let i = 0; i <= steps; i += 1) {
    const carto = geodesic.interpolateUsingFraction(i / steps);
    const lift = Math.sin((i / steps) * Math.PI) * peak;
    positions.push(Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, lift));
  }
  return positions;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function confidenceLabel(conf) {
  if (conf === 'high') return 'HQ located';
  if (conf === 'medium') return 'HQ approximate';
  if (conf === 'low') return 'Country only';
  return 'Not placed';
}

/**
 * Carbon-credit flows from named retirement beneficiaries to the countries
 * of the projects whose credits they retired.
 */
export function createCarbonFlowsLayer() {
  let _viewer = null;
  let _dataset = null;
  let _enabled = false;
  let _loading = false;
  let _error = null;
  let _points = null;
  let _lines = null;
  let _materials = [];
  let _filters = defaultFilters();
  let _cycle = true;
  let _cycleTimer = null;
  let _cycleIndex = 0;
  let _ranking = [];
  let _arcCount = 0;
  let _preRender = null;
  let _click = null;
  let _panel = null;
  let _loadPromise = null;
  let _selection = null;

  function activeFilters() {
    const filters = { ..._filters };
    if (_cycle && !filters.pacific && !filters.buyerCountry && filters.buyerIndex == null && _ranking.length) {
      filters.buyerCountry = _ranking[_cycleIndex % _ranking.length].cc;
    }
    return filters;
  }

  function clearScene() {
    if (_points) _points.entities.removeAll();
    if (_lines) _lines.removeAll();
    _materials = [];
    _arcCount = 0;
  }

  function renderScene() {
    if (!_viewer || !_dataset || !_enabled) return;
    clearScene();
    const filters = activeFilters();
    const summary = summarize(_dataset, filters);
    const arcs = arcsFor(_dataset, filters, ARC_LIMIT);
    const maxTonnes = arcs.reduce((max, arc) => Math.max(max, arc.tonnes), 1);
    const buyerSeen = new Set();
    const countrySeen = new Set();

    for (const arc of arcs) {
      const nature = arc.natureTonnes >= arc.tonnes / 2;
      const color = arc.conf === 'low' ? '#c9842a' : (nature ? '#1f8a4c' : '#6e8494');
      const alpha = arc.conf === 'low' ? 0.75 : 0.92;
      const width = 1.15 + 3.4 * (Math.log10(arc.tonnes + 10) / Math.log10(maxTonnes + 10));
      const material = flowMaterial(color, alpha);
      _materials.push(material);
      _lines.add({
        positions: arcPositions(arc.lon1, arc.lat1, arc.lon2, arc.lat2),
        width,
        material,
      });
      if (!buyerSeen.has(arc.buyerIndex)) {
        buyerSeen.add(arc.buyerIndex);
        const buyer = _dataset.buyers[arc.buyerIndex];
        _points.entities.add({
          id: `carbon:buyer:${arc.buyerIndex}`,
          position: Cesium.Cartesian3.fromDegrees(arc.lon1, arc.lat1, 20000),
          point: {
            pixelSize: arc.conf === 'low' ? 7 : 9,
            color: Cesium.Color.fromCssColorString(arc.conf === 'low' ? '#e0a45a' : '#f4fff6'),
            outlineColor: Cesium.Color.fromCssColorString('#145c32'),
            outlineWidth: 2,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          properties: { kind: 'buyer', buyerIndex: arc.buyerIndex, name: buyer?.name || '' },
        });
      }
      if (!countrySeen.has(arc.cc)) {
        countrySeen.add(arc.cc);
        const country = _dataset.countries[arc.cc];
        _points.entities.add({
          id: `carbon:country:${arc.cc}`,
          position: Cesium.Cartesian3.fromDegrees(country.lon, country.lat, 15000),
          point: {
            pixelSize: 8,
            color: Cesium.Color.fromCssColorString('#1f8a4c'),
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 1,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          properties: { kind: 'country', cc: arc.cc },
        });
      }
    }
    _arcCount = arcs.length;
    paintPanel(summary, filters);
    governorRequestRender('carbon-flows-render');
  }

  function stopCycle() {
    _cycle = false;
    if (_cycleTimer) {
      clearInterval(_cycleTimer);
      _cycleTimer = null;
    }
  }

  function startCycle() {
    if (_cycleTimer || !_ranking.length) return;
    _cycle = true;
    _cycleTimer = setInterval(() => {
      if (!_enabled || !_cycle) return;
      _cycleIndex = (_cycleIndex + 1) % _ranking.length;
      _selection = null;
      renderScene();
    }, CYCLE_MS);
  }

  function ensurePanel() {
    if (_panel) return _panel;
    const panel = el('section', 'carbon-panel');
    panel.id = 'carbon-flows-panel';
    panel.setAttribute('aria-label', 'Follow the carbon money');
    document.body.appendChild(panel);
    panel.addEventListener('click', onPanelClick);
    panel.addEventListener('change', onPanelChange);
    panel.addEventListener('input', onPanelInput);
    _panel = panel;
    return panel;
  }

  function paintPanel(summary, filters) {
    const panel = ensurePanel();
    panel.hidden = false;
    const choices = filterChoices(_dataset);
    const meta = _dataset.meta || {};
    panel.replaceChildren();

    const head = el('div', 'carbon-panel-head');
    head.append(
      el('p', 'carbon-kicker', 'Follow the carbon money'),
      el('h2', 'carbon-caption', summary.caption),
      el('p', 'carbon-tonnes', `${formatTonnes(summary.tonnes)} retired`),
    );
    panel.append(head);

    const controls = el('div', 'carbon-controls');
    controls.append(
      selectControl('buyerCountry', 'Buyer country', [
        ['', 'All countries'],
        ...choices.buyerCountries.map((country) => [country.cc, country.name]),
      ], filters.buyerCountry),
      selectControl('group', 'Project type', [
        ['', 'All types'],
        ['nature', 'Nature-based'],
        ...choices.groups.map((group) => [group.id, group.label]),
      ], filters.group),
      selectControl('registry', 'Registry', [
        ['', 'All registries'],
        ...choices.registries.map((code) => [code, registryLabel(code)]),
      ], filters.registry),
      selectControl('year', 'Year', [
        ['', 'All years'],
        ...yearChoices(),
      ], filters.year == null ? '' : String(filters.year)),
    );
    panel.append(controls);

    const search = el('label', 'carbon-search');
    search.append(el('span', 'carbon-label', 'Buyer'));
    const input = el('input');
    input.type = 'search';
    input.placeholder = 'Shell, or “show me all Shell credits”';
    input.dataset.carbonInput = 'buyer';
    input.value = filters.buyerIndex == null ? '' : (_dataset.buyers[filters.buyerIndex]?.name || '');
    search.append(input);
    panel.append(search);
    const projectSearch = el('label', 'carbon-search');
    projectSearch.append(el('span', 'carbon-label', 'Project'));
    const projectInput = el('input');
    projectInput.type = 'search';
    projectInput.placeholder = 'April Salumei, VCS1122, or “who bought from…”';
    projectInput.dataset.carbonInput = 'project';
    projectInput.value = filters.projectIndex == null ? '' : (_dataset.projects[filters.projectIndex]?.name || '');
    projectSearch.append(projectInput);
    panel.append(projectSearch);
    const results = el('div', 'carbon-search-results');
    results.dataset.carbonResults = '1';
    panel.append(results);

    const toggles = el('div', 'carbon-toggles');
    toggles.append(
      toggleButton('pacific', filters.pacific ? 'Pacific on' : 'Pacific projects', filters.pacific),
      toggleButton('cycle', _cycle ? 'Cycling countries' : 'Cycle countries', _cycle),
      toggleButton('low', filters.hideLowConfidence ? 'Hiding low confidence' : 'Hide low confidence', filters.hideLowConfidence),
    );
    panel.append(toggles);

    const detail = el('div', 'carbon-detail');
    detail.dataset.carbonDetail = '1';
    panel.append(detail);
    paintDetail(detail, filters);

    const foot = el('p', 'carbon-source');
    foot.append(
      document.createTextNode(`${meta.dataset || 'Berkeley VROD'} ${meta.version || ''} · through ${meta.dataThrough || ''} · retrieved ${meta.retrieved || ''} · ${meta.license || 'CC BY 4.0'}. `),
    );
    const link = el('a', null, 'Source');
    link.href = meta.sourceUrl || 'https://gspp.berkeley.edu/berkeley-carbon-trading-project/offsets-database';
    link.target = '_blank';
    link.rel = 'noopener';
    foot.append(link);
    const counts = meta.counts || {};
    const gap = el('span', 'carbon-gap', ` ${counts.buyersUnplaced?.toLocaleString?.('en-US') || counts.buyersUnplaced || 0} named buyers have no placed headquarters.`);
    foot.append(gap);
    panel.append(foot);
  }

  function yearChoices() {
    const years = [];
    for (let year = 2026; year >= 2005; year -= 1) years.push([String(year), String(year)]);
    return years;
  }

  function selectControl(name, label, options, value) {
    const wrap = el('label', 'carbon-field');
    wrap.append(el('span', 'carbon-label', label));
    const select = el('select');
    select.dataset.carbonSelect = name;
    for (const [optionValue, optionLabel] of options) {
      const option = el('option', null, optionLabel);
      option.value = optionValue;
      if (String(optionValue) === String(value)) option.selected = true;
      select.append(option);
    }
    wrap.append(select);
    return wrap;
  }

  function toggleButton(name, label, pressed) {
    const button = el('button', `carbon-chip${pressed ? ' is-on' : ''}`, label);
    button.type = 'button';
    button.dataset.carbonToggle = name;
    button.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    return button;
  }

  function paintDetail(container, filters) {
    container.replaceChildren();
    if (!_selection) {
      container.append(el('p', 'carbon-hint', 'Click a buyer or a project country. Arcs run from a headquarters Wikidata could place to the project country. VROD has no project coordinates, so a country point is the whole host country. Names without a dot were published but not located.'));
      const leaders = topBuyers(_dataset, filters, 12);
      if (leaders.length) {
        container.append(el('p', 'carbon-detail-meta', 'Largest named buyers in this view'));
        const list = el('div', 'carbon-list');
        for (const buyer of leaders) {
          const button = el('button', 'carbon-row', '');
          button.type = 'button';
          button.dataset.carbonBuyer = String(buyer.buyerIndex);
          const place = buyer.placed ? (buyer.cc || 'placed') : 'not placed';
          button.append(
            el('span', 'carbon-row-name', buyer.name),
            el('span', 'carbon-row-meta', `${place} · ${confidenceLabel(buyer.conf)} · ${formatTonnes(buyer.tonnes)}`),
          );
          list.append(button);
        }
        container.append(list);
      }
      return;
    }
    if (_selection.kind === 'buyer') {
      const detail = buyerDetail(_dataset, _selection.buyerIndex, filters);
      if (!detail) return;
      container.append(el('h3', 'carbon-detail-title', detail.name));
      const where = [detail.hq, detail.country].filter(Boolean).join(', ');
      container.append(el('p', 'carbon-detail-meta', `${where || 'Headquarters unknown'} · ${confidenceLabel(detail.conf)} · ${formatTonnes(detail.tonnes)}`));
      if (detail.years.length) {
        container.append(el('p', 'carbon-detail-meta', `Retired ${detail.years[0]}–${detail.years[detail.years.length - 1]}`));
      }
      if (detail.variants?.length) {
        container.append(el('p', 'carbon-detail-meta', `Also recorded as ${detail.variants.join('; ')}`));
      }
      container.append(projectTable(detail.projects));
      return;
    }
    if (_selection.kind === 'country') {
      const projects = countryProjects(_dataset, _selection.cc, filters);
      container.append(el('h3', 'carbon-detail-title', countryName(_dataset, _selection.cc)));
      container.append(el('p', 'carbon-detail-meta', `${projects.length.toLocaleString('en-US')} projects in this view. Open one to see its buyers.`));
      container.append(projectTable(projects));
      return;
    }
    if (_selection.kind === 'project') {
      const detail = projectDetail(_dataset, _selection.projectIndex, filters);
      if (!detail) return;
      container.append(el('h3', 'carbon-detail-title', detail.name));
      container.append(el('p', 'carbon-detail-meta', `${detail.id} · ${registryLabel(detail.registry)} · ${detail.type} · ${detail.country || 'country unknown'}`));
      if (detail.site) container.append(el('p', 'carbon-detail-meta', detail.site));
      const seller = detail.developer || detail.operator || detail.owner;
      if (seller) {
        const role = detail.developer ? 'Developer / seller' : (detail.operator ? 'Operator / seller' : 'Owner / seller');
        container.append(el('p', 'carbon-detail-meta', `${role}: ${seller}`));
      } else {
        container.append(el('p', 'carbon-detail-meta', 'No project developer or proponent in VROD for this project.'));
      }
      if (detail.issued || detail.retiredRegistry) {
        container.append(el('p', 'carbon-detail-meta', `Registry totals: ${formatTonnes(detail.issued)} issued · ${formatTonnes(detail.retiredRegistry)} retired`));
      }
      container.append(el('p', 'carbon-detail-meta', `Retirement records: ${formatTonnes(detail.tonnes)} named + ${formatTonnes(detail.undisclosed)} undisclosed = ${formatTonnes(detail.accountedTonnes)}`));
      if (detail.vintages?.length) {
        const shown = detail.vintages.slice(0, 6).map(([year, issued, retired]) => `${year} ${formatTonnes(issued)} issued / ${formatTonnes(retired)} retired`);
        const extra = detail.vintages.length > 6 ? ` · +${detail.vintages.length - 6} years` : '';
        container.append(el('p', 'carbon-detail-meta', `Vintages: ${shown.join('; ')}${extra}`));
      }
      const list = el('div', 'carbon-list');
      for (const buyer of detail.buyers.slice(0, 80)) {
        const button = el('button', 'carbon-row', '');
        button.type = 'button';
        button.dataset.carbonBuyer = String(buyer.buyerIndex);
        button.append(
          el('span', 'carbon-row-name', buyer.name),
          el('span', 'carbon-row-meta', `${buyer.country || confidenceLabel(buyer.conf)} · ${buyer.years[0] || '—'}${buyer.years.length > 1 ? `–${buyer.years[buyer.years.length - 1]}` : ''} · ${formatTonnes(buyer.tonnes)}`),
        );
        list.append(button);
      }
      if (detail.buyers.length > 80) {
        list.append(el('p', 'carbon-hint', `${(detail.buyers.length - 80).toLocaleString('en-US')} more named buyers are in the data.`));
      }
      if (detail.undisclosed > 0) {
        const row = el('div', 'carbon-row');
        row.append(
          el('span', 'carbon-row-name', 'Undisclosed buyer'),
          el('span', 'carbon-row-meta', `Beneficiary left blank on the registry · ${formatTonnes(detail.undisclosed)}`),
        );
        list.append(row);
      }
      container.append(list);
    }
  }

  function projectTable(projects) {
    const list = el('div', 'carbon-list');
    if (!projects.length) {
      list.append(el('p', 'carbon-hint', 'No projects match these filters.'));
      return list;
    }
    for (const project of projects.slice(0, 200)) {
      const button = el('button', 'carbon-row', '');
      button.type = 'button';
      button.dataset.carbonProject = String(project.projectIndex);
      const place = project.country || countryName(_dataset, _dataset.projects[project.projectIndex]?.cc);
      button.append(
        el('span', 'carbon-row-name', project.name),
        el('span', 'carbon-row-meta', `${project.id} · ${registryLabel(project.registry)} · ${groupLabel(_dataset, project.group) || project.type}${place ? ` · ${place}` : ''} · ${formatTonnes(project.tonnes)}`),
      );
      list.append(button);
    }
    if (projects.length > 200) {
      list.append(el('p', 'carbon-hint', `${(projects.length - 200).toLocaleString('en-US')} more projects are in the data and not listed here.`));
    }
    return list;
  }

  function onPanelClick(event) {
    const project = event.target.closest?.('[data-carbon-project]');
    if (project) {
      focusProject(Number(project.dataset.carbonProject));
      return;
    }
    const projectHit = event.target.closest?.('[data-carbon-project-result]');
    if (projectHit) {
      focusProject(Number(projectHit.dataset.carbonProjectResult));
      return;
    }
    const buyer = event.target.closest?.('[data-carbon-buyer]');
    if (buyer) {
      focusBuyer(Number(buyer.dataset.carbonBuyer));
      return;
    }
    const result = event.target.closest?.('[data-carbon-result]');
    if (result) {
      focusBuyer(Number(result.dataset.carbonResult));
      return;
    }
    const toggle = event.target.closest?.('[data-carbon-toggle]');
    if (!toggle) return;
    const name = toggle.dataset.carbonToggle;
    if (name === 'pacific') {
      _filters = { ..._filters, pacific: !_filters.pacific };
      if (_filters.pacific) stopCycle();
      _selection = null;
      renderScene();
      if (_filters.pacific) flyToPacific();
      return;
    }
    if (name === 'cycle') {
      if (_cycle) stopCycle();
      else {
        _filters = { ...defaultFilters() };
        _selection = null;
        refreshRanking();
        startCycle();
      }
      renderScene();
      return;
    }
    if (name === 'low') {
      _filters = { ..._filters, hideLowConfidence: !_filters.hideLowConfidence };
      _selection = null;
      renderScene();
    }
  }

  function onPanelChange(event) {
    const select = event.target.closest?.('[data-carbon-select]');
    if (!select) return;
    const name = select.dataset.carbonSelect;
    stopCycle();
    const next = { ..._filters };
    if (name === 'buyerCountry') {
      next.buyerCountry = select.value;
      next.buyerIndex = null;
      next.projectIndex = null;
      next.pacific = false;
    } else if (name === 'group') next.group = select.value;
    else if (name === 'registry') next.registry = select.value;
    else if (name === 'year') next.year = select.value ? Number(select.value) : null;
    _filters = next;
    _selection = null;
    renderScene();
    if (name === 'buyerCountry' && select.value) flyToCountry(select.value);
  }

  function focusProject(projectIndex) {
    stopCycle();
    _filters = { ...defaultFilters(), projectIndex };
    _selection = { kind: 'project', projectIndex };
    renderScene();
    const project = _dataset?.projects?.[projectIndex];
    const country = project?.cc ? _dataset.countries?.[project.cc] : null;
    if (country) flyTo(country.lon, country.lat, 5500000);
  }

  function focusBuyer(buyerIndex) {
    stopCycle();
    _filters = { ...defaultFilters(), buyerIndex };
    _selection = { kind: 'buyer', buyerIndex };
    renderScene();
    flyToBuyer(buyerIndex);
  }

  function onPanelInput(event) {
    const input = event.target.closest?.('[data-carbon-input]');
    if (!input || !_panel) return;
    const results = _panel.querySelector('[data-carbon-results]');
    if (!results) return;
    results.replaceChildren();
    const resolved = resolveCarbonQuery(_dataset, input.value);
    const wantProject = input.dataset.carbonInput === 'project'
      ? resolved.kind !== 'buyer'
      : resolved.kind === 'project';
    if (wantProject) {
      const hits = resolved.kind === 'project' ? resolved.hits : searchProjects(_dataset, input.value, 8);
      for (const hit of hits) {
        const button = el('button', 'carbon-result', `${hit.id} · ${hit.name}`);
        button.type = 'button';
        button.dataset.carbonProjectResult = String(hit.index);
        results.append(button);
      }
      return;
    }
    const hits = resolved.kind === 'buyer' ? resolved.hits : searchBuyers(_dataset, input.value, 8);
    for (const hit of hits) {
      const button = el('button', 'carbon-result', `${hit.name}${hit.cc ? ` · ${hit.cc}` : ''}`);
      button.type = 'button';
      button.dataset.carbonResult = String(hit.index);
      results.append(button);
    }
  }

  function flyTo(lon, lat, height) {
    _viewer?.camera?.flyTo?.({
      destination: Cesium.Cartesian3.fromDegrees(lon, lat, height),
      duration: 1.5,
    });
  }

  function flyToBuyer(index) {
    const buyer = _dataset?.buyers?.[index];
    if (buyer?.lon == null) return;
    flyTo(buyer.lon, buyer.lat, 4500000);
  }

  function flyToCountry(code) {
    const country = _dataset?.countries?.[code];
    if (!country) return;
    flyTo(country.lon, country.lat, 6500000);
  }

  function flyToPacific() {
    flyTo(172, -8, 9000000);
  }

  function refreshRanking() {
    _ranking = _dataset ? buyerCountryRanking(_dataset, _filters).slice(0, 24) : [];
    if (_cycleIndex >= _ranking.length) _cycleIndex = 0;
  }

  async function loadDataset() {
    if (_dataset) return _dataset;
    if (_loadPromise) return _loadPromise;
    _loading = true;
    _error = null;
    _loadPromise = fetch(DATA_URL)
      .then((response) => {
        if (!response.ok) throw new Error(`Carbon dataset HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        if (!payload?.buyers || !payload?.projects || !payload?.flows) {
          throw new Error('Carbon dataset is missing buyers, projects, or flows');
        }
        _dataset = payload;
        _loading = false;
        refreshRanking();
        return payload;
      })
      .catch((error) => {
        _loading = false;
        _loadPromise = null;
        _error = error?.message || 'Carbon dataset failed to load';
        throw error;
      });
    return _loadPromise;
  }

  function installInput(viewer) {
    if (_click || !viewer?.scene?.canvas) return;
    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction((click) => {
      if (!_enabled || !_points) return;
      const picked = viewer.scene.pick(click.position);
      const entity = picked?.id;
      const id = typeof entity?.id === 'string' ? entity.id : '';
      if (!id.startsWith('carbon:')) return;
      if (id.startsWith('carbon:buyer:')) {
        focusBuyer(Number(id.slice('carbon:buyer:'.length)));
        return;
      }
      if (id.startsWith('carbon:country:')) {
        _selection = { kind: 'country', cc: id.slice('carbon:country:'.length) };
        renderScene();
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    _click = handler;
  }

  function installClock(viewer) {
    if (_preRender || !viewer?.scene?.preRender) return;
    _preRender = viewer.scene.preRender.addEventListener(() => {
      if (!_enabled || !_materials.length) return;
      const time = (performance.now() / 1000) * 0.28;
      for (const material of _materials) {
        if (material?.uniforms) material.uniforms.time = time;
      }
    });
  }

  const layer = {
    id: 'carbon-flows',
    name: 'Carbon Flows',
    icon: '🍃',
    source: 'Berkeley VROD',
    updateInterval: 0,
    statsRefreshInterval: 2000,

    init(viewer) {
      _viewer = viewer;
      _points = new Cesium.CustomDataSource('carbon-flows');
      _points.show = false;
      viewer.dataSources.add(_points);
      _lines = new Cesium.PolylineCollection();
      _lines.show = false;
      viewer.scene.primitives.add(_lines);
      installInput(viewer);
      installClock(viewer);
    },

    async enable(viewer) {
      _viewer = viewer || _viewer;
      _enabled = true;
      holdContinuousRender(RENDER_HOLD);
      if (_points) _points.show = true;
      if (_lines) _lines.show = true;
      ensurePanel();
      try {
        await loadDataset();
        if (_cycle) startCycle();
        if (_enabled) renderScene();
      } catch (error) {
        _error = error?.message || 'Carbon dataset failed to load';
        console.warn('[Data:carbon-flows]', error);
      }
    },

    disable() {
      _enabled = false;
      if (_cycleTimer) {
        clearInterval(_cycleTimer);
        _cycleTimer = null;
      }
      if (_points) _points.show = false;
      if (_lines) _lines.show = false;
      if (_panel) _panel.hidden = true;
      releaseContinuousRender(RENDER_HOLD);
      governorRequestRender('carbon-flows-hide');
    },

    async update(viewer) {
      _viewer = viewer || _viewer;
      try {
        await loadDataset();
        if (_enabled) renderScene();
        return true;
      } catch (error) {
        _error = error?.message || 'Carbon dataset failed to load';
        return false;
      }
    },

    destroy(viewer) {
      layer.disable();
      if (_click) {
        _click.destroy();
        _click = null;
      }
      if (_preRender) {
        _preRender();
        _preRender = null;
      }
      if (_points && viewer) viewer.dataSources.remove(_points, true);
      if (_lines && viewer) viewer.scene.primitives.remove(_lines);
      if (_panel) {
        _panel.remove();
        _panel = null;
      }
      _points = null;
      _lines = null;
      _viewer = null;
      _materials = [];
    },

    getStats() {
      const through = _dataset?.meta?.dataThrough;
      return {
        count: _arcCount,
        lastUpdate: _dataset ? Date.now() : null,
        loading: _loading,
        error: _error,
        source: 'Berkeley VROD',
        loadingLabel: through ? `through ${through}` : (_loading ? 'loading retirements…' : ''),
      };
    },

    /** Test and screenshot seam. */
    getFilters() {
      return { ...activeFilters(), cycle: _cycle };
    },

    setFilters(partial = {}) {
      stopCycle();
      _filters = { ..._filters, ...partial };
      if (partial.buyerIndex === null) _filters.buyerIndex = null;
      if (partial.year === null) _filters.year = null;
      _selection = partial.selection || null;
      if (_enabled && _dataset) renderScene();
      return layer.getFilters();
    },

    flyToView() {
      const filters = activeFilters();
      if (filters.projectIndex != null) {
        const project = _dataset?.projects?.[filters.projectIndex];
        const country = project?.cc ? _dataset.countries?.[project.cc] : null;
        if (country) flyTo(country.lon, country.lat, 5500000);
        return;
      }
      if (filters.pacific) flyToPacific();
      else if (filters.buyerIndex != null) flyToBuyer(filters.buyerIndex);
      else if (filters.buyerCountry) flyToCountry(filters.buyerCountry);
    },

    /**
     * Spoken or typed lookup. Enables nothing by itself; the caller enables
     * the layer first. Returns a plain summary the voice tool can speak.
     */
    lookup(text) {
      if (!_dataset) return { ok: false, error: 'Carbon dataset is not loaded yet.' };
      const resolved = resolveCarbonQuery(_dataset, text);
      const hit = resolved.hits?.[0];
      if (!hit) return { ok: false, error: `Nothing matched “${resolved.query || text}”.`, query: resolved.query };
      if (resolved.kind === 'project') {
        focusProject(hit.index);
        const detail = projectDetail(_dataset, hit.index, _filters);
        return {
          ok: true,
          kind: 'project',
          id: detail?.id,
          name: detail?.name,
          seller: detail?.developer || detail?.operator || detail?.owner || null,
          country: detail?.country || null,
          namedTonnes: detail?.tonnes || 0,
          undisclosedTonnes: detail?.undisclosed || 0,
          buyers: (detail?.buyers || []).slice(0, 8).map((buyer) => ({ name: buyer.name, tonnes: buyer.tonnes })),
          buyerCount: detail?.buyers?.length || 0,
        };
      }
      focusBuyer(hit.index);
      const detail = buyerDetail(_dataset, hit.index, _filters);
      return {
        ok: true,
        kind: 'buyer',
        name: detail?.name,
        variants: detail?.variants || [],
        tonnes: detail?.tonnes || 0,
        projectCount: detail?.projects?.length || 0,
        projects: (detail?.projects || []).slice(0, 8).map((project) => ({
          id: project.id,
          name: project.name,
          country: project.country,
          tonnes: project.tonnes,
          years: project.years,
        })),
      };
    },

    /** Buyer-country codes in the cycle, largest retirements first. */
    rankedCountries() {
      return _ranking.map((entry) => entry.cc);
    },

    /** Latest flow-shader clock, so a screenshot run can see the arcs move. */
    getAnimationClock() {
      const time = _materials[0]?.uniforms?.time;
      return Number.isFinite(time) ? time : null;
    },
  };

  return layer;
}

const carbonFlowsLayer = createCarbonFlowsLayer();

export default carbonFlowsLayer;
