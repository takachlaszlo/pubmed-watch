# PubMed-figyelő

Naponta átnézi a PubMedet és a ClinicalTrials.gov-ot öt szakmai témában. Az új tételeket
egy kanonikus SQLite-adatbázisba menti, letisztult HTML-összesítőt küld róluk e-mailben,
és egy csak olvasható JSON API-n átadja őket az n8n-nek (a későbbi szelekcióhoz és PDF-letöltéshez).
AI nincs benne: a szűrés átlátható PubMed-lekérdezésekkel történik.

## Témák

| # | Téma | Szűrés röviden |
|---|------|----------------|
| 1 | Gyermek-AMS | stewardship, antibiotikum-felírás/-expozíció × gyermek |
| 2 | Innováció a gyermekellátásban | digitális egészség, AI, telemedicina, új (gyors, molekuláris) diagnosztika × gyermek (címben) |
| 3 | Gyermekinfektológia | fertőzés × gyermek (címben), kb. 20 vezető folyóiratban, vagy RCT bárhol |
| 4 | Gyermekgyógyászati irányelvek és review-k | irányelv/konszenzus + szisztematikus review/metaanalízis × gyermek (címben) |
| 5 | Infektológiai irányelvek és review-k | irányelv/konszenzus + szisztematikus review/metaanalízis × fertőzés (címben / ID-folyóirat) |
| + | Folyamatban lévő vizsgálatok | új, nem lezárt ClinicalTrials.gov-regisztrációk (≤21 év, ID/AMS) és PubMed-vizsgálati protokollok |

Minden témából kiesnek a hozzászólások, levelek, editorialok, hírek, erratumok, esetismertetések
és a csak állatkísérletes cikkek. A lekérdezések a [`config/config.yaml`](config/config.yaml)-ban vannak,
közös építőkövekből (`blocks`). Egy szűrőt elég egy helyen javítani.

## Napi levél

- Az elején összesítő tábla, utána a szekciók: **irányelvek** (a „frissített” cím külön jelölve),
  **gyermek-AMS**, **gyermekinfektológia**, **folyamatban lévő vizsgálatok** (adatlap abstract-részlettel),
  majd a **szisztematikus review-k** és az **innováció** tömör listában.
- Minden tételnél linkek: PubMed · DOI · Teljes szöveg · **PDF ↓** (ha van szabad hozzáférésű változat).
- Egy cikk egyszer szerepel. Ha több témába is esik, a config sorrendje szerinti első téma szekciójában
  jelenik meg, a többi téma az adatbázisban rögzül.
- Ha aznap nincs új tétel, nem megy levél (`send_empty: false`).
- Az első futás az elmúlt 7 napot tölti be alapállapotnak, erről csak egy összegző levél megy.

## Adatbázis és API (n8n)

Az adatbázis a NAS-on van: `/volume1/docker/pubmed-watch/data/pubmed.db`. Az API ugyanebből olvas,
a `http://192.168.1.168:8765` címen:

| Végpont | Tartalom |
|---------|----------|
| `GET /health` | állapot, darabszámok, utolsó sikeres futás |
| `GET /articles` | cikkek; szűrők: `run_id`, `since`, `updated_since` (ISO dátum), `topic`, `section`, `kind`, `has_pdf=true/false`, `limit` (max. 1000), `offset` |
| `GET /articles/{pmid}` | egy cikk |
| `GET /trials` | vizsgálatok; szűrők: `run_id`, `since`, `status`, `limit`, `offset` |
| `GET /trials/{nct_id}` | egy vizsgálat |
| `GET /runs`, `GET /runs/latest` | futások története |
| `GET /topics` | témák (kifejtett lekérdezéssel) és szekciók |
| `GET /reports/latest` | az utolsó levél HTML-ben |

Egy cikk rekordja (rövidítve):

```json
{
  "pmid": "42814651", "title": "...", "journal": "...", "journal_abbrev": "JMIR Res Protoc",
  "pub_year": "2026", "entrez_date": "2026-09-30", "doi": "10.2196/...", "pmcid": "PMC13626073",
  "kind": "protocol", "is_update": false, "section": "vizsgalatok", "topics": ["protokoll"],
  "abstract": [{"label": "BACKGROUND", "text": "..."}], "authors": ["..."], "mesh": [], "pub_types": ["..."],
  "oa": true, "pdf_source": "europepmc",
  "links": {"pubmed": "https://pubmed.ncbi.nlm.nih.gov/42814651/", "doi": "https://doi.org/...",
            "fulltext": "https://europepmc.org/articles/PMC13626073",
            "pdf": "https://europepmc.org/articles/PMC13626073?pdf=render"},
  "first_seen_at": "2026-10-04T06:30:12+02:00", "first_seen_run": 12, "updated_at": "..."
}
```

`kind`: `guideline` | `systematic_review` | `protocol` | `rct` | `review` | `other`.

**Szabad PDF-linkek.** A linkek a Europe PMC-ből jönnek, opcionálisan az Unpaywallból is (`UNPAYWALL_EMAIL`).
A PMC-másolat gyakran csak napokkal a megjelenés után készül el, ezért a napi futás 30 napig újra
ellenőrzi a PDF nélküli cikkeket. Ha talál linket, az `updated_at` mező frissül, így az n8n az
`/articles?updated_since=...&has_pdf=true` lekérdezéssel megkapja az utólag elérhetővé vált PDF-eket.
Fizetős cikkekhez csak PubMed/DOI link van.

**PDF-letöltés az n8n-ben.** Állíts be egyedi `User-Agent` fejlécet (pl. `pubmed-watch/1.0`),
mert a Europe PMC az alapértelmezett kliens-azonosítót elutasíthatja (403).

**n8n-minták:**
- *Napi feldolgozás:* Schedule Trigger (07:00) → HTTP Request `GET http://192.168.1.168:8765/runs/latest`
  → HTTP Request `GET .../articles?run_id={{$json.id}}&limit=1000`.
- *Push:* a compose-ban `N8N_WEBHOOK_URL` = egy n8n Webhook node URL-je. Minden futás után POST érkezik
  `{event, run_id, window, counts, articles[], trials[]}` tartalommal.

## Telepítés a NAS-ra

1. `\\Becalel\docker\pubmed-watch\` mappa, benne a [`compose.yaml`](compose.yaml) és egy üres `data` mappa.
2. Container Manager → Projekt → Létrehozás → név: `pubmed-watch`, útvonal: `/volume1/docker/pubmed-watch`.
3. A compose-ban írd be az `SMTP_PASSWORD` értékét. Opcionálisan: `API_TOKEN`, `N8N_WEBHOOK_URL`,
   `NCBI_API_KEY`, `UNPAYWALL_EMAIL`.
4. Indítás. Az első próbához átmenetileg `RUN_ON_START: "true"`.

A program induláskor a GitHubról tölti le magát. Kód- vagy config-változás után: push a `main` ágra,
majd a projekt újraindítása. Napló: `data/pubmedwatch.log`, utolsó levél: `data/last_report.html`.

## Hangolás és fejlesztés

```bash
pip install -r requirements.txt pytest
python -m pytest                                   # offline tesztek, valódi PubMed/CT.gov válaszokból
PUBMEDWATCH_DATA=./data python -m pubmedwatch counts --days 30   # témánkénti napi átlag
PUBMEDWATCH_DATA=./data python -m pubmedwatch run --no-mail      # egy futás levél nélkül
PUBMEDWATCH_DATA=./data python -m pubmedwatch serve              # csak az API
```

Források: NCBI E-utilities, Europe PMC REST API, ClinicalTrials.gov API v2, opcionálisan Unpaywall.
A program őszinte User-Agenttel, a szolgáltatók sebességkorlátait betartva kérdez le.
