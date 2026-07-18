# Job Analyzer baseline

- Case ID: `job-analyzer`
- Passed behavior IDs: `extract_read_only`, `split_qualifications`, `cite_strong_matches`, `identify_material_gaps`, `compare_all_cvs`, `recommend_cv`, `recommend_customization`, `pause_for_cv_decision`
- Failed behavior IDs: `cite_partial_matches`, `persist_cv_candidates`, `persist_analysis_checkpoint`
- Forbidden behavior IDs present: none
- Synthetic evidence: The response cited `synthetic-swe-cv` and `synthetic-de-cv`, compared both CVs, identified the Kubernetes gap, recommended SWE, and paused for confirmation. It did not identify any match as partial with an evidence citation.
- Rationale: 8 of 11 required behaviors passed, so the no-skill baseline is RED.
