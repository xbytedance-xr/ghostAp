# Engine Mode Routing Restoration Design

## Goal Snapshot

- Goal: restore Deep, Spec, Worktree, and Workflow to their established
  autonomous execution behavior after the recent Feishu/ACP changes.
- Success criteria: explicit engine commands and topic continuations reach the
  engine strategy, never a normal programming conversation; engine internals
  and convergence behavior remain unchanged.
- Constraints: communication parsing and dispatch may change, but engine
  algorithms, lifecycle semantics, prompts, and topic-scoped strategy contracts
  must not be redesigned.
- Non-goals: replacing the engine implementations, merging Autonomous employee
  roles into engine selection, or broadly reverting valid ACP/model fixes.

## Evidence and Root Cause

The production message at `2026-07-12 01:54 UTC` was fetched through the Lark
message API. Its raw body was a flat post object:

```json
{
  "title": "",
  "content": [
    [{"tag": "text", "text": "/deep ..."}],
    [{"tag": "img", "image_key": "img_v3_..."}]
  ],
  "content_v2": []
}
```

`FeishuImageHandler._parse_post_message()` only recognizes the older localized
shape `{ "zh_cn": { "content": ... } }`. For the flat shape it selects the
empty string in `title` as the candidate post object, then returns empty text
and no images. `_dispatch_empty_text()` subsequently forwards an empty prompt
to the persistent Traex programming session. Production logs consequently show
`ProgrammingModeHandler` activity and no Deep engine start; the returned card
says that the user supplied no request.

The last-day Git audit found no rewrite of the Deep or Spec execution engines.
The relevant changes were ACP activation/card communication, Traex model
variants, Workflow model validation, and new Autonomous command wiring. The
existing historical contract from commit `aaabd52` remains present: Deep, Spec,
Worktree, and Workflow are topic-scoped strategies, and plain-text continuation
inside a bound topic routes back to that engine.

## Chosen Approach

Repair the message boundary and reinforce the existing routing contract.

1. Parse both official flat post bodies and legacy localized post bodies.
2. Prefer `content`; use `content_v2` only when `content` is absent or invalid.
3. Preserve text row order and image order. Ignore unknown elements without
   inventing text.
4. Add an ingress-level regression using the captured production payload. It
   must prove that `/deep` plus an image remains visible to slash parsing.
5. Add routing matrix regressions proving that explicit Deep, Spec, Worktree,
   and Workflow commands override every persistent programming mode.
6. Keep the engine implementations untouched. Existing topic-continuation and
   autonomous Spec prompt tests remain the authoritative engine-behavior gate.

This is preferable to reverting all July 10-11 commits because a broad revert
would remove valid official Codex adapter, Traex selection, and Workflow fixes.
It is also preferable to special-casing `/deep` in the dispatcher, which would
leave Spec, Worktree, Workflow, and non-command rich posts broken.

## Components and Data Flow

```text
Lark post event
  -> FeishuImageHandler.parse_message()
  -> text + image_keys
  -> image download/reference enrichment
  -> SlashCommandParser.parse(text)
  -> explicit engine command priority
  -> Deep/Spec/Worktree/Workflow handler
  -> existing autonomous engine lifecycle
```

The fix stays at the first boundary. The downstream routing order already
implements the historical contract when it receives non-empty text.

## Error Handling

- Invalid JSON retains the current best-effort text fallback.
- Structurally invalid post content returns no extracted elements and logs no
  sensitive payload.
- Unknown rich-post element tags are ignored.
- A valid flat body is never treated as a locale map merely because `title` is
  its first key.
- The dispatcher continues to use its current safe fallback when no command is
  present; no new intent guessing is introduced.

## Testing and Acceptance

- A parser regression uses the exact flat `title/content/content_v2` structure
  observed in production and expects the `/deep` text plus image key.
- Legacy `zh_cn`, `en_us`, and first-available-locale tests continue to pass.
- Parameterized routing tests cover `/deep`, `/spec`, `/wt`, and `/wf` while
  the project is in each normal programming mode, including Traex.
- Existing topic strategy tests prove free text in Deep/Spec/Worktree/Workflow
  topics remains autonomous.
- Existing Spec prompt assertions prove ambiguity is recorded but does not wait
  for user answers.
- Run focused parser/routing tests, then the shared Feishu/engine/card subset,
  then the full suite because routing and communication are shared code.

## Recent Autonomous Card Finding

The July 11 employee card emits callbacks whose select payloads contain only a
`key`; no action is registered, so the two observed “unrecognized operation”
messages are expected. The create button is likewise not registered in the
main dispatcher. This is an incomplete new Autonomous communication surface,
not part of the historical Deep/Spec engine logic. It must not be used as a
replacement selection flow for those modes. Its repair should preserve the
Autonomous domain design and is tracked separately from this engine restoration
unless it is required to prove engine routing.
