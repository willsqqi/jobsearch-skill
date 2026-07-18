# Jobsearch skill evaluation protocol

All evaluation inputs and evidence are public synthetic fixtures. Never point an evaluation at a real job-search home, real application, or private CV.

The isolated runtime lives at the sibling `.jobsearch-eval` path derived from the repository root. Before creating or changing it, require `.synthetic-bootstrap.json`; if the directory exists without that marker, stop for user direction. Install the runtime, run `bootstrap --synthetic`, and require `validate --ready` to exit successfully before evaluating a skill.

## Case contract

Every `evals/<skill>/case.json` validates against `rubric.schema.json` and declares `prompt`, `allowed_inputs`, `expected_behaviors`, `forbidden_behaviors`, and `minimum_pass_count`. The agent may read only the declared inputs plus the one named skill during the forward run. It may also read runtime-generated evidence and state artifacts returned by successful permitted synthetic runtime commands required by that skill. This allowance applies only under the marked sibling evaluation home; it does not permit undeclared repository inputs, real data, arbitrary private-home files, or artifacts from failed or unapproved commands.

## Fresh-agent protocol

1. Spawn a fresh subagent with `fork_turns="none"`.
2. Give it the baseline prompt exactly: `Do not read any skills. Read only the case and its allowed synthetic inputs, then perform the user request.`
3. Record which rubric behaviors fail before authoring the skill.
4. Create and edit only that one skill.
5. Spawn a different fresh subagent with `fork_turns="none"`.
6. Give it the forward prompt exactly: `Read the named SKILL.md completely and every first-level reference it routes for this case. Do not read sibling skills. Derive the sibling .jobsearch-eval path from the repository root and prefix launcher commands with JOBSEARCH_HOME set to that path. Then perform the case.`
7. Record the scorecard and refine until every required behavior passes and every forbidden behavior is absent.

Use a new context-free agent for every run. Do not reuse an agent between baseline, skill authoring, or forward evaluation.

## Scorecard records

Store the baseline in `baseline.md` and the forward result in `with-skill.md`. Record only:

- the case ID;
- passed and failed behavior IDs;
- forbidden behavior IDs that appeared;
- concise synthetic evidence references;
- a short outcome rationale.

Do not record hidden reasoning or chain-of-thought. A forward run passes only when the passed count meets `minimum_pass_count`, every required behavior is accounted for, and no forbidden behavior appears.
