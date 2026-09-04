from __future__ import annotations

from decimal import Decimal

from sec_inline_financials.models import (
    AnnualResult,
    Company,
    Fact,
    QuarterlyReportData,
    QuarterlyResult,
    ReconciliationIssue,
    ReportData,
)

_MISSING = "—"

ReportResult = AnnualResult | QuarterlyResult


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    return f"{value:,f}".rstrip("0").rstrip(".")


def _period_text(fact: Fact) -> str:
    if fact.period_start is None:
        return f"instant at {fact.period_end.isoformat()}"
    return f"{fact.period_start.isoformat()} to {fact.period_end.isoformat()}"


def render_report(report: ReportData) -> str:
    """Render the annual human-inspectable TXT artifact."""
    annual_results = sorted(report.annual_results, key=lambda result: result.fiscal_year)
    return _render_period_report(
        company=report.company,
        period_results=tuple((str(result.fiscal_year), result) for result in annual_results),
        title="ANNUAL INLINE XBRL DATA",
        period_summary="Fiscal years",
        period_kind="fiscal year",
    )


def render_quarterly_report(report: QuarterlyReportData) -> str:
    """Render the quarterly artifact using the same table and evidence contract."""
    quarterly_results = sorted(
        report.quarterly_results, key=lambda result: result.filing.report_date
    )
    return _render_period_report(
        company=report.company,
        period_results=tuple(
            (f"FY{result.fiscal_year} {result.fiscal_period}", result)
            for result in quarterly_results
        ),
        title="QUARTERLY INLINE XBRL DATA",
        period_summary="10-Q quarters",
        period_kind="quarter",
    )


def _render_period_report(
    *,
    company: Company,
    period_results: tuple[tuple[str, ReportResult], ...],
    title: str,
    period_summary: str,
    period_kind: str,
) -> str:
    periods = [period for period, _result in period_results]
    primary_by_period: dict[str, dict[str, Fact]] = {
        period: {fact.concept: fact for fact in result.primary_facts}
        for period, result in period_results
    }
    result_by_period = dict(period_results)
    dimensional_by_cell: dict[tuple[str, str], list[Fact]] = {}
    issue_by_cell: dict[tuple[str, str], ReconciliationIssue] = {}

    examples: dict[str, Fact] = {}
    for period, result in period_results:
        for fact in (*result.primary_facts, *result.dimensional_facts):
            examples.setdefault(fact.concept, fact)
        for fact in result.dimensional_facts:
            dimensional_by_cell.setdefault((fact.concept, period), []).append(fact)
        for issue in result.reconciliation_issues:
            issue_by_cell[(issue.concept, period)] = issue
    concepts = sorted(
        set(examples) | {concept for concept, _period in issue_by_cell}, key=str.casefold
    )

    evidence_ids: dict[tuple[str, str], str] = {}
    evidence: list[tuple[str, Fact, ReportResult]] = []
    for concept in concepts:
        for period in periods:
            primary_fact = primary_by_period[period].get(concept)
            if primary_fact is None:
                continue
            evidence_id = f"E{len(evidence) + 1:03d}"
            evidence_ids[(concept, period)] = evidence_id
            evidence.append((evidence_id, primary_fact, result_by_period[period]))

    dimensional_ids: dict[tuple[str, str], str] = {}
    dimensional_evidence: list[tuple[str, str, str, list[Fact]]] = []
    for concept in concepts:
        for period in periods:
            facts = dimensional_by_cell.get((concept, period))
            if not facts:
                continue
            evidence_id = f"D{len(dimensional_evidence) + 1:03d}"
            dimensional_ids[(concept, period)] = evidence_id
            dimensional_evidence.append((evidence_id, concept, period, facts))

    issue_ids: dict[tuple[str, str], str] = {}
    issue_evidence: list[tuple[str, str, ReconciliationIssue]] = []
    for concept in concepts:
        for period in periods:
            cell_issue = issue_by_cell.get((concept, period))
            if cell_issue is None:
                continue
            evidence_id = f"R{len(issue_evidence) + 1:03d}"
            issue_ids[(concept, period)] = evidence_id
            issue_evidence.append((evidence_id, period, cell_issue))

    first_column = "CONCEPT"
    first_width = max([len(first_column), *(len(concept) for concept in concepts)])
    cell_text: dict[tuple[str, str], str] = {}
    period_widths: dict[str, int] = {}
    for period in periods:
        values: list[str] = []
        for concept in concepts:
            table_fact = primary_by_period[period].get(concept)
            if table_fact is not None:
                value = f"{_format_decimal(table_fact.value)} [{evidence_ids[(concept, period)]}]"
            elif (concept, period) in issue_ids:
                value = f"CONFLICT [{issue_ids[(concept, period)]}]"
            elif (concept, period) in dimensional_ids:
                value = f"DIMENSIONAL [{dimensional_ids[(concept, period)]}]"
            else:
                value = _MISSING
            cell_text[(concept, period)] = value
            values.append(value)
        period_widths[period] = max([len(period), *(len(value) for value in values)])

    lines = [
        f"{company.name.upper()} ({company.ticker}) — {title}",
        f"CIK: {company.cik}",
        f"{period_summary}: {', '.join(periods)}",
        "Scope: reported numeric facts; missing values are shown as — and are never inferred.",
        "",
        "STRUCTURED DATA TABLE",
    ]
    header = first_column.ljust(first_width)
    for period in periods:
        header += f" | {period.rjust(period_widths[period])}"
    lines.append(header)
    lines.append("-" * len(header))
    for concept in concepts:
        row = concept.ljust(first_width)
        for period in periods:
            row += f" | {cell_text[(concept, period)].rjust(period_widths[period])}"
        lines.append(row)

    lines.extend(["", "EVIDENCE", "=" * 80])
    for evidence_id, fact, result in evidence:
        lines.extend(
            [
                f"[{evidence_id}] {fact.concept} — {fact.label}",
                f"Value: {fact.raw_value}",
                f"Period: {_period_text(fact)}",
                f"Unit: {fact.unit}",
                f"Decimals: {fact.decimals or 'not reported'}",
                "Dimensions: "
                + (", ".join(fact.dimensions) if fact.dimensions else "none (consolidated)"),
                f"Arelle validity: {fact.arelle_validity}",
                f"Reconciliation: {fact.reconciliation_note}",
                f"Filing: {result.filing.form} filed {result.filing.filing_date.isoformat()}",
                f"Accession: {result.filing.accession}",
                "",
            ]
        )

    lines.extend(["DIMENSIONAL FACT EVIDENCE", "=" * 80])
    if not dimensional_evidence:
        lines.extend(["None.", ""])
    for evidence_id, concept, period, facts in dimensional_evidence:
        lines.append(f"[{evidence_id}] {concept} — {period_kind} {period}")
        for index, fact in enumerate(facts, start=1):
            lines.extend(
                [
                    f"  Component {index}: {fact.raw_value} {fact.unit}",
                    f"  Period: {_period_text(fact)}",
                    f"  Dimensions: {', '.join(fact.dimensions)}",
                    f"  Context: {fact.context_id}",
                ]
            )
        lines.append("")

    lines.extend(["RECONCILIATION ISSUES", "=" * 80])
    if not issue_evidence:
        lines.extend(["None.", ""])
    for evidence_id, period, issue in issue_evidence:
        lines.extend(
            [
                f"[{evidence_id}] {issue.concept} — {period_kind} {period}",
                f"Reason: {issue.reason}",
                f"Candidate contexts: {', '.join(issue.candidate_context_ids)}",
            ]
        )
        for summary in issue.candidate_summaries:
            lines.append(f"Candidate: {summary}")
        lines.extend(["Resolution: quarantined; no table value was selected.", ""])

    lines.extend(["ARELLE VALIDATION MESSAGES", "=" * 80])
    for period, result in period_results:
        lines.append(f"{period_kind.capitalize()} {period} — {result.filing.accession}")
        if not result.validation_messages:
            lines.append("  None reported at warning-or-higher.")
        for message in result.validation_messages:
            lines.append(f"  [{message.level}] {message.code}: {message.message}")
        lines.append("")

    lines.extend(["CALCULATION RELATIONSHIPS", "=" * 80])
    for period, result in period_results:
        lines.append(f"{period_kind.capitalize()} {period} — {result.filing.accession}")
        if not result.calculation_relationships:
            lines.append("  None reported by Arelle.")
        current_role = None
        for relationship in result.calculation_relationships:
            if relationship.role != current_role:
                current_role = relationship.role
                lines.append(f"  Role: {current_role or '(unspecified)'}")
            lines.append(
                f"    {relationship.parent} = ({_format_decimal(relationship.weight)}) "
                f"{relationship.child}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
