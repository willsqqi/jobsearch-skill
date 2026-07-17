# Task 7 report: review hardening for verified CV artifacts

## Outcome

Closed all Critical and Important Task 7 review findings. The CV pipeline now uses descriptor-bound input snapshots, exact run/CV/source evidence and facts binding, strict manifests, a restricted LaTeX boundary, private crash-recoverable compiler workspaces, process-group timeout termination, value-free diagnostics, and same-bytes PDF verification. `jobsearch_skill.cv` remains the compatible public API through a 28-line facade over focused modules.

## TDD and review evidence

- LaTeX/preparation hardening: 19 adversarial cases were RED before the restricted source/asset and descriptor boundary; all moved GREEN.
- Isolated compilation: ambient home/system/project rc and helper-surface cases were captured RED; the isolation slice moved GREEN with the exact argv unchanged.
- Evidence/facts/manifest rebinding: transplanted run, CV, aggregate hash, ordered source list, source text, copied-file mapping, expected-TeX, and expected-PDF cases were captured RED and moved GREEN.
- Structured diagnostics: tokenized stdout/stderr cases were RED because substitution redaction retained canary fragments; all moved GREEN with a closed JSON record.
- Reviewer regressions captured and closed: starred `\includegraphics`, duplicate/reordered evidence hashes, changed verified PDF, same-stem declared-PDF collision, child process survival after timeout, crash-unsafe source unlink, stale raw compiler workspace recovery, unsafe stale symlink handling, and option-like source references.
- Final focused CV/registry/schema/build collection: 134 passed.
- Final full suite: 324 passed.
- Ruff: `All checks passed!`; `git diff --check`: clean.
- Final nested reviewer verdict: no Critical or Important code findings remain; its independent full suite, Ruff, diff, and privacy checks were also green.

## Security and behavior verified

- Inputs are opened component-by-component with `O_NOFOLLOW` and `O_NONBLOCK` before rejecting special files. The exact opened bytes are validated, hashed, extracted, copied atomically, and digest-checked after copy.
- TeX accepts one `article` document with a deliberately narrow command/environment/package allowlist. Declared assets are CRC-valid PNG files only; active source/code assets, rc files, special files, symlinks, undeclared inputs, dangerous primitives, and option-like path components fail closed.
- Builds retain the required argument array `latexmk -pdf -interaction=nonstopmode -halt-on-error <tex-name>`, with `shell=False`, captured output, and a 120-second timeout.
- Compilation occurs in a mode-`0700` private workspace beneath the generated run root. Digest-checked TeX/assets are staged there; the manifest-bound source tree is never unlinked or overwritten. The verified artifact is atomically persisted separately at `output/<tex-stem>.pdf`.
- `latexmk` starts in a new POSIX session. Timeout handling terminates the entire process group, waits, escalates to `SIGKILL` when needed, and waits again before cleanup. A real stubborn-child regression proves no late write occurs.
- Normal workspace cleanup removes compiler sidecars on exit. At the next build, stale `.compiler-work-*` directories from an interpreter/host crash are validated without following links and removed before compiler execution; symlink/special entries fail closed.
- A private isolated HOME/XDG/TEXMF/TMP environment supplies a trusted rc that disables later automatic rc loading. Shell escape is disabled and TeX input/output policies are restricted. Ambient rc sentinels do not execute; the real build database showed no Ghostscript, `ps2pdf`, or curl helper.
- Evidence is rebound to the current run, CV name, deterministic aggregate, exact ordered source hashes, and text re-extracted from current bytes. Build repeats that check against prepared bytes and revalidates every facts anchor before compilation.
- Manifest reads validate schema plus exact source-hash/copied-file ordering, aggregate recomputation, path-safe expected TeX/PDF membership, semantic output location, and current copied-file digests. `source_root` is not persisted.
- Failure logs are closed JSON with only allowlisted reason/stage, integer return code/page count, and boolean presence flags. Raw stdout, stderr, exception values, paths, CV values, and compiler sidecars are not persisted.
- Verified rebuilds rehash every prepared input and require the existing output digest. PDF parsing, text extraction, identity checking, hashing, and final persistence all use one descriptor-read byte snapshot.
- No fallback CV, browser, upload, application, review, submission, or phase transition was introduced.

## Module structure

- `cv.py`: public compatibility facade.
- `cv_models.py`: public immutable result/selection models.
- `cv_core.py`: shared service dependencies and invariants.
- `cv_registry.py`: conservative CV resolution.
- `cv_security.py`: descriptor I/O, TeX/PNG validation, private modes, and isolated environment.
- `cv_evidence.py`: evidence extraction and facts binding.
- `cv_prepare.py`: staging and strict manifest persistence.
- `cv_build.py`: private compiler workspace, process-group lifecycle, diagnostics, recovery cleanup, and PDF verification.

## Fresh PDF and filesystem QA

- Fresh real build: `/tmp/jobsearch-task7-final-qa.Y62PV1`; verified artifact at `.../output/resume.pdf`. Render intermediates were removed after inspection.
- `pdfinfo`: pdfTeX PDF 1.7, Letter, one page, 58,811 bytes, not encrypted, no JavaScript.
- Rendered every page at 150 DPI and inspected the only page at original resolution. Text, hierarchy, alignment, margins, and glyphs are clear; no clipping, overlap, black boxes, or broken rendering.
- Copied `resume.tex` SHA-256: `babd5b6593f6dda9ec819459d566e21789cf00815ca05f45d78740fac8e1c626`.
- Declared source `resume.pdf` SHA-256: `6a6da99c153904098fe8c59552a7447dc109325ee7d7d50590b60c55ecf94e62`.
- Verified output PDF SHA-256: `ba5daf3a0cb07e87de949248cc74c0ffb0c57b4867ec2f55724ff00f60c1bd5e`, exactly equal to `manifest.verification.pdf_sha256`.
- Every generated directory is `0700`; the manifest, trusted fixed rc, declared source copies, and output PDF are `0600`.
- `source/` contains only the declared `resume.tex` and `resume.pdf`; `output/` contains the verified `resume.pdf`; no `.log`, `.aux`, `.fls`, `.fdb_latexmk`, or `.compiler-work-*` remains.

## Files changed

- Public/focused implementation: `src/jobsearch_skill/cv.py`, `cv_models.py`, `cv_core.py`, `cv_registry.py`, `cv_security.py`, `cv_evidence.py`, `cv_prepare.py`, `cv_build.py`.
- Contracts: `cv-evidence.v1.schema.json`, `cv-facts.v1.schema.json`, `cv-manifest.v1.schema.json`.
- Tests/fixtures: `tests/fixtures/latex-cv/evidence.json`, `test_cv_prepare.py`, `test_cv_facts.py`, `test_cv_build.py`, `test_schema.py`.
- Plan: `docs/superpowers/plans/2026-07-18-task-7-review-hardening.md`.

## Delivery

- Base commit: `688e625 feat: build verified tailored CV artifacts`.
- Review-fix commit: `9f2131c fix: harden verified CV build boundary`.
- Push target: `origin/codex/jobsearch-v0.1`.
