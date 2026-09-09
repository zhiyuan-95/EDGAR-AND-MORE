from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from arelle.ModelDocument import Type
from arelle.ModelValue import QName

from sec_inline_financials.evidence_extraction import detach_filing_evidence
from sec_inline_financials.evidence_models import ExtractionProfile
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.fingerprints import payload_hash, source_manifest_hash


class _Concept:
    def __init__(self, prefix: str, namespace: str, local_name: str, *, numeric: bool) -> None:
        self.qname = QName(prefix, namespace, local_name)
        self.type = SimpleNamespace(qname=QName("xbrli", "xbrli", "decimalItemType"))
        self.periodType = "instant"
        self.balance = None
        self.isNumeric = numeric
        self.isAbstract = False

    def label(self, **_kwargs: object) -> str:
        return self.qname.localName


def _fact(
    document: object,
    concept: _Concept | None,
    order: int,
    *,
    context: object | None,
    unit: object | None,
    value: object,
    numeric: bool,
    nil: bool = False,
    tuple_children: tuple[object, ...] = (),
) -> SimpleNamespace:
    qname = concept.qname if concept is not None else QName("bad", "urn:bad", "Unknown")
    return SimpleNamespace(
        modelDocument=document,
        objectIndex=order,
        qname=qname,
        concept=concept,
        context=context,
        contextID=getattr(context, "id", None),
        unit=unit,
        unitID=getattr(unit, "id", None),
        isNumeric=numeric,
        isNil=nil,
        isTuple=bool(tuple_children),
        modelTupleFacts=list(tuple_children),
        xValid=4,
        xValue=value,
        value=str(value) if value is not None else "",
        decimals="-6" if numeric else None,
        precision=None,
        xmlLang="en" if not numeric else None,
        id=f"fact-{order}",
        sourceline=order + 1,
    )


def test_detachment_covers_nested_remaining_nil_nonnumeric_and_undefined_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "filing.htm"
    source.write_bytes(b"<html>exact source bytes</html>")
    filing = Filing(
        accession="0000000123-25-000001",
        filing_date=date(2025, 2, 1),
        report_date=date(2024, 12, 31),
        form="10-K",
        primary_document="filing.htm",
        url="https://www.sec.gov/Archives/edgar/data/123/accession/filing.htm",
    )
    company = Company(ticker="TEST", cik="0000000123", name="Test Company")
    document = SimpleNamespace(
        uri=filing.url,
        filepath=str(source),
        type=Type.INLINEXBRL,
    )
    end = datetime.combine(filing.report_date, datetime.min.time()) + timedelta(days=1)
    context = SimpleNamespace(
        modelDocument=document,
        objectIndex=1,
        id="c1",
        isInstantPeriod=True,
        isStartEndPeriod=False,
        isForeverPeriod=False,
        startDatetime=None,
        endDatetime=end,
        instantDatetime=end,
        entityIdentifier=("https://www.sec.gov/CIK", company.cik),
        qnameDims={},
        segment=None,
        scenario=None,
    )
    unit = SimpleNamespace(
        modelDocument=document,
        objectIndex=2,
        id="u1",
        measures=((QName("iso4217", "iso4217", "USD"),), ()),
    )
    tuple_concept = _Concept("test", "urn:test", "Tuple", numeric=False)
    assets = _Concept("us-gaap", "urn:us-gaap", "Assets", numeric=True)
    name = _Concept("dei", "urn:dei", "EntityRegistrantName", numeric=False)
    nested = _fact(
        document,
        assets,
        4,
        context=context,
        unit=unit,
        value=Decimal("100"),
        numeric=True,
    )
    tuple_fact = _fact(
        document,
        tuple_concept,
        3,
        context=None,
        unit=None,
        value=None,
        numeric=False,
        tuple_children=(nested,),
    )
    nil_fact = _fact(
        document,
        assets,
        5,
        context=context,
        unit=unit,
        value=None,
        numeric=True,
        nil=True,
    )
    text_fact = _fact(
        document,
        name,
        6,
        context=context,
        unit=None,
        value="Test Company",
        numeric=False,
    )
    remaining = _fact(
        document,
        assets,
        7,
        context=context,
        unit=unit,
        value=Decimal("200"),
        numeric=True,
    )
    undefined = _fact(
        document,
        None,
        8,
        context=context,
        unit=None,
        value="unresolved",
        numeric=False,
    )

    def relationship_set(_arcrole: str) -> SimpleNamespace:
        return SimpleNamespace(modelRelationships=())

    model = SimpleNamespace(
        urlDocs={filing.url: document},
        facts=(tuple_fact, nil_fact, text_fact),
        factsInInstance=(nested, nil_fact, text_fact, remaining),
        undefinedFacts=(undefined,),
        contexts={"c1": context},
        units={"u1": unit},
        relationshipSet=relationship_set,
    )
    profile = ExtractionProfile(
        application_version="0.1.0",
        extractor_version="evidence-extractor-v1",
        arelle_version="2.41.7",
        validation_options=(("validate", "true"),),
        transform_plugin_revision="fixture",
        transform_plugin_hashes=(),
    )
    first = detach_filing_evidence(
        model,
        company=company,
        filing=filing,
        raw_log_json="{}",
        capture_area=tmp_path / "capture-one",
        extraction_profile=profile,
    )
    second = detach_filing_evidence(
        model,
        company=company,
        filing=filing,
        raw_log_json="{}",
        capture_area=tmp_path / "capture-two",
        extraction_profile=profile,
    )

    assert len(first.observations) == 6
    assert first.coverage_manifest.recognized_fact_count == 5
    assert first.coverage_manifest.unresolved_observation_count == 1
    assert first.coverage_manifest.nil_count == 1
    assert first.coverage_manifest.nonnumeric_count == 3
    assert any(observation.parent_fact_key for observation in first.observations)
    assert payload_hash(first) == payload_hash(second)
    assert source_manifest_hash(first) == source_manifest_hash(second)
    assert Path(first.source_documents[0].captured_path or "").read_bytes() == source.read_bytes()
