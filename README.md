# SEC Inline Financials

An evidence-backed financial metric explorer built on SEC EDGAR Inline XBRL
filings and Arelle. The current application is a local CLI that produces annual
and quarterly concept-by-period TXT reports with fact, dimensional,
reconciliation, calculation, and validation evidence.

The direction in [core.txt](core.txt) extends that foundation with retained
filings and structured evidence, mapping for seven financial metrics, LLM
recommendations for unresolved metrics, incremental updates, and a local
frontend with evidence downloads and mapping review.
See the [project proposal](docs/project_proposal.txt) for the proposed design,
alternatives, acceptance criteria, and open decisions.

## Current status

| Layer | Status |
| --- | --- |
| SEC discovery and Arelle report-period extraction | Implemented |
| Annual and latest-12-filed-10-Q TXT reports with E/D/R evidence | Implemented |
| Durable filing archive and relational evidence storage | Planned |
| Direct metric mapping using `mapping.txt` | Planned for seven metrics |
| Broader concept discovery, LLM packets, and recommendation checks | Planned |
| Incremental refresh and historical result versions | Planned |
| Frontend and evidence downloads | Planned |

The current extractor uses in-memory result objects and local caches. It does
not yet provide a database, a complete filing archive, a mapping engine, an LLM
integration, or a frontend. The commands below run the existing CLI.

The existing report workflow is complete and remains explicitly invoked. The
planned ingestion and update workflows will not generate reports automatically.

## Planned metric workflow

```text
SEC filings -> Arelle extraction -> retained filings and linked evidence
    -> direct mapping for each metric and exact period
    -> broader candidate discovery and LLM recommendation when unresolved
    -> deterministic checks and stored mapping decisions
    -> refresh affected results -> frontend inspection and downloads
```

The project covers **seven metrics**: Revenue, Operating Income, Net Income,
Total Assets, Total Liabilities, Equity, and Operating Cash Flow.

[mapping.txt](mapping.txt) supplies the exact concept names used for Direct
Mapping. Mapping is evaluated separately for each company, metric, and exact
period. Detailed precedence and evidence rules belong to the later mapping
design.

- Evaluate mappings separately for every annual or filed-quarter period.
- Keep a valid reported **zero**. Zero does not mean missing or trigger fallback.
- When Direct Mapping cannot populate a metric, retrieve target-specific stored
  evidence and request an LLM Mapping Recommendation.
- Keep dimensional-only, conflict, unsupported-period, insufficient-evidence,
  and pending-review states visible.
- A Mapping Recommendation requires user approval before it becomes accepted.

## Planned storage and evidence retention

The proposal recommends **one SQLite evidence store plus retained file artifacts**
for the local application. JSON/JSONL supplies model packets and downloads;
MySQL is an alternative for a future shared deployment.

Preserve all three layers: original filings/resources, Arelle-processed financial
observations, and supporting evidence. Store selected and dimensional observations
in linked `facts` and `fact_dimensions` records; link reconciliation issues to
their candidate facts; retain calculation arcs, role memberships, and validation
records. Persist structured extraction before TXT rendering, using permanent
IDs separate from report-local E/D/R references.

Build packets by company, metric, exact period, selected accession, and extraction
version. Include relevant facts, dimensions, conflicts, complete grouped
calculation relationships, checks, and retrieval-coverage information. A declared
calculation relationship does not by itself prove that the values reconcile.

Keep the **exact packet bytes sent to the model**, with a hash, versions, linked
responses, and acceptance decision. Evidence stays available after ingestion,
prompting, and refresh so the frontend can provide reproducible downloads. A TXT
report is retained when the project owner explicitly generates one; report
generation is separate from ingestion and updates.

This requires richer extraction: the current report filters out observations
outside its dates/duration windows, and conflict candidates are returned as text
summaries. A complete observed-concept catalog, structured candidate lineage,
presentation roles, and numeric-check records are planned additions.

## Set up

Requirements: Python 3.10 or newer and
[uv](https://docs.astral.sh/uv/).

From PowerShell in this directory:

```powershell
uv sync --link-mode copy
```

SEC automated access requires an identifying User-Agent containing a name or
organization and a contact email. The program reads only `SEC_USER_AGENT` from
the first available source in this order:

1. Current process environment
2. `config.env`
3. `config.txt`

For example, the local configuration file may contain:

```text
SEC_USER_AGENT="Your Name your.email@example.com"
```

Alternatively, set it for the current PowerShell process:

```powershell
$env:SEC_USER_AGENT = "Your Name your.email@example.com"
```

If it is already saved as a Windows user environment variable, refresh it into
the current process without displaying its value:

```powershell
$env:SEC_USER_AGENT = [Environment]::GetEnvironmentVariable("SEC_USER_AGENT", "User")
```

Do not commit the identifying value. Both supported configuration filenames
are ignored. Other API keys in these files are neither loaded nor modified by
this application.

## Run

```powershell
uv run --no-sync sec-inline-financials
```

Example interaction:

```text
Company ticker: AAPL
Number of latest annual fiscal years: 5
```

The results are saved as:

```text
output/AAPL_latest_5_years.txt
output/AAPL_latest_12_10q_quarters.txt
```

Each report is written atomically. The annual report is saved first, so a later
quarterly discovery or processing failure can leave a completed annual report.

The CLI reports progress because Arelle processes the selected filings
sequentially. Arelle sessions use shared process state and must not be run
concurrently in threads.

## Meaning of the request

`AAPL` plus `5` means:

- resolve AAPL to its SEC CIK;
- inspect recent and archived SEC submissions metadata;
- select eligible `10-K` filings whose SEC `isInlineXBRL` flag is true,
  grouping by report-date year and keeping the latest filed candidate per year;
- take the latest five selected annual periods;
- exclude `10-K/A` amendments from consuming a fiscal year;
- select the latest twelve exact-form `10-Q` filings whose SEC `isInlineXBRL`
  flag is true;
- exclude `10-Q/A` amendments from the quarterly report;
- process all selected filing documents with Arelle.

The annual-history input accepts 1–20 years and controls only the 10-K report.
The quarterly report requires 12 eligible 10-Q filings. Insufficient annual or
quarterly history produces an error instead of a silently shortened report.
Because companies do not file a 10-Q for Q4, these are the latest 12 filed 10-Q
quarters, not 12 consecutive fiscal quarters.

The application uses SEC submissions only for company and filing discovery.
Financial values come from the filing-scoped Inline XBRL models produced by
Arelle, not from the SEC Company Facts API.

## Table and evidence rules

- The annual report's columns are fiscal years, ordered from oldest to newest.
- The quarterly report's columns are the latest 12 filed 10-Q periods, labeled
  `FY<year> Q1`, `FY<year> Q2`, or `FY<year> Q3` and ordered oldest to newest.
  Missing DEI fiscal labels fall back to the report-date year or an ended-date
  quarter label.
- Rows are the union of eligible numeric concepts across the report's filings.
- Only facts ending on the filing's report date are eligible. Annual duration
  facts must span 300–400 days. Quarterly duration facts must span 60–120 days,
  so six- and nine-month year-to-date facts are excluded from quarterly cells.
- Quarter-end instant facts are eligible in the quarterly report.
- The current table uses eligible dimension-free facts as its primary values.
  Planned metric mapping also needs to verify entity and target scope.
- Dimensional-only concepts remain visible as `DIMENSIONAL [D...]`, with their
  components below the table.
- Exact dimension-free duplicates collapse to the most precise reported fact.
- Conflicting values are shown as `CONFLICT [R...]` and quarantined. They are
  never averaged or guessed.
- Missing values are `—`; the application does not calculate or fill them.
  Reported zeros remain zero. A dash means no eligible observation in this
  report, not proof that the filing contains no relevant evidence.
- `[E...]` references connect table cells to Arelle fact evidence: raw value,
  label, period, unit, decimals, dimensions, validity, filing, and accession.
- Arelle calculation relationships and warning/error messages follow the fact
  evidence. Calculation 1.0 validation runs in deduplicating mode (`c10d`).

This application validates XBRL and calculation relationships with Arelle. It
does not claim to run the SEC's complete EDGAR Filer Manual validation suite.

These filters also limit planned mapping coverage. Six- or nine-month cash-flow
facts cannot fill discrete-quarter Operating Cash Flow or CapEx cells, and
cover-page shares dated after the report date cannot fill period-end shares.
The expanded store will preserve those observations with their actual dates and
exclusion reasons. YTD subtraction and Q4 derivation remain deferred; not every
metric is guaranteed to have a value in every period.

## SEC Inline transformation plugin

The application obtains the SEC's custom Inline transformations separately. On
first use, it downloads only the official SEC `transform` plugin
files from a pinned commit in
[Arelle/EDGAR](https://github.com/Arelle/EDGAR), verifies their SHA-256 hashes,
and stores them under `.cache/`. A checksum mismatch stops processing.

## Verification

Offline tests, formatting, linting, and strict type checking:

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src
```

Opt-in live test of one Apple annual filing, requiring `SEC_USER_AGENT` in the
current process environment:

```powershell
$env:SEC10K_RUN_LIVE = "1"
uv run --no-sync pytest tests/test_live_arelle.py -q
```

The existing tests cover the extraction/reporting foundation. This live test
does not verify quarterly discovery, storage, metric mapping, LLM recommendations,
updates, or the frontend. Each planned phase has separate acceptance criteria in
the [proposal](docs/project_proposal.txt).

## Next implementation step

Persist one accession's original filing, structured facts, dimensions, conflict
candidates, and calculation relationships, then reload them and compare the
result with the existing report. Establish that round trip before adding direct
mapping and LLM calls. Detailed mapping rules, model/provider choices, amendment
handling, API contracts, and frontend screen design remain open decisions.
