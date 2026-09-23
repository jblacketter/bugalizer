# Jev triage experiment — continuation notes

Recorded September 22, 2026. Status: exploration only; no integration or adoption decision.

## Why this exists

Jack explored Jev and community Open-Jev implementations in a separate learning project and asked to preserve a starting point here. The strongest candidate is Bugalizer's Stage 2 classification work. Start with an evaluation that records suggestions alongside the existing pipeline, without changing report fields or workflow status.

User preference: visual learning. Use the interactive lesson when explaining proposed behavior, thresholds, and tradeoffs.

## Resume from the working demo

- Local project: `/Users/jackblacketter/Documents/jev-experiment`
- Git commit: `f4004ba` — `Add interactive Jev learning demo and project-fit guide`
- Open `index.html` in a browser; it works from disk with no dependencies.
- Run `python3 demo.py --all` from that project for the terminal equivalent.
- Read its `README.md` for the real hosted API setup and `docs/project-fit.md` for the broader assessment.

The browser lesson has four synthetic reports, probability bars, a confidence slider, the matching policy rule, and expandable request/response/code views. Try the login scenario at 0.80 and 0.95: identical model answers lead to different routing proposals.

**Evidence so far:** seven Python tests passed; browser scenario selection, threshold presets, and slider interaction were verified. All lesson probabilities and confidence values are hand-authored. They are not real Jev predictions or evidence of accuracy, speed, calibration, or cost savings. No TypeSafe key was configured during the experiment; the live HTTP path was tested with mocks, not a real inference call. Local Open-Jev inference has not been installed or verified on this Apple Silicon machine.

## The technology distinction

TypeSafe Jev accepts state plus typed questions and returns bounded decisions. Choice selects from named options; Score rates an ordered rubric; Noul gives a yes/no probability. Keep each question narrow and compose the result in code. Typed answers can still be incorrect. Choice/Score confidence describes the probability distribution and must not be treated as a guarantee of correctness.

The community repository `Zefan-Cai/Open-Jev` is a separate implementation with published adapters, not TypeSafe's proprietary weights. Its documented trained-checkpoint workflow targets Linux/GPU hardware with upstream Qwen weights and only partial API compatibility. Recheck current requirements and artifact licenses before selecting a local runtime. Other projects using similar names are not necessarily interchangeable.

Sources checked during the exploration:

- [TypeSafe introduction](https://docs.typesafe.ai/introduction)
- [HTTP API contract](https://docs.typesafe.ai/api)
- [Confidence semantics](https://docs.typesafe.ai/confidence)
- [Community Open-Jev](https://github.com/Zefan-Cai/Open-Jev)

## Current Bugalizer integration points

Source inspected at Bugalizer commit `6471feb`:

- [`src/bugalizer/pipeline/triage.py`](../src/bugalizer/pipeline/triage.py): `triage_report` resolves the project's provider/model, calls `complete`, parses JSON, persists an analysis and usage, updates severity/feature area, and chooses `clarification_needed` or `triaged`. Network I/O happens outside `db_write_lock`.
- [`src/bugalizer/llm/prompts.py`](../src/bugalizer/llm/prompts.py): the triage response includes severity, category, feature area, summary, needs-clarification, clarification questions, and a confidence value.
- [`src/bugalizer/llm/client.py`](../src/bugalizer/llm/client.py): provider resolution and the existing completion interface; inspect before proposing any adapter change.
- [`tests/test_pipeline.py`](../tests/test_pipeline.py) and [`tests/test_config_tiering.py`](../tests/test_config_tiering.py): starting points for understanding triage behavior and configuration expectations.

Important mismatch: the teaching demo's area choices are authentication/payments/interface/unknown; Bugalizer's category choices are ui/api/data/auth/performance/infrastructure/other. Bugalizer's feature area is free text or null, not that category enum. Design an explicit mapping rather than copying the demo taxonomy.

Similarly, the demo has a fractional severity score from 0 to 3; Bugalizer expects critical/high/medium/low. Decide whether to ask a Choice directly or define a documented Score-to-label policy. Do not silently round a weighted score and assume semantic equivalence.

Jev's bounded decisions do not replace the free-text `summary` or arbitrary `clarification_questions`. Preserve a generative step, use explicitly defined templates, or limit the experiment to classification fields. Do not substitute Jev's confidence for the existing LLM's self-reported confidence as though they measure the same thing.

The existing triage exception handler marks the analysis failed, sets report status to `triaged`, and re-raises. A shadow experiment should not reuse this side-effectful path for its own errors. Record a failed comparison separately and leave the established pipeline behavior intact.

## Proposed first increment

1. Review the current project instructions and workflow state. This note is research context, not an approved phase plan. If work proceeds, create the appropriate plan through the existing lead/reviewer workflow.
2. Define a provider-neutral evaluation record: report reference, rubric version, exact returned model revision, raw typed answers, derived proposal, latency, usage, and explicit error outcome. Keep shadow records separate from the production analysis result.
3. Build a standalone evaluation runner before integrating the worker. Use 50–100 sanitized, human-labeled reports, with separate tuning and held-out sets. Include ambiguous descriptions, negation, multiple symptoms, missing information, and critical incidents.
4. Compare existing triage with hosted Jev on the same classification questions. Optionally add an open implementation after its runtime and response semantics are verified. Keep summaries and other generative outputs out of an unfair like-for-like classification comparison.
5. Measure category accuracy, missed critical reports, clarification precision/recall, review rate, accuracy among proposed automatic routes, median/p95 latency, and actual cost. Evaluate probability calibration separately from classification accuracy.
6. Choose acceptance criteria before scoring the held-out data. Tune thresholds only on the tuning set. Retain distributions and error cases so a high average score cannot conceal failures on critical reports.
7. Only after a useful evaluation, propose an opt-in shadow integration. It must not update severity, feature area, status, or downstream execution from experimental output. Production adoption would be a separate decision.

## Constraints and open decisions

- Hosted Jev sends supplied report content to an external API; Bugalizer's current local triage has a different data boundary. Start with synthetic/sanitized inputs and explicitly decide what real report content may be sent.
- Keep credentials server-side and out of reports, fixtures, browser code, and logs.
- Validate answer labels, types, probability ranges/sums, and Score semantics before using output. Timeouts, rate limits, and invalid responses are failures, not successful triage or fabricated predictions.
- Preserve provider/model overrides and the existing lock boundary. No database lock around network calls.
- The demo's 0.80 confidence, 0.50 clarification, and 0.20 critical-risk thresholds are illustrative. A production policy needs measured tradeoffs, especially critical-incident recall.
- Leave localization, fix generation, approvals, and permissions with their existing mechanisms.
- Decide the feature-area taxonomy, treatment of multi-category reports, source of textual summaries, and evaluation acceptance criteria before implementation.

## Suggested next-session prompt

> Read `docs/jev-triage-experiment.md` and the project workflow instructions. Help plan a standalone shadow evaluation of Jev for Bugalizer Stage 2, preserving the existing pipeline. Start by inspecting the current triage schema and tests, then propose a labeled dataset and measurable acceptance criteria. Use the visual demo in `/Users/jackblacketter/Documents/jev-experiment` to explain the decision policy. Do not enable hosted processing of real reports or change production routing as part of the initial evaluation.
