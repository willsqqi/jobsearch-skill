# Task 7 report: final review hardening for verified CV artifacts

## Outcome

Closed the renewed Critical and Important Task 7 findings without changing the `jobsearch_skill.cv` public API or the required compiler argv. The final boundary adds strict portable source references, symlink-preserving registry validation, complete `\includegraphics` syntax consumption, bounded whole-process-group teardown, memory-first PDF validation and stale-output recovery, selected-CV binding, and exact repeat-preparation inventory/mode checks.

No browser, upload, application, review, submission, fallback-CV, or network-request behavior was introduced. The command-substitution boundary regression uses only inert local fake executables that touch local sentinel files.

## Witnessed RED/GREEN evidence

- Portable references and schemas: the new registry/prepare/schema slice was witnessed at 46 failed and 14 passed before the strict grammar. After implementation, the focused portable-reference collection passed 63 tests.
- Real compiler filename boundary: both backticked `curl` and `gs` names reached inert local sentinel helpers through real `latexmk` before the fix. Both are rejected before compiler execution after the fix; no network request exists in either helper.
- Graphics grammar: five malformed, unconsumed, absolute, or undeclared variants failed before implementation while four approved spacing/comment variants passed. All nine pass after full-consumption enforcement.
- Timeout lifecycle: the deterministic closed-pipe model proved the old cleanup sent only `SIGTERM` and returned while the group remained alive. The new code probes the group independently, escalates to `SIGKILL`, and keeps every wait bounded; both the modeled regression and a real forked TERM-ignoring child regression pass.
- PDF lifecycle: stale-output purge, pre-publication parsing, and page-extraction normalization each failed before implementation. The complete lifecycle slice, including malformed, empty, encrypted, no-text, and symlink output cases, passes after memory-first validation.
- Selected-CV binding: a consistently transplanted manifest/facts/evidence bundle reached the compiler before the fix. It now stops at `cv_manifest_mismatch` before facts/evidence loading or compiler execution.
- Repeat prepare: extra rc/sidecar/directory entries, public modes, and case-fold collisions produced eight witnessed failures before implementation. Exact file/directory inventory, case-folded namespace uniqueness, and `0700`/`0600` enforcement now reject each mutation, including nested directory mode and directory-prefix case collisions.
- Independent-review follow-ups: invalid compiler UTF-8, explicit files beneath nonportable parent directories, validated absolute registered roots, symlinked declared-file directories, and symlinked relative-root ancestors each received a witnessed RED regression and focused GREEN fix.
- Final focused registry/prepare/schema/build collection: 205 passed.

## Security and behavior verified

- Runtime and JSON schemas share an ASCII portable-component grammar for staged source references and basenames. Those compiler-relevant components reject whitespace, control characters, Unicode, shell metacharacters, leading dot/hyphen, trailing dot, traversal, and wrong or case-changed source-kind extensions.
- Explicit paths and absolute registered roots retain support for spaces, Unicode, and hidden parent components because parent spelling never becomes a compiler token. Relative registered roots remain portable refs; absolute roots are control-free paths.
- Registry and explicit paths retain their declared spelling through validation. Component-by-component directory opens, `lstat`, `O_NOFOLLOW`, `O_NONBLOCK`, regular-file checks, stable descriptor identity, and exact byte-count reads reject final or nested declared symlinks and special files without resolving through them.
- TeX permits only the restricted command, class, package, and environment surface. Every file-loading command occurrence must begin one complete approved `\includegraphics` production; legal whitespace, star, option, and stripped-comment variants remain supported only for declared PNG assets.
- Builds retain exactly `latexmk -pdf -interaction=nonstopmode -halt-on-error <tex-name>`, `shell=False`, captured output, the 120-second timeout, and the isolated private LaTeX environment.
- Timeout cleanup sends `SIGTERM`, waits only to a monotonic deadline, probes the whole process group with `killpg(pgid, 0)`, sends `SIGKILL` if any member remains, and performs only bounded drain/poll operations.
- Compiler pipes decode explicitly as UTF-8 with replacement, so arbitrary invalid bytes can only set value-free stdout/stderr presence flags and cannot escape structured diagnostics.
- Non-verified builds purge any stale output tree before compiler execution. Compiled bytes are parsed, encryption-checked, page-counted, text-extracted, identity-checked, and hashed in memory before the output directory is created or bytes are atomically published.
- Expected pypdf and page-extraction failures enter the closed `cv_pdf_verification` diagnostic path. Invalid bytes never appear at the persistent output path.
- A prepared manifest must match the current run ID, job fingerprint, and `selected_cv.name` before its facts/evidence artifacts are accepted.
- Repeat preparation requires an exact declared source-file inventory, exactly the needed parent directories, no symlink/special/extra entry, case-insensitive namespace uniqueness, destination/source/nested directory mode `0700`, and manifest/source file mode `0600`.
- The bootstrap preference seed now uses the schema-valid inert placeholder `UNCONFIGURED.pdf`; this preserves bootstrap behavior under exact extension validation.
- Failure logs remain value-free closed JSON. No raw stdout/stderr, exception value, private path, compiler sidecar, or unverified PDF persists.

## Fresh real PDF and filesystem QA

- Fresh QA root: `/private/tmp/jobsearch-task7-final-qa.eMW1oe`.
- Verified artifact: `.../output/resume.pdf`; one Letter page, 58,811 bytes, PDF 1.7, pdfTeX 1.40.27, not encrypted, no JavaScript, no forms.
- Rendered every page at 150 DPI with Poppler and inspected the only page at original resolution. Typography, hierarchy, alignment, spacing, margins, and glyphs are clear; there is no clipping, overlap, black box, broken rendering, or unreadable text.
- Original and prepared `resume.tex` SHA-256: `babd5b6593f6dda9ec819459d566e21789cf00815ca05f45d78740fac8e1c626`.
- Original and prepared declared `resume.pdf` SHA-256: `6a6da99c153904098fe8c59552a7447dc109325ee7d7d50590b60c55ecf94e62`.
- Verified output SHA-256: `965f3330f932693ab7911454c29e9beebf813f6f6461619a2a65b4d685155f6d`, exactly equal to `manifest.verification.pdf_sha256`.
- `source/` contains exactly `resume.tex` and `resume.pdf`; `output/` contains exactly the verified `resume.pdf`.
- Every generated directory, including LaTeX isolation directories, is `0700`; the manifest, trusted fixed rc, declared copies, and verified output are `0600`.
- No `.log`, `.aux`, `.fls`, `.fdb_latexmk`, or `.compiler-work-*` sidecar remains.

## Module and contract changes

- `cv_security.py`: portable reference/path predicates, exact extensions, complete graphics parsing, descriptor snapshots.
- `cv_registry.py`: non-resolving registry/explicit path validation and declared symlink rejection.
- `cv_prepare.py`: case-fold namespace validation plus exact repeat inventory and private modes.
- `cv_build.py`: selected-CV binding, bounded process groups, stale-output purge, memory-first PDF verification/publication.
- `preferences.v1`, `cv-manifest.v1`, `cv-evidence.v1`, and `cv-facts.v1`: portable source-kind reference contracts, with an absolute-root compatibility alternative where no path becomes a compiler token.
- `home.py`: schema-valid bootstrap PDF placeholder.
- Registry, prepare, schema, and build tests: adversarial RED/GREEN regressions for every reviewer finding.

## Delivery Verification

- Base feature commit: `688e625 feat: build verified tailored CV artifacts`.
- Prior review-fix commit: `9f2131c fix: harden verified CV build boundary`.
- Prior evidence commit: `8847144 docs: record Task 7 hardening verification`.
- Final implementation commit: `6824ad2dfe4fada0b49c3365b497ae062da82d66 fix: close final CV review findings`.
- Final delivery/report: this report commit immediately follows the implementation commit in branch history.
- Push target: `origin/codex/jobsearch-v0.1`.
- Independent Critical/Important-only review: no remaining Critical or Important findings. The reviewer rechecked every original boundary, exact argv, public facade/signatures, UTF-8 diagnostics, explicit paths, and absolute-root compatibility.
- Final focused suite: 205 passed in 13.59 seconds.
- Final full suite: 413 passed in 17.94 seconds.
- Independent reviewer verification: focused 205 passed; full suite 413 passed; Ruff and `git diff --check` clean.
- Final Ruff, diff, schema JSON, and tracked-diff privacy results: clean.
- Delivery verification: local `HEAD` and `origin/codex/jobsearch-v0.1` were checked for exact equality after push; the worktree was clean.
