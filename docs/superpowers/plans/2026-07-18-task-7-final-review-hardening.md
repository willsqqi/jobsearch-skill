# Task 7 Final Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final Task 7 filename-injection, process-lifecycle, pre-publication verification, run-binding, graphics-parser, symlink, and repeat-preparation findings without changing the public CV API or required latexmk argv.

**Architecture:** `cv_security.py` remains the single source of truth for portable references, descriptor input reads, and TeX grammar. `cv_registry.py` preserves declared paths through lstat/open validation; `cv_prepare.py` enforces an exact private staged inventory; `cv_build.py` owns selected-CV binding, stale-output recovery, memory-first PDF verification/publication, and bounded process-group teardown.

**Tech Stack:** Python 3.11+, pytest, jsonschema, PyYAML, pypdf, latexmk/pdfTeX, POSIX process groups, Poppler.

## Global Constraints

- Preserve exact argv: `latexmk -pdf -interaction=nonstopmode -halt-on-error <tex-name>`.
- Preserve `jobsearch_skill.cv` exports and `CVService` public signatures.
- Portable declared reference components use only ASCII letters, digits, underscore, dot, and hyphen; they cannot be empty, dot components, leading-dot, or leading-hyphen components.
- Declared source extensions are exactly `.tex`, `.pdf`, and `.png` by source kind.
- No unverified PDF, raw compiler sidecar, fallback CV, browser, upload, review, application, or submission action may persist.
- Every behavior change follows a witnessed RED test, minimal implementation, and focused GREEN run.

---

### Task 1: Portable references and symlink-preserving registry

**Files:**
- Modify: `tests/unit/test_cv_registry.py`
- Modify: `tests/unit/test_cv_prepare.py`
- Modify: `tests/unit/test_schema.py`
- Modify: `tests/integration/test_cv_build.py`
- Modify: `src/jobsearch_skill/cv_security.py`
- Modify: `src/jobsearch_skill/cv_registry.py`
- Modify: `src/jobsearch_skill/data/schemas/preferences.v1.schema.json`
- Modify: `src/jobsearch_skill/data/schemas/cv-manifest.v1.schema.json`
- Modify: `src/jobsearch_skill/data/schemas/cv-evidence.v1.schema.json`
- Modify: `src/jobsearch_skill/data/schemas/cv-facts.v1.schema.json`

**Interfaces:**
- Produces: `is_safe_ref(value: str) -> bool` as the canonical runtime portable-reference predicate.
- Consumes: registry preference `root`, `tex`, `pdf`, and `assets` declarations plus explicit `.tex`/`.pdf` paths.

- [x] Add parameterized schema/runtime RED cases for whitespace, control characters, Unicode, backticks, `$()`, quotes, semicolons, glob characters, leading dot/hyphen, invalid extensions, and case-fold collisions.
- [x] Add RED registry tests proving normal and explicit symlink file declarations are rejected while ordinary paths retain their declared absolute spelling.
- [x] Add a real-latexmk RED sentinel that injects backticked fake `curl`/`gs` helpers through a currently accepted TeX basename and proves the helper executes before the fix.
- [x] Implement one portable-component grammar in runtime and all CV reference schemas; apply source-kind extension checks at registry/snapshot boundaries.
- [x] Replace pre-validation `.resolve()` on declared/explicit files with preserved absolute paths plus `lstat`, `O_NOFOLLOW`, regular-file, and readability checks.
- [x] Run the exact new registry/schema/security/sentinel tests and confirm GREEN with the exact latexmk argv unchanged.

### Task 2: Fully consume graphics/file-loading grammar

**Files:**
- Modify: `tests/unit/test_cv_prepare.py`
- Modify: `src/jobsearch_skill/cv_security.py`

**Interfaces:**
- Produces: `validate_tex_bytes(data: bytes, asset_refs: set[str]) -> str` with a complete file-loading-command invariant.

- [x] Add RED cases for `\includegraphics *{...}`, spaces around star/options/braces, comment-newline variants, undeclared paths, and malformed extra tokens.
- [x] Expand the approved graphics regex to legal spacing after the control word and require every file-loading command occurrence to begin one fully matched approved graphics production.
- [x] Keep declared-asset, option, command, package, and path validation fail closed; run the graphics slice GREEN.

### Task 3: Bounded whole-process-group timeout teardown

**Files:**
- Modify: `tests/integration/test_cv_build.py`
- Modify: `src/jobsearch_skill/cv_build.py`

**Interfaces:**
- Produces: `_terminate_process_group(process)` that probes the process group independently of leader/pipe state and never waits without a bound.

- [x] Add a RED real-process test whose descendant ignores TERM, redirects/closes inherited stdout/stderr, survives leader exit, and attempts a delayed sentinel write.
- [x] After TERM, poll the group with `killpg(pgid, 0)` through the grace deadline; if any member remains, send SIGKILL regardless of `communicate()` result.
- [x] Drain/reap only with bounded waits, poll group disappearance after SIGKILL, and rerun both process-group timeout regressions GREEN.

### Task 4: Verify PDF bytes before publication and recover stale output

**Files:**
- Modify: `tests/integration/test_cv_build.py`
- Modify: `src/jobsearch_skill/cv_build.py`

**Interfaces:**
- Produces: an in-memory PDF verification result consumed by atomic output publication.

- [x] Add RED tests asserting the output path is absent when `PdfReader` starts, a page `extract_text()` `TypeError` becomes `cv_pdf_verification`, stale output is gone before a non-verified rebuild compiler starts, and failures leave no output/workspace/sidecars.
- [x] Purge the output tree at startup whenever the manifest is not verified.
- [x] Parse, decrypt-check, count, extract, identity-check, and hash `compiled_pdf_bytes` before creating the output directory or calling `copy_bytes_atomic`.
- [x] Normalize expected pypdf/page extraction exceptions into the existing structured failure path; publish only verified bytes; keep verified-rebuild digest checks.
- [x] Run the new PDF lifecycle tests and existing malformed/encrypted/no-text/symlink cases GREEN.

### Task 5: Bind prepared manifest to the run-selected CV

**Files:**
- Modify: `tests/integration/test_cv_build.py`
- Modify: `src/jobsearch_skill/cv_build.py`

**Interfaces:**
- Consumes: `RunState.data["selected_cv"]["name"]`.

- [x] Add a RED consistently transplanted manifest/facts/evidence CV-name bundle while the run retains its original selected CV; assert compiler never runs.
- [x] In `_prepared_manifest`, require a selected-CV mapping with a nonempty name exactly equal to `manifest.source_cv_name` before facts/evidence are loaded.
- [x] Run the transplant and existing rebinding cases GREEN.

### Task 6: Exact repeat-preparation inventory, modes, and case uniqueness

**Files:**
- Modify: `tests/unit/test_cv_prepare.py`
- Modify: `src/jobsearch_skill/cv_prepare.py`

**Interfaces:**
- Produces: `_verify_manifest_sources(destination, manifest)` as an exact source inventory and private-mode verifier.

- [x] Add RED repeat-prepare cases for extra `.latexmkrc`, compiler sidecar, extra directory, source file `0644`, source directory `0755`, manifest `0644`, and case-fold-colliding manifest source keys.
- [x] Reject duplicate case-folded source references during manifest semantic validation.
- [x] Enumerate the staged source tree without following links; require exactly the declared files and only their necessary parent directories.
- [x] Require destination/source/nested directories to be `0700` and manifest/declared files to be `0600`; reject rather than silently normalize repeat state.
- [x] Run all repeat/idempotence and initial-private-mode tests GREEN.

### Task 7: Final verification, independent review, report, and delivery

**Files:**
- Modify: `.superpowers/sdd/task-7-report.md`
- Modify: `docs/superpowers/plans/2026-07-18-task-7-review-hardening.md`
- Modify: this plan

**Interfaces:**
- Produces: pushed commits on `origin/codex/jobsearch-v0.1` and a clean worktree.

- [x] Run focused CV/registry/schema/build tests and the complete pytest suite from a clean test process.
- [x] Run Ruff, `git diff --check`, and tracked-diff privacy scans.
- [x] Build a fresh real PDF, inspect `pdfinfo`, render every page, visually inspect all pages, and audit hashes, `0700`/`0600` modes, source immutability, output placement, workspace cleanup, and sidecar absence.
- [x] Obtain an independent final code review with no Critical or Important findings.
- [x] Refresh both hardening plans and `.superpowers/sdd/task-7-report.md` with exact evidence and commit SHAs; explicitly stage the ignored report.
- [x] Commit implementation and delivery record, push `codex/jobsearch-v0.1`, verify local/remote SHA equality, and confirm a clean worktree.
