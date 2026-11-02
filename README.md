# Price Transparency Explorer

Search the negotiated rates a health insurer publishes under the CMS Transparency in Coverage rule.

- **Load:** an ETL step reads a CMS **index file** (the table of contents), downloads the in-network
  rate files it lists, and flattens them into that index's own SQLite database.
- **Search:** a FastAPI app opens it through two doors, **Prices By Procedure** (a billing code or
  procedure name) and **Prices By Provider** (a name, NPI or EIN), in all networks or one.
- **See:** every price, who charges it, and which file it came from, compared like with like.

Built against Centene's Fidelis Care index of 2026-09-28: 2 in-network files (277 MB of JSON,
94 plans) → 1,185,888 rate rows. Desktop browsers only, by design (see Design Decisions).

## Getting Started

You need **Python 3.12** and **2 GB of free disk** (the app won't start a load with less). There is
no database to install or run: SQLite is built into Python (the standard library's `sqlite3`), and
each index's database is a single file under `data/parsed/`. Run everything from the project folder.

### 1. Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start The App

```bash
uvicorn app:app
```

Leave it running and open **http://127.0.0.1:8000** in your browser.

Port 8000 taken, or want another? Add `--port`:

```bash
uvicorn app:app --port 8080   # then open http://127.0.0.1:8080
```

### 3. Load The Data (First Time Only)

The app opens on **Load Data**. Centene's two Fidelis index files are already listed (they're
committed in `data/raw/index_files/`).

1. Under **1. Choose An Index File**, click `2026-09-28_fidelis_index.json`.
2. Under **2. In-Network Files This Index Describes**, tick one or more files (table below). You
   don't need them all: pick the networks you care about. **Download and keep** is already chosen.
3. Press **Load selected files**. Each file downloads once, then loads in seconds (both together:
   277 MB, about 10 s to load). Progress shows on the page.

| File | Network | Plans | Size |
|---|---|---|---|
| fidelis-ex | Ambetter | 88 | 95 MB |
| fidelis-es | Essential Plan | 6 | 182 MB |

### 4. Search

- **Prices By Procedure:** type a code or words of its name, e.g. `99213` or `massage`.
- **Prices By Provider:** type a name, NPI or EIN, e.g. `Mount Sinai`.

**Next time:** the data stays loaded. Just run `source .venv/bin/activate` and
`uvicorn app:app`, and open http://127.0.0.1:8000.

## Other Ways To Run It (Optional)

You don't need any of this: Getting Started is the whole setup. Use these only when:

| You Want To… | Do This |
|---|---|
| Load without a browser: a script, or a scheduled monthly refresh | [Load From The Command Line](#load-from-the-command-line) |
| Try the app in seconds, without the 277 MB download | [A Tiny Sample, No Download](#a-tiny-sample-no-download) |
| Keep data sets apart (say, the sample and the full data) | start the app with `PARSED_DIR=<folder>` |
| Always open one specific database file | start the app with `RATES_DB=<file>` (loading any index then rebuilds that file) |
| Work on the code | `DEV_TOOLS=1 uvicorn app:app --reload`: reloads on save and adds a **Restart server** button |

By default the app shows the **active** index's database in `data/parsed/`: the one named in
`data/parsed/active_index`, else the newest.

### Load From The Command Line

For scripts and scheduled refreshes: the same load as step 3, without the browser. It loads every
file the index lists.

```bash
python -m etl.load --index tests/etl/test_data/2026-09-28_fidelis_index.json
# -> data/parsed/2026-09-28_fidelis_index.db, now the active index
```

- The index in `tests/etl/test_data/` is an unmodified copy of Centene's 2026-09-28 Fidelis index.
  `--index` also takes a URL (the current one is linked from
  https://www.centene.com/price-transparency-files.html).
- Files already in `--raw-dir` are not downloaded again.
- Each index gets its own database, `<parsed dir>/<index file stem>.db`, which the load makes the
  active one (see Design Decisions).
- Loading the same index again backs its database up to `--backup-dir` and replaces it, only if at
  least one file loaded.

| Option | Default | Meaning |
|---|---|---|
| `--index` | (required) | index JSON: a local path or an http(s) URL |
| `--index-dir` | `data/raw/index_files` | where an index given by URL is saved |
| `--raw-dir` | `data/raw/in_network_files` | where in-network files are kept |
| `--parsed-dir` | `data/parsed` | the folder of index databases |
| `--db` | `<parsed-dir>/<index file stem>.db` | build this file instead (not made active) |
| `--backup-dir` | `data/parsed/backups` | where a database being replaced is copied first |
| `--no-download` | off | only use files already in `--raw-dir` |

| Exit Code | Meaning |
|---|---|
| `0` | every file loaded |
| `2` | the index couldn't be read or failed schema validation (nothing written) |
| `3` | some files loaded, some failed |
| `4` | no file loaded |

### A Tiny Sample, No Download

To see the app working in seconds, load the test slice instead: 1,828 rates for 90853, 10006 and
RC 470, already in the repo. The second file isn't on disk, so it is
reported as an error and skipped (exit code 3), which also shows that one bad file doesn't stop the
load. A separate `--parsed-dir` keeps it apart from the full data:

```bash
python -m etl.load --index tests/etl/test_data/2026-09-28_fidelis_index.json \
  --raw-dir tests/etl/test_data --parsed-dir data/sample --no-download
PARSED_DIR=data/sample uvicorn app:app   # start the app on the sample
```

## Run The Tests

```bash
python -m unittest discover -s tests -t .
```

643 tests, about 37 s. They use small slices of the real files in `tests/etl/test_data/` (cut with
`scripts/make_fixture.py`), no mocks; the download tests run against a local HTTP server.

## Architecture And Data Flow

```
 CMS index file (path or URL)
        │  etl/run.py, etl/validate.py   read it, check it against the CMS JSON Schema
        ▼
 in-network file URLs + plans
        │  etl/fetch.py                  download each file once into data/raw/in_network_files/
        ▼                                (or read it straight from the web, in memory)
 in-network JSON files
        │  etl/load.py, etl/run.py       resolve provider references, flatten
        ▼
 data/parsed/<index stem>.db (SQLite)    index_info · index_in_network_files · source_files · plans · rates
        │  etl/indexes.py                one database per index; data/parsed/active_index names the active one
        │  api/search.py                 read-only SQL: filter, sort, page, price groups, statistics
        │  api/grouping.py               rows → provider prices (Count by Provider / NPI / Every row)
        │  api/providers.py              organizations (EIN) with their names, NPIs, codes
        │  api/pickers.py                what each picker offers, and no-dead-end notices
        │  api/search_in.py              Search in: all networks or one, and Find your plan
        ▼
 app.py (FastAPI)  ──►  /api/* JSON
        │  ui/procedure_page.py         Prices By Procedure: routes and view logic
        │  ui/provider_page.py          Prices By Provider: routes and view logic
        ▼
 Jinja2 templates (server-rendered HTML, inline SVG chart from ui/chart.py)
```

The ETL runs once, up front. Search never fetches or parses JSON; it only queries what's loaded.
Loading can be started from the CLI (`etl/load.py`) or the Load Data page (`ui/load_page.py`,
which adds per-file choices and runs the load in a background thread); both build the index's
database through `etl/run.py`.

### Separation Of Responsibilities

Three layers. Each one only calls the one below it.

| Layer | What It Does | Files |
|---|---|---|
| **1. ETL** | Downloads the files, flattens them, and writes the database. The only layer that writes. | `etl/` |
| **2. API** | Answers questions about the data with read-only SQL: prices, groups, statistics, providers, plans. Returns data, never HTML or display text. | `api/` |
| **3. UI** | The web pages: takes the API's answers, puts them in words and shows them. Runs no SQL. | `ui/` (pages, `formatting.py`, `labels.py`, `chart.py`, `templates/`, `static/`) |

Each job and the layer responsible for it:

| Job | Layer |
|---|---|
| Download files, check them against the CMS schema, record load errors | ETL |
| Flatten prices into rows (one per price × NPI) and write the database | ETL |
| Filter, sort and page the rates | API |
| Group prices; compute medians, quartiles, percentiles, outliers, × Ref, What Explains Price | API |
| Provider lists, picker choices, and what to offer when something typed has no rates (its other codes, other files, hospital rates) | API |
| Plain-English labels for modifiers and places of service ("mod 25", "office (11)") | UI |
| Formatting numbers and money ($1,234.56, 1,234) | UI |
| Turning the API's "no rates" facts into a message with links ("NPI … has no rates for 99213. It has rates for 4 other codes.") | UI |
| Page wording: headings, column names, tooltips, notes | UI |
| Links between pages, keeping Search in and filters in the URL | UI |
| Sorting the Compare Groups table by the clicked column | UI |
| The distribution chart (SVG) | UI |
| Type-ahead, column sorting and resizing in the browser | UI |

`app.py` at the top of the repo starts the app and connects the three folders; the API and UI run as one
FastAPI process. `tests/api/test_layers.py` checks the rules: the UI runs no SQL, the API
never imports the web framework or the UI, pages don't import each other, and the ETL never imports the
app.

### Tables

| Table | One Row Per | Used For |
|---|---|---|
| `index_info` | the index the database was built from: location, publisher, date, version, valid, load time | the Load Data page; knowing what a database holds |
| `index_in_network_files` | in-network file the index lists, loaded or not, with its plans | "Contains N of the index's M files" |
| `source_files` | in-network file: URL, local path, reporting entity, spec version, last updated, rate count, load error | search by file; traceability; health |
| `plans` | plan in the index (plan ID + name + market type), pointing to its file | search by plan |
| `rates` | price × provider group × NPI, pointing to its file | the search itself |

`rates` columns: billing code, code type, name, description, negotiated type and rate, billing
class, setting, place of service (`service_codes`), modifiers, expiration date, NPI, EIN, business
name, `additional_information`, source file. Indexed on billing code, NPI, EIN, source file and
`additional_information`.

**Why flatten.** In the files, NPI and EIN aren't on the prices: each price points to a
`provider_references` entry by ID, and that entry lists groups of NPIs under a TIN. The ETL resolves
the link once, so every row carries its own NPI, EIN and name, and a search is a filter on one
indexed table (`WHERE billing_code = ? AND npi = ?`). The cost is repetition (see Tradeoffs).

**Why SQLite, when in-memory would do.** The files are 95 MB and 182 MB of JSON and take about
860 MB of memory to parse. Parsing once into a 389 MB file means:

- the app starts instantly and uses little memory; restarts (and `--reload`) don't re-parse;
- the data is inspectable with any SQLite client, which is how most of the findings below were
  checked;
- indexes, `GROUP BY` and sorting come for free, and paging gets an accurate total;
- nothing to install or run: SQLite ships with Python (stdlib `sqlite3`), no server, no extra
  dependency, and the database is one file.

Search only reads the database. A load builds a fresh database for its index; an existing one is
backed up first and swapped only if at least one file loaded.

## Design Decisions

| Decision | Why |
|---|---|
| **Python 3.12, FastAPI, Jinja2, SQLite, `unittest`.** Server-rendered pages, no build step. | One language, few moving parts, nothing to install beyond `pip install`. |
| **Load once, up front; search only reads.** | A search never waits on a download or a 182 MB parse. |
| **Load every file the index lists; pick the network when searching.** | Switching networks, or finding your plan, is instant: no reload. |
| **One database per index, one active at a time.** Loading an index makes it active; loading it again rebuilds it after a backup. | Switching indexes is opening another file (**Show this one**), and months or payers never mix in one result. |
| **Every rate keeps its source file.** | Any price traces back to its file's URL, date, and the plans that use it. |
| **Compare like with like.** Prices are grouped by modifier × place of service × billing class before any statistic. | The same code can price different work under different modifiers or places (see Data Findings And Assumptions). |
| **Median, quartiles and percentiles; no average.** Quartiles interpolate like Excel `QUARTILE.INC` (R type 7). | An average can be a price nobody negotiated. |
| **Spread as a share of the median** (IQR ÷ median, plus Q1 below and Q3 above it). | Spreads compare across codes: 99213 · mod 25 · office is 6.9% (−1.4% / +5.5%). |
| **Outliers: more than 1.5 × (Q3 − Q1) below Q1 or above Q3.** | Standard deviation is inflated by the long tails. In 99213 · mod 25 · office, 535 of 2,662 rates are outliers. |
| **Count provider prices, not rows (Count by).** Default: each provider's price once; NPI and Every row are a click away. | One contract repeats once per NPI: 99214 has 24,262 rows but 2,430 provider prices. **The medians differ because the weighting differs.** By Provider is unweighted: every provider's price counts once, whether it has 1 clinician or 500. By NPI and Every row weight each price by how many clinicians carry it, so a few large groups dominate. In 99214 · mod 25 · office, one provider lists 551 NPIs at $142.27, 40% of the 1,378 rows, so the median is $142.27 by Every row and by NPI but $161.64 by Provider (59 provider prices). Neither is wrong: by Provider says what a typical contract pays, by NPI what a typical clinician is paid. |
| **Two doors: by procedure and by provider.** Each page's one box filters the full list beneath it. | Both questions are natural, and browsing and searching become one thing. |
| **No dead ends.** Pickers offer only choices that have prices; anything typed that has none says so, with alternatives. | Nobody lands on an empty page without knowing why or where to go. Checks go by EIN or NPI, never by name. |
| **Search in: all networks or one, named by its plans.** Find your plan picks the plan's network and says so. | Plans in one file share every rate, so a 94-plan list would add nothing. Every page states what it searched. |
| **Desktop only.** Built and checked at 1024–1920 px wide. | The app runs where its data lives: Python, the downloaded files and the database are all on the same computer, so it's used from that computer's browser. Phone and tablet layouts didn't make sense for this architecture and weren't built or tested; on them the price pages keep their 1,000 px layout and the whole page scrolls sideways. The brief lists a responsive UI among its criteria; leaving it out was a deliberate cut within the time budget (see With More Time). |

## Features

Nav: **Load Data** · **Prices By Procedure** · **Prices By Provider** · **Docs** (this README and
[docs/modifiers.md](docs/modifiers.md), rendered in the app). Swagger is at `/api/docs`.

### Load Data (`/`)

- **Active index** first, then the other loaded indexes. Each shows its index (publisher, valid,
  date, load time), "Contains N of the index's M in-network files", and each file ✓ loaded (plans,
  rates, load date, **Preview**) or ✗ not in it, with the failure reason.
- **Show this one** makes an index active; **Remove** backs it up, then deletes it, after a
  confirmation.
- **Load An Index, step 1** lists your index files (`data/raw/index_files/`). **Open** another by
  URL or path, or from your computer, and it joins the list. It is validated against the CMS
  table-of-contents JSON Schema; an invalid index blocks loading and lists the problems.
- **Step 2** shows each in-network file with a **Preview** (its plans, and the size, date and
  header of a local copy, read without loading or fetching), its source (**Local copy**, **Download
  and keep**, the default when not on disk, or straight from the web), and what Load will download.
- **Before Load:** a heads-up for big loads (5+ files or 200+ MB) and the free disk space. Load
  refuses to start with under 2 GB free.
- **During a load:** it runs in the background and makes that index active. A progress panel (at
  the top of the page from any reload) names each file's stage: Downloading (MB of MB), Reading,
  Saving rates (rows so far, database size), ✓ or ✗.
- **A load killed part-way** (say, the server restarting) is cleaned up on startup and said once:
  "A load of <index> was interrupted at <time>; nothing was changed. Load again?"
- `/load` redirects here.

### Prices By Procedure (`/by-procedure`)

- **The list:** one box over every procedure (1,236 in the Fidelis index), most rates first. Typing
  a code, its start or words of its name filters it ("massage" finds 97124; a revenue code matches
  only its department name). Every column sorts by its header.
- **A procedure's page** (`/by-procedure/{code}`) has, top to bottom: the code type when one number
  is two codes (731); **Search in** (all networks or one) and **Find your plan**; **Narrow to one
  provider** (then each group also shows everyone's median there); and **Count by** (Provider, the
  default, NPI or Every row).
- **The assignment's inputs:** the billing code is the procedure box, and its page. The optional NPI
  and EIN are the **Narrow to one provider** box, which takes one value: an NPI, an EIN or a name.
  Picking a provider narrows the page by that NPI (when one was typed) or by its EIN. Both together
  are accepted by the URL and the API (`/by-procedure/{code}?npi=&ein=`,
  `/api/search?billing_code=&npi=&ein=`): the page is then narrowed to both, and says so.
- **Compare Groups:** one row per price group (modifier set × place of service × billing class),
  compared like with like. Columns: Modifier, Place Of Service, Min · Q1 · **Median** · Q3 · Max,
  provider prices, providers, clinicians (distinct NPIs), × Ref (median against a reference group)
  and outliers; every column sorts by its header.
- **What Explains Price** (above Compare Groups): how much of the spread is modifier, place of
  service, or which provider.
- **Statistics** for one group: median, quartiles, IQR ÷ median, percentiles, outliers, and a
  distribution chart you can drag across to pick a price range.
- **Rates:** every rate, sortable and filterable by column.
- **Hospital rates** billed under a revenue code with this procedure named are groups of their own,
  in their own table, with **Hide** / **Show**.
- Provider names and EINs link to their provider page. Old `/search` and `/codes` links redirect
  here with the same meaning.

### Prices By Provider (`/by-provider`)

- **The list:** one box over every provider (EIN), most rates first. A name (ignoring case,
  punctuation and Inc/LLC/PC), EIN or NPI filters it, showing the names that matched. Every column
  sorts by its header.
- **A provider's page** (`/by-provider/{ein}`): its names (pick one to see only its codes) and
  NPIs, **Search in**, and a **Narrow to one procedure** picker.
- **Prices By Code:** each code compared **only within that code**: its most common like-for-like
  group, this provider's median against everyone's there (flagged "mostly this provider" when it
  holds most of the group), the difference, and the prices by group and name.
- Each code links to that code at this provider and to that code from all providers. Old
  `/providers` links redirect here.

### JSON API

| Endpoint | Returns |
|---|---|
| `GET /api/search?billing_code=99213` | Paged results with total, the code's types, price groups, statistics (overall and per group, with relative price) and what explains price. Optional: `npi`, `ein`, `plan` (plan row id), `source_file_id` (repeatable), `code_type`, column filters with `<column>_op`, `rate_min`/`rate_max`, `include_facility`, `sort`, `dir`, `limit` (≤ 1000), `offset`. Bad input → 422 with a message; no database → 503. |
| `GET /api/procedures?q=` | The procedures a code, its start or words of its name match, procedures before hospital departments, most rates first, each with its page's `href`. |
| `GET /api/procedures/{code}/providers?q=` | The narrow-to-a-provider picker: providers with rates for the code in scope (`in`, `include_facility`, `code_type`) that a name, EIN or NPI matches, and a `notice` with alternatives when what was typed has none. |
| `GET /api/plans?q=` | Find your plan: plans in the active index whose name or plan ID contains `q`, each with its network (in-network file) and the `in` value (`plan-<id>`) that searches it. |
| `GET /api/providers/{ein}/codes?q=` | The narrow-to-a-procedure picker: the codes the EIN has rates for in scope (`in`), and a `notice` with alternatives when a code it has none for is typed. |
| `GET /api/codes?q=` | Every loaded billing code, filtered. |
| `GET /api/providers?q=` | Every organization (EIN) with names, NPIs, code and rate counts (no price range: it would span every code), filtered by name, EIN or NPI, with the names that matched and its page's `href`. |
| `GET /api/providers/{ein}` | One organization: names, NPIs, codes. Unknown EIN → 404. |
| `GET /api/load/preview?index=&url=` | One in-network file's preview (plans, local copy's header and size), for an index in the list. Never fetches. |
| `GET /api/load/progress` | The running load's progress: per file, its stage, bytes downloaded, rows saved and database size; `{"running": false}` when none. |
| `GET /api/health` | The active index database, whether it has data, total rates, whether a load is running, and per file: rate count, dates, load error. |

## Data Findings And Assumptions

Numbers are from the two Fidelis in-network files (`fidelis-ex`: 88 Ambetter plans; `fidelis-es`:
6 Essential Plans) unless stated.

### The Files

- Both follow CMS spec **v2.0.0**, as they declare (not the latest v2.2.1): they validate with 0
  errors against v2.0.0 and fail v2.1.0+, which added stricter NPI rules.
- The assignment's 2026-08-31 index link was outdated, so the 2026-09-28 index from Centene's
  page is used, as the assignment allows.
- Every rate is `negotiated`, setting `outpatient`: these files publish no inpatient rates.
- Billing class follows code type: every CPT and HCPCS rate is `professional`, every RC (revenue
  code) rate `institutional`.
- Descriptions stop at 100 characters: Centene's files cut each one off mid-word ("…TO DEVELOP
  STRENGTH A" for 97110), so the page shows them up to the last whole word with "…". The full text
  isn't in the files.

### Plans And Files

- A plan's rates are all the rates in its file. The files don't say which plan a price belongs to,
  so picking a plan picks its file; results can't be narrowed further. In this index no plan is
  under more than one file.
- Plan ID alone isn't unique; plans are stored as plan ID + name.

### Providers

- NPI `0` means "no NPI" and is dropped. A group listing only `0` becomes one row with no NPI
  (12 rows in fidelis-ex). One NPI has 9 digits; it's kept as published.
- **EIN and provider name are not one-to-one.** 3,012 of 7,097 EINs carry 2+ names (a health
  system's departments: one EIN has 36 Mount Sinai names), and 1,002 names span 2+ EINs (IPAs
  contracting for many practices: CAIPA under 282 EINs). EIN is who gets paid; NPI is the clinician
  or facility.
- The name matters: for the same EIN and service under 2+ names, 53% have different prices
  (separately negotiated contracts). The same name under 2+ EINs shares one price 76% of the time
  (an IPA negotiating for its members). So the pricing unit is EIN + name.
- It's not a spelling problem: only 35 of those 3,012 EINs have names that differ just by
  punctuation or suffix ("Conifer Park Inc" vs "Conifer Park, Inc."); normalizing every name takes
  6,721 distinct names only to 6,613. Names are stored as published.
- The same EIN can carry a different business name in each file.

### Codes

- The same number can be two codes: 731 and 940 are both a CPT anesthesia code and an unrelated
  revenue code. The procedure page asks which instead of mixing them.
- **Revenue codes name a hospital department, not a procedure.** Every RC price carries
  `additional_information`, which holds the actual procedure code (99213, 45380, 96360...). The ETL
  keeps it, groups RC results by it, and lets a CPT search find hospital rates filed under an RC.
- Centene repeats some RC price lists under several procedure-style names (RC 260: "IV Therapy
  General" plus four IV-infusion names with identical prices); 63 of 133 RC codes have more than
  one name, and code 470 appears as two items with different names but identical prices.

### Comparing Prices

More in [docs/modifiers.md](docs/modifiers.md).

- 48% of rows have no modifier; there are 179 modifier combinations from 98 modifiers.
- Modifiers change what a price covers: chest X-ray 71046 is published only as `26` (reading,
  $7.69–$15.99) and `TC` (taking, $21.26–$39.26), never whole; 10040 with `51` is $145.97, with `59`
  $69.67–$140.17. Comparing across them is misleading, so results are grouped by modifier set.
- **Place of service often matters more than the modifier** (table below), so groups are modifier ×
  place of service × billing class.
- Same EIN, same service, different name, different price (769 cases): 41% differ by under 5%,
  30% by over 25%, 10% by 2× or more. The widest are revenue codes without a procedure code (one
  EIN's RC 401 ranges $142.40 to $5,123.56).

Share of price variation (measured in percentages) explained by modifier, place of service, and both:

| Code | Modifier | Place Of Service | Both | Which Provider (The Rest) |
|---|---|---|---|---|
| 99213 | 10% | 61% | 70% | 30% |
| 99214 | 34% | 68% | 78% | 22% |
| 90837 | 31% | 63% | 78% | 22% |
| 90834 | 24% | 14% | 45% | 55% |

## Tradeoffs And Limitations

| Choice Or Limit | What It Means |
|---|---|
| **Rows are not provider prices** | A row is price × provider group × NPI, so one contract with a 40-NPI group appears 40 times. 99214 has 24,262 rows but 2,430 provider prices, and its mod 25 · office median is $142.27 by rows vs $161.64 by provider price. The flat table keeps search simple and fast; Count by forms provider prices at query time, and that is the default. |
| **The flat table has a scale limit** | Centene's Ambetter index (28 in-network files) lists provider groups of up to 1,861 NPIs, so two small files (ambetter-nj 19 MB, 3,625 prices; ambetter-nv 20 MB, 26,079 prices) expand to about 24 million rows, one per price × group × NPI: over 2 GB of database and minutes of loading. Fidelis's 277 MB is only 1.2 million rows. Storing each price once per provider group, with NPIs in their own table joined at query time, would cut this by orders of magnitude. |
| **Whole-file parsing** | Each file is read with `json.load`: simple and fast (full load 8.5 s), but about 860 MB peak memory. Fine for these files; a multi-GB file needs a streaming parser. |
| **No cancel for a load** | Loads run in the background, one at a time, with live progress. A killed load is cleaned up on the next start and leaves the old database as it was. |
| **One index shown at a time** | Several can be loaded and switched between, but nothing compares them yet. |
| **Download and keep is the default** | Faster reloads, retries without re-downloading, and the exact source stays on disk to check numbers against. Reading straight from the web only saves about 280 MB per month of Fidelis files. |
| **Errors are recorded, not fatal** | A failed download or parse is stored on its file's row and shown on Load Data and `/api/health`; the other files still load. A missing database gives a clear message, not a crash. |
| **Downloads are checked** | A download shorter or longer than its declared size is rejected. Local copies get safe names (`<readable stem>.<sha256(url)[:12]>.json`), so a URL can't write outside the folder and two URLs never share a file. |
| **No login; meant for one computer** | Load Data writes files and databases. Every POST must come from the app's own pages (Origin or Referer); anything else gets 403 and changes nothing. |
| **Modifiers and places are stored sorted** | `95,GT` and `GT,95` land in one price group. (Fidelis never mixes the order: 179 combinations either way.) |
| **Labels are our own** | Modifier and place-of-service labels are short descriptions in our own words (CPT text is AMA-copyrighted) and cover the common codes; others show as the bare code. |

## With More Time

### Comparing Prices

| Idea | What It Would Do |
|---|---|
| **Pick your own reference** | A "Use as reference" action on each Compare Groups row sets the × Ref baseline (e.g. the office visit). Today the largest group is used, which on 99213 is mod 25 · telehealth (02), so office visits read about 2.2×. |
| **Compare against Medicare** | Show each price as "% of Medicare", the usual industry baseline, from CMS's physician fee schedule. |
| **Modifier and place of service as separate choices** | A grid with modifiers down the side and places of service across, so you can read one effect while holding the other fixed. Today a group is still the pair. |
| **Duplicates from what's on screen** | Rows identical in the visible columns collapse into one line with a count ("× 12"), and the statistics count them the same way. |
| **Predict a provider's price** | Estimate what a provider would charge for a service it hasn't published, and show how each modifier changes that provider's price (e.g. its 95 telehealth vs office price), learned from its own prices and its peers'. |
| **Price drivers across all codes** | A model of log(price) on code, modifier, place of service and provider, to estimate typical effects (telehealth, `26`/`TC`, network). Same-provider pairs are scarce (59 for telehealth vs office), so it needs care. |

### Charts

| Idea | What It Would Do |
|---|---|
| **Bell curve option** | Overlay a fitted curve on the histogram. Prices skew right, so fit it on log price (log-normal). |
| **Scatterplot view** | Each provider's price against its size (NPI count), or each group's median against its spread. |

### Over Time And Across Files

| Idea | What It Would Do |
|---|---|
| **Month over month** | Open August's index beside September's and compare like-for-like rates: up, down, unchanged, new, gone. |
| **Across network files** | The same provider in fidelis-ex vs fidelis-es: of 9,093 like-for-like matches, about a third differ in price. |
| **In-network vs out-of-network** | Load the allowed-amount files and show where being in network isn't cheaper. |
| **Sub-indexes (an index of indexes)** | Explore loading a CMS index that points to further index files rather than straight to in-network files, as some Centene brands publish (e.g. QualChoice), so one top-level index can load a whole family of plans. |

### Performance

| Idea | What It Would Do |
|---|---|
| **Faster busy pages** | 99213 takes about 0.4 s (Rates tab 1.1 s), the biggest provider 0.75 s, because one page reads the same rows 8–10 times. Read them once per page, cache the filter dropdowns, and add covering indexes. |
| **Lazy loading** | Prices By Procedure and Prices By Provider show the first rows at once and fetch the rest as you scroll. |
| **Stream large files** | Parse files without loading them whole, and let a running load be cancelled. |

### Ease Of Use

| Idea | What It Would Do |
|---|---|
| **Search In that sticks** | The network picked, or the plan found with Find your plan, stays chosen across searches and both price pages. |
| **Responsive layout** | The price pages keep a 1,000 px minimum width today. A pass would let each table collapse or scroll on its own, and the search and scope bars wrap, so a phone or tablet shows one column at a time instead of scrolling the whole page sideways. |
