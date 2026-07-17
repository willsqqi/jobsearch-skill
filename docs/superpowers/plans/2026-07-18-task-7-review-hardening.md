# Task 7 Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every Task 7 review finding with a fail-closed LaTeX execution boundary, exact source/run rebinding, strict manifests, descriptor-based I/O, structured diagnostics, and focused modules.

**Architecture:** `cv.py` is the compatible public facade. `cv_models.py`, `cv_core.py`, and `cv_registry.py` own shared contracts, invariants, and selection; `cv_security.py` owns descriptor reads/copies, passive-asset and restricted-TeX preflight; `cv_evidence.py` owns evidence/facts binding; `cv_prepare.py` owns manifests and staging; `cv_build.py` owns isolated compilation, structured diagnostics, cleanup, and PDF verification.

**Tech Stack:** Python 3.11+, PyYAML, pypdf, latexmk/pdfTeX, pytest, Ruff, Poppler.

## Global Constraints

- Keep the exact build argv `latexmk -pdf -interaction=nonstopmode -halt-on-error <tex-name>`, `shell=False`, captured output, timeout 120.
- No rc execution, shell escape, Ghostscript, external network helper, active declared asset, raw compiler output, original absolute root, fallback CV, browser, upload, or submission.
- All private directories are `0700`, files `0600`; all mutations are contained and atomic.
- Facts/evidence/manifests bind exact run, CV, ordered path-safe source hashes, and deterministic aggregate hash.
- Preserve `jobsearch_skill.cv` public imports and `CVService` signatures.

---

### Task 1: Reproduce and close the executable LaTeX boundary

**Files:**
- Modify: `tests/unit/test_cv_prepare.py`
- Modify: `tests/integration/test_cv_build.py`
- Create: `src/jobsearch_skill/cv_security.py`

**Interfaces:**
- Produces: `validate_tex_bytes`, `validate_passive_asset`, `safe_read_bytes`, `safe_copy_bytes`, `isolated_latex_environment`.

- [x] Write parameterized tests for `.latexmkrc`, `latexmkrc`, `.sty/.cls/.tex/.lua/.pl/.sh` assets, invalid image magic, `\write18`, `\input`, `\csname`, `\catcode`, disallowed packages/classes, Ghostscript and curl helpers.
- [x] Run focused tests and capture RED from currently accepted active inputs and ambient environment.
- [x] Implement a restricted TeX command/environment/package allowlist, passive PNG assets, `O_NOFOLLOW` descriptor reads, digest-checked copies, and an isolated environment with controlled rc files, isolated HOME/TEXMF dirs, `shell_escape=0`, `openin_any=p`, and `openout_any=p`.
- [x] Prove exact argv is unchanged, malicious ambient/system/project rc sentinels are not executed, and shell/Ghostscript/network surfaces fail before subprocess.

### Task 2: Strengthen evidence/facts/run binding

**Files:**
- Modify: `tests/unit/test_cv_facts.py`
- Create: `src/jobsearch_skill/cv_evidence.py`
- Modify: `src/jobsearch_skill/data/schemas/cv-evidence.v1.schema.json`

**Interfaces:**
- Produces: `CVEvidenceStore.evidence`, `store_facts`, `load_bound_facts`.

- [x] Add RED tests that transplant valid evidence/facts across run IDs, CV names, source lists, aggregate hashes, extracted text, and evidence anchors.
- [x] Validate persisted evidence fields and exact re-extracted source records against the current run/selection; recompute aggregate hashes and reject any mismatch.
- [x] Before build, require facts to equal manifest run/CV/source hash/source hashes and revalidate all anchors against current prepared evidence.

### Task 3: Harden manifest and preparation atomicity

**Files:**
- Modify: `tests/unit/test_cv_prepare.py`
- Modify: `tests/integration/test_cv_build.py`
- Create: `src/jobsearch_skill/cv_prepare.py`
- Modify: `src/jobsearch_skill/data/schemas/cv-manifest.v1.schema.json`

**Interfaces:**
- Produces: `CVPreparer.prepare`, `load_bound_manifest`, `verify_prepared_sources`.

- [x] Add tests for empty/unsafe hash keys, copied-file inequality, wrong expected TeX, wrong aggregate, missing/changed idempotent copies, and root leakage.
- [x] Remove `source_root`; make path refs schema-safe and nonempty; enforce exact source-hash/copied-file equality and expected TeX membership.
- [x] Recompute aggregate hashes at every read; recursively chmod company/role/nested directories `0700`.
- [x] Read once with `O_NOFOLLOW`, extract/hash/copy those exact bytes, and verify post-copy digest.

### Task 4: Replace raw logs and isolate compilation

**Files:**
- Modify: `tests/integration/test_cv_build.py`
- Create: `src/jobsearch_skill/cv_build.py`

**Interfaces:**
- Produces: `CVBuilder.build` and allowlisted JSON diagnostic writer.

- [x] Add RED tests with split/tokenized path, identity, contact, and asset canaries in stdout/stderr and compiler sidecars.
- [x] Persist only fixed reason/stage, integer return code/page count, and boolean presence flags; never persist output text, exception values, or raw compiler sidecars.
- [x] Verify environment isolation, no rc sentinels, no unexpected executables, PDF regularity/text/identity/hash, private modes, and idempotent revalidation.

### Task 5: Preserve facade compatibility and verify

**Files:**
- Modify: `src/jobsearch_skill/cv.py`
- Modify: `src/jobsearch_skill/cli.py` only if delegation requires it
- Modify: `.superpowers/sdd/task-7-report.md`

**Interfaces:**
- Consumes all focused services; preserves `CVSelection`, `PreparedCV`, `CVEvidence`, `CVBuildResult`, `CVRegistry`, and `CVService` imports.

- [x] Move behavior behind focused modules and reduce `cv.py` to a 28-line facade.
- [x] Run 134 focused CV tests, full pytest (324 passed), Ruff, `git diff --check`, and a tracked-value/privacy scan.
- [x] Build a fresh synthetic PDF, run `pdfinfo`, render every page with `pdftoppm`, inspect all PNGs, and audit hashes/modes/sidecar cleanup.
- [x] Update the report, explicitly stage scoped files, commit a new fix commit, push `codex/jobsearch-v0.1`, and record the SHA.

### Renewed final-review closure (2026-07-18)

The follow-up implementation is specified in `2026-07-18-task-7-final-review-hardening.md` and preserves every constraint above.

- [x] Replace permissive CV refs with one strict ASCII portable grammar and exact `.tex`/`.pdf`/`.png` source-kind contracts.
- [x] Preserve declared paths through `lstat`/`O_NOFOLLOW` checks and reject registered or explicit symlink declarations.
- [x] Require every file-loading command to be one fully consumed approved `\includegraphics` production.
- [x] Probe and terminate the whole compiler process group independently of leader pipe state with no unbounded wait.
- [x] Purge stale non-verified output and validate compiled PDF bytes completely in memory before publication.
- [x] Bind the manifest/facts/evidence bundle to the run-selected CV name before compiler execution.
- [x] Enforce exact repeat-prepare inventory, case-fold namespace uniqueness, and `0700`/`0600` modes.
- [x] Run focused tests, the full suite, Ruff, diff/privacy checks, and a fresh real PDF render/filesystem audit.
- [x] Record the independent final Critical/Important-only review and pushed commit SHAs in `.superpowers/sdd/task-7-report.md`.
