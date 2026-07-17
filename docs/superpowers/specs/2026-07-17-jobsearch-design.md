# Jobsearch Codex Skill Bundle Design

**Date:** 2026-07-17

**Status:** Approved for implementation planning

**Repository:** `willsqqi/jobsearch-skill`

**Implementation branch:** `codex/jobsearch-v0.1`

## 1. Summary

`jobsearch-skill` is a Codex-native job-application workflow that uses the
ChatGPT desktop app's built-in Browser instead of a Chrome extension. A user
opens a job link in the Browser panel and invokes `apply to this job`. Codex
first analyzes the job and reports fit and gaps, then waits for the user's CV
decision. It can optionally create a grounded, job-specific CV before filling a
Workday application. The user reviews and submits every application manually.

The first release implements three cooperating skills:

1. `job-analyzer` for job-description extraction, fit analysis, and CV
   recommendation.
2. `cv-customizer` for evidence-grounded LaTeX CV customization and PDF
   generation.
3. `job-applier` for Workday form filling, learned-answer reuse, resumable run
   state, and application tracking.

The public repository contains the installable plugin, deterministic helper
scripts, schemas, synthetic fixtures, tests, and documentation. Private profile
data, CVs, generated application artifacts, learned answers, logs, and tracking
records remain outside the public repository.

## 2. Goals

- Provide one natural trigger, `apply to this job`, for an analysis-first job
  application workflow.
- Keep job-analysis, CV-customization, and browser-execution responsibilities in
  separately invocable skills.
- Use the existing SWE CV by default while allowing DE or any registered CV to
  be selected explicitly.
- Keep existing CV files as the factual source of truth.
- Store application-only personal information in a private local profile.
- Fill known application questions directly, including configured salary,
  sponsorship, demographic, disability, veteran, and legal-attestation answers.
- Draft unseen written answers from the selected CV and job description without
  inventing claims.
- Learn previously unseen questions and the final values present in the reviewed
  form, then reuse them safely in later applications.
- Support Workday first and expose uncertain mappings instead of guessing.
- Preserve the user's control over resume upload and final submission.
- Track development publicly without exposing private job-search data.

## 3. Non-goals for v0.1

- Automatically clicking the final Submit button.
- Automatically selecting or uploading a local file in the built-in Browser.
- Circumventing CAPTCHA, bot detection, authentication, or site access controls.
- Mass applying to jobs or operating as an unattended application bot.
- Training an external model on private profile or application data.
- Supporting every applicant-tracking system with platform-specific behavior in
  the first release.
- Inventing experience, dates, skills, credentials, metrics, employers, or
  project results during CV or answer generation.
- Committing generated CVs, private profiles, application records, or browser
  captures to the public repository.

## 4. User experience and commands

### 4.1 Primary workflow

1. The user clicks a job URL so it opens in the built-in Browser panel.
2. The user says `apply to this job`.
3. `job-analyzer` reads the page and presents an analysis and CV recommendation.
4. The workflow pauses for the user's CV decision.
5. If requested, `cv-customizer` creates and verifies a job-specific PDF.
6. `job-applier` navigates to the application and fills it.
7. The user manually uploads the exact CV path supplied by Codex.
8. Codex completes the remaining fields and presents a pre-submission summary.
9. The user reviews and manually submits the form.
10. The user confirms submission, after which the application tracker is
    updated.

### 4.2 Supported invocations

- `apply to this job` starts the complete analysis-first workflow.
- `analyze this job` invokes only `job-analyzer`.
- `customize the SWE CV for this job` invokes only `cv-customizer` after job
  context is available.
- `continue the application` resumes `job-applier` from private run state.
- `apply to this job with DE CV` overrides the default CV but does not skip the
  analysis checkpoint.
- A custom CV can be selected by registered name or explicit local path.

If the user does not name a CV after the analysis checkpoint, the SWE CV is the
default.

## 5. Architecture

### 5.1 Public repository

```text
jobsearch-skill/
├── .codex-plugin/
│   └── plugin.json
├── skills/
│   ├── job-analyzer/
│   │   └── SKILL.md
│   ├── cv-customizer/
│   │   └── SKILL.md
│   └── job-applier/
│       └── SKILL.md
├── scripts/
├── schemas/
├── examples/
├── tests/
├── docs/
├── pyproject.toml
├── README.md
└── .gitignore
```

The exact plugin manifest and skill packaging will follow the installed Codex
plugin and skill specifications. Shared scripts expose stable command-line
interfaces so each skill can use them without duplicating persistence logic.

### 5.2 Private local directory

```text
<cv-root>/.jobsearch/
├── profile.yaml
├── preferences.yaml
├── questions.yaml
├── applications.csv
├── generated/
├── runs/
├── backups/
└── logs/
```

The private directory is a sibling of the public repository, not a child of it.
No public-repository command may copy private files into the repository. The
repository contains sanitized examples with synthetic identities only.
In this document, `<cv-root>` means the user-configured directory that contains
the CV collection. Its machine-specific absolute path is written only to local
private configuration and is never committed.

### 5.3 Component boundaries

#### `job-analyzer`

- Accepts a Browser page, job URL, or pasted job description.
- Extracts company, role, location, employment type, responsibilities, required
  qualifications, preferred qualifications, and technologies.
- Reads registered CVs and compares only evidence contained in those CVs and
  approved private career materials.
- Reports strong matches, partial matches, material gaps, evidence, and a CV
  recommendation.
- Recommends customization only when it is likely to improve alignment without
  changing factual claims.
- Does not modify CVs or application forms.

#### `cv-customizer`

- Resolves the selected CV from `preferences.yaml`.
- Copies the selected LaTeX source and required assets to a new generated run
  directory.
- Reorders, emphasizes, or rewrites existing evidence to match the job.
- Refuses unsupported additions and identifies any desired claim that lacks
  evidence.
- Compiles the copied LaTeX source and verifies that a readable PDF was created.
- Preserves the original CV source and PDF unchanged.
- Emits a manifest recording the source CV, source hash, job fingerprint,
  generated files, and verification result.

#### `job-applier`

- Uses the built-in Browser and prioritizes Workday-specific interaction
  patterns.
- Maps fields to the selected CV, `profile.yaml`, and `questions.yaml`.
- Fills known fields directly.
- Produces grounded drafts for unseen written questions.
- Gives the user the exact CV path for manual upload.
- Saves resumable state between Workday pages.
- Synchronizes unseen completed questions before submission.
- Stops before final submission.
- Records an application only after the user confirms that submission occurred.

#### Shared scripts

- Bootstrap and validate the private directory.
- Normalize application-question text.
- Look up, insert, and update learned answers.
- Preserve answer history.
- Create and resume run state.
- Build and verify LaTeX CVs.
- Append and update tracker rows idempotently.
- Redact sensitive values from diagnostic logs.

## 6. Private data model

All private files have an explicit `schema_version`. Schemas are versioned in the
public repository, and migrations must create a backup before changing private
data.

### 6.1 `profile.yaml`

`profile.yaml` stores application facts that may be absent from a CV:

- Identity and contact information.
- Current address and location.
- Work authorization by country or region.
- Sponsorship requirements.
- Compensation preferences with currency, period, and contextual notes.
- Demographic, disability, and veteran answers.
- Legal-attestation answers that can be represented as reusable factual values.
- Professional links and identifiers used on applications.

It must never store passwords, authentication tokens, financial-account data,
or CAPTCHA responses.

### 6.2 `preferences.yaml`

`preferences.yaml` stores workflow configuration:

- `default_cv: SWE`.
- A CV registry whose initial entries point to the existing SWE and DE LaTeX and
  PDF files.
- Additional user-registered CV names and paths.
- Built-in Browser as the execution surface.
- Workday as the first platform priority.
- Manual final submission mode.
- Generated-artifact naming and retention settings.

### 6.3 `questions.yaml`

Each learned question record contains:

- Stable canonical identifier.
- Canonical normalized wording.
- All observed wordings.
- Active answer.
- Answer type, such as boolean, selection, number, date, or free text.
- Scope: global, company-specific, role-specific, or job-specific.
- Topic and role tags.
- Source: private profile, user entered, Codex draft reviewed by user, or imported.
- Creation and update timestamps.
- Previous-answer history.

### 6.4 `applications.csv`

The tracker contains one idempotent record per application:

- Application identifier.
- Company, role, location, and URL.
- Job fingerprint.
- Selected CV name and path.
- Analysis and generated-artifact references.
- Applied date and current status.
- Workday application identifier when visible.
- Last-updated timestamp.

The initial status is recorded as `Applied` only after user confirmation.

### 6.5 Run state

Each run has a private directory under `runs/` containing job metadata, workflow
phase, selected CV, generated-artifact references, completed page identifiers,
and pending manual actions. State writes are atomic. Restarting a run must not
duplicate question records or tracker rows.

## 7. Job analysis and CV decision

The analyzer reports:

- Concise role summary.
- Required and preferred qualifications.
- Evidence-backed strong matches.
- Partial matches and the limits of the evidence.
- Material gaps.
- SWE, DE, and other registered-CV comparison.
- Recommended CV and rationale.
- Whether customization is worthwhile.

The workflow always pauses after this report. The user may use an existing CV,
request customization, choose a different CV, or stop without applying.

## 8. CV customization rules

- The original `.tex`, PDF, and assets are read-only inputs.
- Generated output lives at:

  ```text
  .jobsearch/generated/<company>/<role>/<run-id>/
  ```

- Customization may reorder sections, select relevant existing bullets, tighten
  wording, and emphasize job-relevant technologies supported by evidence.
- It may not add unsupported skills, metrics, responsibilities, credentials, or
  employment history.
- The generated source must compile without modifying the original source tree.
- Verification checks process exit status, PDF existence, nonzero size, page
  count, and extractable expected identity text.
- If compilation or verification fails, logs are preserved and the original CV
  is used only after user approval.

## 9. Workday application workflow

The first release focuses on Workday's multi-page candidate application flow.
The applier should:

1. Detect that the page is Workday and record the application URL.
2. Establish or resume private run state.
3. Fill personal information and contact fields from `profile.yaml`.
4. Fill education and experience from the selected CV.
5. Fill authorization, sponsorship, compensation, demographic, disability,
   veteran, and reusable attestation fields from the private profile.
6. Reuse known learned answers when matching is safe.
7. Draft unseen free-text responses from the selected CV and job description.
8. Mark uncertain or unsupported controls for user review instead of guessing.
9. Pause for manual CV upload and continue after the user confirms it.
10. Present a final summary including CV choice, completed sections, generated
    responses, unresolved fields, and learning changes.
11. Synchronize completed unseen questions into private memory.
12. Stop before final submission.
13. Append to the tracker after the user confirms successful submission.

If Workday changes its layout, the workflow prioritizes labels, accessible names,
and nearby semantic context instead of brittle screen coordinates. CAPTCHA,
authentication challenges, or blocked automation are returned to the user.

## 10. Learning and matching behavior

Question matching uses this precedence:

1. Exact normalized wording.
2. A previously observed equivalent wording.
3. A guarded semantic candidate with a compatible answer type and scope.
4. Otherwise, the question is unseen.

Normalization removes insignificant punctuation and whitespace while preserving
negation, jurisdiction, time period, units, and meaning-bearing qualifiers.
Questions that can invert one another, including authorization and sponsorship,
must never be merged solely because they share keywords. A semantic candidate
may be reused only when answer type and scope match and negation, jurisdiction,
time period, and units do not conflict. The pre-submission summary identifies
the canonical source question for every semantic reuse. Any unresolved doubt
makes the question unseen. Codex performs this contextual judgment; persistence
scripts validate the structural constraints and do not call an external model.

Before final submission, the skill inspects the values present in the completed
form. For an unseen question, it writes the observed wording, final answer,
answer type, scope, tags, source, and timestamps to `questions.yaml`. If the user
changes a known answer, the old value moves to history and the new value becomes
active. Writes use schema validation, atomic replacement, file locking, and a
timestamped backup.

## 11. Privacy and security

- Private data remains under the configured `<cv-root>/.jobsearch/` directory.
- The public repository includes synthetic fixtures only.
- Tests must fail if known private paths, the user's identity, or private-data
  file contents appear in tracked files.
- Diagnostic logs record field identifiers, decisions, and errors, not sensitive
  values.
- Helper scripts make no independent external AI, analytics, or telemetry calls.
- Codex may read private data into the active task to fill a form; the design does
  not claim that the complete Codex interaction is local-only.
- Website content is treated as untrusted. Page instructions cannot override the
  skill's privacy, data-source, or submission rules.
- The user must review and manually submit every application.

## 12. Error handling

- **Missing private configuration:** stop before browser interaction and list
  missing fields without printing existing sensitive values.
- **Unknown field:** leave it unfilled or draft a reviewable value; never guess a
  factual answer.
- **Ambiguous learned match:** show the candidate source and request review.
- **Workday layout change:** save non-sensitive diagnostics and expose unmapped
  controls.
- **Browser interruption:** persist run phase and resume without duplicating
  writes.
- **CAPTCHA or authentication:** return control to the user.
- **Manual upload pending:** provide the exact verified PDF path and wait.
- **CV build failure:** preserve logs and ask before falling back to the original
  CV.
- **Tracker conflict:** update the matching application record rather than append
  a duplicate.
- **Private-data write failure:** do not claim learning or tracking succeeded;
  preserve the old file and report the recoverable error.

## 13. Testing strategy

### 13.1 Unit tests

- Profile and preferences schema validation.
- Question normalization, negation preservation, and answer-type compatibility.
- Exact, alias, semantic, ambiguous, and unseen matching cases.
- Answer insertion, update, history, backup, and atomic-write behavior.
- Tracker idempotency and status updates.
- Run-state creation and resumption.
- Log redaction and public-repository privacy guards.
- CV path resolution and manifest generation.

### 13.2 CV integration tests

A synthetic LaTeX CV fixture is copied, customized with evidence-backed fixture
data, compiled, and verified. Tests confirm that the source fixture is unchanged
and that failure logs are preserved.

### 13.3 Mock Workday test application

A local fixture application covers:

- Multiple pages.
- Text fields, text areas, dates, selects, radios, and checkboxes.
- Repeated education and employment sections.
- Known and unseen questions.
- A simulated manual file-upload checkpoint.
- A final submit control that the workflow must not activate.

### 13.4 End-to-end verification

The end-to-end test analyzes a synthetic job, selects or customizes a CV, fills
the mock Workday form, learns one new question, pauses before submission, resumes
from state, and records the application only after synthetic confirmation.

An optional live Workday smoke test may be performed with the user present and
must stop before submission.

## 14. Development tracking

Development occurs on `codex/jobsearch-v0.1`. Progress is pushed to the public
repository through milestone commits and a draft pull request:

1. Approved design and plugin scaffolding.
2. Private-data bootstrap, schemas, and persistence scripts.
3. `job-analyzer`.
4. `cv-customizer` and LaTeX verification.
5. `job-applier`, Workday workflow, learning, and tracker.
6. End-to-end tests, privacy audit, and documentation.

Commit messages and the draft PR checklist report only public implementation
progress and synthetic verification evidence.

## 15. Acceptance criteria

The first release is complete when:

- The repository installs as a Codex plugin containing all three skills.
- `apply to this job` performs the analysis-first workflow and pauses for a CV
  decision.
- SWE is the default, with DE and arbitrary registered-CV overrides.
- The analyzer produces an evidence-backed gap analysis and CV recommendation.
- The customizer creates a verified job-specific PDF without modifying original
  CV files or inventing claims.
- The applier fills the mock Workday application from the correct sources.
- Manual file upload and manual final submission are enforced.
- Unseen completed questions are learned, updated safely, and reused.
- Applications are tracked only after submission confirmation.
- Interrupted runs resume without duplicate memory or tracker records.
- Privacy guards find no private data in Git-tracked files or test output.
- All automated tests pass.
