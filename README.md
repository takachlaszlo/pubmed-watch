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
- Az első futás az elmúlt 7 napot tölti be az adatbázisba. A levél ilyenkor az utolsó 2 nap tételeit mutatja
  teljes tartalommal (`baseline_digest_days`), a többi az adatbázisban van.
- Egyszeri újraküldés: `DIGEST_ON_START_DAYS: "2"` a compose-ban és a projekt újraindítása. Az adatbázisból az utolsó 2
  nap összesítőjét küldi el (minden értéknél csak egyszer). Kézzel: `python -m pubmedwatch digest --days 2 --send`.

## Adatbázis és API (n8n)

Az adatbázis a NAS-on van: `/volume1/docker/pubmed-watch/data/pubmed.db`. Az API ugyanebből olvas,
a `http://192.168.1.168:8765` címen:

| Végpont | Tartalom |
|---------|----------|
| `GET /health` | állapot, darabszámok, utolsó sikeres futás |
| `GET /articles` | cikkek; szűrők: `run_id`, `since`, `updated_since` (ISO dátum), `topic`, `section`, `kind`, `has_pdf=true/false`, `pdf_source`, `limit` (max. 1000), `offset` |
| `GET /articles/{pmid}` | egy cikk |
| `GET /trials` | vizsgálatok; szűrők: `run_id`, `since`, `status`, `limit`, `offset` |
| `GET /trials/{nct_id}` | egy vizsgálat |
| `GET /runs`, `GET /runs/latest` | futások története |
| `GET /topics` | témák (kifejtett lekérdezéssel) és szekciók |
| `GET /reports/latest` | az utolsó levél HTML-ben |
| `GET /downloads/due` | amit az n8n most letölthet (lásd lent); szűrő: `hours` (alapból 24) |
| `GET /downloads` | a letöltési nyilvántartás; szűrő: `status=ok/failed` |
| `POST /downloads/report` | az n8n jelenti egy letöltés eredményét (az API egyetlen írási művelete) |

Egy cikk rekordja (rövidítve):

```json
{
  "pmid": "42814651", "title": "...", "journal": "...", "journal_abbrev": "JMIR Res Protoc",
  "pub_year": "2026", "volume": "15", "issue": "", "entrez_date": "2026-09-30", "doi": "10.2196/...", "pmcid": "PMC13626073",
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

**PDF-linkek és források.** Minden cikknél a legjobb elérhető szabad PDF-link tárolódik, ebben a sorrendben:
1. `pmc-s3`: a PMC Article Datasets nyilvános AWS-tárolója (`pmc-oa-opendata`). Közvetlen PDF, programból is
   megbízhatóan letölthető, és gépi hozzáférésre készült. Csak az újrafelhasználást engedő licencű cikkek vannak benne.
2. `unpaywall`: kiadói vagy repozitóriumi szabad PDF, amelyet a figyelő a felfedezéskor egyszer kipróbált, és
   valóban PDF-et adott (pl. Springer, Nature, Cambridge, bioRxiv, medRxiv). Programból is letölthető.
3. `europepmc`: a Europe PMC webes PDF-je. Böngészőből kattintva működik (ez van a levélben is, ha más nincs),
   de programnak nem szabad letölteni, mert Cloudflare böngészőellenőrzés áll előtte, amit nem kerülünk meg.
4. `unpaywall-web`: szabad PDF, de a kiadó oldala elutasítja a programot (pl. Wiley, ScienceDirect, OUP,
   Taylor & Francis). Böngészőből kattintva működik, ezért a levélben megjelenik, az n8n azonban nem próbálkozik vele.

Az Unpaywall használatához egy kapcsolattartó e-mail-cím kell (`UNPAYWALL_EMAIL` a NAS-oldali compose-ban; az
Unpaywall szabálya szerint minden lekérdezéssel el kell küldeni). A cím nem kerül a repóba. Ha az Unpaywall
szerint a cikk nyílt hozzáférésű, de csak a kiadó HTML-oldala van meg, az a „Teljes szöveg” link lesz.

Az `/articles` rekord `pdf_source` mezője megmondja, melyik forrásról van szó, szűrni is lehet rá
(`?pdf_source=pmc-s3`). A PMC-másolat gyakran csak napokkal a megjelenés után kerül a tárolóba, ezért a napi
futás újra ellenőrzi azokat, amelyeknek még nincs `pmc-s3` vagy `unpaywall` linkjük: az első 7 napban naponta,
30 napig hetente, utána havonta, egy évig (hogy az Unpaywallt se terheljük fölöslegesen, és a fél-egy év után
megnyíló cikkek is előkerüljenek). Ha jobb forrást talál, az `updated_at` mező frissül, így az n8n az `/articles?updated_since=...` lekérdezéssel megkapja az utólag letölthetővé
váltakat. Fizetős cikkekhez csak PubMed/DOI link van.

### PDF-letöltés (n8n) és nyilvántartás

A „mi töltődött le, mi nem” a figyelő adatbázisában van (`downloads` tábla), nem az n8n memóriájában, így
túléli a workflow újraimportálását és a kézi próbafutásokat is. Az n8n a figyelőtől kérdezi meg, mit töltsön le,
és ide jelent vissza. Szabályok:

- **Csak az elmúlt 24 óra.** A `GET /downloads/due` csak azokat a program által letölthető (`pmc-s3`/`unpaywall`)
  cikkeket ajánlja fel, amelyek az elmúlt 24 órában jelentek meg vagy kaptak PDF-linket (`downloads.window_hours`).
- **Ami megvan, azt nem tölti le újra.** Három védelem van: (1) a 24 órás ablak, (2) a nyilvántartás: a sikeres
  letöltés végleges, és a későn érkező hibajelentés sem írhatja felül, (3) lemezellenőrzés: ami a PDF-mappában már
  ott van (a fájlnév tartalmazza a PMID-t, bárhol a mappafában), azt letöltöttnek veszi, akkor is, ha az n8n nem
  jelentett vissza.
- **Ami nem sikerült, azt havonta újrapróbálja, egy évig.** A hibás letöltést a figyelő 30 nap múlva ajánlja fel újra,
  az első próbálkozástól számított 365 napig (`retry_every_days`, `retry_for_days`). Ha közben a PDF-link megváltozott
  (új forrás), nem várja meg a hónapot. A mentési hibát (az n8n nem tudott írni a mappába) holnap újrapróbálja, mert az
  a mi oldalunk hibája, nem a cikké.
- **Mappák: folyóirat, azon belül issue.** `Pediatr Infect Dis J/2026_vol-45_issue-10/2026-<pmid>-<cím>.pdf`
  (`downloads.folders`: `section`, `journal`, `issue`, `year` kombinálható). Az issue-t a PubMed kötet/szám adata adja;
  az online először megjelent, még issue nélküli cikk az `2026_online-first` mappába kerül, és áthelyezésre kerül a
  helyes issue-mappába, amint a PubMed kiadja az issue-t (csak akkor, ha a fájl még ott van, ahová tettük, a te
  kézzel máshová tett fájljaidhoz nem nyúl).

Az n8n nem tud mappát létrehozni, ezért minden PDF az `_inbox` mappába kerül, és a figyelő a jelentés után teszi át
a végleges mappába (sosem ír felül fájlt, és a PDF-mappán kívülre nem nyúl). A figyelő konténerének ehhez írható
módon kell látnia ugyanazt a mappát (`/volume1/docker/n8n/shared/pubmed-pdf:/pdfs`, lásd a compose-ot).

**Kész n8n-workflow: [`n8n/pubmed-pdf-letoltes.workflow.json`](n8n/pubmed-pdf-letoltes.workflow.json).**
Naponta 07:00-kor (a figyelő 06:30-as futása után): `GET /downloads/due?hours=24`, a „Szelekció” Code node
kiválasztja a letöltendőket (alapból az irányelvek, a gyermek-AMS és a gyermekinfektológia szekció; a szabályok a
node tetején szerkeszthetők), letölti a PDF-eket (3 próbálkozással), az `_inbox` mappába menti, és minden eredményt
jelent (`ok`, letöltési hiba, mentési hiba). Importálás: n8n → Workflows → Import from File, majd Publish. A régi
változatot előbb vond vissza (Unpublish/Archive), különben mindkettő lefut. Az `_inbox` mappát (`shared/pubmed-pdf/_inbox`)
előre létre kell hozni, és az n8n-nek írnia kell tudnia bele.

Egyéb minta: a compose-ban az `N8N_WEBHOOK_URL`-t egy n8n Webhook node URL-jére állítva minden futás után
POST érkezik `{event, run_id, window, counts, articles[], trials[]}` tartalommal.

## Telepítés a NAS-ra

1. `\\Becalel\docker\pubmed-watch\` mappa, benne a [`compose.yaml`](compose.yaml) és egy üres `data` mappa.
2. Container Manager → Projekt → Létrehozás → név: `pubmed-watch`, útvonal: `/volume1/docker/pubmed-watch`.
3. A compose-ban írd be az `SMTP_PASSWORD` értékét. Opcionálisan: `API_TOKEN`, `N8N_WEBHOOK_URL`,
   `NCBI_API_KEY`, `UNPAYWALL_EMAIL`. Egyszeri műveletek: `DIGEST_ON_START_DAYS` (összesítő levél),
   `LINK_REFRESH_ON_START` (új érték = egyszeri teljes PDF-link frissítés, pl. új forrás bevezetése után).
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
PUBMEDWATCH_DATA=./data python -m pubmedwatch refresh-bibliography  # kötet/szám a PubMedből
PUBMEDWATCH_DATA=./data PDF_DIR=./pdfs python -m pubmedwatch organize  # PDF-ek mappákba rendezése
```

Források: NCBI E-utilities, Europe PMC REST API, ClinicalTrials.gov API v2, opcionálisan Unpaywall.
A program őszinte User-Agenttel, a szolgáltatók sebességkorlátait betartva kérdez le.
