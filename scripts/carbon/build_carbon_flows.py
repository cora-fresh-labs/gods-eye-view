#!/usr/bin/env python3
"""Build the Follow the Carbon Money dataset from public Berkeley VROD files.

Downloads (cached under .gev-cache/carbon/, never committed):

  - Voluntary Registry Offsets Database v2026-08 (projects, scopes, types, countries)
    https://gspp.berkeley.edu/berkeley-carbon-trading-project/offsets-database
  - VROD registry files v2026-08 (retirement tables with beneficiary names)
  - Natural Earth 50m admin-0 countries (public domain) for country centroids
  - GeoNames cities5000 (CC BY 4.0) for registry-supplied buyer cities

Buyer headquarters that the registries do not locate are looked up on Wikidata
(headquarters statement P159, else coordinate location P625). Results are
cached in scripts/carbon/hq-cache.json so a later refresh does not depend on
Wikidata staying up, and so low-confidence or missed names are not silently
retried into a different guess.

Nothing in the output is hand-authored. A buyer with no registry city and no
single Wikidata hit is left unplaced (or, when the registry itself names a
country and the flow is large or Pacific, placed on that country's centroid
and flagged low confidence).

Usage:
  python3 -m pip install -r scripts/carbon/requirements.txt
  python3 scripts/carbon/build_carbon_flows.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / '.gev-cache' / 'carbon'
OUT_PATH = ROOT / 'src' / 'data' / 'local_data' / 'carbon' / 'carbon-flows.json'
HQ_CACHE_PATH = Path(__file__).resolve().parent / 'hq-cache.json'

VROD_VERSION = 'v2026-08'
VROD_PAGE = 'https://gspp.berkeley.edu/berkeley-carbon-trading-project/offsets-database'
VROD_XLSX_URL = 'https://gspp.berkeley.edu/assets/uploads/page/Voluntary-Registry-Offsets-Database--v2026-08.xlsx'
REGISTRY_XLSX_URL = 'https://gspp.berkeley.edu/assets/uploads/page/VROD-registry-files--2026-08.xlsx'
NE_URL = 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson'
GEONAMES_URL = 'https://download.geonames.org/export/dump/cities5000.zip'
DATA_THROUGH = '2026-08-31'
RETRIEVED = '2026-11-21'
CITATION = (
    'Pamela Quartson, Barbara K Haya, Tyler Bernard, Aline Abayo, Xinyun Rong, '
    'Ivy S So, Micah Elias. (2026). Voluntary Registry Offsets Database v2026-08, '
    'Berkeley Carbon Trading Project, University of California, Berkeley. '
    f'Retrieved from {VROD_PAGE}'
)

YEAR0 = 1996
YEAR1 = 2026
# Country-only buyer dots are a weak location. Draw them for material flows
# and for any buyer of Pacific credits; smaller non-Pacific country-only
# buyers stay in the unplaced gap counts.
COUNTRY_ONLY_MIN_TONNES = 5000
WIKIDATA_MIN_TONNES = 5000
WIKIDATA_ALT_MIN_TONNES = 50000
WIKIDATA_BATCH = 12
USER_AGENT = 'CORA-gods-eye-view-carbon-flows/1.0 (public carbon-credit map; research)'

# Pacific Islands Forum members except Australia and New Zealand, plus US and
# French Pacific territories that sit in the same island region. The preset
# answers "who buys credits from Pacific island projects", not from AU/NZ.
PACIFIC_ISO = (
    'AS', 'CK', 'FJ', 'FM', 'GU', 'KI', 'MH', 'MP', 'NC', 'NR', 'NU', 'PF',
    'PG', 'PW', 'SB', 'TK', 'TO', 'TV', 'VU', 'WF', 'WS',
)

# VROD "Type" values grouped the way the layer filter presents them.
# Wetland Restoration is the harmonized type that contains mangrove and other
# coastal projects; VROD does not split mangroves out. IFM is forestry and is
# kept as its own nature group rather than folded into "other".
TYPE_GROUP = {
    'REDD+': 'redd',
    'Jurisdictional REDD+': 'redd',
    'Avoided Forest Conversion': 'redd',
    'Afforestation/Reforestation': 'arr',
    'Improved Forest Management': 'ifm',
    'Wetland Restoration': 'blue',
    'Biochar': 'biochar',
    'Biomass Carbon Removal & Storage': 'biochar',
    'Sustainable Agriculture': 'soil',
    'Sustainable Grassland Management': 'soil',
    'Avoided Grassland Conversion': 'soil',
    'Improved Irrigation Management': 'soil',
    'Improved irrigation management': 'soil',
    'Nitrogen Management': 'soil',
    'Bundled Compost Production and Soil Application': 'soil',
    'Rice Emission Reductions': 'soil',
}
GROUP_LABEL = {
    'redd': 'REDD+',
    'arr': 'ARR',
    'ifm': 'IFM',
    'blue': 'Wetland / blue carbon',
    'soil': 'Soil',
    'biochar': 'Biochar',
    'tree': 'Tree',
    'construction': 'Construction',
    'artisan': 'Artisan',
    'dac': 'DAC / geologic storage',
    'beccs': 'BECCS',
    'erw': 'Enhanced weathering',
    'biomass': 'Biomass storage',
    'mineral': 'Carbonated materials',
    'other': 'Other',
}

LEGAL_SUFFIXES = {
    'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp', 'corporation',
    'co', 'company', 'gmbh', 'ag', 'sa', 'sas', 'sarl', 'bv', 'nv', 'plc',
    'pty', 'pte', 'lp', 'llp', 'pllc', 'pc', 'se', 'ab', 'oy', 'as', 'spa',
    'srl', 'kg', 'kgaa', 'sl', 'aps', 'saog', 'ooo', 'pjsc', 'jsc',
}

SKIP_BUYER_KEYS = {
    'anonymous', 'unknown', 'n a', 'na', 'none', 'null', 'not available',
    'not disclosed', 'undisclosed', 'retired', 'retirement', 'various',
    'multiple', 'other', 'self', 'on behalf of', 'third party', 'voluntary',
    'environmental benefit', 'acr', 'apx', 'verra', 'vcs', 'gold standard',
    'climate action reserve', 'american carbon registry',
    'architecture for redd transactions', 'isometric', 'winrock',
    'buffer', 'buffer pool', 'cancellation', 'canceled', 'cancelled',
    'registry', 'the registry',
}

# Extra labels for Natural Earth names that VROD spells differently.
# Values are ISO2. Only used when the Natural Earth name index misses.
COUNTRY_ALIASES = {
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
    'us': 'US',
    'uk': 'GB',
    'united kingdom': 'GB',
    'great britain': 'GB',
    'russia': 'RU',
    'russian federation': 'RU',
    'south korea': 'KR',
    'republic of korea': 'KR',
    'korea republic of': 'KR',
    'north korea': 'KP',
    'viet nam': 'VN',
    'vietnam': 'VN',
    'laos': 'LA',
    'lao pdr': 'LA',
    'lao peoples democratic republic': 'LA',
    'cote d ivoire': 'CI',
    'cote divoire': 'CI',
    'ivory coast': 'CI',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'tanzania': 'TZ',
    'united republic of tanzania': 'TZ',
    'democratic republic of the congo': 'CD',
    'dem rep congo': 'CD',
    'dr congo': 'CD',
    'drc': 'CD',
    'congo democratic republic': 'CD',
    'republic of the congo': 'CG',
    'congo': 'CG',
    'congo republic': 'CG',
    'cape verde': 'CV',
    'cabo verde': 'CV',
    'swaziland': 'SZ',
    'eswatini': 'SZ',
    'turkey': 'TR',
    'turkiye': 'TR',
    'syria': 'SY',
    'syrian arab republic': 'SY',
    'iran': 'IR',
    'iran islamic republic of': 'IR',
    'bolivia': 'BO',
    'venezuela': 'VE',
    'moldova': 'MD',
    'republic of moldova': 'MD',
    'brunei': 'BN',
    'brunei darussalam': 'BN',
    'myanmar': 'MM',
    'burma': 'MM',
    'taiwan': 'TW',
    'taiwan province of china': 'TW',
    'hong kong': 'HK',
    'macau': 'MO',
    'macao': 'MO',
    'palestine': 'PS',
    'state of palestine': 'PS',
    'the gambia': 'GM',
    'gambia': 'GM',
    'the bahamas': 'BS',
    'bahamas': 'BS',
    'north macedonia': 'MK',
    'macedonia': 'MK',
    'east timor': 'TL',
    'timor leste': 'TL',
    'federated states of micronesia': 'FM',
    'micronesia': 'FM',
    'micronesia federated states of': 'FM',
    'marshall islands': 'MH',
    'solomon islands': 'SB',
    'papua new guinea': 'PG',
    'new caledonia': 'NC',
    'french polynesia': 'PF',
    'american samoa': 'AS',
    'guam': 'GU',
    'northern mariana islands': 'MP',
    'wallis and futuna': 'WF',
    'cook islands': 'CK',
    'falkland islands': 'FK',
    'falkland islands malvinas': 'FK',
    'netherlands': 'NL',
    'the netherlands': 'NL',
    'uae': 'AE',
    'united arab emirates': 'AE',
    'slovak republic': 'SK',
    'slovakia': 'SK',
    'republic of ireland': 'IE',
    'ireland': 'IE',
    'curacao': 'CW',
    'reunion': 'RE',
    'saint lucia': 'LC',
    'st lucia': 'LC',
    'saint vincent and the grenadines': 'VC',
    'antigua and barbuda': 'AG',
    'trinidad and tobago': 'TT',
    'dominican republic': 'DO',
    'sao tome and principe': 'ST',
    'equatorial guinea': 'GQ',
    'central african republic': 'CF',
    'south sudan': 'SS',
    'bosnia and herzegovina': 'BA',
    'bosnia': 'BA',
}


def log(message: str) -> None:
    print(message, flush=True)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f'cache hit {dest.name}')
        return
    log(f'download {url}')
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + '.part')
    with urllib.request.urlopen(req, timeout=180) as response, tmp.open('wb') as handle:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
    tmp.replace(dest)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def norm(value) -> str:
    text = unicodedata.normalize('NFKD', str(value or ''))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = text.replace('&', ' and ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def token_key(token: str) -> str:
    """Fold a single token so 'S.A.' and 'Ltd.' match the suffix list."""
    return norm(token).replace(' ', '')


def strip_suffix_display(name: str) -> str:
    parts = str(name or '').replace(',', ' ').split()
    while parts and token_key(parts[-1]) in LEGAL_SUFFIXES:
        parts.pop()
    while parts and token_key(parts[-1]) in {'the'}:
        parts.pop()
    return ' '.join(parts).strip(' .,-')


# Leading phrases that are not the buyer. Longer phrases are tried first.
# A mid-string "on behalf of" is handled separately: the beneficiary is the
# party after the last one ("Retired by X on behalf of Y" → Y).
BUYER_PREFIXES = (
    'retired on behalf of ',
    'retirement on behalf of ',
    'retired for ',
    'retirement for ',
    'retired by ',
    'retirement by ',
    'on behalf of ',
    'behalf of ',
    'for ',
)


def peel_buyer_name(raw: str) -> str:
    text = clean_name(raw)
    if not text:
        return ''
    lowered = text.casefold()
    for marker in (' on behalf of ', ' behalf of '):
        index = lowered.rfind(marker)
        if index != -1:
            text = text[index + len(marker):].strip(' .,-')
            break
    changed = True
    while changed and text:
        changed = False
        low = text.casefold()
        for prefix in BUYER_PREFIXES:
            if low.startswith(prefix):
                text = text[len(prefix):].strip(' .,-')
                changed = True
                break
    return text


def buyer_key(name: str) -> str:
    stripped = strip_suffix_display(peel_buyer_name(name) or name)
    key = norm(stripped)
    if len(key) < 3:
        key = norm(name)
    if key.startswith('the ') and len(key) > 8:
        key = key[4:]
    return key


def load_buyer_aliases() -> dict[str, str]:
    """Map a normalised beneficiary key to the canonical display name."""
    path = ROOT / 'src' / 'data' / 'local_data' / 'carbon' / 'buyer-aliases.json'
    payload = json.loads(path.read_text(encoding='utf-8'))
    aliases = {}
    for source, canonical in payload.items():
        if str(source).startswith('_'):
            continue
        canonical_name = clean_name(canonical)
        if not canonical_name:
            continue
        aliases[buyer_key(source)] = canonical_name
        aliases[buyer_key(canonical_name)] = canonical_name
    return aliases


def blank(value) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == '' or text.lower() in {'none', 'null', 'n/a', 'na', '-'}


def removal_class(value) -> str:
    text = clean_name(value).casefold()
    if not text:
        return ''
    if 'mixed' in text:
        return 'mixed'
    if 'removal' in text:
        return 'removal'
    if 'reduction' in text:
        return 'avoidance'
    return ''


def repair_mojibake(text: str) -> str:
    """Undo a UTF-8 string that was decoded as Mac Roman (é → √©)."""
    if not any(ord(ch) > 127 for ch in text):
        return text
    try:
        return text.encode('mac_roman').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def clean_name(value) -> str:
    if blank(value):
        return ''
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return repair_mojibake(text)


def tonnes_of(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, str):
        text = value.replace(',', '').strip()
        if not text or text.lower() in {'n/a', 'na', 'none'}:
            return 0
        try:
            value = float(text)
        except ValueError:
            return 0
    if isinstance(value, (int, float)):
        if value != value or value <= 0:  # NaN or non-positive
            return 0
        return int(round(float(value)))
    return 0


def year_of(value) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, date):
        return value.year
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial (days since 1899-12-30) or an already-year integer.
        if 1990 <= int(value) <= 2035 and float(value) == int(value):
            return int(value)
        if 30000 < float(value) < 80000:
            ordinal = datetime(1899, 12, 30).toordinal() + int(value)
            return datetime.fromordinal(ordinal).year
        return None
    text = str(value).strip()
    match = re.match(r'(\d{4})', text)
    if not match:
        return None
    year = int(match.group(1))
    if 1990 <= year <= 2035:
        return year
    return None


def year_mask(years: set[int]) -> int:
    mask = 0
    for year in years:
        if YEAR0 <= year <= YEAR1:
            mask |= 1 << (year - YEAR0)
    return mask


def header_index(headers) -> dict[str, int]:
    index = {}
    for i, header in enumerate(headers):
        if header is None:
            continue
        key = re.sub(r'\s+', ' ', str(header)).strip()
        index[key] = i
    return index


def cell(row, index, *names):
    for name in names:
        position = index.get(name)
        if position is not None and position < len(row):
            return row[position]
    return None


def load_sheet(path: Path, name: str):
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_path(str(path))
    return workbook.get_sheet_by_name(name).to_python()


def load_countries(path: Path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    by_iso = {}
    by_name = {}
    for feature in payload.get('features', []):
        props = feature.get('properties') or {}
        iso = props.get('ISO_A2') or ''
        if not re.fullmatch(r'[A-Z]{2}', str(iso)):
            iso = props.get('ISO_A2_EH') or ''
        if not re.fullmatch(r'[A-Z]{2}', str(iso)):
            continue
        lon = props.get('LABEL_X')
        lat = props.get('LABEL_Y')
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        # French Guiana and similar overseas labels can share an ISO with the
        # sovereign state. Keep the first (admin-0) feature; 50m admin-0 is
        # one feature per country, so this is the sovereign label point.
        if iso in by_iso:
            continue
        record = {
            'cc': iso,
            'name': props.get('NAME_LONG') or props.get('ADMIN') or props.get('NAME') or iso,
            'lon': round(float(lon), 4),
            'lat': round(float(lat), 4),
        }
        by_iso[iso] = record
        for field in ('NAME', 'NAME_LONG', 'ADMIN', 'GEOUNIT', 'BRK_NAME', 'ABBREV', 'NAME_SORT', 'FORMAL_EN'):
            label = props.get(field)
            if label:
                by_name.setdefault(norm(label), iso)
        by_name.setdefault(norm(record['name']), iso)
    for label, iso in COUNTRY_ALIASES.items():
        if iso in by_iso:
            by_name.setdefault(label, iso)
    return by_iso, by_name


def load_cities(zip_path: Path):
    """(country, normalized name) -> list of candidate cities."""
    index = defaultdict(list)
    with zipfile.ZipFile(zip_path) as archive:
        member = next(name for name in archive.namelist() if name.endswith('.txt'))
        with archive.open(member) as handle:
            for raw in handle:
                parts = raw.decode('utf-8').split('\t')
                if len(parts) < 15:
                    continue
                try:
                    lat = float(parts[4])
                    lon = float(parts[5])
                    population = int(parts[14] or 0)
                except ValueError:
                    continue
                cc = parts[8].strip().upper()
                if not re.fullmatch(r'[A-Z]{2}', cc):
                    continue
                admin1 = parts[10].strip().upper()
                for label in (parts[1], parts[2]):
                    key = norm(label)
                    if len(key) < 2:
                        continue
                    index[(cc, key)].append({
                        'name': parts[1],
                        'lon': lon,
                        'lat': lat,
                        'pop': population,
                        'admin1': admin1,
                    })
    return index


def geocode_city(cities, city: str, cc: str, state: str):
    key = norm(city)
    if len(key) < 2 or not re.fullmatch(r'[A-Z]{2}', cc or ''):
        return None
    # "Seattle, WA" -> Seattle
    if ',' in str(city):
        key = norm(str(city).split(',', 1)[0])
    candidates = cities.get((cc, key)) or []
    if not candidates:
        return None
    state_code = norm(state).upper().replace(' ', '')
    if state_code:
        narrowed = [item for item in candidates if item['admin1'] == state_code or norm(item['admin1']).upper() == state_code]
        if len(narrowed) == 1:
            hit = narrowed[0]
            return {**hit, 'conf': 'high', 'method': 'geonames-city'}
        if narrowed:
            candidates = narrowed
    best = max(candidates, key=lambda item: item['pop'])
    conf = 'high' if len(candidates) == 1 else 'medium'
    return {**best, 'conf': conf, 'method': 'geonames-city'}


def sparql(query: str):
    data = urllib.parse.urlencode({'query': query}).encode('utf-8')
    req = urllib.request.Request(
        'https://query.wikidata.org/sparql',
        data=data,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/sparql-results+json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode('utf-8'))


import urllib.parse  # noqa: E402  (kept next to sparql)


def sparql_escape(label: str) -> str:
    return label.replace('\\', '\\\\').replace('"', '\\"')


def wikidata_lookup(labels: list[str]) -> dict[str, list[dict]]:
    values = ' '.join(f'"{sparql_escape(label)}"@en' for label in labels)
    query = f"""
SELECT ?name ?item ?coord ?cc ?hqLabel WHERE {{
  VALUES ?name {{ {values} }}
  ?item rdfs:label ?name .
  FILTER(LANG(?name) = "en")
  OPTIONAL {{
    ?item wdt:P159 ?hq .
    OPTIONAL {{ ?hq wdt:P625 ?hqCoord }}
    OPTIONAL {{ ?hq rdfs:label ?hqLabel FILTER(LANG(?hqLabel) = "en") }}
  }}
  OPTIONAL {{ ?item wdt:P625 ?anyCoord }}
  BIND(COALESCE(?hqCoord, ?anyCoord) AS ?coord)
  FILTER(BOUND(?coord))
  FILTER(
    EXISTS {{ ?item wdt:P159 ?hqFilter }} ||
    EXISTS {{
      ?item wdt:P31 ?type .
      VALUES ?type {{
        wd:Q4830453 wd:Q6881511 wd:Q783794 wd:Q43229 wd:Q167037
        wd:Q891723 wd:Q18388277 wd:Q2659904 wd:Q1664720 wd:Q18127
      }}
    }}
  )
  OPTIONAL {{ ?item wdt:P17/wdt:P297 ?cc }}
}}
"""
    payload = sparql(query)
    grouped = defaultdict(list)
    seen = set()
    for row in payload.get('results', {}).get('bindings', []):
        name = row.get('name', {}).get('value', '')
        qid = row.get('item', {}).get('value', '').rsplit('/', 1)[-1]
        coord = row.get('coord', {}).get('value', '')
        match = re.search(r'Point\(([-\d.eE]+) ([-\d.eE]+)\)', coord)
        if not name or not match:
            continue
        lon = float(match.group(1))
        lat = float(match.group(2))
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        has_hq = 'hqLabel' in row or 'hq' in query  # presence of hq label means P159 resolved a label
        # hqLabel is only bound when both P159 and an English label exist.
        # Treat a bound hqLabel as a headquarters hit. A coordinate without
        # hqLabel may still be P159 (unlabeled) or P625; the query uses
        # COALESCE, so we re-read whether the headquarters label bound.
        has_hq = 'hqLabel' in row
        dedupe = (norm(name), qid)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        cc = row.get('cc', {}).get('value', '')
        grouped[norm(name)].append({
            'qid': qid,
            'lon': round(lon, 4),
            'lat': round(lat, 4),
            'cc': cc if re.fullmatch(r'[A-Z]{2}', cc or '') else '',
            'hq': row.get('hqLabel', {}).get('value', ''),
            'has_hq': has_hq,
        })
    return grouped


def choose_hit(hits: list[dict], registry_cc: str, cap: str):
    if not hits:
        return None
    with_hq = [hit for hit in hits if hit['has_hq']]
    pool = with_hq or hits
    chosen = None
    if registry_cc:
        matched = [hit for hit in pool if hit['cc'] == registry_cc]
        if len(matched) == 1:
            chosen = matched[0]
        elif len(matched) > 1:
            return None
    if chosen is None and len(pool) == 1:
        chosen = pool[0]
    if chosen is None:
        return None
    conf = 'high' if chosen['has_hq'] else 'medium'
    if cap == 'medium' and conf == 'high':
        conf = 'medium'
    method = 'wikidata-hq' if chosen['has_hq'] else 'wikidata-coord'
    return {**chosen, 'conf': conf, 'method': method}


def load_hq_cache() -> dict:
    if not HQ_CACHE_PATH.exists():
        return {}
    return json.loads(HQ_CACHE_PATH.read_text(encoding='utf-8'))


def save_hq_cache(cache: dict) -> None:
    HQ_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')


def query_wikidata(pending: list[tuple[str, str, str]]) -> None:
    """pending items are (cache_key, label, cap) where cap is 'high' or 'medium'."""
    cache = load_hq_cache()
    todo = [(key, label, cap) for key, label, cap in pending if key not in cache]
    log(f'wikidata lookups {len(todo)} (cache {len(cache)})')
    for start in range(0, len(todo), WIKIDATA_BATCH):
        batch = todo[start:start + WIKIDATA_BATCH]
        labels = [label for _, label, _ in batch]
        try:
            grouped = wikidata_lookup(labels)
        except Exception as error:  # noqa: BLE001 — keep the build going, record the miss next retry
            log(f'wikidata batch failed ({error}); backing off')
            time.sleep(8)
            try:
                grouped = wikidata_lookup(labels)
            except Exception as error2:  # noqa: BLE001
                log(f'wikidata batch skipped ({error2})')
                time.sleep(2)
                continue
        for key, label, cap in batch:
            hits = grouped.get(norm(label), [])
            # Registry country is applied later; cache the raw hits so a
            # country hint can still disambiguate without another query.
            cache[key] = {
                'label': label,
                'cap': cap,
                'hits': [
                    {k: hit[k] for k in ('qid', 'lon', 'lat', 'cc', 'hq', 'has_hq')}
                    for hit in hits
                ],
            }
        save_hq_cache(cache)
        log(f'  cached {min(start + WIKIDATA_BATCH, len(todo))}/{len(todo)}')
        time.sleep(1.1)


ORG_DESCRIPTION = re.compile(
    r'compan|subsidiar|airline|bank|corporat|manufactur|consultanc|business|firm|automotiv|energy|financ',
    re.IGNORECASE,
)
REJECT_DESCRIPTION = re.compile(
    r'\b(pole|genus|species|film|album|song|mountain|river|lake|opera|fossil|protein|taxon)\b',
    re.IGNORECASE,
)


def search_headquarters(name: str, cache: dict):
    """Prefer the Wikidata search hit for the full published name.

    A suffix-stripped SPARQL label ("AUDI" from "AUDI AG") can land on a
    different organization that uses the short name. The search API ranks the
    full name. Geographic and biological hits are rejected. Results are cached.
    """
    key = 'search:' + norm(name)
    if key in cache:
        return cache[key].get('hit')
    query = urllib.parse.urlencode({
        'action': 'wbsearchentities',
        'search': name,
        'language': 'en',
        'format': 'json',
        'limit': 1,
        'type': 'item',
    })
    req = urllib.request.Request(
        'https://www.wikidata.org/w/api.php?' + query,
        headers={'User-Agent': USER_AGENT},
    )
    payload = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode('utf-8'))
            break
        except Exception as error:  # noqa: BLE001
            log(f'wikidata search failed for {name!r} (attempt {attempt + 1}): {error}')
            time.sleep(5 * (attempt + 1))
    if payload is None:
        return None
    found = (payload.get('search') or [None])[0]
    hit = None
    description = (found or {}).get('description') or ''
    qid = (found or {}).get('id') or ''
    if qid and ORG_DESCRIPTION.search(description) and not REJECT_DESCRIPTION.search(description):
        try:
            rows = sparql(f'''
SELECT ?coord ?cc ?hqLabel WHERE {{
  BIND(wd:{qid} AS ?item)
  OPTIONAL {{
    ?item wdt:P159 ?hq .
    OPTIONAL {{ ?hq wdt:P625 ?hqCoord }}
    OPTIONAL {{ ?hq rdfs:label ?hqLabel FILTER(LANG(?hqLabel) = "en") }}
  }}
  OPTIONAL {{ ?item wdt:P625 ?anyCoord }}
  BIND(COALESCE(?hqCoord, ?anyCoord) AS ?coord)
  FILTER(BOUND(?coord))
  OPTIONAL {{ ?item wdt:P17/wdt:P297 ?cc }}
}}''')
        except Exception as error:  # noqa: BLE001
            log(f'wikidata coord failed for {qid}: {error}')
            rows = {}
        binding = ((rows.get('results') or {}).get('bindings') or [None])[0]
        coord = (binding or {}).get('coord', {}).get('value', '')
        match = re.search(r'Point\(([-\d.eE]+) ([-\d.eE]+)\)', coord)
        if match:
            cc = (binding or {}).get('cc', {}).get('value', '')
            hit = {
                'qid': qid,
                'lon': round(float(match.group(1)), 4),
                'lat': round(float(match.group(2)), 4),
                'cc': cc if re.fullmatch(r'[A-Z]{2}', cc or '') else '',
                'hq': (binding or {}).get('hqLabel', {}).get('value', ''),
                'has_hq': 'hqLabel' in (binding or {}),
                'conf': 'medium',
                'method': 'wikidata-search',
            }
    cache[key] = {'label': name, 'cap': 'medium', 'hits': [hit] if hit else [], 'hit': hit}
    save_hq_cache(cache)
    time.sleep(1.1)
    return hit


def resolve_cached(cache: dict, key: str, registry_cc: str):
    entry = cache.get(key)
    if not entry:
        return None
    return choose_hit(entry.get('hits') or [], registry_cc, entry.get('cap') or 'medium')


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    vrod_path = CACHE / 'vrod-v2026-08.xlsx'
    registry_path = CACHE / 'vrod-registry-v2026-08.xlsx'
    ne_path = CACHE / 'ne_50m_admin_0_countries.geojson'
    cities_zip = CACHE / 'cities5000.zip'
    download(VROD_XLSX_URL, vrod_path)
    download(REGISTRY_XLSX_URL, registry_path)
    download(NE_URL, ne_path)
    download(GEONAMES_URL, cities_zip)

    log('indexing countries and cities')
    countries, country_names = load_countries(ne_path)
    cities = load_cities(cities_zip)
    aliases = load_buyer_aliases()
    log(f'  {len(countries)} countries, {len(cities)} city keys, {len(aliases)} buyer aliases')

    log('reading projects')
    project_rows = load_sheet(vrod_path, 'PROJECTS')
    header_row = next(i for i, row in enumerate(project_rows) if row and row[0] == 'Project ID')
    headers = header_index(project_rows[header_row])
    issued_cols, retired_cols = {}, {}
    section = ''
    section_row = project_rows[header_row - 1] if header_row else []
    for index, header in enumerate(project_rows[header_row]):
        above = section_row[index] if index < len(section_row) else None
        if above:
            section = str(above)
        try:
            year = int(float(header))
        except (TypeError, ValueError):
            continue
        if not YEAR0 <= year <= YEAR1:
            continue
        if section.startswith('Credits issued by vintage'):
            issued_cols[year] = index
        elif section.startswith('Credits retired'):
            retired_cols[year] = index
    projects = {}
    unmatched_countries = Counter()
    for row in project_rows[header_row + 1:]:
        project_id = clean_name(row[0] if row else '')
        if not project_id or project_id == 'Project ID':
            continue
        registry = clean_name(cell(row, headers, 'Voluntary Registry'))
        country_name = clean_name(cell(row, headers, 'Country'))
        iso = country_names.get(norm(country_name), '')
        if country_name and not iso and norm(country_name) not in {'international', ''}:
            unmatched_countries[country_name] += 1
        project_type = clean_name(cell(row, headers, 'Type')) or 'Unknown'
        projects[project_id] = {
            'id': project_id,
            'name': clean_name(cell(row, headers, 'Project Name')) or project_id,
            'registry': registry,
            'country': country_name,
            'cc': iso,
            'type': project_type,
            'group': TYPE_GROUP.get(project_type, 'other'),
            'state': clean_name(cell(row, headers, 'State')),
            'site': clean_name(cell(row, headers, 'Project Site Location')),
            'developer': clean_name(cell(row, headers, 'Project Developer')),
            'owner': clean_name(cell(row, headers, 'Project Owner')),
            'operator': clean_name(cell(row, headers, 'Offset Project Operator')),
            'issued': tonnes_of(cell(row, headers, 'Total Credits Issued')),
            'retiredRegistry': tonnes_of(cell(row, headers, 'Total Credits Retired')),
            'vintages': [],
            'undisclosed': 0,
            'undisclosed_years': set(),
            'lat': None,
            'lon': None,
            'unit': 'tCO2e',
            'removal': removal_class(cell(row, headers, 'Reduction / Removal')),
            'standard': '',
            'status': '',
        }
        vintages = []
        for year in range(YEAR0, YEAR1 + 1):
            issued = tonnes_of(row[issued_cols[year]]) if year in issued_cols and issued_cols[year] < len(row) else 0
            retired = tonnes_of(row[retired_cols[year]]) if year in retired_cols and retired_cols[year] < len(row) else 0
            if issued or retired:
                vintages.append([year, issued, retired])
        projects[project_id]['vintages'] = vintages
    log(f'  {len(projects)} projects; unmatched countries: {unmatched_countries.most_common(20)}')

    buyers = {}

    def ensure_buyer(name: str):
        raw = clean_name(name)
        peeled = peel_buyer_name(raw)
        pre_alias = buyer_key(peeled or raw)
        letters = sum(ch.isalpha() for ch in pre_alias)
        # Account numbers and one-letter leftovers are not buyer names.
        if letters < 3 or pre_alias in SKIP_BUYER_KEYS:
            return None
        canonical = aliases.get(pre_alias)
        key = buyer_key(canonical) if canonical else pre_alias
        if key in SKIP_BUYER_KEYS:
            return None
        entry = buyers.get(key)
        if entry is None:
            entry = {
                'key': key,
                'canonical': canonical or '',
                'names': Counter(),
                'lookup_keys': set(),
                'tonnes': 0,
                'years': set(),
                'cities': Counter(),
                'countries': Counter(),
                'domains': Counter(),
                'pacific': False,
                'flows': {},
            }
            buyers[key] = entry
        if canonical:
            entry['canonical'] = canonical
        entry['names'][raw] += 1
        entry['lookup_keys'].add(pre_alias)
        entry['lookup_keys'].add(key)
        return entry

    stats = Counter()

    def add_flow(entry, project_id, qty, year, city, state, cc, domain, located_as_holder):
        project = projects.get(project_id)
        if project is None:
            stats['unjoined_project_rows'] += 1
            stats['unjoined_project_tonnes'] += qty
            return
        if not project['cc']:
            stats['unlocated_project_tonnes'] += qty
            return
        entry['tonnes'] += qty
        if year:
            entry['years'].add(year)
        if domain:
            entry['domains'][domain] += 1
        if located_as_holder and cc:
            entry['countries'][cc] += qty
            if city:
                entry['cities'][(city, cc, state or '')] += qty
        flow = entry['flows'].get(project_id)
        if flow is None:
            flow = {'tonnes': 0, 'years': set()}
            entry['flows'][project_id] = flow
        flow['tonnes'] += qty
        if year:
            flow['years'].add(year)
        if project['cc'] in PACIFIC_ISO:
            entry['pacific'] = True
        stats['joined_tonnes'] += qty
        stats['joined_rows'] += 1

    def consume(sheet_name, loader):
        log(f'reading {sheet_name}')
        rows = load_sheet(registry_path, sheet_name)
        if not rows:
            return
        index = header_index(rows[0])
        kept = 0
        for row in rows[1:]:
            stats['retirement_rows'] += 1
            parsed = loader(row, index)
            if parsed is None:
                stats['skipped_rows'] += 1
                continue
            if parsed.get('undisclosed'):
                project = projects.get(parsed['project_id'])
                if project is None:
                    stats['unjoined_project_rows'] += 1
                    continue
                project['undisclosed'] += parsed['tonnes']
                if parsed.get('year'):
                    project['undisclosed_years'].add(parsed['year'])
                stats['undisclosed_rows'] += 1
                stats['undisclosed_tonnes'] += parsed['tonnes']
                continue
            entry = ensure_buyer(parsed['name'])
            if entry is None:
                stats['nameless_or_skipped_buyer_rows'] += 1
                continue
            add_flow(
                entry,
                parsed['project_id'],
                parsed['tonnes'],
                parsed['year'],
                parsed.get('city') or '',
                parsed.get('state') or '',
                parsed.get('cc') or '',
                parsed.get('domain') or '',
                parsed.get('located_as_holder', False),
            )
            kept += 1
        log(f'  kept {kept} named rows')

    def registry_project_id(raw, prefix):
        text = clean_name(raw)
        if not text:
            return ''
        if text.upper().startswith(prefix):
            return prefix + text[len(prefix):]
        # Numeric ids arrive as ints (1904) or numeric strings.
        if isinstance(raw, float) and raw == int(raw):
            text = str(int(raw))
        elif isinstance(raw, int):
            text = str(raw)
        return prefix + text

    def acr_like(prefix):
        def loader(row, index):
            tonnes = tonnes_of(cell(row, index, 'Quantity of Credits'))
            if tonnes <= 0:
                return None
            behalf = clean_name(cell(row, index, 'Retired On Behalf Of'))
            holder = clean_name(cell(row, index, 'Credit Holder'))
            # "Retired On Behalf Of Type" is a category, not a name. Only the
            # name column is a beneficiary. When it is empty the retiring
            # account holder is the named buyer, and the holder city/country
            # describe that buyer. A distinct on-behalf-of name does not
            # inherit the holder's address.
            if behalf and buyer_key(behalf) not in SKIP_BUYER_KEYS:
                name = behalf
                located = False
            else:
                name = holder
                located = True
            if not name:
                return {
                    'undisclosed': True,
                    'project_id': registry_project_id(cell(row, index, 'Project Id'), prefix),
                    'tonnes': tonnes,
                    'year': year_of(cell(row, index, 'Status Effective (GMT)')),
                }
            cc = clean_name(cell(row, index, 'Credit Holder Country')).upper()
            if not re.fullmatch(r'[A-Z]{2}', cc):
                cc = ''
            return {
                'name': name,
                'project_id': registry_project_id(cell(row, index, 'Project Id'), prefix),
                'tonnes': tonnes,
                'year': year_of(cell(row, index, 'Status Effective (GMT)')),
                'city': clean_name(cell(row, index, 'Credit Holder City')) if located else '',
                'state': clean_name(cell(row, index, 'Credit Holder State/Province')) if located else '',
                'cc': cc if located else '',
                'domain': '',
                'located_as_holder': located,
            }
        return loader

    def car_loader(row, index):
        tonnes = tonnes_of(cell(row, index, 'Quantity of Offset Credits'))
        name = clean_name(cell(row, index, 'Account Holder'))
        project_id = registry_project_id(cell(row, index, 'Project ID'), 'CAR')
        if tonnes <= 0:
            return None
        if not name:
            return {'undisclosed': True, 'project_id': project_id, 'tonnes': tonnes, 'year': year_of(cell(row, index, 'Status Effective'))}
        return {
            'name': name,
            'project_id': project_id,
            'tonnes': tonnes,
            'year': year_of(cell(row, index, 'Status Effective')),
            'located_as_holder': False,
        }

    def gold_loader(row, index):
        tonnes = tonnes_of(cell(row, index, 'Quantity'))
        name = clean_name(cell(row, index, 'Using Entity'))
        project_id = registry_project_id(cell(row, index, 'GSID'), 'GS')
        if tonnes <= 0:
            return None
        if not name:
            return {'undisclosed': True, 'project_id': project_id, 'tonnes': tonnes, 'year': year_of(cell(row, index, 'Retirement Date'))}
        # Notes sometimes name another party. They are free text and are not
        # parsed, so a vague "Using Entity" stays as published.
        return {
            'name': name,
            'project_id': project_id,
            'tonnes': tonnes,
            'year': year_of(cell(row, index, 'Retirement Date')),
            'located_as_holder': False,
        }

    def iso_loader(row, index):
        tonnes = tonnes_of(cell(row, index, 'Size Credits'))
        name = clean_name(cell(row, index, 'Beneficiary Name'))
        project = clean_name(cell(row, index, 'Project ID'))
        if project and not project.startswith('ISO'):
            project = 'ISO' + project
        if tonnes <= 0:
            return None
        if not name:
            return {'undisclosed': True, 'project_id': project, 'tonnes': tonnes, 'year': year_of(cell(row, index, 'Retired At'))}
        domain = clean_name(cell(row, index, 'Beneficiary Domain')).lower()
        return {
            'name': name,
            'project_id': project,
            'tonnes': tonnes,
            'year': year_of(cell(row, index, 'Retired At')),
            'domain': domain,
            'located_as_holder': False,
        }

    def verra_loader(row, index):
        tonnes = tonnes_of(cell(row, index, 'Retirement QTY'))
        name = clean_name(cell(row, index, 'Beneficial Owner'))
        project_id = registry_project_id(cell(row, index, 'Project ID'), 'VCS')
        year = year_of(cell(row, index, 'Retirement Date'))
        if tonnes <= 0:
            return None
        if not name:
            # Counted here and also stored on the project as undisclosed tonnes
            # so a project view can reconcile named + blank beneficiaries.
            stats['verra_blank_beneficiary_rows'] += 1
            stats['verra_blank_beneficiary_tonnes'] += tonnes
            return {'undisclosed': True, 'project_id': project_id, 'tonnes': tonnes, 'year': year}
        # Country on this sheet is the project host country, not the buyer.
        return {
            'name': name,
            'project_id': project_id,
            'tonnes': tonnes,
            'year': year,
            'located_as_holder': False,
        }

    consume('ACR Retirements', acr_like('ACR'))
    consume('ART Retirements', acr_like('ART'))
    consume('CAR Retirements', car_loader)
    consume('Gold Retirements', gold_loader)
    consume('Isometric Retirement Batches', iso_loader)
    consume('Verra VCS Retirements', verra_loader)

    snapshot_dir = CACHE / 'snapshots'
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    extra_sources_dir = Path(__file__).resolve().parent
    if str(extra_sources_dir) not in sys.path:
        sys.path.insert(0, str(extra_sources_dir))
    from extra_sources import fetch_csi, fetch_puro, normalise
    csi_payload = puro_payload = None
    try:
        csi_payload = fetch_csi(snapshot_dir)
    except Exception as error:  # noqa: BLE001
        log(f'CSI public fetch failed: {error}')
    try:
        puro_payload = fetch_puro(snapshot_dir)
    except Exception as error:  # noqa: BLE001
        log(f'Puro public fetch failed: {error}')
    extra = normalise(csi_payload, puro_payload)
    extra_skipped = extra.get('skipped') or []
    for record in extra.get('projects') or []:
        projects[record['id']] = {
            'id': record['id'],
            'name': record.get('name') or record['id'],
            'registry': record.get('registry') or '',
            'country': '',
            'cc': record.get('cc') or '',
            'type': record.get('type') or '',
            'group': record.get('group') or 'other',
            'state': '',
            'site': '',
            'developer': record.get('developer') or '',
            'owner': '',
            'operator': '',
            'issued': record.get('issued') or 0,
            'retiredRegistry': record.get('retired') or 0,
            'vintages': [],
            'undisclosed': 0,
            'undisclosed_years': set(),
            'lat': record.get('lat'),
            'lon': record.get('lon'),
            'unit': record.get('unit') or 'tCO2e',
            'removal': record.get('removal') or 'removal',
            'standard': record.get('standard') or '',
            'status': record.get('status') or '',
        }
    for row in extra.get('retirements') or []:
        if row.get('tonnes', 0) <= 0 or row.get('project_id') not in projects:
            continue
        if not row.get('buyer'):
            projects[row['project_id']]['undisclosed'] += row['tonnes']
            if row.get('year'):
                projects[row['project_id']]['undisclosed_years'].add(row['year'])
            continue
        entry = ensure_buyer(row['buyer'])
        if entry is None:
            continue
        add_flow(entry, row['project_id'], row['tonnes'], row.get('year'), '', '', '', '', False)
    log(f'extra projects {len(extra.get("projects") or [])} retirements {len(extra.get("retirements") or [])}')

    log(f'buyers {len(buyers)} stats {dict(stats)}')

    # Wikidata: buyers at or above the tonne floor, and every Pacific buyer
    # who has no registry city. The exact published name is queried first.
    # A suffix-stripped or title-cased retry runs only after that exact name
    # has already missed, and only for larger (or Pacific) buyers.
    cache = load_hq_cache()
    pending = []
    for entry in buyers.values():
        if entry['tonnes'] < WIKIDATA_MIN_TONNES and not entry['pacific']:
            continue
        # A registry city already places the account holder. Do not let a
        # same-named Wikidata entity move that dot.
        if entry['cities']:
            continue
        raw = entry['names'].most_common(1)[0][0]
        if len(raw) > 120 or '\n' in raw:
            continue
        labels = [(raw, 'high', '')]
        allow_alt = entry['pacific'] or entry['tonnes'] >= WIKIDATA_ALT_MIN_TONNES
        primary = cache.get(entry['key'])
        primary_missed = primary is not None and not primary.get('hits')
        if allow_alt and primary_missed:
            if raw.isupper() and len(raw) > 3:
                labels.append((raw.title(), 'medium', '|alt1'))
            stripped = strip_suffix_display(raw)
            if stripped and norm(stripped) != norm(raw) and len(stripped) >= 3:
                labels.append((stripped, 'medium', '|alt2'))
        for label, cap, suffix in labels:
            cache_key = entry['key'] + suffix
            if cache_key not in cache:
                pending.append((cache_key, label, cap, entry['tonnes']))
    seen_keys = set()
    unique_pending = []
    for item in pending:
        if item[0] in seen_keys:
            continue
        seen_keys.add(item[0])
        unique_pending.append(item)
    unique_pending.sort(key=lambda item: -item[3])
    query_wikidata([(key, label, cap) for key, label, cap, _tonnes in unique_pending[:4000]])
    cache = load_hq_cache()
    alt_pending = []
    for entry in buyers.values():
        allow_alt = entry['pacific'] or entry['tonnes'] >= WIKIDATA_ALT_MIN_TONNES
        if not allow_alt or entry['cities']:
            continue
        primary = cache.get(entry['key'])
        if primary is None or primary.get('hits'):
            continue
        raw = entry['names'].most_common(1)[0][0]
        alts = []
        if raw.isupper() and len(raw) > 3:
            alts.append((raw.title(), '|alt1'))
        stripped = strip_suffix_display(raw)
        if stripped and norm(stripped) != norm(raw) and len(stripped) >= 3:
            alts.append((stripped, '|alt2'))
        for label, suffix in alts:
            cache_key = entry['key'] + suffix
            if cache_key not in cache:
                alt_pending.append((cache_key, label, 'medium', entry['tonnes']))
    alt_pending.sort(key=lambda item: -item[3])
    if alt_pending:
        log(f'wikidata alternate labels {min(len(alt_pending), 1500)}')
        query_wikidata([(key, label, cap) for key, label, cap, _tonnes in alt_pending[:1500]])
        cache = load_hq_cache()

    placed = []
    unplaced = []
    for entry in buyers.values():
        display = entry.get('canonical') or entry['names'].most_common(1)[0][0]
        location = None
        if entry['cities']:
            (city, cc, state), _votes = entry['cities'].most_common(1)[0]
            hit = geocode_city(cities, city, cc, state)
            if hit and cc in countries:
                location = {
                    'lon': round(hit['lon'], 4),
                    'lat': round(hit['lat'], 4),
                    'cc': cc,
                    'hq': hit['name'],
                    'conf': hit['conf'],
                    'method': hit['method'],
                }
        if location is None:
            registry_cc = ''
            if entry['countries']:
                registry_cc = entry['countries'].most_common(1)[0][0]
                if registry_cc not in countries:
                    registry_cc = ''
            found = []
            for base in [entry['key'], *sorted(entry.get('lookup_keys') or [])]:
                for suffix in ('', '|alt1', '|alt2'):
                    hit = resolve_cached(cache, base + suffix, registry_cc)
                    if hit:
                        found.append((suffix, hit))
            if found:
                rank = {'high': 3, 'medium': 2, 'low': 1}
                suffix, hit = max(
                    found,
                    key=lambda item: (rank.get(item[1].get('conf'), 0), 1 if item[1].get('has_hq') else 0),
                )
                if suffix:
                    # A shortened label can hit the wrong organisation. The
                    # full canonical name is the tie-break, and only then.
                    searched = search_headquarters(display, cache)
                    if searched and searched.get('qid') and searched['qid'] != hit.get('qid'):
                        hit = searched
                location = {
                    'lon': hit['lon'],
                    'lat': hit['lat'],
                    'cc': hit['cc'] or registry_cc,
                    'hq': hit.get('hq') or '',
                    'conf': hit['conf'],
                    'method': hit['method'],
                    'qid': hit.get('qid') or '',
                }
        if location is None and entry['countries']:
            cc = entry['countries'].most_common(1)[0][0]
            country = countries.get(cc)
            if country and (entry['tonnes'] >= COUNTRY_ONLY_MIN_TONNES or entry['pacific']):
                location = {
                    'lon': country['lon'],
                    'lat': country['lat'],
                    'cc': cc,
                    'hq': '',
                    'conf': 'low',
                    'method': 'registry-country-centroid',
                }
        variants = [name for name, _count in entry['names'].most_common(8) if name != display]
        record = {
            'key': entry['key'],
            'name': display,
            'variants': variants,
            'tonnes': entry['tonnes'],
            'pacific': entry['pacific'],
            'domain': entry['domains'].most_common(1)[0][0] if entry['domains'] else '',
            'location': location,
            'flows': entry['flows'],
            'years': entry['years'],
        }
        (placed if location else unplaced).append(record)

    placed.sort(key=lambda item: (-item['tonnes'], item['name']))
    unplaced.sort(key=lambda item: (-item['tonnes'], item['name']))

    # Every registry project stays searchable, including ones whose retirements
    # name nobody. Buyer search covers every merged name, placed or not.
    project_records = []
    for project_id in sorted(projects):
        project = projects[project_id]
        project_records.append({
            'id': project['id'],
            'name': project['name'],
            'cc': project['cc'],
            'type': project['type'],
            'group': project['group'],
            'registry': project['registry'],
            'site': project['site'],
            'developer': project.get('developer') or '',
            'owner': project.get('owner') or '',
            'operator': project.get('operator') or '',
            'issued': project.get('issued') or 0,
            'retiredRegistry': project.get('retiredRegistry') or 0,
            'vintages': project.get('vintages') or [],
            'undisclosed': project.get('undisclosed') or 0,
            'undisclosedMask': year_mask(project.get('undisclosed_years') or set()),
            'lat': project.get('lat'),
            'lon': project.get('lon'),
            'unit': project.get('unit') or 'tCO2e',
            'removal': project.get('removal') or '',
            'standard': project.get('standard') or '',
        })

    buyer_records_src = placed + unplaced
    buyer_index = {entry['key']: i for i, entry in enumerate(buyer_records_src)}
    project_index = {record['id']: i for i, record in enumerate(project_records)}

    buyer_out = []
    for entry in buyer_records_src:
        loc = entry['location'] or {}
        buyer_out.append({
            'name': entry['name'],
            'variants': entry.get('variants') or [],
            'cc': loc.get('cc') or '',
            'lon': loc.get('lon'),
            'lat': loc.get('lat'),
            'hq': loc.get('hq') or '',
            'conf': loc.get('conf') or 'unplaced',
            'method': loc.get('method') or '',
            'qid': (entry['location'] or {}).get('qid') or '',
            'domain': entry['domain'],
            'pacific': entry['pacific'],
        })

    flows_out = []
    for entry in buyer_records_src:
        b_index = buyer_index[entry['key']]
        for project_id, flow in entry['flows'].items():
            p_index = project_index.get(project_id)
            if p_index is None:
                continue
            flows_out.append([
                b_index,
                p_index,
                flow['tonnes'],
                year_mask(flow['years']),
            ])

    conf_counts = Counter(buyer['conf'] for buyer in buyer_out)
    group_counts = Counter(project['group'] for project in project_records)
    pacific_project_countries = Counter(
        project['cc'] for project in project_records if project['cc'] in PACIFIC_ISO
    )
    unplaced_tonnes = sum(entry['tonnes'] for entry in unplaced)
    unplaced_pacific = [entry['name'] for entry in unplaced if entry['pacific']]

    meta = {
        'dataset': 'Berkeley Carbon Trading Project Voluntary Registry Offsets Database',
        'version': VROD_VERSION,
        'citation': CITATION,
        'sourceUrl': VROD_PAGE,
        'license': 'CC BY 4.0',
        'dataThrough': DATA_THROUGH,
        'retrieved': RETRIEVED,
        'files': {
            'projects': {'url': VROD_XLSX_URL, 'sha256': sha256(vrod_path)},
            'registryRetirements': {'url': REGISTRY_XLSX_URL, 'sha256': sha256(registry_path)},
        },
        'projectLocation': {
            'method': 'natural-earth-50m-label-point',
            'source': 'Natural Earth 50m admin-0 countries (public domain)',
            'note': 'VROD publishes a project country and site description, not coordinates, so those arcs end at the country label point. CSI and Puro coordinates are stored when the public record includes them, and a single-project view ends the arc at that site.',
        },
        'buyerLocation': {
            'cityGazetteer': 'GeoNames cities5000 (CC BY 4.0)',
            'hqLookup': 'Wikidata P159 headquarters, else P625 coordinate location',
            'cache': 'scripts/carbon/hq-cache.json',
            'countryOnlyMinTonnes': COUNTRY_ONLY_MIN_TONNES,
            'wikidataMinTonnes': WIKIDATA_MIN_TONNES,
        },
        'typeGroups': GROUP_LABEL,
        'pacificIso': list(PACIFIC_ISO),
        'pacificExcluded': ['AU', 'NZ'],
        'year0': YEAR0,
        'year1': YEAR1,
        'skippedSources': extra_skipped,
        'extraSources': {
            'csi': (csi_payload or {}).get('retrieved'),
            'puro': (puro_payload or {}).get('retrieved'),
        },
        'counts': {
            'buyersPlaced': len(placed),
            'buyersInFile': len(buyer_out),
            'buyersUnplaced': len(unplaced),
            'unplacedTonnes': unplaced_tonnes,
            'projects': len(project_records),
            'flows': len(flows_out),
            'confidence': dict(conf_counts),
            'groups': dict(group_counts),
            'pacificProjectCountries': dict(pacific_project_countries),
            'retirementRows': stats['retirement_rows'],
            'joinedRows': stats['joined_rows'],
            'joinedTonnes': stats['joined_tonnes'],
            'unjoinedProjectRows': stats['unjoined_project_rows'],
            'verraBlankBeneficiaryRows': stats['verra_blank_beneficiary_rows'],
            'verraBlankBeneficiaryTonnes': stats['verra_blank_beneficiary_tonnes'],
            'undisclosedRows': stats['undisclosed_rows'],
            'undisclosedTonnes': stats['undisclosed_tonnes'],
            'buyersWithAliases': sum(1 for entry in buyer_records_src if entry.get('variants')),
            'unmatchedProjectCountries': unmatched_countries.most_common(30),
        },
        'unplacedPacificBuyers': unplaced_pacific[:40],
        'excluded': [
            'Retirement rows with no beneficiary name are kept on the project as undisclosed-buyer tonnes. They are not assigned to a guessed company. Verra leaves Beneficial Owner blank on a large share of rows.',
            'Buyer names are merged by stripping on-behalf-of prefixes and legal suffixes, then applying buyer-aliases.json. Raw published strings stay on the buyer as variants.',
            'Gold Standard free-text retirement notes are not parsed. Only the Using Entity column is treated as the buyer.',
            'Cancellations, buffer-pool movements, and California / Quebec / Washington compliance retirement totals are not included. Those tables do not name a voluntary beneficiary in a way this build can attribute.',
            'Buyer names were grouped by a normalized form with legal suffixes removed. A shared name can still merge two different organizations; Wikidata matches with more than one equally good hit are left unplaced instead of picking one.',
        ],
    }

    # Drop empty qid keys to keep the file smaller? Keep them; they document
    # the Wikidata item. Empty strings are noisy — strip empties.
    for buyer in buyer_out:
        if not buyer['qid']:
            buyer.pop('qid')
        if not buyer['domain']:
            buyer.pop('domain')
        if not buyer['hq']:
            buyer.pop('hq')
        if not buyer['variants']:
            buyer.pop('variants')
        if buyer['lon'] is None:
            buyer.pop('lon')
            buyer.pop('lat')
    for project in project_records:
        if not project['developer']:
            project.pop('developer')
        if not project['owner']:
            project.pop('owner')
        if not project['operator']:
            project.pop('operator')
        if not project['site']:
            project.pop('site')
        if not project['vintages']:
            project.pop('vintages')
        if not project['undisclosed']:
            project.pop('undisclosed')
            project.pop('undisclosedMask')
        if not project['issued']:
            project.pop('issued')
        if not project['retiredRegistry']:
            project.pop('retiredRegistry')
        if project.get('lat') is None or project.get('lon') is None:
            project.pop('lat', None)
            project.pop('lon', None)
        if not project.get('removal'):
            project.pop('removal', None)
        if not project.get('standard'):
            project.pop('standard', None)
        if project.get('unit') in (None, '', 'tCO2e'):
            project.pop('unit', None)

    payload = {
        'meta': meta,
        'countries': {iso: {'name': rec['name'], 'lon': rec['lon'], 'lat': rec['lat']} for iso, rec in sorted(countries.items())},
        'projects': project_records,
        'buyers': buyer_out,
        'flows': flows_out,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix('.json.part')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(OUT_PATH)
    log(f'wrote {OUT_PATH} ({OUT_PATH.stat().st_size / 1e6:.1f} MB)')
    log(json.dumps(meta['counts'], indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
