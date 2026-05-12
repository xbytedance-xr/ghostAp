# Adaptive Spec Review Roles Design

## Background

Spec Engine already runs iterative cycles:

```text
spec -> plan -> task -> build -> review -> next cycle
```

The current review phase is software-oriented. It uses five fixed
perspectives:

- Architect
- Product
- User
- Tester
- Designer

That works well for programming tasks, which are still the primary GhostAP use
case. It does not fit non-code tasks as well. For example, writing an article
may need an editor, fact checker, target-reader reviewer, and visual designer.
Research tasks may need source verification, methodology review, and opposing
view review.

The goal is to keep the current programming review quality while letting Spec
derive task-specific reviewer roles automatically.

## Goals

- Keep the existing five programming roles as the default role set for software
  tasks.
- Generate additional or alternative roles for non-software or mixed tasks.
- Run reviewers concurrently by default.
- Support explicit role dependencies when one role needs another role's output.
- Feed high-confidence review suggestions back into later Spec cycles.
- Reduce hallucination by requiring evidence, aggregation, deduplication, and
  convergence checks.
- Finish only after acceptance criteria and blocking review roles pass for two
  consecutive review rounds.

## Non-goals

- Do not turn every role into a long-running agent with shared mutable state.
- Do not make the role set manually configured for every task.
- Do not remove the existing programming role behavior.
- Do not allow unbounded review loops without budget, timeout, or convergence
  guards.
- Do not treat unsupported low-evidence suggestions as blockers.

## Recommended Approach

Implement a new adaptive review strategy:

```text
AdaptiveRoleReviewStrategy
```

It will sit beside the existing `MultiPerspectiveStrategy` and reuse the
current review pipeline foundations:

- `ReviewArtifacts`
- `CycleBudget`
- ephemeral review sessions
- parallel worker execution
- retry/circuit-breaker behavior

The strategy has four phases:

1. Role planning
2. Concurrent role review
3. Suggestion aggregation
4. Convergence accounting

## Role Planning

Add a role planner that inspects:

- user requirement
- current Spec / Plan / Task / Build output
- touched files
- current diff
- prior review result
- current acceptance criteria status

It emits a structured role plan.

```python
@dataclass
class ReviewRoleSpec:
    role_id: str
    display_name: str
    category: str
    mission: str
    review_focus: list[str]
    must_check: list[str]
    evidence_policy: str
    blocking: bool = True
    depends_on: list[str] = field(default_factory=list)
    max_suggestions: int = 5
```

For programming tasks, the planner always includes the fixed software roles:

- `architect`
- `product`
- `user`
- `tester`
- `designer`

Then it may append dynamic roles when evidence suggests they are useful:

- `security_reviewer`
- `sre_reviewer`
- `api_contract_reviewer`
- `data_privacy_reviewer`
- `mobile_ux_reviewer`
- `performance_reviewer`
- `docs_reviewer`

For writing tasks, likely roles include:

- `editor_in_chief`
- `style_editor`
- `fact_checker`
- `target_reader`
- `visual_designer`
- `distribution_editor`

For research tasks, likely roles include:

- `researcher`
- `source_verifier`
- `methodology_reviewer`
- `domain_expert`
- `opposing_view_reviewer`
- `conclusion_editor`

The planner should prefer a small role set. Defaults:

- programming task: fixed 5 roles plus up to 3 dynamic roles
- non-programming task: 4 to 6 task-specific roles
- hard cap: configurable, default 8 roles

## Role Plan Prompt Contract

The role planner should output JSON only:

```json
{
  "task_kind": "programming|writing|research|design|mixed|other",
  "roles": [
    {
      "role_id": "fact_checker",
      "display_name": "事实核查员",
      "category": "research",
      "mission": "验证文章中的事实、数据、来源与过度推断",
      "review_focus": ["事实准确性", "来源可信度", "引用完整性"],
      "must_check": ["是否存在无来源事实", "是否存在无法支持的结论"],
      "evidence_policy": "每条阻塞建议必须引用原文片段或来源缺口",
      "blocking": true,
      "depends_on": [],
      "max_suggestions": 5
    }
  ]
}
```

If role planning fails, fall back safely:

- programming tasks use the fixed five roles
- non-programming tasks use `product`, `user`, `tester`, and a generic
  `domain_reviewer`

## Concurrent Execution

Review roles are concurrent by default. Dependencies are represented as a DAG.

Execution policy:

- Roles with no dependencies run in the first batch.
- A role runs after all `depends_on` roles complete.
- Roles in the same dependency layer run in parallel.
- If the dependency graph has a cycle, log a warning, remove dependencies, and
  run roles concurrently.

Example:

```text
batch 1: editor_in_chief, fact_checker, target_reader, visual_designer
batch 2: conclusion_editor depends_on fact_checker
```

This keeps the default fast path concurrent, while allowing sequential review
only when the planner explicitly needs it.

## Role Worker

Add a dynamic role worker beside the current `PerspectiveWorker`.

```python
class RoleReviewWorker:
    def __init__(self, role: ReviewRoleSpec, timeout: float, ...):
        ...

    def run(self, artifacts: ReviewArtifacts, prompt_runner: PromptRunner) -> RoleReviewOutcome:
        ...
```

For fixed programming roles, we can either:

1. Convert each `ReviewPerspective` into a `ReviewRoleSpec`, then use one worker
   path for all roles.
2. Keep `PerspectiveWorker` for fixed roles and add `RoleReviewWorker` for
   dynamic roles.

Recommendation: convert fixed perspectives to `ReviewRoleSpec`. One worker path
will make aggregation, dependency batching, evidence scoring, and card display
more consistent.

## Role Review Prompt

Each role gets a narrow prompt:

- only one role identity
- current task goal
- role mission and must-check list
- relevant artifacts
- output schema
- evidence requirement

Output JSON:

```json
{
  "role_id": "fact_checker",
  "verdict": "PASS|FAIL",
  "summary": "short summary",
  "suggestions": [
    {
      "severity": "blocker|major|minor|observation",
      "confidence": "high|medium|low",
      "evidence": "quoted artifact/diff/source gap",
      "recommendation": "specific change",
      "target": "file/path or artifact section if known"
    }
  ]
}
```

Rules:

- `blocker` and `major` suggestions require evidence.
- Missing evidence downgrades the suggestion to `observation`.
- PASS means no blocker or major suggestion from this role.
- A role may still return observations with PASS.

## Suggestion Aggregation

Raw role output should not directly drive repair. Add a review aggregator:

```python
@dataclass
class AggregatedSuggestion:
    suggestion_id: str
    severity: str
    confidence: str
    role_ids: list[str]
    evidence: list[str]
    recommendation: str
    target: str = ""
    blocking: bool = False
```

Aggregator responsibilities:

- normalize outputs
- discard malformed suggestions
- downgrade low-evidence blockers
- deduplicate similar suggestions
- merge supporting evidence from multiple roles
- detect contradictory recommendations
- produce a compact repair guidance block for the next build cycle

Conflict policy:

- If two high-confidence suggestions conflict, mark the group as
  `requires_decision`.
- The main agent must make an explicit decision in the next Spec/Plan cycle.
- Do not apply both blindly.

## Repair Feedback Loop

Current Spec already feeds review suggestions into the next cycle through
`build_refinement_input`. Replace direct failed-perspective suggestions with
aggregated repair guidance.

Next-cycle input should contain:

- blocking suggestions
- major suggestions
- unresolved conflicts
- role summaries
- repeated failures from previous cycles

Minor observations should be kept in artifacts but should not block completion.

## Convergence

Use the user's desired rule:

```text
complete when all acceptance criteria pass and review passes twice in a row
```

Precise rule:

- all acceptance criteria are satisfied
- all blocking roles return PASS
- no new high-confidence blocking suggestion exists
- no unresolved high-confidence conflict exists
- the above conditions hold for two consecutive review rounds

Add to project state:

```python
review_pass_streak: int = 0
last_role_plan_hash: str = ""
last_blocking_suggestion_hash: str = ""
```

When the role set changes materially, keep the pass streak only if the new roles
are non-blocking. If a new blocking role is added, reset the pass streak.

This prevents a run from passing once with one role set and once with a
different role set, then incorrectly declaring convergence.

## Settings

Add settings:

```python
spec_review_strategy = "adaptive_roles"
spec_review_role_planner_enabled = True
spec_review_dynamic_roles_enabled = True
spec_review_dynamic_roles_max = 3
spec_review_total_roles_max = 8
spec_review_pass_streak_required = 2
spec_review_role_dependencies_enabled = True
spec_review_role_evidence_required = True
```

Existing settings still apply:

- `spec_review_timeout`
- `spec_review_max_parallel`
- retry settings
- circuit-breaker settings

Important concurrency behavior:

- `spec_review_max_parallel` should cap parallel workers per batch.
- If roles exceed the cap, run that dependency layer in waves.
- Default should remain fast for common programming tasks.

## Card And UX

Spec cards should show role review compactly:

- "审查角色: 5 固定 + 2 动态"
- "并发批次: 2"
- "阻塞建议: 1"
- "连续 PASS: 1/2"

Dynamic role details should be collapsed by default. The user should see:

```text
🔍 审查完成 · 7 个角色 · 2 批并发 · 1 条阻塞建议
```

Expanded content shows:

- role name
- PASS/FAIL
- top 1 to 3 suggestions
- evidence summary

Do not show raw role prompts or full model output in the card.

## Implementation Plan

Phase 1: data model and planner

- Add `src/spec_engine/review_roles.py`.
- Add `ReviewRoleSpec`, `RolePlan`, role hash helpers.
- Add fixed programming role conversion from `ReviewPerspective`.
- Add deterministic heuristic planner as fallback.
- Add LLM role planner behind a setting.

Phase 2: worker and aggregation

- Add `RoleReviewWorker`.
- Add JSON role review prompt and parser.
- Add `review_aggregation.py`.
- Convert fixed perspectives into role specs for the adaptive path.

Phase 3: adaptive strategy

- Add `AdaptiveRoleReviewStrategy`.
- Register it in `review_strategy.py`.
- Wire SpecEngine to select strategy from settings.
- Keep existing `multi_perspective` fallback.

Phase 4: convergence

- Add pass streak fields to `SpecProject`.
- Update cycle finalization to require configured pass streak.
- Reset streak on new blocking role or new blocking suggestion hash.

Phase 5: card display and tests

- Add compact role review summary to Spec renderer callbacks/cards.
- Add tests for programming task role preservation.
- Add tests for writing/research role generation.
- Add tests for dependency batching and max parallel enforcement.
- Add tests for evidence downgrade and pass streak behavior.
- Add compatibility tests proving `multi_perspective` still works.

## Risks And Mitigations

Risk: dynamic roles hallucinate extra requirements.

Mitigation: require evidence for blockers and aggregate before repair.

Risk: too many roles slow down review.

Mitigation: cap roles and run dependency layers concurrently.

Risk: role changes make pass streak meaningless.

Mitigation: hash blocking role set and reset streak when blocking roles change.

Risk: non-code tasks lack diff artifacts.

Mitigation: review prompts should use Spec/Plan/Build outputs and generated
content artifacts when diff is empty.

Risk: JSON output parsing fails.

Mitigation: strict JSON first, tolerant parser second, fail-safe role result
with actionable parse-failure suggestion.

## Acceptance Criteria

- Programming Spec tasks still include Architect/Product/User/Tester/Designer by
  default.
- Writing and research tasks can generate task-specific roles without code
  changes.
- Role workers run concurrently unless explicit dependencies force batching.
- `spec_review_max_parallel` caps concurrency.
- Blocking suggestions without evidence are downgraded and do not block
  completion.
- Aggregated suggestions, not raw role output, drive the next repair cycle.
- Spec completes only after acceptance criteria and blocking role review pass for
  two consecutive rounds by default.
- Existing `multi_perspective` strategy remains available as fallback.
