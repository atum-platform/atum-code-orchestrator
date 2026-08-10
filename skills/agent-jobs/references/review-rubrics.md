# Review Rubrics

Load only the section relevant to the current checkpoint and incorporate it into
the submitted `instructions`.

## Code Review

Review the scoped diff and relevant surrounding code. Lead with actionable
findings ordered by severity. Prioritize correctness, security, data loss,
behavioral regressions, concurrency, lifecycle failure, and missing tests. Cite
files and lines. Distinguish introduced defects from pre-existing risks. End with
an explicit ship or do-not-ship verdict.

## Planning And Architecture

Challenge assumptions, ownership boundaries, dependencies, failure semantics,
security, migration and rollback, observability, testing, and operational burden.
Separate blocking decisions from follow-ups. Return a concrete executable plan
with acceptance checks.

## UI/UX And Design

Evaluate task flows, information hierarchy, interaction states, accessibility,
responsive behavior, visual consistency, domain fit, and implementation
feasibility. For visual QA, compare actual rendered evidence rather than prose.
Return prioritized changes and objective verification criteria.

## Product Judgment

Assess user value, scope, sequencing, incentives, failure modes, adoption cost,
and whether the proposal solves the stated problem. Identify assumptions that
need evidence and recommend the smallest complete outcome.

## Copywriting

Use Claude Opus by default. Evaluate audience, offer clarity, differentiation,
specificity, voice, hierarchy, objections, and conversion intent. Preserve facts;
flag unsupported claims. Return final copy plus only the rationale needed for
important choices.

## Research Synthesis

Separate sourced facts, model inference, uncertainty, and recommendations.
Reconcile conflicting evidence, identify missing primary sources, and return a
decision-oriented synthesis rather than a link dump.
