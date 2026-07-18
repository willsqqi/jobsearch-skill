# Job Analyzer with skill

- Case ID: `job-analyzer`
- Passed behavior IDs: `extract_read_only`, `split_qualifications`, `cite_strong_matches`, `cite_partial_matches`, `identify_material_gaps`, `compare_all_cvs`, `recommend_cv`, `recommend_customization`, `pause_for_cv_decision`, `persist_cv_candidates`, `persist_analysis_checkpoint`
- Failed behavior IDs: none
- Forbidden behavior IDs present: none
- Synthetic evidence: The response separated required and preferred qualifications, cited strong and partial SWE and DE evidence, identified the Kubernetes gap, compared both CVs, recommended SWE with evidence-backed customization, treated DE only as a preference, and paused for an explicit choice.
- Persisted state: Independent verification found one private, schema-valid, source-bound evidence/facts pair for each of SWE and DE; all anchors occur literally in the inspected source. The validated analysis finished in phase `analyzed` with `selected_cv: null` and `generated_artifacts: []`.
- Rationale: 11 of 11 required behaviors passed and no forbidden behavior occurred, so the strengthened with-skill evaluation is GREEN.
