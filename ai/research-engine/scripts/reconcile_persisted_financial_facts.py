"""Targeted, acquisition-free reprocessing of already-persisted official NSE
financial documents for a single instrument.

Purpose
-------
After a fix to app/financial_projection.py (or app/financial_metric_extraction.py),
an already-stored official NSE financial-result document may project
additional or corrected FinancialFact rows without requiring a new document
fetch. This script re-runs the existing extraction/projection logic against
documents already sitting in the database for one instrument, and -- only
with --apply -- writes the result back through the same reconciliation
primitive production uses.

Safety, by construction
------------------------
* Imports are limited to app.settings, app.persistence, app.models,
  app.financial_projection and app.structured_research. Nothing from
  app.fetcher, app.discovery, app.official_filing_discovery,
  app.official_shareholding_discovery or app.search_discovery_service is
  imported, and RepositoryImpl._refresh_live / the readiness "ensure" path
  is never called. There is therefore no code path in this module capable
  of issuing an NSE or Yahoo network request.
  tests/test_reconcile_persisted_financial_facts.py::test_module_imports_no_acquisition_machinery
  asserts this statically.
* Without --apply this is a pure read: it loads already-stored documents
  and facts, recomputes the projection, and prints a before/after diff. It
  calls no persistence write method at all.
* --apply writes exclusively through
  ResearchPersistence.reconcile_financial_facts_for_source -- the same
  atomic (single DB transaction), idempotent primitive
  RepositoryImpl._reconcile_persisted_official_financial_document uses in
  production for this exact purpose. It only ever touches rows whose
  source_identity is the one document being reconciled, and it only
  deletes a row when that row is both owned by this document AND inside a
  FinancialReconciliationScope the projection itself reported either
  COMPLETE_FOR_SOURCE_SCOPE (proven-complete evidence superseding the old
  value) or, distinctly, INVALIDATED_FOR_SOURCE_SCOPE (confirmed-unreliable
  extraction such as AMBIGUOUS_DECIMAL_SCALE, with no corrected value
  offered at all) -- it cannot delete facts owned by another source or
  document, cannot touch another instrument, and never deletes anything in
  dry-run mode (no deletes are issued without --apply).
* --document-id further narrows a run to one specific document if needed;
  --instrument-id is always required, so a run is never accidentally
  unscoped.

Usage
-----
Dry run (no writes), all eligible CORDELIA documents:
    python -m scripts.reconcile_persisted_financial_facts \
        --instrument-id 002e10ae-c9e7-4ac8-9b05-7ebb310140f0

Apply (writes via reconcile_financial_facts_for_source):
    python -m scripts.reconcile_persisted_financial_facts \
        --instrument-id 002e10ae-c9e7-4ac8-9b05-7ebb310140f0 --apply

Must be run with the same AIP_* environment the research-engine pod uses
(so it opens the same persistence backend/connection as the running
application), e.g. via `kubectl exec` into the live pod, or a one-off Job
using the same image/env and no other command.
"""
from __future__ import annotations

import argparse
import sys
from uuid import UUID

from app.financial_projection import nse_authority_rejection, project_semantic_financial_facts
from app.models import SourceClassification, SourceMode, SourceType
from app.persistence import ResearchPersistence, persistence_from_settings
from app.settings import Settings
from app.structured_research import official_financial_sector_ratio_facts


def eligible_official_documents(persistence: ResearchPersistence, instrument_id: UUID) -> list:
    """Already-persisted official NSE financial-result documents for one instrument.

    Mirrors the eligibility checks RepositoryImpl already applies before
    parsing/reconciling a document (see _financial_document_parse_unchanged
    and _reusable_financial_fact_reconcile_skip_reason in app/repository.py):
    REAL source mode, an EXCHANGE announcement, non-empty extracted text,
    and not an NSE-authority rejection. Reads already-stored rows only --
    no network access.
    """
    identities = persistence.load_document_identities(instrument_id=instrument_id)
    document_ids = [identity[0] for identity in identities]
    if not document_ids:
        return []
    documents = persistence.load_documents_by_ids(document_ids)
    return [
        document
        for document in documents
        if document.instrument_id == instrument_id
        and document.source_mode == SourceMode.REAL
        and document.source_type == SourceType.EXCHANGE_ANNOUNCEMENT
        and document.source_classification == SourceClassification.EXCHANGE
        and (document.normalized_text or "").strip()
        and not nse_authority_rejection(document)
    ]


def project_document_facts(document):
    """Pure extraction against already-stored document text. No I/O."""
    projection = project_semantic_financial_facts(document)
    facts = list(projection.facts) + list(official_financial_sector_ratio_facts(document))
    return facts, projection.scopes, list(projection.diagnostics)


def _summarize(facts) -> dict:
    out: dict = {}
    for fact in facts:
        out.setdefault(fact.key.metric, []).append(str(fact.value.value))
    return out


def run(
    persistence: ResearchPersistence,
    instrument_id: UUID,
    *,
    apply: bool,
    document_id: UUID | None = None,
    out=sys.stdout,
) -> int:
    documents = eligible_official_documents(persistence, instrument_id)
    if document_id is not None:
        documents = [d for d in documents if d.document_id == document_id]
    if not documents:
        print(f"NO_ELIGIBLE_DOCUMENTS instrument_id={instrument_id}", file=out)
        return 0

    existing_facts = persistence.load_financial_facts({instrument_id})
    total_written = 0

    for document in documents:
        before_owned = {
            fact.key: fact for fact in existing_facts if fact.source_identity == str(document.document_id)
        }
        facts, scopes, diagnostics = project_document_facts(document)
        if any(
            fact.key.instrument_id != instrument_id or fact.source_identity != str(document.document_id)
            for fact in facts
        ):
            print(f"document_id={document.document_id} outcome=SKIPPED reason=UNEXPECTED_FACT_IDENTITY", file=out)
            continue

        new_keys = {fact.key for fact in facts} - set(before_owned)
        changed_keys = {
            fact.key
            for fact in facts
            if fact.key in before_owned
            and (
                before_owned[fact.key].value.value != fact.value.value
                or before_owned[fact.key].value.unit != fact.value.unit
            )
        }
        removed_keys = set(before_owned) - {fact.key for fact in facts}

        print(f"--- document_id={document.document_id} published_at={document.published_at} "
              f"canonical_url={document.canonical_url}", file=out)
        print(f"  diagnostics: {diagnostics or '(none)'}", file=out)
        print(f"  before (owned by this document): {_summarize(before_owned.values())}", file=out)
        print(f"  projected (this run):             {_summarize(facts)}", file=out)
        print(f"  would add metrics:    {sorted(key.metric for key in new_keys)}", file=out)
        print(f"  would change metrics: {sorted(key.metric for key in changed_keys)}", file=out)
        print(f"  would remove metrics (owned by this doc, no longer projected): "
              f"{sorted(key.metric for key in removed_keys)}", file=out)

        if not apply:
            continue
        has_invalidation = any(getattr(scope, "completeness", None) == "INVALIDATED_FOR_SOURCE_SCOPE" for scope in scopes)
        if not facts and not has_invalidation:
            print("  APPLY: no facts extracted, nothing written", file=out)
            continue

        written = persistence.reconcile_financial_facts_for_source(
            instrument_id,
            str(document.document_id),
            facts,
            allow_official_revision=False,
            complete_scopes=scopes,
        )
        total_written += written
        print(f"  APPLY: reconcile_financial_facts_for_source wrote {written} row(s)", file=out)

    print(f"TOTAL rows written: {total_written}" if apply else "DRY RUN -- no database writes performed", file=out)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-id", required=True, type=UUID)
    parser.add_argument("--document-id", type=UUID, default=None,
                         help="Limit to one specific already-persisted document id (optional)")
    parser.add_argument("--apply", action="store_true",
                         help="Persist changes via reconcile_financial_facts_for_source. Omit for a dry run.")
    args = parser.parse_args(argv)
    settings = Settings()
    persistence = persistence_from_settings(settings)
    return run(persistence, args.instrument_id, apply=args.apply, document_id=args.document_id)


if __name__ == "__main__":
    sys.exit(main())
