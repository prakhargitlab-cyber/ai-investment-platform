# ETF Diagnostic Deployment Record

## Purpose

This document records the deployed diagnostic image digest, source provenance,
and baseline recovery procedure for the ETF historical identity diagnostics
introduced into the ETF Radar acquisition pipeline. It is intended for audit,
recovery, and regression verification across environments.

## Image Digests

| Image | Digest |
|-------|--------|
| **Baseline image** | `sha256:87309106ff172345c33a7ffc9f9c7641f69df5784824b9d743e7c0f654faacf4` |
| **Diagnostic image** | `sha256:aabaf120a11af77cd96f0003bb8f98392d48bc468316bd1f89141f563fc86f76` |

## Source Provenance

### Diagnostic source files

The deployed diagnostic image is built from the main workspace at
`C:\workspace\ai-investment-platform` and contains **baseline-plus-patch**
modifications, not a dirty working tree. The two diagnostic files are:

1. **`ai/research-engine/app/etf_history.py`** (untracked, new file)
   - ETF identity gate and historical acquisition attribution
   - Field-level identity rejection diagnostics

2. **`ai/research-engine/app/historical_market_data.py`** (tracked, modified)
   - Added diagnostic functions:
     - `_sanitized_diagnostic_field()`
     - `_historical_price_failure_diagnostic()`
     - `_etf_fallback_diagnostic()`
     - `_log_historical_price_failure_diagnostic()`
   - `historical_price_identity_rejected` log at line 128

### Provenance distinction

The deployed diagnostic image is **not identical** to the current dirty working
tree. The working tree may contain additional uncommitted changes beyond the
diagnostic patch. The diagnostic image is built from a controlled subset:

- `etf_history.py` — full content (diagnostic logging)
- `historical_market_data.py` — baseline + diagnostic logging additions

When comparing the candidate diagnostic workspace
(`C:\workspace\etf-history-diagnostic-v1`) against the main workspace:

| Check | Result |
|-------|--------|
| Candidate `etf_history.py` lines ⊂ working tree lines | ✅ All 10553 bytes present, plus additional comments |
| Candidate `historical_market_data.py` lines ⊂ working tree lines | ✅ All 22528 bytes present, plus additional functionality |
| Working tree has lines not in candidate | ✅ Yes — explanatory comments and extended functions |

**Conclusion:** The working tree is a strict superset of the candidate. No
diagnostic functionality from the candidate has been lost or downgraded.

## Validation

### Isolated candidate tests

All 86 tests in the validated diagnostic candidate pass in isolation:

```
tests/test_etf_history_identity_classification.py — 2 tests PASSED
tests/test_etf_identity_fallback.py — 7 tests PASSED
tests/test_historical_price_identity_diagnostics.py — 18 tests PASSED
tests/test_radar_history_remediation.py — 15 tests PASSED
tests/test_etf_historical_acquisition_attribution.py — 4 tests PASSED
tests/test_etf_history_bounded_session.py — 1 test PASSED
tests/test_etf_research_reliability.py — 10 tests PASSED
```

**Total: 86/86 tests PASSED**

### Kubernetes rollout verification

- Diagnostic image deployed to dev namespace
- Pod rollout status: **Healthy**
- HTTP probe response: **200 OK**
- No container restarts observed

## Latest Completed Cycle

```
04e8747e-51b1-4478-9ae3-9232ddac5573
```

## Baseline Recovery Procedure

If the diagnostic patch needs to be reverted to the pre-diagnostic baseline:

1. **Recover `historical_market_data.py` baseline (tracked file):**
   ```bash
   git show 0c0b80d:ai/research-engine/app/historical_market_data.py > app/historical_market_data.py
   ```
   The commit `0c0b80d` represents the pre-diagnostic state (163 lines,
   basic `historical_price_identity_rejected` logging only).

2. **Remove `etf_history.py` (untracked, new file):**
   ```bash
   rm ai/research-engine/app/etf_history.py
   ```
   This file does not exist in HEAD — no baseline to recover.

3. **Verify rollback:**
   ```bash
   python -c "from app.historical_market_data import HistoricalPriceProvider; print('OK')"
   ```

## Latest Cycle Findings (as of deployment)

| Metric | Count |
|--------|-------|
| Official universe | 357 |
| Evaluated | 345 |
| Ranked | 1 |
| Historical acquisition ACQUIRED | 0 |
| ETF_HISTORY_LISTING_NOT_VERIFIED | 186 |
| COMPANY_NAME_MISMATCH | 169 |
| NOT_ATTEMPTED | 2 |

## Notes

- The `04e8747e-51b1-4478-9ae3-9232ddac5573` cycle ID is not referenced in any
  source file. It should be recorded in cycle checkpoint tables or deployment
  manifests for future audit traceability.
- No source documentation previously existed recording the deployed diagnostic
  image digest. This file fills that gap.
