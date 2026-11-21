"""Public registry snapshots beyond Berkeley VROD.

Only endpoints a logged-out visitor already loads. No authenticated registry
APIs, no password-protected tools, no paywalled ratings.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = 'CORA-gods-eye-view-carbon-flows/1.0 (public carbon-credit map; research)'
SNAPSHOT_DIR_NAME = 'snapshots'

CSI_BASE = 'https://global-c-sink.easy-cert.cloud/v1/'
PURO_PROJECTS = 'https://registry.puro.earth/projects'
PURO_RETIREMENTS = 'https://registry.puro.earth/retirements'

# Puro methodology codes published on the public project records.
PURO_GROUPS = {
    'C03000000': ('biochar', 'Biochar'),
    'C01000000': ('construction', 'Wooden building elements'),
    'C04000000': ('soil', 'Soil amendment'),
    'C05000000': ('dac', 'Geologically stored carbon'),
    'C05202402': ('dac', 'Geologically stored carbon'),
    'C05202404': ('dac', 'Geologically stored carbon'),
    'C06000000': ('biomass', 'Terrestrial storage of biomass'),
    'C07000000': ('erw', 'Enhanced weathering'),
    'C09000000': ('mineral', 'Carbonated materials'),
}


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, timeout: int = 90, attempts: int = 4) -> bytes:
    last_error = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json,text/html'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise
            time.sleep(1.5 * (attempt + 1))
        except urllib.error.URLError as error:
            last_error = error
            if attempt + 1 == attempts:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last_error


def fetch_json(url: str):
    return json.loads(fetch(url).decode('utf-8'))


def tonnes_of(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, str):
        text = value.replace(',', '').strip()
        if not text:
            return 0
        try:
            value = float(text)
        except ValueError:
            return 0
    if isinstance(value, (int, float)) and value == value and value > 0:
        return int(round(float(value)))
    return 0


def year_of(value) -> int | None:
    if value is None:
        return None
    match = re.search(r'(19|20)\d{2}', str(value))
    if not match:
        return None
    year = int(match.group(0))
    return year if 1990 <= year <= 2035 else None


def csi_group(standard: str, product: str):
    text = f'{standard} {product}'.lower()
    if 'biochar' in text or 'artisan' in text:
        return 'biochar', standard or 'Biochar'
    if 'tree' in text:
        return 'tree', 'Tree'
    if 'construction' in text:
        return 'construction', 'Construction'
    label = standard or product or 'C-Sink'
    return 'other', label


def paginate(url: str, page_size: int = 100):
    """Follow offset pages. A 500 from one bad row is skipped and the page size shrinks."""
    seen = set()
    offset = 0
    total = None
    size = page_size
    joiner = '&' if '?' in url else '?'
    while total is None or offset < int(total):
        page_url = f'{url}{joiner}limit={size}&offset={offset}'
        try:
            page = json.loads(fetch(page_url, attempts=2).decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code < 500 or size == 1:
                if error.code >= 500 and size == 1:
                    log(f'  skip batch offset {offset} after HTTP {error.code}')
                    offset += 1
                    size = page_size
                    continue
                raise
            size = max(1, size // 5)
            log(f'  HTTP {error.code} at offset {offset}; page size {size}')
            continue
        data = page.get('data') or {}
        rows = data.get('results') or []
        if data.get('count') is not None:
            total = int(data['count'])
        if not rows:
            break
        for row in rows:
            key = row.get('id')
            if key in seen:
                continue
            seen.add(key)
            yield row
        offset += len(rows)
        log(f'  {url.split("/v1/")[-1][:48]} {len(seen)}' + (f'/{total}' if total else ''))
        if size < page_size and offset % 20 == 0:
            size = page_size
        time.sleep(0.2)


def fetch_csi(snapshot_dir: Path) -> dict:
    """Public C-Sink registry endpoints the logged-out site calls."""
    log('CSI public projects and C-sink batches')
    projects = list(paginate(CSI_BASE + 'projects/public-projects/'))
    batches = list(paginate(CSI_BASE + 'products/batch-list/'))
    summary = fetch_json(CSI_BASE + 'transactions/csink-summary/?icroa_compliant=false')
    retrieved = datetime.now(timezone.utc).date().isoformat()
    payload = {
        'source': 'Carbon Standards International Global C-Sink Registry',
        'registry': 'CSI',
        'url': 'https://global-c-registry.org/',
        'api': CSI_BASE,
        'retrieved': retrieved,
        'licenceNote': (
            'Public registry pages and the unauthenticated GET endpoints they call '
            '(projects/public-projects and products/batch-list). The password-protected '
            'C-Sink Tool and its bearer-token import API were not used.'
        ),
        'summary': (summary.get('data') or summary),
        'projects': projects,
        'batches': batches,
    }
    path = snapshot_dir / f'csi-{retrieved}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    latest = snapshot_dir / 'csi-latest.json'
    latest.write_text(json.dumps({
        'retrieved': retrieved,
        'path': path.name,
        'projects': len(projects),
        'batches': len(batches),
    }, indent=2), encoding='utf-8')
    log(f'  CSI snapshot {path.name}: {len(projects)} projects, {len(batches)} batches')
    return payload


def next_flight_data(html: str):
    """Largest JSON data array embedded in a public Next.js flight payload."""
    arrays = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, flags=re.S):
        try:
            pushed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not (isinstance(pushed, list) and len(pushed) >= 2 and isinstance(pushed[1], str)):
            continue
        text = pushed[1]
        start = 0
        while True:
            index = text.find('{"data":[', start)
            if index < 0:
                break
            try:
                obj, _end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                start = index + 8
                continue
            data = obj.get('data') if isinstance(obj, dict) else None
            if isinstance(data, list) and data and isinstance(data[0], dict):
                arrays.append(data)
            start = index + 8
    if not arrays:
        return []
    return max(arrays, key=len)


def fetch_puro(snapshot_dir: Path) -> dict:
    """Project and retirement tables embedded in the public registry HTML."""
    log('Puro public registry pages')
    projects_html = fetch(PURO_PROJECTS, timeout=120).decode('utf-8', errors='replace')
    time.sleep(1)
    retire_html = fetch(PURO_RETIREMENTS, timeout=180).decode('utf-8', errors='replace')
    projects = next_flight_data(projects_html)
    retirements = next_flight_data(retire_html)
    projects = [row for row in projects if isinstance(row, dict) and row.get('projectId')]
    retirements = [row for row in retirements if isinstance(row, dict) and row.get('type') == 'Retirement']
    retrieved = datetime.now(timezone.utc).date().isoformat()
    payload = {
        'source': 'Puro.earth Registry',
        'registry': 'PURO',
        'url': 'https://registry.puro.earth/',
        'retrieved': retrieved,
        'licenceNote': (
            'Records embedded in the public /projects and /retirements pages served to '
            'logged-out visitors. The role-based Registry API was not called.'
        ),
        'projects': projects,
        'retirements': retirements,
    }
    path = snapshot_dir / f'puro-{retrieved}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    (snapshot_dir / 'puro-latest.json').write_text(json.dumps({
        'retrieved': retrieved,
        'path': path.name,
        'projects': len(projects),
        'retirements': len(retirements),
    }, indent=2), encoding='utf-8')
    log(f'  Puro snapshot {path.name}: {len(projects)} projects, {len(retirements)} retirements')
    return payload


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Fetch public CSI and Puro snapshots')
    parser.add_argument('--only', choices=['csi', 'puro', 'all'], default='all')
    args = parser.parse_args()
    snapshot_dir = Path(__file__).resolve().parents[2] / '.gev-cache' / 'carbon' / 'snapshots'
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if args.only in ('csi', 'all'):
        fetch_csi(snapshot_dir)
    if args.only in ('puro', 'all'):
        fetch_puro(snapshot_dir)
    return 0


def skipped_sources() -> list[dict]:
    return [
        {
            'name': 'Isometric Registry',
            'reason': (
                'https://api.isometric.com/registry/v0/projects returns 401 without an '
                'X-Client-Secret, and the public projects page does not embed the ledger. '
                'Isometric retirements already present in Berkeley VROD (registry ISO) stay '
                'in the dataset. No client secret was requested or used.'
            ),
        },
        {
            'name': 'ART TREES',
            'reason': 'Already inside Berkeley VROD as registry ART, including TREES retirements that name an account holder.',
        },
        {
            'name': 'Plan Vivo',
            'reason': 'No unauthenticated project-and-retirement bulk feed comparable to the public CSI or Puro pages was found. The Markit registry is a login-gated viewer.',
        },
        {
            'name': 'Cercarbono',
            'reason': 'The public EcoRegistry project search is an interactive viewer without a documented unauthenticated retirement export. Not ingested.',
        },
        {
            'name': 'Riverse',
            'reason': 'The public project catalogue does not publish a retirement ledger with beneficiaries.',
        },
        {
            'name': 'Social Carbon',
            'reason': 'No public bulk retirement feed with beneficiary names was found. SOCIALCARBON is a co-benefit standard often issued through Verra, which is already in VROD.',
        },
        {
            'name': 'BeZero',
            'reason': (
                'Headline ratings are described as free, but the public listings page marks its '
                'table as illustrative fictitious samples. The ratings API requires OAuth. No ratings were copied.'
            ),
        },
        {
            'name': 'Sylvera',
            'reason': 'Ratings are a paid catalogue. No free public ratings list was ingested.',
        },
        {
            'name': 'Calyx Global',
            'reason': 'Ratings sit behind the Calyx platform. No free public ratings list was ingested.',
        },
        {
            'name': 'MSCI Carbon Markets',
            'reason': 'No free public project-ratings feed. Not ingested.',
        },
    ]


def normalise(csi: dict | None, puro: dict | None) -> dict:
    """Shape both sources into projects and retirement rows for the VROD merge."""
    projects = []
    retirements = []
    if csi:
        by_id = {}
        for row in csi.get('projects') or []:
            display = str(row.get('project_id_for_display') or f"GCSP{row.get('id')}")
            group, method = csi_group(str(row.get('standard') or ''), str(row.get('name') or ''))
            try:
                lat = float(row.get('latitude')) if row.get('latitude') not in (None, '') else None
                lon = float(row.get('longitude')) if row.get('longitude') not in (None, '') else None
            except (TypeError, ValueError):
                lat, lon = None, None
            record = {
                'id': f'CSI:{display}',
                'name': row.get('name') or display,
                'cc': (row.get('country_code') or '')[:2].upper(),
                'type': method,
                'group': group,
                'registry': 'CSI',
                'developer': row.get('company_name') or '',
                'lat': lat,
                'lon': lon,
                'unit': 'c-sink',
                'removal': 'removal',
                'standard': row.get('standard') or '',
                'status': row.get('status_text') or '',
            }
            by_id[row.get('id')] = record
            by_id[display] = record
            projects.append(record)
        issued = {}
        retired = {}
        for batch in csi.get('batches') or []:
            details = batch.get('project_details') or {}
            project = by_id.get(details.get('id')) or by_id.get(details.get('project_id_for_display'))
            if project is None:
                continue
            qty = tonnes_of(batch.get('c_sink_quantity') or batch.get('initial_quantity'))
            current = tonnes_of(batch.get('current_quantity'))
            year = year_of(batch.get('sink_validated_date') or batch.get('sink_date'))
            beneficiaries = [name for name in (batch.get('beneficiary') or []) if str(name).strip()]
            moved = max(0, qty - current)
            if beneficiaries and current == 0 and qty > 0:
                share = qty // len(beneficiaries)
                remainder = qty - share * len(beneficiaries)
                for index, name in enumerate(beneficiaries):
                    retirements.append({
                        'project_id': project['id'],
                        'buyer': name,
                        'tonnes': share + (remainder if index == 0 else 0),
                        'year': year,
                    })
                retired[project['id']] = retired.get(project['id'], 0) + qty
            elif moved > 0:
                retirements.append({
                    'project_id': project['id'],
                    'buyer': '',
                    'tonnes': moved,
                    'year': year,
                })
                retired[project['id']] = retired.get(project['id'], 0) + moved
            issued[project['id']] = issued.get(project['id'], 0) + qty
        for project in projects:
            if project.get('registry') != 'CSI':
                continue
            project['issued'] = issued.get(project['id'], 0)
            project['retired'] = retired.get(project['id'], 0)
    if puro:
        by_code = {}
        for row in puro.get('projects') or []:
            code = str(row.get('code') or row.get('projectId') or '')
            if not code:
                continue
            method = (row.get('methodology') or {}).get('name') or ''
            group, label = PURO_GROUPS.get(str(row.get('methodologyCode') or ''), ('other', method or 'Puro removal'))
            if 'biochar' in method.lower():
                group, label = 'biochar', 'Biochar'
            elif 'weather' in method.lower():
                group, label = 'erw', 'Enhanced weathering'
            elif 'geolog' in method.lower() or 'direct air' in method.lower():
                group, label = 'dac', method or 'Geologically stored carbon'
            try:
                lat = float(row['latitude']) if row.get('latitude') not in (None, '') else None
                lon = float(row['longitude']) if row.get('longitude') not in (None, '') else None
            except (TypeError, ValueError, KeyError):
                lat, lon = None, None
            record = {
                'id': f'PURO:{code}',
                'name': row.get('name') or code,
                'cc': (row.get('countryCode') or '')[:2].upper(),
                'type': label,
                'group': group,
                'registry': 'PURO',
                'developer': row.get('supplierName') or '',
                'lat': lat,
                'lon': lon,
                'unit': 'corc',
                'removal': 'removal',
                'standard': method,
                'issued': 0,
                'retired': 0,
            }
            by_code[code] = record
            projects.append(record)
        for row in puro.get('retirements') or []:
            details = row.get('retirementDetails') or {}
            buyer = details.get('beneficiaryName') or ''
            bundles = row.get('bundles') or []
            if not bundles:
                continue
            # Split the retirement volume across bundles by their own volume.
            for bundle in bundles:
                code = str(bundle.get('productionFacilityCode') or '')
                project = by_code.get(code)
                if project is None:
                    continue
                qty = tonnes_of(bundle.get('volume'))
                if qty <= 0:
                    continue
                year = bundle.get('vintage') if isinstance(bundle.get('vintage'), int) else year_of(row.get('completedOn'))
                project['retired'] += qty
                retirements.append({
                    'project_id': project['id'],
                    'buyer': buyer,
                    'tonnes': qty,
                    'year': year if isinstance(year, int) else year_of(row.get('completedOn')),
                })
    return {'projects': projects, 'retirements': retirements, 'skipped': skipped_sources()}


if __name__ == '__main__':
    raise SystemExit(main())
