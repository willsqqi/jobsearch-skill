# CV Customizer baseline

- Case ID: `cv-customizer`
- Passed behavior IDs: `evidence_backed_edits`
- Failed behavior IDs: `bind_approved_cv`, `resolve_customizable_source`, `preserve_originals`, `stage_generated_copy`, `persist_claim_evidence`, `compile_verified_pdf`, `return_exact_artifact`, `checkpoint_cv_ready`
- Forbidden behavior IDs present: none
- Synthetic evidence: The response proposed emphasizing the supplied Python API, SQL, CI, and reliability evidence and explicitly kept Kubernetes as a gap. It did not access the synthetic runtime, record hashes, stage or edit a generated source, persist a claim ledger, compile a PDF, or checkpoint a run.
- Rationale: 1 of 9 required behaviors passed, so the no-skill baseline is RED.
