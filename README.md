# SEC Inline Financials

An evidence-backed financial data foundation built on SEC EDGAR Inline XBRL
filings and Arelle. The current CLI ingests or refreshes a selected company,
retains filing resources in a content-addressed archive, and stores complete
detached filing evidence plus narrative sections in SQLite.

Direct Mapping for seven financial metrics is implemented over stored evidence.
The next product layers are evidence-backed LLM recommendations for unresolved
metrics and a local frontend with evidence downloads and mapping review.
[core.txt](core.txt) is the original requirements notebook; the
[project proposal](docs/designs/project_proposal.txt) is the current blueprint.

## Current status

| Layer | Status |
| --- | --- |
| SEC discovery and complete Arelle evidence extraction | Implemented |
| Stored-evidence annual/quarterly TXT and complete-facts JSON reports | Implemented; interactive script |
| Durable filing-resource archive and relational evidence storage | Implemented; live 5/12 windows verified |
| Versioned Direct Mapping for seven metrics | Implemented; stored AAPL 5/12 acceptance verified |
| Broader concept discovery, LLM packets, and recommendation checks | Planned |
| On-demand filing refresh and historical evidence retention | Implemented; new-accession live acceptance remains |
| Frontend and evidence downloads | Planned |

Reports are generated only from stored snapshots and their persisted `report-v1`
evaluations. The interactive script in `tests/inspect_ingestion.py` does not
contact the SEC or open Arelle. The two ingestion commands also evaluate or reuse
Direct Mapping after evidence publication; `sec-inline-financials` adds elapsed
time and the resolved storage paths. `sec-inline-financials-map` runs only the
stored-evidence mapping path.

The evidence-ingestion path detaches every Arelle-exposed observation, stores linked
evidence in SQLite, and retains loaded filing resources in an immutable
content-addressed archive. Direct Mapping uses exact published snapshot bindings,
persists immutable annual and quarterly evaluations, and refreshes or reuses them
after filing-window updates. LLM integration and the frontend remain later
milestones.

The same filing snapshot also stores normalized narrative sections from the retained
primary document. For a 10-K these are Items 1, 1A, 3, 7, 7A, and 8. For a 10-Q
they are Part I Items 1-4 and Part II Items 1 and 1A. Each required section records
its source range and content hash; missing or unparseable headings remain explicit
instead of being treated as an empty disclosure.

The report workflow remains explicitly invoked through the interactive script.
Ingestion and update workflows do not generate reports automatically.

## Documentation

- [Project blueprint](docs/designs/project_proposal.txt): implemented foundation,
  planned product layers, milestones, and completion criteria.
- [Ingestion and update design](docs/designs/ingestAndUpdate.txt): refresh decisions,
  Arelle processing, snapshot storage, failure behavior, and status semantics.
- [Evidence-storage design](docs/designs/evidence-storage.md): detailed evidence,
  schema, artifact, replay, and integrity contracts.
- [Evidence-storage runbook](docs/evidence_storage_runbook.md): commands, runtime
  paths, audits, recovery, backup, and verification.
<<<<<<< HEAD
- [Interactive frontend prototype](docs/analyst_dashboard_wireframe.html) and
  [static preview](docs/analyst_dashboard_wireframe.png): demo-only one-page,
  single-company metric-lineage and filing-download selector; local API integration
  remains planned.
=======
- [Milestone 2 Direct Mapping plan](docs/designs/direct_mapping_implementation_plan.txt):
  implemented rules, resolver, persistence, refresh, CLI, and acceptance contract.
- [Planned frontend wireframe](docs/analyst_dashboard_wireframe.html) and
  [static preview](docs/analyst_dashboard_wireframe.png): demo-only future interface.
>>>>>>> master

## Metric workflow

```text
SEC filings -> Arelle extraction -> retained filings and linked evidence
    -> direct mapping for each metric and exact period
    -> broader candidate discovery and LLM recommendation when unresolved
    -> deterministic checks and stored mapping decisions
    -> refresh affected results -> frontend inspection and downloads
```

The project covers **seven metrics**: Revenue, Operating Income, Net Income,
Total Assets, Total Liabilities, Equity, and Operating Cash Flow.

[mapping.txt](mapping.txt) remains the readable source list and parity benchmark.
Runtime code uses the frozen `direct-mapping-v1` rules and the
`target-metrics-v1` definitions. Mapping is evaluated separately for each company,
report kind, Target Metric, and exact period.

- Evaluate mappings separately for every annual or filed-quarter period.
- Keep a valid reported **zero**. Zero does not mean missing or trigger fallback.
- Persist either `reported` with one exact fact link or `missing` with
  `mapping_not_found` or `no_selectable_fact_for_period`.
- Keep prior published evaluations when new evidence is incomplete or mapping fails;
  readers expose the prior evaluation as stale when its window no longer matches.
- When Direct Mapping cannot populate a metric, retrieve target-specific stored
  evidence and request an LLM Mapping Recommendation.
- Keep dimensional-only, conflict, unsupported-period, insufficient-evidence,
  and pending-review states visible.
- A Mapping Recommendation requires user approval before it becomes accepted.

## Storage and evidence retention

The application uses **one SQLite evidence store plus retained file artifacts**.
JSON/JSONL will supply later model packets and downloads. SQLite remains the
structured source of truth for this local, single-user application.

Preserve all three layers: original filings/resources, Arelle-processed financial
observations, and supporting evidence. Store selected and dimensional observations
in linked `facts` and `fact_dimensions` records; link reconciliation issues to
their candidate facts; retain calculation arcs, role memberships, and validation
records. Store the required 10-K/10-Q narrative items in `filing_sections`, linked
to the retained primary source document. Persist structured extraction before TXT
rendering, using permanent IDs separate from report-local E/D/R references.

Build packets by company, metric, exact period, selected accession, and extraction
version. Include relevant facts, dimensions, conflicts, complete grouped
calculation relationships, checks, and retrieval-coverage information. A declared
calculation relationship does not by itself prove that the values reconcile.

Keep the **exact packet bytes sent to the model**, with a hash, versions, linked
responses, and acceptance decision. Evidence stays available after ingestion,
prompting, and refresh so the frontend can provide reproducible downloads. A TXT
report is retained when the project owner explicitly generates one; report
generation is separate from ingestion and updates.

Evidence extraction occurs before the existing report filters. The store keeps
the complete observed-concept catalog, all Arelle-exposed fact occurrences,
structured conflict-candidate lineage, report roles, validation messages, and
role-scoped calculation relationships. The report remains a derived presentation.

See the [evidence-storage design](docs/designs/evidence-storage.md) and
[storage runbook](docs/evidence_storage_runbook.md) for the implemented interface,
runtime layout, audit, backup, and recovery procedures.

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

Ingest or update the retained five-annual/twelve-quarter evidence window:

```powershell
uv run --no-sync sec-inline-financials AAPL
```

This primary command prints the final company/run summary, elapsed ingestion time,
evidence root, artifact directory, and SQLite database path to stdout. Progress is
written to stderr and flushed after every stage and filing.

The lower-level aliases run the same ingestion without the final elapsed-time and
path lines:

```powershell
uv run --no-sync sec-inline-financials-ingest AAPL
uv run --no-sync python -m sec_inline_financials.company_ingestion AAPL
```

The final summaries remain on stdout, so progress and machine-consumable results
can be redirected independently.

The first successful call returns `initialized`. Later calls first inspect local
state. A complete current window returns `reused_local` without SEC or Arelle until
an annual/quarterly check is due. `--force`, missing evidence, or an active legacy
snapshot without profile-v2 narrative sections also triggers SEC discovery and
filing processing. A check that finds no new selected accession returns
`checked_no_update`, even when it created newer-profile snapshots. A new selected
accession returns `updated`. If an SEC refresh fails for a company with stored
evidence, the command returns `refresh_failed_using_local_data` and leaves the
published local window unchanged.

Evaluate or reuse Direct Mapping from the published local evidence window:

```powershell
uv run --no-sync sec-inline-financials-map AAPL
```

This command requires a complete active 5/12 window with exact snapshot bindings
and persisted `report-v1` evaluations. It prints annual and quarterly evaluation
IDs plus reported/missing counts. It does not require `SEC_USER_AGENT`, contact the
SEC, open Arelle, call an LLM, or create TXT/JSON reports.

Preview deletion of all stored evidence owned by one company:

```powershell
uv run --no-sync sec-inline-financials-purge AAPL
```

The preview does not delete company data. It lists the matched company,
filing/snapshot/run counts, exclusive artifact bytes, company staging/cache
directories, and shared artifacts that will be preserved. Execute the same purge
only after reviewing that scope:

```powershell
uv run --no-sync sec-inline-financials-purge AAPL --execute
uv run --no-sync sec-inline-financials-purge AAPL MSFT NVDA --execute
```

The operation deletes the selected companies as one SQLite transaction, refuses to
run while any ingestion is active, removes company-owned `staging/<attempt-id>`
directories, and removes immutable objects only when no unselected company still
references them. File deletion is journaled after the database commit and can be
retried with `sec-inline-financials-purge --cleanup-pending`. The shared SEC
transform-plugin cache and manually generated files under `output/` are not
company-owned and are not deleted.

Generate a report from stored evidence:

```powershell
uv run --no-sync python tests/inspect_ingestion.py
```

The script prompts for a ticker, an annual or quarterly report, and one or more
available fiscal years. Quarterly choices are limited to years with stored Q1, Q2,
and Q3 snapshots. It writes both the rendered report and every retrieved fact
occurrence:

```text
output/<TICKER>_<annual|quarterly>_<YEARS>.txt
output/<TICKER>_<annual|quarterly>_<YEARS>_all_facts.json
```

Report generation reads SQLite only. It uses the newest stored snapshot for each
selected annual year or quarter and fails explicitly when the required `report-v1`
evaluation is absent.

## Meaning of an ingestion request

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

The ingestion CLI accepts positive `--annual-count` and `--quarterly-count` values;
the default is 5/12. Insufficient requested annual or quarterly history produces an
error instead of a silently shortened result.
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
  Direct Mapping additionally verifies entity and Target Metric scope.
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

Ingestion validates XBRL and calculation relationships with Arelle. It does not
claim to run the SEC's complete EDGAR Filer Manual validation suite.

These filters also limit Direct Mapping coverage. Six- or nine-month cash-flow
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

The live pytest covers complete evidence extraction into a detached bundle; it does
not persist that bundle or exercise stored report replay. Storage tests use
deterministic local fixtures. Separate production CLI runs have verified complete
5/12 evidence-v2 windows, narrative sections, and artifact integrity. Those recorded
runs do not replace the opt-in live test and do not prove every future new-accession
refresh or the SEC's full EFM validation suite.

## Next implementation step

Proceed to Milestone 3 evidence-backed LLM Mapping Recommendations and human
review. A separate live acceptance should still exercise an ingestion update that
discovers a genuinely new accession. Model/provider choices, recommendation
validation, API contracts, and frontend implementation remain open decisions.
