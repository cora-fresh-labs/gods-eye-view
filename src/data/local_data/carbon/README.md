# Follow the carbon money

Animated arcs from the organizations that retire carbon credits to the countries of the projects those credits came from. This is a layer on God's Eye View for Clint Borle / CORA Global Projects. The application's MIT license is unchanged and covers the code only. The dataset is not MIT.

## What you are looking at

Each arc starts at a buyer's headquarters (or, when that is all the registries published, the buyer's country) and ends at the **country** of a project whose credits that buyer retired. One arc is one buyer–country pair. Tonnes are retired credits, and one credit is one metric tonne of CO₂-equivalent, as Berkeley records it.

Search **Buyer** (`Shell`, or `show me all Shell credits`) to list every project that buyer retired and draw arcs from its headquarters. Search **Project** (`April Salumei`, `VCS1122`, or `who bought from April Salumei`) for the seller, registry totals, vintages, every named buyer, and the undisclosed tonnes. The buyer panel lists the raw registry strings merged into that name.

The app already has a voice agent (`GEV_REALTIME_TOOLS` in `vite.config.js`, handled in `src/voice/gevActions.js`). Those same phrases call `lookup_carbon`, which uses `resolveCarbonQuery`. No second voice stack was added.

The caption follows Bernat Fortet's "Follow the carbon money" map: `Switzerland: 10 buyers → 18 countries`. With no country selected, the globe cycles the largest buyer countries. **Pacific projects** limits the map to credits from Pacific island host countries.

![Globe with carbon-credit arcs](../../../../docs/media/carbon/globe-arcs.png)

![Switzerland: 10 buyers to 18 countries](../../../../docs/media/carbon/switzerland.png)

![Pacific projects: who buys island credits](../../../../docs/media/carbon/pacific.png)

![Nespresso's projects after clicking the buyer](../../../../docs/media/carbon/switzerland-nespresso.png)

![Shell buyer view](../../../../docs/media/carbon/shell.png)

![April Salumei buyers and seller](../../../../docs/media/carbon/april-salumei.png)

![Merged Qantas entry](../../../../docs/media/carbon/qantas.png)

## Setup

The layer needs no API key. Google 3D Tiles (`GOOGLE_MAPS_API_KEY`) and Cesium ion (`CESIUM_ION_TOKEN`) stay optional, as in the root [`.env.example`](../../../../.env.example). With neither key the globe uses keyless Esri imagery and the arcs still draw.

```bash
npm ci
npm run dev
```

Open the app, then turn on **Carbon Flows** in the data layers panel. Voice phrases that enable it: "carbon", "carbon credits", "follow the carbon money".

The shipped file is `carbon-flows.json` next to this README. You do not need to rebuild it to run the app.

## Data refresh

Source, both CC BY 4.0, data through 31 August 2026, version **v2026-08**:

- Projects workbook: <https://gspp.berkeley.edu/assets/uploads/page/Voluntary-Registry-Offsets-Database--v2026-08.xlsx>
- Registry retirement files: <https://gspp.berkeley.edu/assets/uploads/page/VROD-registry-files--2026-08.xlsx>
- Dataset page and citation: <https://gspp.berkeley.edu/berkeley-carbon-trading-project/offsets-database>

```bash
python3 -m pip install -r scripts/carbon/requirements.txt
npm run carbon:build
```

The script caches the workbooks, Natural Earth 50m admin-0 countries, and GeoNames cities5000 under `.gev-cache/carbon/` (gitignored). Wikidata headquarters lookups are cached in `scripts/carbon/hq-cache.json` (committed) so a later run does not invent a new coordinate for a name already resolved, and does not retry a recorded miss into a different guess. Delete a cache key only when you intend to look that name up again.

Cite the database if you redistribute the JSON:

> Pamela Quartson, Barbara K Haya, Tyler Bernard, Aline Abayo, Xinyun Rong, Ivy S So, Micah Elias. (2026). Voluntary Registry Offsets Database v2026-08, Berkeley Carbon Trading Project, University of California, Berkeley. Retrieved from https://gspp.berkeley.edu/berkeley-carbon-trading-project/offsets-database

Also attribute GeoNames (CC BY 4.0) for buyer cities, Wikidata for headquarters, and Natural Earth (public domain) for country label points. The in-app attribution popover carries the same credit.

This build (retrieved 2026-11-21, retirements through 2026-08-31): 499,789 retirement rows, 208,415 joined to a named buyer, 246,349 rows with a blank beneficiary kept as undisclosed tonnes (about 586 million). 50,254 merged buyers, 546 placed on the globe, 11,856 projects, 76,968 buyer–project flows, 3,031 buyers that absorbed at least one alternate published name. Verra blank beneficial-owner rows: 137,420 (about 463 million tonnes), included in the undisclosed line rather than assigned to a company. Pacific host countries in the project table include Papua New Guinea (21), Fiji (2), Solomon Islands (1), New Caledonia (1), and Guam (1). The same figures are in `carbon-flows.json` → `meta.counts`.

## Buyer names

`scripts/carbon/build_carbon_flows.py` merges beneficiaries before geocoding:

1. Take the text after the last “on behalf of”, when that phrase is present.
2. Strip leading `retired on behalf of`, `on behalf of`, `retired by`, `retired for`, and `for`.
3. Drop legal suffixes (`Ltd`, `Limited`, `Pty`, `Inc`, `SA`, `AG`, `plc`, `SL`, `GmbH`, `LLC`, `Corp`, and the same list the script already used).
4. Fold case and punctuation.
5. Apply `buyer-aliases.json`. That file is the only hand-written name list. Keys are published names; values are the canonical buyer. The build normalises both sides, so adding `Qantas Airways Limited` → `Qantas` also catches `Qantas Airways Ltd`.

The panel keeps the published strings under “Also recorded as”. Programmes that are not the company (`Qantas Duty Travel`, `Qantas Fly Carbon Neutral Programme`) are not in the alias map and stay separate.

## How a buyer is located

Confidence is stored on each buyer as `conf` and drawn on the globe (amber dots and arcs are low confidence).

| `conf` | `method` | Meaning |
|---|---|---|
| `high` | `geonames-city` | The registry named the account holder's city and country, and that city is unique in GeoNames cities5000 (or the state code picked one city). **v2026-08 left the ACR/ART city and country columns empty, so this build has no GeoNames matches.** |
| `medium` | `geonames-city` | Several cities shared the name. The most populous candidate was used. |
| `high` | `wikidata-hq` | One Wikidata item matched the published name and has a headquarters coordinate (P159). This is how every placed buyer in the shipped file was located. |
| `medium` | `wikidata-coord` | A coordinate that is not a headquarters statement. |
| `medium` | `wikidata-search` | The shortened name matched a different Wikidata item, and a search for the full published name returned another organization. The search result is used. |
| `low` | `registry-country-centroid` | The registry named a country and no city or Wikidata headquarters was accepted. Drawn for buyers of at least 5,000 tonnes, and for any buyer of Pacific credits. The dot is the country label point. |
| `unplaced` | (none) | A name was published, but neither a city nor a single Wikidata hit nor a country was available. No arc. Pacific buyers in this state are still listed in the panel. |

A registry city is kept even when Wikidata has a different item with the same name. Several Wikidata hits with no country to separate them are a miss, not a guess.

## Known gaps

These are omissions, not fills.

- **Blank beneficiaries stay undisclosed.** They are summed on the project as “Undisclosed buyer” and are not given a company. Verra’s blank Beneficial Owner rows are `meta.counts.verraBlankBeneficiaryRows`. A project’s named tonnes plus undisclosed tonnes are the retirement rows this build read. The registry’s own issued and retired totals are shown beside that and can be higher, because cancellations and California / Quebec / Washington compliance retirements are not in these retirement sheets.
- **Gold Standard notes are not parsed.** Only the `Using Entity` column is the buyer. A note that names a different company stays unused.
- **Cancellations, buffer-pool moves, and California / Quebec / Washington compliance totals are excluded.** Those tables do not name a voluntary beneficiary this build can attribute.
- **No project coordinates.** VROD has a country and a site description. Arcs end at the Natural Earth country label point. A site string is shown on the project card and is not geocoded. Same-country flows draw a short loop at that label point so the retirement stays visible.
- **Buyer names are grouped** by a normalized form with legal suffixes (`Inc`, `Ltd`, `GmbH`, …) removed. Two different organizations that share that form can be merged. Account numbers and names with fewer than three letters are dropped.
- **Wikidata is not exhaustive.** Buyers under 5,000 tonnes are not looked up unless they retired Pacific credits or the registry already gave a city. A headquarters coordinate can be a campus or a loosely labeled place; it is still the published Wikidata statement, flagged by the method above.
- **Pacific preset** is Pacific Islands Forum island members plus American Samoa, Guam, the Northern Mariana Islands, Tokelau, and Wallis and Futuna. Australia and New Zealand are excluded on purpose: the preset is "who buys credits from Pacific island projects". Countries in that list with no project in v2026-08 simply do not appear. The host countries that do appear are `meta.counts.pacificProjectCountries`.
- **Nature-based types** follow VROD's harmonized `Type` column: REDD+ (including jurisdictional REDD+ and avoided forest conversion), ARR, IFM, wetland restoration (VROD does not split mangroves out of this type), soil (sustainable agriculture, grassland, rice, nitrogen, irrigation, compost), and biochar (including biomass carbon removal and storage). Everything else is `other`, including cookstoves and renewable energy.
- **Not the 296-company map.** Fortet's LinkedIn post is the visual model. This layer does not reproduce that headcount. The buyer count is whatever the retirement tables name.

## Files

| Path | Role |
|---|---|
| `carbon-flows.json` | Derived dataset the globe loads |
| `scripts/carbon/build_carbon_flows.py` | Download and rebuild |
| `scripts/carbon/hq-cache.json` | Wikidata lookup cache |
| `scripts/carbon/requirements.txt` | Python build dependency (`python-calamine`) |
| `src/data/carbonFlows.js` | Cesium layer |
| `src/data/carbonFlowsModel.js` | Filters, captions, click-through records |
