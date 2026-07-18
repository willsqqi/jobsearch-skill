---
name: cv-customizer
description: Use when a user explicitly chooses a registered LaTeX CV after job analysis and asks to tailor, customize, or optimize that CV for the analyzed job.
---

# CV Customizer

Create an evidence-grounded private CV variant, compile it through the isolated runtime, and stop with a verified `cv_ready` artifact.

Read [references/workflow.md](references/workflow.md) completely before starting.

## Boundary

- Require an explicit post-analysis CV decision. Do not infer selection from the original analysis request.
- Modify neither the registered CV nor the prepared immutable source snapshot. Submit the complete customized TeX and claim ledger through the runtime so it creates a separate generated source.
- Every rewritten claim must occur literally in the customized TeX, cite literal anchors from the original CV evidence, and bind those anchors to exact records in the validated `cv-facts.v1` document.
- Never add, soften, or imply unsupported experience. Keep requested Kubernetes experience as a gap unless it exists in the original evidence.
- Do not open an application, upload a file, or fall back to an original CV after failure.

## Workflow

1. Validate the private runtime and require one matching analyzed run, or resume its recoverable `cv_selected` customization state.
2. Resolve the chosen CV with `--for-customization`; bind it when still analyzed, then promote only that CV's pre-selection facts.
3. Prepare the generated workspace and record the manifest's original aggregate and per-file hashes.
4. Build a complete `cv-customization.v1` request. Reorder or rewrite only supported claims and record every new source line in the claim ledger with its CV-fact references. A reorder-only edit may use an empty ledger.
5. Call the runtime customization command, then the isolated build command. Treat only a successful envelope with `verified: true` as an artifact.
6. Recheck original hashes, checkpoint the returned manifest/customization/source/PDF references, and transition `cv_selected -> cv_ready`.
7. Return the exact verified PDF path, the evidence-backed changes, and unresolved gaps. Stop before application work.

## Failure rule

If resolve, promotion, preparation, customization, compilation, verification, or hashing fails, leave the run recoverable, preserve the runtime's redacted diagnostics, report the fixed reason code, and ask before any fallback. A corrected request may replace a failed customization through the runtime; never edit its generated files directly. Never present an unverified path.
