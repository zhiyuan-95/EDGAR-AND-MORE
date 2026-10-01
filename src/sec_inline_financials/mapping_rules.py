from __future__ import annotations

import re

from sec_inline_financials.mapping_models import (
    ConceptCandidate,
    DirectMappingRuleSet,
    MetricRule,
)

DIRECT_MAPPING_RULE_VERSION = "direct-mapping-v2"
TARGET_METRIC_DEFINITION_VERSION = "target-metrics-v1"


def _candidates(*local_names: str) -> tuple[ConceptCandidate, ...]:
    return tuple(
        ConceptCandidate(namespace_family="us-gaap", local_name=local_name, rank=rank)
        for rank, local_name in enumerate(local_names)
    )


DIRECT_MAPPING_RULES = DirectMappingRuleSet(
    definition_version=TARGET_METRIC_DEFINITION_VERSION,
    mapping_rule_version=DIRECT_MAPPING_RULE_VERSION,
    metrics=(
        MetricRule(
            metric_key="revenue",
            display_name="Revenue",
            expected_period_type="duration",
            expected_unit_family="monetary",
            candidates=_candidates(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
            ),
        ),
        MetricRule(
            metric_key="operating_income",
            display_name="Operating Income",
            expected_period_type="duration",
            expected_unit_family="monetary",
            candidates=_candidates("OperatingIncomeLoss"),
        ),
        MetricRule(
            metric_key="net_income",
            display_name="Net Income",
            expected_period_type="duration",
            expected_unit_family="monetary",
            candidates=_candidates("NetIncomeLoss", "ProfitLoss"),
        ),
        MetricRule(
            metric_key="total_assets",
            display_name="Total Assets",
            expected_period_type="instant",
            expected_unit_family="monetary",
            candidates=_candidates("Assets"),
        ),
        MetricRule(
            metric_key="total_liabilities",
            display_name="Total Liabilities",
            expected_period_type="instant",
            expected_unit_family="monetary",
            candidates=_candidates("Liabilities"),
        ),
        MetricRule(
            metric_key="equity",
            display_name="Equity",
            expected_period_type="instant",
            expected_unit_family="monetary",
            candidates=_candidates(
                "StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                "PartnersCapital",
            ),
        ),
        MetricRule(
            metric_key="operating_cash_flow",
            display_name="Operating Cash Flow",
            expected_period_type="duration",
            expected_unit_family="monetary",
            candidates=_candidates(
                "NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ),
        ),
    ),
)

_US_GAAP_NAMESPACE = re.compile(
    r"^https?://(?:fasb\.org|xbrl\.us)/us-gaap(?:/[^/]+)?/?$", re.IGNORECASE
)


def namespace_family(namespace_uri: str) -> str | None:
    """Return a configured namespace family for an exact stored namespace URI."""
    if _US_GAAP_NAMESPACE.fullmatch(namespace_uri.rstrip("/")):
        return "us-gaap"
    return None
