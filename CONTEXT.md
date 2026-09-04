# SEC Inline Financial Evidence

This context defines the language used to distinguish filing evidence from the financial metrics and reports built from it.

## Language

**Target Metric**:
A conventional financial measure that the project attempts to populate from filing evidence for an exact company and period.
_Avoid_: Matrix, metrice

**Metric Set**:
The seven Target Metrics in the current project scope: Revenue, Operating Income, Net Income, Total Assets, Total Liabilities, Equity, and Operating Cash Flow.
_Avoid_: All metrics, full metric set

**Observed Filing Fact**:
One XBRL fact occurrence present in a selected filing, retained regardless of whether it qualifies for the current report.
_Avoid_: Report fact, extracted value

**Report-Eligible Fact**:
An Observed Filing Fact that satisfies the current report's period, numeric, context, and validity rules.
_Avoid_: Valid fact

**Showcase Report**:
A finished, human-readable inspection artifact generated only when the user explicitly requests it. It is neither generated automatically during ingestion or updates nor used as an authoritative input to storage.
_Avoid_: Source of truth, complete filing record

**Frontend**:
The planned local browser interface for viewing the seven Target Metrics and their evidence, requesting evidence downloads, reviewing Mapping Recommendations, and initiating refreshes through the local backend.
_Avoid_: Showcase Report, direct database client

**Filing Window**:
The latest five selected Inline XBRL 10-K filings and the latest twelve selected Inline XBRL 10-Q filings for a company. The quarterly portion contains filed 10-Q periods rather than inferred Q4 periods.
_Avoid_: Complete filing history, twelve consecutive fiscal quarters

**Direct Mapping**:
A mapping resolved by matching a configured XBRL concept name to a fact for a Target Metric and exact period, without LLM inference.
_Avoid_: Mapping Recommendation, inferred concept match

**Reported Zero**:
An Observed Filing Fact whose reported numeric value is zero. It is a present value and does not trigger fallback to another concept.
_Avoid_: Missing value, empty value

**Mapping Recommendation**:
An LLM-proposed mapping produced when Direct Mapping cannot populate a Target Metric. It remains pending until a user approves it.
_Avoid_: Accepted mapping, final metric value
