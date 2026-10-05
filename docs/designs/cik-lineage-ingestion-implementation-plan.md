# Verified CIK Lineage Ingestion Implementation Plan

- **Status:** Historical broader proposal; operator-verified lineage subset implemented
- **Scope:** First-seen lineage and candidate inspection, backend filing discovery, ingestion,
  evidence provenance, and Direct Mapping
- **Primary defect:** A current ticker CIK can have insufficient history because older filings
  belong to a verified predecessor or successor registrant lineage
- **Initial acceptance company:** XOM
- **Current implementation baseline:** `gstack` at `75bbe45881c80a5b19746d3323d4ac3d99b6eea1`

> **Implementation note (2026-10-05):** The current runtime implements explicit
> operator-approved predecessor maintenance, current-to-oldest bounded discovery,
> partial filing-window publication, company-scoped filing provenance, effective
> transition dates, `report-v2`, and lineage-aware Direct Mapping. It does **not**
> implement this plan's automated unknown-candidate discovery, pending-candidate
> persistence, reviewed catalog promotion, or automatic first-seen lineage resolution.
> Treat those sections as historical/future design, not current CLI behavior. Current
> operational behavior is documented in `docs/evidence_storage_runbook.md` and
> `docs/designs/ingestAndUpdate.txt`.

## 1. Outcome

The ingestion system must satisfy a requested filing window across a company's verified SEC
registrant lineage rather than assuming that the ticker's current CIK owns all historical
filings.

For XOM, the ticker currently resolves to ExxonMobil Holdings Corporation, CIK
`0002115436`. Exxon Mobil Corporation, CIK `0000034088`, owns earlier filing history. The
SEC filings describe the Texas entity as the successor registrant effective July 1, 2026.
The product must treat these CIKs as two registrants in one reviewed business lineage, not as
one SEC filer whose CIK value was edited.

After this plan is implemented, a normal request for five annual and twelve quarterly XOM
filings will:

1. resolve the ticker to the current SEC company and CIK;
2. during first-seen company initialization, resolve and persist the checked-in,
   source-backed lineage for that current CIK before filing-window discovery;
3. search the current registrant first using the initialized lineage;
4. search verified predecessors only when the current registrant cannot complete the
   requested window;
5. select and deduplicate eligible filings across the lineage;
6. preserve the canonical company, filing registrant, and EDGAR archive owner as distinct
   identities;
7. ingest and publish complete evidence when the lineage satisfies the request;
8. during first-seen ingestion, automatically search for and inspect source-backed predecessor
   candidates when the verified lineage is exhausted before the requested window is complete;
9. persist any candidate relationship, source evidence, and observed eligible-filing counts
   for review while keeping its filings out of the production window;
10. return explicitly incomplete evidence with candidate diagnostics when the verified
    lineage remains insufficient; and
11. refuse to promote or ingest an unknown predecessor based only on names, tickers,
    accession prefixes, or SEC `formerNames`.

The mechanism is reusable for other reviewed successor registrants. The first version
automatically searches for unknown predecessor candidates during first-seen initialization,
but it does not automatically trust or adopt them as verified lineage members.

## 2. Problem statement

### 2.1 Current failure path

The current implementation has a single-CIK assumption at every relevant layer:

1. `SecClient.resolve_company()` resolves a ticker to one current `Company.cik`.
2. `SecClient._discover_inline_filings()` requests only
   `data.sec.gov/submissions/CIK{company.cik}.json`.
3. `_parse_columns()` builds every filing URL from `company.cik`.
4. `discover_annual_inline_filings()` and `discover_quarterly_inline_filings()` raise
   `DiscoveryError` when that one CIK cannot satisfy the count.
5. `CompanyIngestionService` rejects a stored ticker when the newly resolved SEC CIK differs
   from the stored `companies.cik`.
6. `EvidenceStore._upsert_company()` treats the ticker-to-CIK mismatch as unrelated
   ownership.
7. `EvidenceStore.list_mapping_inputs()` supplies the current company CIK to every filing
   period.
8. Direct Mapping rejects a fact when its context entity identifier differs from that current
   CIK, even when the fact correctly belongs to a predecessor filing.

This makes a successor-registrant transition look like missing filing history, a ticker
ownership conflict, or an XBRL entity mismatch depending on where execution stops.

### 2.2 CIK semantics

An SEC CIK is a filer identity. It is unique to a filer and is not recycled. A public company
can reorganize so that a different legal registrant with a different CIK becomes the public
successor under the same ticker. The product-level business lineage can therefore contain
multiple SEC filer identities.

Use these terms consistently:

| Term | Meaning | Example for XOM |
|---|---|---|
| Canonical company | The product-level company requested by ticker | XOM |
| Current CIK | The CIK currently returned for the ticker | `0002115436` |
| Filing registrant CIK | The lineage member responsible for a filing's reporting period | `0000034088` for earlier periods |
| Archive-owner CIK | The CIK directory under which a selected EDGAR filing URL was discovered | The CIK used in `/Archives/edgar/data/{cik}/...` |
| Accession prefix | The first ten digits of an accession number | Not an archive-ownership rule |

The filing registrant and archive owner are usually the same, but transition and co-filing
cases mean the data model must not require them to be identical. The accession prefix also
must not be used to infer either one.

### 2.3 Product behavior when history is incomplete

The phrase "output whatever we have" means:

- a count shortfall is a structured incomplete discovery outcome, not automatically a remote
  request failure;
- usable discovered filings may still be processed and retained;
- the CLI and Python result must disclose requested counts, found counts, CIKs searched, and
  why discovery stopped;
- on first-seen short discovery, the result must also disclose whether candidate search ran,
  any possible predecessor CIKs, their source evidence, observed eligible-filing counts, and
  pending review status;
- a partial default 5/12 window must not be presented as a complete Direct Mapping result;
- an incomplete refresh must not silently replace a better published local window; and
- zero usable filings for an initial company remains a failure.

## 3. Fixed design decisions

These decisions define `cik-lineage-v1`.

1. **Verified lineage only for production adoption.** Production ingestion traverses only
   checked-in relationships supported by an SEC source accession and URL. First-seen
   ingestion may discover and inspect an unknown predecessor candidate, but candidate filings
   remain advisory and cannot enter the production filing window until the relationship is
   reviewed and promoted into the verified catalog.
2. **First-seen lineage initialization and automatic traversal.** The first ingestion of a
   company resolves and persists its validated lineage before filing-window discovery; this
   lookup does not wait for a filing-count shortfall. Once a lineage is configured, callers
   do not need to request predecessor searching. Discovery still searches the current member
   first and fetches predecessor submissions only if the current member cannot fill the
   requested window. Unknown lineages initialize as single-CIK; if that first filing-window
   request remains short, initialization automatically runs the candidate-inspection path
   before returning its partial result.
3. **Current first.** Search the current registrant before predecessors and stop as soon as
   both requested windows are complete.
4. **One deep verified-filing discovery interface.** Callers request one combined
   annual/quarterly window.
   CIK traversal, SEC pagination, date filtering, deduplication, archive ownership, and
   completeness diagnostics stay behind that interface.
5. **Report-date lineage ranges.** A lineage member's effective range applies to
   `Filing.report_date`, not filing date or accession date.
6. **Accession is filing identity.** Deduplicate globally by accession after collecting
   candidates from all searched registrants.
7. **Archive ownership is explicit.** Build the selected source URL from the CIK submissions
   feed that exposed the filing. Never derive the archive directory from the accession
   prefix.
8. **Exact forms remain unchanged.** Only exact-form Inline XBRL `10-K` and `10-Q` filings
   consume positions. Amendments remain outside the active window.
9. **Annual selection remains fiscal-year based.** Keep the latest-filed eligible annual
   filing for each report-date year.
10. **Quarterly selection remains report-date based.** Keep the latest-filed eligible
    quarterly filing for each report date.
11. **Snapshots remain immutable.** New provenance is added through new rows and a versioned
    metadata shape. Historical snapshot payloads and artifacts are not rewritten.
12. **Current company identity remains singular.** `Company.cik` continues to mean current
    CIK at the public ingestion interface. Lineage-specific provenance travels in a discovery
    wrapper and is normalized into sidecar storage before ordinary filing processing.
13. **Verified transitions may update stored current identity.** A stored ticker/current-CIK
    mismatch is accepted only when the resolved lineage explicitly connects both CIKs.
14. **Unknown transitions remain untrusted.** Matching name, ticker, address, exchange, or
    former name is insufficient to merge two stored company identities. A source-backed
    possible predecessor may be stored as a pending candidate, but it is not a lineage member
    and cannot authorize a company merge or filing adoption.
15. **Incomplete evidence is not missing financial data.** Direct Mapping must fail preflight
    for an incomplete required window rather than persist seven missing results for absent
    filings.
16. **No worse active window.** A partial refresh may improve or preserve a published partial
    window, but it may not reduce existing coverage or replace a complete window.
17. **One canonical adoption seam.** After a discovered predecessor filing is registered to
    the current canonical `company_id`, Arelle extraction, evidence bundles, source documents,
    facts, contexts, report classification, and ordinary filing consumers use the existing
    schemas and interfaces. They do not branch on current versus predecessor CIK.
18. **A filing shortage is not an artifact substitution request.** Lineage traversal may fill
    absent positions in the requested filing window. After a filing is selected, a missing or
    unreadable primary document, taxonomy schema, or captured artifact remains a discovery or
    processing failure for that filing; ingestion must not substitute a predecessor filing for
    the failed artifact.

Here, "automatic" has three timing guarantees. First, every first-seen company ingestion invokes
`resolve_verified_lineage()` during company initialization and persists the validated result
before filing-window discovery. Second, if current-CIK discovery is insufficient, the same
request traverses all initialized predecessors without a caller flag or manual retry. It does
not mean that runtime ingestion trusts an unknown predecessor. Third, if the verified lineage
is still short during a company's first ingestion, the initializer automatically searches for
source-backed predecessor candidates, inspects whether their SEC submissions contain eligible
filings for the missing window, and persists the candidates for review. The schema supports
any validated multi-member lineage, while promotion of a newly discovered relationship remains
the reviewed process in sections 7.5 and 20.

## 4. Non-goals

This plan does not implement:

- automatic acceptance of a CIK relationship inferred from a company name or ticker;
- a web crawler that discovers arbitrary merger and reorganization histories;
- LLM-based lineage inference or approval;
- foreign-private-issuer forms such as `20-F` or `6-K`;
- IFRS mapping or foreign-issuer period semantics;
- economic comparability decisions across acquisitions, divestitures, or predecessor
  accounting;
- restatement or amendment selection;
- changes to the existing SEC extension-taxonomy 503 retry policy;
- broader SEC Company Facts ingestion;
- background scheduling; or
- a frontend lineage-management interface.

Foreign issuer and IFRS support is a separate product-scope expansion. This plan repairs
discovery for the existing exact-form Inline XBRL `10-K` and `10-Q` promise.

## 5. Current implementation seams and required changes

| Current module | Current assumption | Required change |
|---|---|---|
| `models.py` | `Company.cik` is the only CIK; `Filing` has no CIK provenance | Keep `Company` and core `Filing` stable; add a discovery-only wrapper carrying registrant, archive owner, and lineage version |
| `sec_client.py` | Discover one form from one CIK per call | Expose one combined filing-window interface and traverse a verified lineage internally |
| new candidate finder | No unknown-lineage inspection path | During first-seen short discovery, inspect bounded explicit SEC transition evidence and candidate submissions metadata without adopting filings |
| `evidence_ingestion.py` | `SecGateway` has separate annual and quarterly discovery methods | Replace them with the combined discovery result, normalize its provenance at run creation, then pass ordinary `Filing` objects to the existing processor interface |
| `company_ingestion.py` | Any ticker/CIK change is an ownership conflict | Permit only verified transitions and handle complete versus partial discovery explicitly |
| `storage/sql` | A company row owns one CIK; filing provenance is implicit in URL | Add additive verified-lineage, filing-identity, and advisory candidate-search tables with a legacy backfill |
| `evidence_store.py` | Filing metadata equality includes one source URL and mapping uses current company CIK | Persist lineage provenance, load it per filing, and validate verified identity transitions |
| `mapping_models.py` | `MappingSnapshotInput.company_cik` applies to every period | Replace it with the selected filing registrant CIK |
| `direct_mapping.py` | Context entity must equal current company CIK | Compare against the filing registrant CIK and invalidate old evaluation reuse |
| `report.py` and CLI output | Show only current company CIK | Retain current CIK at company level and expose per-filing lineage diagnostics where relevant |
| `ingestAndUpdate.txt` | Documents single-CIK discovery | Document current-first verified lineage traversal and incomplete-window behavior |

The SEC is a true external dependency. `SecClient` keeps its injected `fetch_json` callable as
the internal test seam; fixture-backed tests provide a mock adapter. Lineage validation and
window selection remain in-process and should be tested directly through their interfaces.

## 6. Target architecture

```text
ticker
  |
  v
SEC ticker resolution -------------------------> current Company
                                                     |
                                                     v
                                      resolve_verified_lineage(current_cik)
                                                     |
                                                     v
                                  first-seen lineage initialization
                                      and persistence in storage
                                                     |
                                                     v
                                      CompanyLineage current -> oldest
                                                     |
                                                     v
                                     discover_filing_window(...)
                                      |      |       |       |
                                      |      |       |       +-- completeness/gaps
                                      |      |       +---------- archive ownership
                                      |      +------------------ global accession dedupe
                                      +------------------------- SEC submissions/history
                                                     |
                             +-----------------------+-----------------------+
                             |                                               |
                             v                                               v
             annual DiscoveredFiling records            quarterly DiscoveredFiling records
                             |                                               |
                             +-----------------------+-----------------------+
                                                     |
                                                     v
                           canonical adoption / storage normalization
                                                     |
                                      +--------------+--------------+
                                      |                             |
                                      v                             v
                         unchanged core Filing             filing_lineage sidecar
                                      |                             |
                                      +--------------+--------------+
                                                     |
                                                     v
                              existing evidence ingestion and storage
                                                     |
                                                     v
                                     active-window publication policy
                                                     |
                                 complete 5/12 ------+------ incomplete
                                      |                         |
                                      v                         v
                              Direct Mapping             evidence + diagnostics
```

The external seam is the combined discovery interface. The canonical adoption seam ends
lineage-specific orchestration. Callers do not loop over lineage members, paginate SEC files,
infer URL owners, or calculate completeness themselves, and downstream evidence modules do
not distinguish current-CIK filings from predecessor-CIK filings.

For a first-seen incomplete result, the ingestion orchestrator runs the bounded
`LineageCandidateFinder` before returning: candidate evidence and metadata-only filing counts
flow to advisory candidate storage and diagnostics, never through canonical filing adoption or
evidence processing.

## 7. Lineage domain model

### 7.1 New module

Create:

```text
src/sec_inline_financials/company_lineage.py
```

The module owns:

- immutable lineage models;
- CIK normalization and validation;
- current-to-oldest ordering;
- date-range membership;
- relationship validation;
- the reviewed lineage catalog; and
- singleton fallback for unconfigured current CIKs.

Suggested models:

```python
@dataclass(frozen=True)
class LineageEvidence:
    accession: str
    url: str
    description: str


@dataclass(frozen=True)
class LineageRegistrant:
    cik: str
    role: Literal["current", "predecessor"]
    report_date_from: date | None
    report_date_through: date | None
    predecessor_cik: str | None
    relationship: str
    evidence: LineageEvidence | None


@dataclass(frozen=True)
class CompanyLineage:
    version: str
    current_cik: str
    registrants: tuple[LineageRegistrant, ...]

    def current_to_oldest(self) -> tuple[LineageRegistrant, ...]: ...
    def registrant_for_report_date(self, report_date: date) -> LineageRegistrant: ...
```

Use `None` only for an open start or end. A member with non-null `predecessor_cik` owns the
link to that predecessor and must carry the source evidence for that link. The oldest member
has no outgoing predecessor link. The current singleton fallback may omit relationship
evidence because it asserts no cross-CIK relationship.

### 7.2 Lookup interface

Expose one lookup function:

```python
def resolve_verified_lineage(current_cik: str) -> CompanyLineage:
    ...
```

Behavior:

- normalize the input to a zero-padded ten-digit CIK;
- return a validated catalog entry when the current CIK is configured;
- otherwise return a validated `singleton-v1` lineage containing only that CIK; and
- never perform network access.

The lookup is deterministic and safe to call for every company. The ingestion orchestrator
must call it when a company is first seen, before requesting that company's filing window.

### 7.3 Validation invariants

Validate every catalog entry at module load or first lookup:

1. `current_cik` and every member CIK contain exactly ten digits.
2. Member CIKs are unique.
3. Exactly one member has role `current`.
4. The current member CIK equals `CompanyLineage.current_cik`.
5. Closed date ranges have `from <= through`.
6. Member report-date ranges do not overlap.
7. Every date belongs to at most one member.
8. Every non-null `predecessor_cik` has a non-empty relationship, source accession, and HTTPS
   SEC URL on the member that owns the link.
9. Every `predecessor_cik` names another member.
10. The predecessor links form one connected chain.
11. The chain has no cycle.
12. `current_to_oldest()` contains every member exactly once.
13. The lineage version is non-empty and stable.

Raise `LineageValidationError` for invalid checked-in configuration. This is a programming or
release error, not an ordinary company-discovery outcome.

### 7.4 Initial XOM catalog entry

Add `_XOM_LINEAGE` keyed by current CIK `0002115436`:

| Role | CIK | Report-date range | Relationship |
|---|---|---|---|
| Current | `0002115436` | 2026-07-01 onward | Successor registrant; links to predecessor `0000034088` |
| Predecessor | `0000034088` | Through 2026-06-30 | Oldest member in this reviewed lineage |

The evidence should cite the SEC filing that states the July 1, 2026 redomiciliation and
successor-registrant relationship, for example:

```text
https://www.sec.gov/Archives/edgar/data/34088/000119312526291986/d70995d8k.htm
```

The catalog entry is reviewed configuration. `CompanyLineage`, validation, and discovery are
generic infrastructure.

### 7.5 Adding future lineage entries

A future entry requires:

1. current and predecessor ten-digit CIKs;
2. the effective report-date cutover;
3. an SEC filing explicitly establishing successor/predecessor status;
4. non-overlapping report-date ranges;
5. a catalog unit test; and
6. a live or captured-fixture discovery acceptance case.

Do not add an entry based only on equal company names, a reused ticker, a matching address,
an accession prefix, or `formerNames`.

### 7.6 First-seen initialization contract

Lineage lookup belongs to company initialization, not to an operator retry and not only to
the count-shortfall branch. When ingestion first resolves a ticker that has no stored
canonical company state, it must:

1. resolve the current SEC `Company`;
2. call `resolve_verified_lineage(company.cik)` immediately;
3. persist the canonical company and the entire validated lineage, including a singleton
   lineage, before filing-window discovery;
4. pass that initialized lineage into the combined discovery interface; and
5. record the initialized lineage version in the processing request even when the current CIK
   alone supplies the complete filing window.

Initialization discovers which reviewed lineage applies; it does not fetch every predecessor
unconditionally. Filing discovery still requests the current member first and requests an
initialized predecessor only when the selected annual or quarterly counts are short. A later
refresh re-resolves the catalog entry so a newer reviewed lineage version can be reconciled
through the verified transition rules.

If the first filing-window discovery remains short after exhausting that initialized lineage,
the same first-seen ingestion request must run the candidate-discovery and inspection workflow
in section 20 before returning. Persist each source-backed possible predecessor and its
observed eligible-filing counts as pending review. Candidate inspection is part of initial
lineage tracking, but a pending candidate does not alter `CompanyLineage`, contribute a
`DiscoveredFiling`, or authorize evidence processing.

## 8. Filing and discovery result models

### 8.1 Keep the core `Filing` model stable

Do not add lineage fields to the existing frozen `Filing` model. A predecessor filing must
become an ordinary filing before Arelle or evidence extraction sees it:

```python
@dataclass(frozen=True)
class Filing:
    accession: str
    filing_date: date
    report_date: date
    form: str
    primary_document: str
    url: str
```

This preserves the existing `EvidenceProcessor.extract_evidence(company, filing,
capture_area)` interface and avoids lineage-specific branches in Arelle processing, evidence
classification, narrative extraction, artifact capture, and report rendering.

Add a discovery-only wrapper:

```python
@dataclass(frozen=True)
class DiscoveredFiling:
    filing: Filing
    registrant_cik: str
    archive_owner_cik: str
    lineage_version: str
```

Definitions:

- `filing` is the unchanged core filing record consumed by the existing downstream pipeline;
- `registrant_cik` is the verified lineage member selected for `filing.report_date`;
- `archive_owner_cik` is the CIK submissions feed used to construct `filing.url`; and
- `lineage_version` records the decision policy used during discovery.

`DiscoveredFiling` exists only between SEC discovery and the storage normalization seam.
After the store attaches its provenance to a canonical `company_id`, downstream processing
uses `DiscoveredFiling.filing`. Keep `Company.cik` as the current canonical CIK and never
replace it with the predecessor CIK while extracting a predecessor filing.

### 8.2 Add structured discovery results

Add immutable result models, either in `models.py` or a focused discovery-model module:

```python
DiscoveryGapReason = Literal[
    "no_verified_predecessor",
    "verified_lineage_exhausted",
    "candidate_predecessor_pending_review",
    "no_lineage_candidate_found",
    "lineage_candidate_search_failed",
]


@dataclass(frozen=True)
class FilingWindowGap:
    form: Literal["10-K", "10-Q"]
    requested: int
    found: int
    reason: DiscoveryGapReason


@dataclass(frozen=True)
class FilingWindowDiscovery:
    company: Company
    lineage: CompanyLineage
    annual: tuple[DiscoveredFiling, ...]
    quarterly: tuple[DiscoveredFiling, ...]
    requested_annual: int
    requested_quarterly: int
    searched_ciks: tuple[str, ...]
    candidate_search_performed: bool
    lineage_candidates: tuple["LineageCandidateInspection", ...]
    gaps: tuple[FilingWindowGap, ...]

    @property
    def complete(self) -> bool: ...

    @property
    def lineage_version(self) -> str:
        return self.lineage.version
```

`complete` is true only when both selected counts equal their requested counts. The result
must never contain more than the requested count for either form.

Carrying the validated `CompanyLineage` lets the storage normalization seam persist member
ranges and source evidence without independently re-resolving configuration. Serialized run
and CLI summaries expose only its stable `lineage_version` and searched CIKs.

Count shortfalls are represented by `gaps`. SEC transport failures, malformed SEC payloads,
and invalid filing metadata remain exceptions.

### 8.3 Pending lineage candidate model

Candidate discovery needs a separate immutable result that cannot be confused with a verified
`LineageRegistrant` or production `DiscoveredFiling`:

```python
LineageCandidateStatus = Literal["pending_review", "rejected", "promoted"]


@dataclass(frozen=True)
class LineageCandidateInspection:
    current_cik: str
    candidate_predecessor_cik: str
    relationship: str
    effective_date: date | None
    evidence_accession: str
    evidence_url: str
    requested_annual: int
    eligible_annual: int
    requested_quarterly: int
    eligible_quarterly: int
    status: LineageCandidateStatus
```

The source evidence must explicitly connect the current and possible predecessor registrants.
The eligible counts come from a read-only inspection of the candidate CIK's SEC submissions
metadata using the same exact-form, Inline XBRL, date, and deduplication rules. Candidate
inspection must not create `Filing`, `filing_lineage`, snapshot, artifact, or active-window
rows. A candidate becomes eligible for production traversal only after review adds a versioned
catalog entry and a later initialization or refresh resolves that verified lineage.

### 8.4 Canonical adoption contract

The handoff from discovery to storage is the point at which a predecessor filing is adopted
into the current company pipeline. The contract is:

1. The wrapper identifies the current canonical `Company`, base `Filing`, filing registrant,
   archive owner, and lineage version.
2. The store resolves or creates one canonical `companies` row for the current ticker and
   current CIK.
3. The store inserts the base filing into the existing `filings` table with that canonical
   `company_id`, regardless of which verified lineage member filed it.
4. The store writes the lineage-specific identities to `filing_lineage`.
5. The processing run references the normal `filings.id`.
6. `EvidenceIngestionService` unwraps the base `Filing` and passes the unchanged current
   `Company` and base `Filing` to `EvidenceProcessor`.
7. Evidence snapshots, source documents, facts, contexts, units, validation messages,
   sections, report evaluations, and metric results continue using their existing filing and
   snapshot foreign keys.

Therefore a five-annual/twelve-quarter active window may contain filings from multiple
verified CIKs, but all seventeen filings belong to one canonical company row and occupy the
same rank, snapshot, fact, report, and mapping schemas used by a single-CIK company.

Do not copy, rewrite, or merge raw facts to make a predecessor CIK look like the current CIK.
Preserve the context entity identifier as filed. The `filing_lineage` sidecar establishes that
the historical registrant belongs to the canonical company, and the mapping input adapter
uses that filing registrant for entity compatibility.

### 8.5 Worked XOM adoption example

Assume `companies.id = 42` is the canonical XOM row after ticker resolution:

```text
companies
  id=42, ticker=XOM, cik=0002115436

company_registrants
  id=100, company_id=42, cik=0002115436, role=current
  id=101, company_id=42, cik=0000034088, role=predecessor
```

Discovery finds one current-CIK filing and an older filing needed for the requested window:

```text
DiscoveredFiling(
  filing=Filing(accession=current-accession, ...),
  registrant_cik=0002115436,
  archive_owner_cik=0002115436,
)

DiscoveredFiling(
  filing=Filing(accession=older-accession, ...),
  registrant_cik=0000034088,
  archive_owner_cik=0000034088,
)
```

Normalization writes both base filings under the same canonical company:

```text
filings
  id=500, company_id=42, accession=current-accession, ...
  id=501, company_id=42, accession=older-accession, ...

filing_lineage
  filing_id=500, registrant_id=100, archive_owner_cik=0002115436, ...
  filing_id=501, registrant_id=101, archive_owner_cik=0000034088, ...
```

The processing loop then calls the unchanged processor interface twice:

```python
processor.extract_evidence(current_xom_company, current_filing, capture_area)
processor.extract_evidence(current_xom_company, older_filing, capture_area)
```

Both filings create ordinary rows in the existing `evidence_snapshots`, `source_documents`,
`facts`, `contexts`, `units`, `filing_sections`, and report-evaluation tables. Those tables
continue joining through `filings.id` and therefore require no predecessor-specific schema.
The older facts retain entity identifier `0000034088` in their contexts. Direct Mapping joins
filing 501 to `filing_lineage`, obtains expected registrant `0000034088`, and applies the same
generic mapping resolver used for filing 500.

No later module treats the predecessor as a second requested company, changes its ticker,
duplicates its facts into current-CIK facts, or needs a separate annual/quarterly schema.

## 9. Combined SEC discovery interface

### 9.1 Replace the public discovery pair

Replace:

```python
discover_annual_inline_filings(company, count)
discover_quarterly_inline_filings(company, count)
```

with:

```python
def discover_filing_window(
    self,
    company: Company,
    lineage: CompanyLineage,
    *,
    annual_count: int,
    quarterly_count: int,
) -> FilingWindowDiscovery:
    ...
```

Update the `SecGateway` protocol and all callers in the same work package. Do not retain two
independent implementations that can make different lineage or pagination decisions. Thin
compatibility wrappers may exist during one commit only and should be removed once internal
callers migrate.

### 9.2 Filing-candidate provenance

Use a private candidate type while parsing SEC columns:

```python
@dataclass(frozen=True)
class _FilingCandidate:
    accession: str
    filing_date: date
    report_date: date
    form: str
    primary_document: str
    discovered_under_cik: str
```

Do not create the final `Filing.url` until the candidate has passed lineage eligibility and
deduplication. This prevents `Company.cik` from accidentally becoming the archive owner.

### 9.3 Discovery algorithm

For each request:

1. Validate positive annual and quarterly counts.
2. Accept the already initialized, validated `CompanyLineage` and verify that its
   `current_cik` equals `company.cik`.
3. Initialize global candidate maps keyed by accession.
4. Iterate lineage members in `current_to_oldest()` order.
5. Request `data.sec.gov/submissions/CIK{member.cik}.json`.
6. Parse recent filings and eligible historical submission files.
7. Keep exact-form Inline XBRL `10-K` and `10-Q` candidates only.
8. Keep candidates whose `report_date` belongs to the member's verified range.
9. Tag every candidate with `discovered_under_cik=member.cik`.
10. Add candidates to the global accession map. A current-first candidate wins a duplicate
    accession unless an earlier candidate is invalid.
11. Recompute the annual and quarterly selections after each submissions block.
12. Stop fetching older history files for a member when both requested windows are complete.
13. Stop traversing the lineage when both windows are complete.
14. Otherwise continue with the next verified predecessor.
15. Build an unchanged base `Filing` from each selected candidate.
16. Build a `DiscoveredFiling` wrapper around that base filing.
17. Set the wrapper's `registrant_cik` from the lineage member for the report date.
18. Set the wrapper's `archive_owner_cik` from `discovered_under_cik`.
19. Build the base filing URL as:

    ```text
    https://www.sec.gov/Archives/edgar/data/{int(archive_owner_cik)}/
      {accession-without-dashes}/{primary_document}
    ```

20. Return a complete result or explicit gaps after the lineage is exhausted.

The accession prefix is never an input to archive-owner resolution.

### 9.4 SEC history-file pruning

The current submissions response includes recent columns and may reference older JSON files.
Use the `filingFrom` and `filingTo` metadata when present to avoid fetching a history file
whose date range cannot contribute to a member's verified report-date range. Treat these
metadata fields as a safe skip optimization only; candidate `reportDate` remains the final
eligibility test.

Continue sequential SEC requests. Do not add threaded discovery or concurrent Arelle
processing in this change.

### 9.5 Deduplication rules

Deduplicate in this order:

1. accession number across all lineage members;
2. annual report-date year, keeping the latest filing date;
3. quarterly report date, keeping the latest filing date; and
4. final descending report-date order.

When one accession is exposed under more than one CIK submissions feed, retain the location
found first by current-to-oldest traversal and record that chosen archive owner. The storage
design leaves room for alternate filing locations later, but `cik-lineage-v1` requires one
deterministic processing URL.

### 9.6 Error semantics

Raise `DiscoveryError` for:

- an SEC request failure;
- a non-mapping submissions payload;
- missing filing metadata structures;
- an invalid required date or URL field on a selected filing; or
- a configured lineage member whose submissions response cannot be read.

Do not raise `DiscoveryError` solely because selected counts are below requested counts.
Represent that condition in `FilingWindowDiscovery.gaps`.

Do not silently convert a failed predecessor SEC request into
`verified_lineage_exhausted`; those conditions have different operational meanings.

Do not treat a missing primary document, extension-taxonomy schema, or captured source
artifact for an already selected filing as a filing-window shortfall. Report the applicable
discovery or processing failure for that filing. Predecessor traversal fills missing filing
positions; it does not replace associated artifacts belonging to a selected filing.

### 9.7 First-seen candidate discovery and inspection

Add a bounded, injected candidate-discovery interface used only when a company's first filing
window remains short after its verified lineage is exhausted:

```python
class LineageCandidateFinder(Protocol):
    def discover_and_inspect(
        self,
        company: Company,
        *,
        requested_annual: int,
        requested_quarterly: int,
        already_found_annual: int,
        already_found_quarterly: int,
    ) -> tuple[LineageCandidateInspection, ...]: ...
```

The production implementation must:

1. inspect a bounded set of SEC-filed source documents associated with the current registrant
   for explicit successor/predecessor language, both CIKs, and an effective date when stated;
2. reject name, ticker, address, accession-prefix, or `formerNames` similarity as sufficient
   evidence by itself;
3. normalize and validate every candidate predecessor CIK and exclude the current CIK;
4. deduplicate candidates by `(current_cik, candidate_predecessor_cik, evidence_accession)`;
5. inspect each surviving candidate's submissions metadata using the ordinary exact-form
   Inline XBRL eligibility rules;
6. record how many requested annual and quarterly positions the candidate appears able to
   supply, without downloading or processing its filing artifacts; and
7. return pending-review inspections only; never mutate `CompanyLineage` or emit a production
   `DiscoveredFiling`.

Candidate search is a first-seen initialization action and runs at most once for a given
company, current CIK, requested-window shape, and candidate-policy version unless an operator
explicitly requests a rescan. Persist the attempt even when it finds no candidate, so ordinary
refreshes do not repeat the same bounded search.

A candidate-search transport or parse failure must be visible as
`lineage_candidate_search_failed` while preserving any usable verified-lineage filings. A
pending candidate that appears able to fill at least one missing position produces
`candidate_predecessor_pending_review`; a successful search with no qualifying candidate
produces `no_lineage_candidate_found`. None of these outcomes turns an incomplete production
window into a complete one.

## 10. Ingestion integration

### 10.1 Update `SecGateway`

Change the protocol in `evidence_ingestion.py` to:

```python
class SecGateway(Protocol):
    def resolve_company(self, ticker: str) -> Company: ...

    def discover_filing_window(
        self,
        company: Company,
        lineage: CompanyLineage,
        *,
        annual_count: int,
        quarterly_count: int,
    ) -> FilingWindowDiscovery: ...
```

Fixture gateways can return one complete or partial immutable result without duplicating the
lineage algorithm.

Keep the existing `EvidenceProcessor` interface unchanged:

```python
class EvidenceProcessor(Protocol):
    def extraction_profile(self) -> ExtractionProfile: ...

    def extract_evidence(
        self,
        company: Company,
        filing: Filing,
        capture_area: Path,
    ) -> FilingEvidenceBundle: ...
```

Neither the production Arelle adapter nor a processor test double needs to understand
`CompanyLineage` or `DiscoveredFiling`.

### 10.2 Initialize lineage when a company is first seen

After `resolve_company()` returns the current SEC identity and before
`discover_filing_window()` performs any SEC submissions request:

1. Determine whether the ticker or any verified member CIK already belongs to a stored
   canonical company, and retain that decision as an immutable `first_seen` flag for the
   request before creating any row.
2. If no canonical state exists, call `resolve_verified_lineage(company.cik)` as part of the
   initial ingestion request.
3. Persist the new canonical company and every member of the validated lineage in one
   initialization transaction. Persist a singleton lineage as an affirmative initialization
   result rather than treating absence from the catalog as an error.
4. Pass the initialized `CompanyLineage` to the combined discovery operation; do not perform a
   second independent lookup inside the SEC pagination loop.
5. Search the current member first. Only if the requested annual or quarterly positions remain
   short may discovery request submissions for the next initialized predecessor.
6. If the current member supplies the complete window, retain the initialized lineage but do
   not fetch any predecessor submissions URL.
7. If the initialized verified lineage is exhausted while the first-seen request remains
   short, invoke `LineageCandidateFinder.discover_and_inspect()` before finalizing the partial
   result.
8. Persist the candidate-search attempt and every pending candidate inspection, then attach
   their summaries and the applicable candidate gap reason to `FilingWindowDiscovery`.

If initialization succeeds but SEC filing discovery later fails, retain the initialized
canonical company and lineage so a retry uses the same explicit identity state. Do not mark a
company's filing evidence complete until the ordinary discovery and publication rules do so.

For an existing company, re-resolve the catalog entry at refresh time and reconcile a changed
reviewed lineage version through the transition and storage rules below.

Inject the candidate finder into `CompanyIngestionService` separately from `SecGateway` so
fixture tests can exercise catalog traversal, candidate discovery, and candidate inspection
without conflating their trust levels. Candidate discovery must never be invoked for a
selected-filing artifact failure.

### 10.3 Verified current-CIK transition

Replace the unconditional stored ticker/CIK conflict with:

1. If no previous company exists, complete the first-seen initialization in section 10.2; no
   identity transition is required.
2. If `previous.company.cik == company.cik`, continue normally.
3. Otherwise use the current company's re-resolved, initialized verified lineage.
4. If the stored CIK is not a member, raise `IngestionError` as today.
5. If the stored CIK is a verified member, preserve the existing `company_id`, register all
   lineage members, and update `companies.cik` to the verified current CIK in the publication
   transaction.
6. If the verified current CIK already belongs to another company row, stop with a dedicated
   integrity error. Do not auto-merge two populated company rows.

The transition must occur inside the store transaction that persists the lineage, not as an
uncoordinated update in `CompanyIngestionService`.

Update `get_company_state_by_cik()` to resolve both `companies.cik` and registered lineage
member CIKs.

### 10.4 Normalize discovery before evidence processing

Add an orchestration method such as:

```python
EvidenceIngestionService.ingest_discovered_window(
    discovery: FilingWindowDiscovery,
) -> RunOutcome
```

Its implementation must:

1. pass the validated lineage and all `DiscoveredFiling` records to one store registration
   operation;
2. reuse the canonical company row initialized before discovery; only the lower-level
   explicit-filing compatibility path may create it here;
3. validate and idempotently re-register lineage members;
4. upsert every `record.filing` into the existing `filings` table under that canonical
   `company_id`;
5. upsert one `filing_lineage` row for each discovered filing;
6. create the processing run with the discovery summary;
7. iterate the discovered records, unwrap `record.filing`, and call the existing per-filing
   processing path; and
8. return the ordinary `RunOutcome` used today.

The store registration operation should return the run ID and registered filing identities
needed by the processing loop. Do not make the Arelle adapter, evidence extractor, report
classifier, or artifact store re-resolve lineage.

`EvidenceStore.save_snapshot()` obtains lineage provenance by joining the attempt's ordinary
`filing_id` to `filing_lineage`; the `FilingEvidenceBundle` does not need lineage-only fields.
This keeps facts and evidence schemas unchanged while allowing snapshot metadata v2 to record
the correct registrant and archive owner.

The existing lower-level explicit-filing path may continue to accept a base `Filing`, but it
must first register singleton or supplied provenance through the same store operation. There
must be no second filing-upsert path that omits `filing_lineage`.

### 10.5 Processing-run provenance

Add the following to `processing_runs.requested_window_json` when the selected window is
ingested:

```json
{
  "annual_count": 5,
  "quarterly_count": 12,
  "lineage_version": "xom-lineage-v1",
  "searched_ciks": ["0002115436", "0000034088"],
  "candidate_search_performed": false,
  "lineage_candidates": [],
  "discovery_complete": true,
  "gaps": [],
  "accessions": []
}
```

The actual accessions remain populated as today. Do not store SEC User-Agent values or other
credentials in the run metadata.

### 10.6 Partial-window behavior

Use this decision table after discovery:

| Previous published evidence | Discovery | Action |
|---|---|---|
| None | Complete and non-empty | Process and publish; normal `initialized` result |
| None | Partial with at least one filing | Run and persist first-seen candidate inspection; process and publish the usable verified partial window; expose incomplete and candidate diagnostics; skip complete-window mapping |
| None | Zero annual and zero quarterly filings | Run and persist first-seen candidate inspection, then raise `DiscoveryError` with searched CIKs, gaps, and candidate diagnostics |
| Existing partial | Complete | Process and publish the complete replacement |
| Existing partial | Partial but improves coverage or recency | Process and publish the non-regressing merged window |
| Existing partial | Partial and does not improve coverage | Preserve the published window and return the discovery diagnostic |
| Existing complete | Complete | Process and publish normally |
| Existing complete | Partial | Preserve the complete published window; optionally retain newly processed immutable snapshots without publishing a worse window |
| Any existing evidence | SEC request failure | Preserve current `refresh_failed_using_local_data` behavior |

For a partial result with existing evidence, form a candidate publication window from the
union of newly discovered filings and the current active filings. Select it using the normal
annual and quarterly rules. It is non-regressing only when, for each form:

- it contains at least as many active positions as the published form window; and
- its newest report date is not older than the published form window's newest report date.

Never delete historical filing or snapshot rows when preserving the older active window.

### 10.7 Publication interface

Change `EvidenceStore.publish_filing_window()` so that it accepts approved base `Filing`
lists plus the explicit discovery/publication decisions needed to publish a partial initial
window. It must not expose `DiscoveredFiling` to report, mapping, or evidence consumers.
Remove the current requirement that both annual and quarterly lists are non-empty.

The store must:

- validate exact forms for all supplied filings;
- validate unique accessions;
- assign ranks independently for each non-empty form;
- write `NULL` latest dates for a form with no active filing on an initial partial window;
- preserve or advance next-check dates according to explicit input policy;
- persist filing lineage links in the same transaction; and
- avoid clearing a prior active form window unless the caller explicitly supplies an approved
  replacement for that form.

Prefer an input that makes the decision explicit, such as separate `replace_annual` and
`replace_quarterly` booleans, rather than inferring destructive replacement from empty lists.

### 10.8 Result and CLI diagnostics

Extend `CompanyIngestionResult` with a compact discovery summary:

```python
discovery_complete: bool
requested_annual_count: int
requested_quarterly_count: int
found_annual_count: int
found_quarterly_count: int
searched_ciks: tuple[str, ...]
lineage_version: str
lineage_candidate_search_performed: bool
lineage_candidates: tuple[LineageCandidateInspection, ...]
discovery_gaps: tuple[FilingWindowGap, ...]
```

Keep existing request-status meanings. Completeness is a separate dimension from whether the
request initialized, updated, reused, or fell back to local evidence.

Progress output should include:

```text
Resolved XOM to current CIK 0002115436.
Initialized verified lineage xom-lineage-v1: 0002115436 -> 0000034088.
Searching current CIK 0002115436.
Current CIK supplied 0/5 annual and 1/12 quarterly filings.
Searching verified predecessor CIK 0000034088.
Selected 5/5 annual and 12/12 quarterly filings across 2 CIKs.
```

For an incomplete singleton:

```text
Selected 2/5 annual and 8/12 quarterly filings.
Initialized singleton lineage singleton-v1 for current CIK 0000000000.
No verified predecessor is configured for that current CIK.
Searching SEC-filed transition evidence for a possible predecessor.
No qualifying predecessor candidate was found.
Evidence is available, but the requested filing window is incomplete.
```

When a source-backed candidate is found, progress must state its CIK, evidence accession, and
observed eligible-filing counts, followed by an explicit message that the candidate remains
pending review and none of its filings entered the production window.

## 11. Storage migration and persistence

### 11.1 Add migration 0007

Create:

```text
src/sec_inline_financials/storage/sql/0007_cik_lineage.sql
```

Keep this migration additive so older evidence and foreign-key relationships remain intact.
Do not rebuild the heavily referenced `companies` or `filings` tables merely to rename a
column.

Continue interpreting `companies.cik` as the current CIK.

### 11.2 `company_registrants`

Add:

```sql
CREATE TABLE company_registrants (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    cik TEXT NOT NULL UNIQUE CHECK (length(cik) = 10),
    role TEXT NOT NULL CHECK (role IN ('current', 'predecessor')),
    report_date_from TEXT,
    report_date_through TEXT,
    predecessor_cik TEXT,
    relationship TEXT NOT NULL,
    evidence_accession TEXT,
    evidence_url TEXT,
    lineage_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (company_id, cik),
    CHECK (
        report_date_from IS NULL OR report_date_through IS NULL
        OR report_date_from <= report_date_through
    )
);

CREATE UNIQUE INDEX company_registrants_one_current_idx
    ON company_registrants(company_id)
    WHERE role = 'current';
```

Application validation enforces connected chains and source requirements. The unique CIK
constraint prevents one SEC registrant from being silently attached to two canonical
companies.

### 11.3 `filing_lineage`

Avoid a risky `filings` table rebuild by adding a required one-to-one lineage table:

```sql
CREATE TABLE filing_lineage (
    filing_id INTEGER PRIMARY KEY REFERENCES filings(id),
    registrant_id INTEGER NOT NULL REFERENCES company_registrants(id),
    archive_owner_cik TEXT NOT NULL CHECK (length(archive_owner_cik) = 10),
    lineage_version TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE INDEX filing_lineage_registrant_idx
    ON filing_lineage(registrant_id);
```

`filings.source_url` remains the deterministic processing URL. `filing_lineage` explains why
that URL and filing belong to the canonical company.

### 11.4 Candidate-search persistence

Persist first-seen candidate searches separately from trusted lineage membership:

```sql
CREATE TABLE lineage_candidate_searches (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    current_cik TEXT NOT NULL,
    requested_annual INTEGER NOT NULL,
    requested_quarterly INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('candidates_found', 'no_candidate_found', 'search_failed')
    ),
    error_summary TEXT,
    searched_at TEXT NOT NULL,
    UNIQUE (
        company_id,
        current_cik,
        requested_annual,
        requested_quarterly,
        policy_version
    )
);

CREATE TABLE company_lineage_candidates (
    id INTEGER PRIMARY KEY,
    search_id INTEGER NOT NULL REFERENCES lineage_candidate_searches(id) ON DELETE CASCADE,
    candidate_predecessor_cik TEXT NOT NULL,
    relationship TEXT NOT NULL,
    effective_date TEXT,
    evidence_accession TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    eligible_annual INTEGER NOT NULL,
    eligible_quarterly INTEGER NOT NULL,
    UNIQUE (search_id, candidate_predecessor_cik, evidence_accession)
);

CREATE TABLE lineage_candidate_reviews (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES company_lineage_candidates(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending_review', 'rejected', 'promoted')),
    catalog_version TEXT NOT NULL DEFAULT '',
    note TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (candidate_id, status, catalog_version)
);
```

These rows are advisory provenance. A candidate CIK must not appear in
`company_registrants`, `filing_lineage`, or the canonical company's production filing window
until a reviewed catalog version verifies the relationship. Persist only redacted error
summaries; never persist the SEC User-Agent.

### 11.5 Legacy backfill

Within migration 0007:

1. Insert one `legacy-singleton-v1` current registrant for every existing company, using
   `companies.cik` and open date bounds.
2. Insert one `filing_lineage` row for every existing filing.
3. Set the filing registrant to its company's singleton registrant.
4. Set `archive_owner_cik` to the company's current CIK because that is the only provenance
   the old schema represented.
5. Do not modify `filings.source_url`, snapshot JSON, or artifacts.

The runtime must tolerate `legacy-singleton-v1` provenance. A later verified refresh can add
predecessor members and use accurate provenance for newly discovered filings without
rewriting old snapshots.

### 11.6 Store registration interface

Add an internal operation such as
`EvidenceStore.initialize_company_lineage(company, lineage)` that creates or resolves the
canonical company and registers a validated `CompanyLineage` in one write transaction before
filing discovery. It must be safe to call with no filing rows and must return the canonical
company identity plus registered lineage-member identities for later discovery registration.
It must:

- upsert members by CIK;
- reject a member already attached to another company;
- verify that existing rows agree with immutable source and range fields;
- update timestamps only for semantically equal rediscovery;
- transition the single `current` role when the verified lineage changes the stored current
  CIK; and
- never delete predecessor rows during ordinary refresh.

Keep this operation internal to `EvidenceStore`; callers pass the validated lineage rather
than issuing table-level writes. The later filing-registration transaction reuses these
registered identities and remains idempotent if it validates the same lineage again.

Add a separate `record_lineage_candidate_search()` operation that atomically records the
attempt, its outcome, all `LineageCandidateInspection` rows, and their initial
`pending_review` events. Review and promotion append new status events rather than rewriting
earlier candidate evidence. The operation must reject any attempt to register those candidate
CIKs as trusted lineage members or attach their inspected filings to the canonical company.

### 11.7 Filing upsert behavior

Keep `_upsert_filing()` responsible for the existing core filing identity:

- canonical `company_id`;
- accession;
- form;
- filing date;
- report date;
- primary document; and
- chosen source URL.

Add `_upsert_filing_lineage()` in the same store module for:

- the registered lineage member;
- archive owner; and
- lineage version.

Call both inside the discovery-registration transaction. This is the normalization seam that
adopts a predecessor filing into the current schema: `filings.company_id` points to the
current canonical company, while `filing_lineage` preserves the historical identity needed
for provenance and entity validation.

An existing accession attached to a different canonical company or an incompatible filing
registrant remains a `FilingMetadataError`.

If the same accession is rediscovered through an alternate valid archive location, preserve
the existing processing URL in `cik-lineage-v1` and verify all other immutable metadata. Do
not rewrite an existing filing merely because current-first traversal found another SEC path.
If alternate locations need to become queryable later, add a separate `filing_locations`
table in a later migration.

### 11.8 Purge, audit, and compaction

Extend company-scoped maintenance to the new tables:

- company purge removes filing-lineage rows through filing deletion and removes registrants
  and candidate-search rows with the company;
- snapshot audit verifies every filing has one lineage row and that the registrant belongs to
  the filing's canonical company;
- database compaction retains the new foreign-key integrity checks; and
- recovery does not alter lineage configuration.

## 12. Snapshot metadata and immutable evidence

### 12.1 Metadata version 2

New snapshots use:

```json
{
  "version": "filing-metadata-v2",
  "accession": "...",
  "filing_date": "...",
  "report_date": "...",
  "form": "10-K",
  "primary_document": "...",
  "source_url": "...",
  "canonical_company_cik": "0002115436",
  "filing_registrant_cik": "0000034088",
  "archive_owner_cik": "0000034088",
  "lineage_version": "xom-lineage-v1"
}
```

Do not call the current CIK `company_cik` in v2 because that name is ambiguous.

### 12.2 Backward-compatible loading

`EvidenceStore.load_snapshot()` must:

- load v2 fields when present;
- load `filing_lineage` for the live filing row;
- interpret v1 `company_cik` as legacy captured company/registrant provenance;
- reconstruct the unchanged core `Filing` from the existing filing columns;
- retain lineage provenance in the store-side loaded record or snapshot metadata adapter when
  a caller explicitly requests it; and
- leave the stored snapshot JSON unchanged.

The loaded canonical company may reflect the current company CIK, while the raw contexts and
`filing_lineage` retain the historical registrant CIK. The ordinary `Filing` interface does
not need to carry that sidecar identity.

### 12.3 Payload and reuse identity

Include v2 lineage metadata in the semantic snapshot payload and captured metadata. A newly
processed predecessor filing must not reuse a semantically incompatible snapshot attributed
to the wrong registrant.

Do this in the store without adding lineage fields to `FilingEvidenceBundle`. Before
calculating snapshot identity, `save_snapshot()` joins the attempt's `filing_id` to
`filing_lineage` and builds a store-side semantic envelope:

```python
payload_hash_v2(
    bundle,
    canonical_company_cik=...,
    filing_registrant_cik=...,
    archive_owner_cik=...,
    lineage_version=...,
)
```

Use the same lookup and hash path in committed-attempt recovery and reuse checks. This keeps
the evidence extraction interface stable while making lineage attribution part of immutable
snapshot identity.

Existing v1 snapshots may still be reused when their filing-lineage row and legacy metadata
agree. If they disagree, fail closed with `FilingMetadataError`; do not silently relabel the
snapshot.

## 13. Direct Mapping integration

### 13.1 Per-filing entity identity

Rename:

```python
MappingSnapshotInput.company_cik
```

to:

```python
MappingSnapshotInput.registrant_cik
```

`EvidenceStore.list_mapping_inputs()` loads this value by joining `filing_lineage` and
`company_registrants` for each active filing.

Update `_compatibility_failures()` so a fact's normalized context entity identifier is
compared with `snapshot.registrant_cik`, not `companies.cik`.

This keeps a valid predecessor fact eligible while still rejecting a fact from an unrelated
entity.

### 13.2 Active-window identity version

Add `registrant_cik` to the canonical active-window hash input and version the serialized
shape, for example:

```json
{
  "version": "active-window-v2",
  "snapshots": []
}
```

This prevents reuse of an older metric evaluation whose entity-compatibility decision was
made against the current company CIK.

The metric rule definitions do not need to change, but existing `active-window-v1` hashes
must not match the new input identity.

### 13.3 Incomplete windows

For the default production path, retain the existing five-annual/twelve-quarter preflight.
When lineage discovery is incomplete:

- evidence snapshots may exist and be inspectable;
- Direct Mapping returns `MappingInputError` or is skipped with an explicit mapping error;
- no 119-cell complete evaluation is published; and
- absent filing periods are not converted into `mapping_not_found` results.

Custom non-default counts may continue to request a smaller explicit mapping window only when
both report kinds are non-empty and the caller deliberately disables the complete 5/12
requirement.

## 14. Error taxonomy and diagnostics

Add or use distinct errors for distinct failure classes:

| Error/outcome | Meaning |
|---|---|
| `LineageValidationError` | Checked-in lineage configuration violates invariants |
| `LineageCandidateSearchFailure` | First-seen advisory candidate search could not complete; preserve usable verified-lineage evidence and report the failure |
| `DiscoveryError` | SEC transport, response, or selected filing metadata failed |
| `FilingWindowGap` | SEC discovery succeeded but verified history did not satisfy a requested count |
| `IngestionError` | No usable initial evidence or an unverified ticker/CIK identity conflict |
| `FilingMetadataError` | Stored immutable filing or lineage provenance conflicts with rediscovery |
| `MappingInputError` | Published evidence cannot satisfy mapping preflight |

Every incomplete diagnostic should contain:

- requested ticker;
- current CIK;
- lineage version;
- ordered searched CIKs;
- requested and found counts by form;
- whether first-seen candidate search ran;
- candidate CIKs, evidence accessions, observed eligible counts, and review status when any
  candidate was found; and
- the applicable verified-lineage or candidate-search gap reason.

Never expose `SEC_USER_AGENT` in errors, progress, run JSON, tests, or documentation output.

## 15. Testing strategy

The combined discovery interface is the primary test surface. Tests should assert observable
selected filings, provenance, fetch order, completeness, persistence, and publication. Avoid
tests that depend on private loop structure.

### 15.1 `tests/test_company_lineage.py`

Cover:

- XOM current-to-oldest order;
- report-date lookup on June 30 and July 1, 2026;
- singleton fallback for an unknown current CIK;
- malformed and duplicate CIK rejection;
- zero or multiple current members;
- overlapping ranges;
- reversed ranges;
- missing predecessor source evidence;
- disconnected members;
- unknown predecessor reference; and
- cycle rejection.

### 15.2 `tests/test_lineage_candidate_discovery.py`

Use captured SEC source-document and submissions fixtures. Cover:

1. A first-seen short singleton window invokes candidate discovery before returning partial.
2. A first-seen complete verified window does not invoke candidate discovery.
3. An existing company's ordinary refresh does not repeat a recorded candidate search.
4. Explicit SEC successor/predecessor language containing both CIKs produces a pending
   candidate with source accession and URL.
5. Name, ticker, address, accession-prefix, or `formerNames` similarity alone produces no
   candidate.
6. Candidate inspection applies the ordinary exact-form Inline XBRL eligibility and
   deduplication rules and reports eligible annual and quarterly counts.
7. A pending candidate never appears in `CompanyLineage`, `DiscoveredFiling`, `filings`,
   `filing_lineage`, or the active window.
8. No-candidate and failed-search attempts are persisted and produce distinct diagnostics.
9. A selected-filing artifact failure never invokes candidate discovery.
10. After review adds a new catalog version, the next refresh promotes the relationship
    through verified lineage resolution and only then traverses the predecessor.

### 15.3 `tests/test_sec_client.py`

Use an injected dictionary-backed `fetch_json` mock adapter. Cover:

1. Current CIK alone completes the request and no predecessor URL is fetched.
2. XOM current history is insufficient, so the predecessor is fetched.
3. Combined XOM history produces exactly five annual and twelve quarterly filings.
4. Current-to-oldest fetch order is stable.
5. A singleton lineage returns partial evidence with `no_verified_predecessor`.
6. A configured lineage returns `verified_lineage_exhausted` when all members are short.
7. The June 30/July 1 XOM report-date cutover is correct.
8. Historical submissions files are fetched only as needed.
9. Duplicate accessions across feeds appear once.
10. Annual duplicate years keep the latest filing.
11. Quarterly duplicate report dates keep the latest filing.
12. An accession prefix different from the archive owner produces the correct URL.
13. SEC transport failure raises instead of returning a gap.
14. A malformed selected filing raises instead of being silently omitted.
15. Exact-form and Inline XBRL filtering remains unchanged.
16. Every selected wrapper contains an unchanged core `Filing` plus complete provenance.

### 15.4 `tests/test_evidence_store_lineage.py`

Cover:

- migration 0007 backfills one singleton registrant per legacy company;
- every legacy filing receives one lineage row;
- repeated initialization is idempotent;
- candidate-search attempts and inspections round-trip without creating registrant or filing
  lineage rows;
- repeated equivalent candidate-search persistence is idempotent;
- verified XOM transition preserves `company_id` while updating current CIK;
- unverified ticker/CIK conflict still fails;
- a CIK cannot belong to two canonical companies;
- predecessor filing save/load round-trips all provenance;
- predecessor and current filings share one canonical `companies.id`;
- predecessor facts, contexts, source documents, and snapshots use the existing tables and
  foreign-key paths without copied or rewritten values;
- v1 snapshot load remains compatible;
- conflicting accession provenance fails closed;
- purge removes lineage rows without affecting another company; and
- foreign-key audit remains clean.

### 15.5 `tests/test_company_ingestion_lineage.py`

Cover:

- first-seen configured lineage resolution and persistence occurs before the first SEC
  submissions request;
- first-seen singleton initialization is persisted as an explicit lineage result;
- a complete current-CIK window retains initialized lineage state without fetching a
  predecessor URL;
- a discovery failure after initialization retains the initialized company and lineage for a
  retry;
- first-seen incomplete verified discovery runs and persists candidate inspection before
  returning its partial result;
- a pending candidate is visible in diagnostics but contributes no production filing;
- complete lineage discovery and filing initialization;
- partial initial publication with explicit gaps;
- zero-filing initial failure;
- complete refresh replacement;
- partial refresh preserving a complete local window;
- improving a partial published window without regression;
- SEC failure using local data;
- processing-run JSON contains lineage provenance; and
- the processor receives the canonical current `Company` and unchanged base `Filing` for a
  predecessor discovery; and
- incomplete default discovery does not publish Direct Mapping.

### 15.6 `tests/test_direct_mapping_lineage.py`

Cover:

- a predecessor fact whose context CIK equals the filing registrant is eligible;
- the same fact is not compared with the current company CIK;
- an unrelated context CIK remains `entity_mismatch`;
- `registrant_cik` changes the active-window hash;
- an older evaluation is not reused under active-window-v2; and
- a partial default active window fails preflight without persisting missing metric results.

### 15.7 Live acceptance

Add a gated live inspection or acceptance command that:

1. uses the production SEC client and configured User-Agent;
2. resolves XOM to current CIK `0002115436`;
3. records traversal to predecessor `0000034088`;
4. selects the requested five annual and twelve quarterly exact-form Inline XBRL filings;
5. verifies every selected URL can be opened by Arelle or the existing SEC fetch path;
6. confirms stored per-filing registrant and archive-owner provenance;
7. confirms a predecessor period is not rejected as an entity mismatch; and
8. records accession lists and counts without recording credentials.

Also run a normal single-CIK company such as AAPL to prove that a complete current history
does not cause predecessor requests.

## 16. Implementation work packages

Each package should leave the repository testable. Prefer small commits in this order.

### P0. Capture the defect and freeze decisions

Files:

- `docs/designs/cik-lineage-ingestion-implementation-plan.md`
- SEC fixture data for the minimal current/predecessor XOM cases and one unconfigured
  candidate-discovery case

Deliverables:

- accepted terminology and fixed decisions;
- captured XOM current and predecessor discovery fixtures;
- captured explicit SEC transition evidence plus candidate submissions fixtures;
- no production behavior change.

Acceptance:

- fixtures contain no User-Agent or local paths;
- the current single-CIK behavior is reproducibly insufficient for the requested XOM window.

### P1. Add lineage domain model and catalog

Files:

- `src/sec_inline_financials/company_lineage.py`
- `src/sec_inline_financials/errors.py`
- `tests/test_company_lineage.py`

Deliverables:

- immutable models;
- strict validation;
- singleton fallback;
- reviewed XOM entry.

Acceptance:

- all lineage tests pass;
- no network or database dependency enters the lineage module.

### P2. Implement combined filing discovery

Files:

- `src/sec_inline_financials/models.py`
- `src/sec_inline_financials/sec_client.py`
- `src/sec_inline_financials/evidence_ingestion.py`
- `tests/test_sec_client.py`

Deliverables:

- one combined discovery interface;
- current-first verified traversal;
- discovery-only `DiscoveredFiling` wrappers around unchanged core `Filing` records;
- structured incomplete results;
- updated internal callers.

Acceptance:

- XOM fixtures complete 5/12 through two CIKs;
- an unknown lineage remains partial without inferred predecessors;
- a complete current CIK performs no predecessor fetch.

### P3. Add lineage persistence and migration

Files:

- `src/sec_inline_financials/storage/sql/0007_cik_lineage.sql`
- `src/sec_inline_financials/storage/evidence_store.py`
- `src/sec_inline_financials/storage/migrations.py` only if required
- `tests/test_evidence_store_lineage.py`

Deliverables:

- additive schema;
- legacy backfill;
- idempotent first-seen company-and-lineage initialization before filing discovery;
- candidate-search attempt and pending-candidate persistence isolated from verified
  registrants and filings;
- verified current-CIK transition;
- canonical adoption of predecessor filings under the current `company_id`;
- filing provenance round-trip;
- purge and audit integration.

Acceptance:

- a copy of a version-6 database migrates to version 7;
- migration is idempotent under normal initialization;
- a new configured company and a new singleton company each persist their lineage before any
  filing row is required;
- pending candidates persist without adding `company_registrants`, `filings`, or
  `filing_lineage` rows;
- old snapshots and artifacts are byte-identical;
- `PRAGMA foreign_key_check` is empty.

### P4. Integrate first-seen candidate inspection and partial-window publication

Files:

- `src/sec_inline_financials/company_ingestion.py`
- `src/sec_inline_financials/evidence_ingestion.py`
- `src/sec_inline_financials/lineage_candidate_discovery.py`
- `src/sec_inline_financials/storage/evidence_store.py`
- CLI summary formatting
- `tests/test_lineage_candidate_discovery.py`
- `tests/test_company_ingestion_lineage.py`

Deliverables:

- first-seen lineage initialization orchestration;
- bounded first-seen candidate discovery and read-only availability inspection after verified
  lineage exhaustion;
- review-gated candidate diagnostics and persistence;
- complete versus partial decision table;
- non-regressing publication;
- result and progress diagnostics;
- processing-run lineage metadata.

Acceptance:

- first-seen ingestion resolves and persists lineage before calling combined discovery;
- a complete current-CIK window does not fetch an initialized predecessor;
- a first-seen short verified window runs candidate inspection once before returning partial;
- pending candidate filings never enter evidence processing or publication;
- partial initial evidence is usable and visibly incomplete;
- incomplete refresh cannot replace a complete active window;
- a processor spy receives the unchanged current `Company` and core `Filing`, not lineage
  configuration or a `DiscoveredFiling` wrapper;
- SEC failures retain current local fallback behavior.

### P5. Correct snapshot and mapping identity

Files:

- `src/sec_inline_financials/storage/evidence_store.py`
- `src/sec_inline_financials/mapping_models.py`
- `src/sec_inline_financials/direct_mapping.py`
- mapping fingerprint helpers
- `tests/test_direct_mapping_lineage.py`

Deliverables:

- filing-metadata-v2;
- backward-compatible v1 loading;
- per-filing registrant checks;
- active-window-v2 identity;
- incomplete-window mapping preflight.

Acceptance:

- valid predecessor facts are selectable;
- unrelated entity facts remain excluded;
- old incompatible mapping evaluations are not reused.

### P6. Documentation and live acceptance

Files:

- `docs/designs/ingestAndUpdate.txt`
- `docs/evidence_storage_runbook.md`
- gated live inspection script or test

Deliverables:

- operational explanation of lineage and gaps;
- instructions for reviewing and adding a lineage entry;
- instructions for reviewing, rejecting, and promoting a persisted candidate;
- XOM and ordinary-company acceptance record.

Acceptance:

- documentation distinguishes reviewed configuration from reusable infrastructure;
- live XOM produces a complete requested window or a source-specific actionable failure;
- live AAPL remains single-CIK.

## 17. Definition of done

The feature is complete only when all of the following are true:

- XOM resolves to current CIK `0002115436`.
- The first ingestion of XOM resolves and persists `xom-lineage-v1` before filing-window
  discovery starts.
- The first ingestion of an unconfigured company persists an explicit singleton lineage
  before filing-window discovery starts.
- Discovery automatically searches verified predecessor `0000034088` when needed.
- A normal 5/12 request selects five annual and twelve quarterly filings when SEC history
  contains them across the verified lineage.
- Current-CIK history is always searched before predecessor history.
- A company with enough current-CIK history does not trigger predecessor requests.
- A first-seen unconfigured company whose verified window is short automatically runs one
  bounded candidate search before returning.
- A source-backed candidate records its possible predecessor CIK, evidence accession and URL,
  relationship, effective date when available, and observed eligible-filing counts.
- A pending candidate never becomes a verified predecessor, company registrant, production
  filing, or mapping input without reviewed catalog promotion.
- An unconfigured company never receives an inferred predecessor as a trusted lineage member.
- An incomplete lineage returns usable filings plus explicit counts and gap reasons.
- Exhausting initialized predecessors never turns absent filing periods into missing metric
  values or a falsely complete result.
- A missing primary document, taxonomy schema, or artifact for a selected filing remains a
  filing-specific failure and never triggers predecessor substitution.
- Zero usable initial filings still fail clearly.
- A partial refresh cannot replace a better complete active window.
- Every selected filing has queryable canonical-company, registrant-CIK, archive-owner-CIK,
  and lineage-version provenance.
- Current and predecessor filings in one selected window reference the same canonical
  `companies.id`; no predecessor-only company row is exposed to downstream consumers.
- A predecessor discovery is normalized to the unchanged core `Filing` before Arelle
  processing.
- Evidence snapshots, source documents, facts, contexts, units, filing sections, report
  evaluations, and metric results continue using the existing schemas and foreign-key paths.
- Raw predecessor facts and context entity identifiers are preserved as filed rather than
  rewritten to the current CIK.
- Archive URLs are not derived from accession prefixes.
- Verified stored old-CIK to current-CIK transition preserves the existing `company_id`.
- Unrelated ticker/CIK conflicts still fail.
- Existing snapshot and artifact bytes remain unchanged.
- Direct Mapping compares facts with each filing's registrant CIK.
- Incomplete default windows do not publish complete metric evaluations.
- Existing single-CIK ingestion, evidence storage, purge, audit, and mapping behavior remain
  passing.
- `pytest -q`, Ruff checks, formatting checks, and strict mypy all pass.
- The live XOM acceptance and a single-CIK regression are recorded.

## 18. Rollout and recovery

### 18.1 Rollout sequence

1. Merge lineage models and fixture tests without changing production discovery.
2. Merge combined discovery behind the internal `SecGateway` interface.
3. Ship migration 0007 and legacy singleton backfill.
4. Enable first-seen company-and-lineage initialization.
5. Enable first-seen candidate discovery, inspection, persistence, and diagnostics.
6. Enable verified transition and partial-publication behavior.
7. Enable mapping identity v2.
8. Run live XOM acceptance against a disposable evidence root.
9. Back up the production evidence database before its first version-7 initialization.
10. Run production XOM ingestion and audit the stored lineage, filings, snapshots, and mapping.
11. Run one normal single-CIK company regression.

### 18.2 Recovery properties

- Migration 0007 is additive and does not rewrite snapshot or artifact content.
- The legacy `companies`, `filings`, and `source_url` columns remain readable.
- A code rollback can ignore the new tables, although it will lose predecessor traversal.
- If a live transition updates `companies.cik`, the old CIK remains recoverable from
  `company_registrants`.
- An incorrect lineage entry is corrected by a new reviewed lineage version; do not mutate
  old snapshot metadata or processing-run records.
- Removing a bad catalog entry stops future traversal but does not delete already retained
  evidence. Purge remains an explicit operator action.

## 19. Performance and operational constraints

- The requested window is small: five annual and twelve quarterly filings.
- Search stops as soon as both windows are complete.
- SEC requests remain sequential and use the existing declared User-Agent.
- History-file date ranges may skip irrelevant files, but candidate report dates remain the
  correctness check.
- The catalog is finite and validated; no unbounded graph traversal is permitted.
- Unknown-lineage candidate discovery is bounded, first-seen only, and cached by company,
  current CIK, requested-window shape, and policy version.
- Candidate availability inspection fetches metadata only and never starts Arelle processing.
- Arelle processing remains sequential because sessions share process state.
- Existing immutable snapshot reuse prevents reprocessing unchanged predecessor filings.
- Progress must make predecessor traversal visible so a slow request is explainable.
- No lineage logic may broaden the existing three-additional-attempt taxonomy retry to other
  SEC requests.

## 20. First-seen candidate review and promotion

Candidate discovery and availability inspection are part of this implementation plan and run
inside the first ingestion request when the initialized verified lineage cannot complete the
requested window. Trust promotion remains deliberately separate from that automated search.

The review workflow is:

1. initialization generates and persists a candidate relationship, explicit SEC evidence,
   and observed eligible-filing counts;
2. a reviewer confirms both CIKs, the relationship, report-date cutover, evidence accession,
   and SEC URL, or marks the candidate rejected;
3. an accepted relationship is added as a new versioned catalog entry with unit and captured
   discovery fixtures through normal code review;
4. after that catalog version is deployed, the next refresh re-resolves the verified lineage,
   marks the matching candidate promoted, and may automatically traverse the predecessor; and
5. historical candidate-search, review-status, processing-run, snapshot, and artifact records
   remain immutable audit evidence.

The first-seen search may inspect explicit SEC language such as "successor registrant,"
effective dates, named predecessor entities, and explicit CIKs. It may not silently convert
heuristics into trusted company identity. A pending candidate is evidence for review, not an
authorization to ingest its filings.

## 21. Governing rule

The concise governing rule for this feature is:

> Automatically search known, verified predecessors when the current CIK cannot complete the
> requested filing window. Resolve and persist that verified lineage when the system first
> sees the company, but fetch verified predecessor submissions only when the current CIK is
> short. If the first-seen verified lineage remains short, automatically discover, inspect,
> and persist source-backed possible predecessors for review without adopting their filings.
> Do not promote unknown predecessors automatically or substitute predecessors for missing
> filing artifacts. Preserve the current company, filing registrant, archive owner, searched
> lineage, candidate evidence, review status, and completeness of the result as explicit
> provenance.
