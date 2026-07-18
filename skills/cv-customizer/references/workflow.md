# Grounded customization workflow

## 1. Establish state and readiness

From the repository root, set the private home explicitly on every launcher invocation:

```bash
cd "<repo-root>"
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch validate --ready
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch run show --latest-open
```

Require `ok: true`. Accept either phase `analyzed` with `selected_cv: null`, or a recoverable phase `cv_selected` whose selected name matches the user's explicit choice, whose path is the registered LaTeX source, and whose `customized` flag is true. Confirm that the user's current message explicitly chooses a CV after seeing the analysis. If the choice is absent, ambiguous, or conflicts with a recoverable selection, stop and ask.

Resolve before mutating state:

```bash
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch cv resolve \
  --cv "<chosen-name>" --for-customization
```

Require a registered LaTeX source. A PDF-only CV cannot be customized.

## 2. Bind and promote grounded facts

If the run is still `analyzed`, select the resolved CV. If it is already the matching recoverable `cv_selected` state, do not select again. Then promote only its validated pre-selection candidate:

```bash
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch run select-cv \
  --run-id "<run-id>" --cv "<chosen-name>" --for-customization
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch cv facts \
  --run-id "<run-id>" --candidate
```

Both envelopes must be successful. The promotion revalidates current registered bytes, source hashes, and literal anchors before registering the selected evidence/facts pair.

## 3. Prepare without editing originals

Run:

```bash
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch cv prepare --run-id "<run-id>"
```

Read the returned private manifest. Record `source_hash` and every `source_hashes` entry. Independently hash the registered paths returned by `cv resolve`; each digest must equal the manifest before customization.

Read the immutable prepared TeX and selected CV facts/evidence. Do not edit either the registered source or `source/<expected_tex>`.

## 4. Construct the customization contract

Create one private mode-0600 JSON input under the run directory:

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "cv_name": "<exact registered name>",
  "source_hash": "<manifest source_hash>",
  "customized_tex": "<complete safe LaTeX document>",
  "claims": [
    {
      "claim": "<exact complete line newly added to customized_tex>",
      "evidence_anchors": ["<literal original CV anchor>"],
      "fact_refs": ["/employment/0"]
    }
  ],
  "unsupported_requirements": ["<job requirement lacking original evidence>"]
}
```

Keep the document class, packages, assets, identity, and factual content grounded in the prepared source. Reordering existing lines is allowed and may use `"claims": []` when it introduces no new non-comment line. Every new non-comment line must equal a `claims[].claim`; each claim must occur literally in `customized_tex`, every cited anchor must occur literally in original runtime evidence, and every `fact_refs` JSON pointer must identify the validated CV-fact record that owns that anchor. The runtime rejects any claim word, action verb, number, date, currency amount, or percentage absent from those fact records. Use `unsupported_requirements` to preserve gaps such as Kubernetes. Never cite the job description as proof of candidate experience.

Send the request through the runtime; do not write into the generated source tree directly:

```bash
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch cv customize \
  --run-id "<run-id>" --input "<private-customization.json>"
```

Require `status: grounded`. Retain the returned `customization_ref` and `customized_tex_ref`.

If a build leaves the manifest at `status: failed`, correct the private request and call `cv customize` again. The runtime safely replaces the failed customization and resets it to `prepared`; do not patch the generated source, ledger, or manifest yourself.

## 5. Build and verify

Run:

```bash
JOBSEARCH_HOME="<private-home>" ./scripts/jobsearch cv build --run-id "<run-id>"
```

Require process success, `ok: true`, `verified: true`, `status: verified`, a positive page count, and a nonempty `pdf_ref`. Read the manifest and require `status: verified`, matching customized-source SHA-256, `verification.verified: true`, `identity_present: true`, a positive page count, and `verification.pdf_sha256` equal to the exact output bytes.

Rehash every registered original and compare it to the pre-customization manifest entries. Any mismatch is failure even if a PDF exists.

## 6. Checkpoint and report

Create a private checkpoint input containing:

```json
{
  "target_phase": "cv_ready",
  "generated_artifacts": [
    "<manifest_ref>",
    "<customization_ref>",
    "<customized_tex_ref>",
    "<pdf_ref>"
  ]
}
```

Run `run checkpoint --run-id <run-id> --input <checkpoint.json>`, then `run show --run-id <run-id>`. Require phase `cv_ready`, the chosen CV selected with `customized: true`, and all four references present.

Return the exact absolute generated PDF path by joining the private-home root to `pdf_ref`. Summarize evidence-backed changes and unsupported requirements, state that originals are unchanged and the PDF is verified, and stop before application work.

## Failure handling

On any failure envelope, do not build further, checkpoint `cv_ready`, expose private content, or substitute the original CV. Runtime build failures persist a redacted log and leave the run in `cv_selected`; report only the fixed reason code and ask the user whether to retry or explicitly use an original CV.
