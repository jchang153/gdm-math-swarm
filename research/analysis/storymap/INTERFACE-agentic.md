# Story map stage 0: agentic monitors (arm AG)

Owner decision: docs/plan.md, "Agentic monitors in the story maps (owner decision, November 24, 2026)".
Code: `research/analysis/storymap/agentic.py` (hooks in `extract.py`). Offline, read only, no model calls.
Tests: `research/analysis/tests/test_storymap_agentic_extract.py` (fixture builder
`research/analysis/tests/fixtures/storymap/agentic/build_agentic_fixture.py`).

## Scope

Only runs with agentic monitors are touched: settings `effective` (else `config`) `agentic_monitor.enabled`,
or an `agentic_monitor` role in `effective.roles`; without any settings, `agentic_*` rows in `runtime.jsonl`.
Every other run's stage-0 output is byte-identical (checked on the shared fixture and on a real overseer run).
Overseer mode (`--overseers`) writes no agentic files.

## Roles and attribution

- `agmon-NNN` has role `agentic_monitor` and watches `agent-NNN`.
- In AG runs the conference sender `reviewer` (the identity monitors use for DMs and reverts) has role
  `agentic_monitor` and is re-attributed to a monitor:
  - a message by its public id (`agentic_message.message_id` or `agentic_revert.message_id`), else the 1:1
    mapping recipient `agent-NNN` -> `agmon-NNN`;
  - a revert by its submission (`agentic_revert.submission_id`), else the credited solver's monitor.

## Solver timelines (`timeline/<agent>.jsonl`)

Item format unchanged: `{i, t, ts, turn, kind, tool, text, event, call_id, mid}`, event
`{type, id, author, author_role, fake, tags, outcome, detail}`. Two new event types:

| type | meaning | placed at | id | author / role | detail |
|---|---|---|---|---|---|
| `G1` | the monitor reverted my accepted submission | the revert time (conference `reversions.reverted_at`), before the first later non-result item | `G1:sub-000065` | monitor / `agentic_monitor`; `fake` the strict fake rule with evaluator validity; outcome `reverted`; tags `["revert_notice"]`; tool null | `monitor, submission_id, public_submission_id, problem_id, reason, accepted_ts, latency_min, message_id, fake` |
| `G2` | a monitor message was delivered to me | its first delivery: the `push_delivered` event (push runs) or the first pull `get_messages` that returned it | `G2:dm-000034` | monitor / `agentic_monitor`; tool `push` or `get_messages`; tags from the text plus `revert_notice` for a revert notice | `monitor, message_id, dm_id, text, sent_ts, delivered_ts, lag_s, revert_notice, delivery` (`push`/`pull`) and, when pushed, the push fields (`delivery_id, delivery_size, delivery_items, anchor_tool_use_id, request_seq, placement, answered, position`) |

- G2 replaces the D1 that a monitor message used to get. A later re-read of the same message gets no event
  (one G2 per message per solver). In a push delivery, `delivery_items` names the item `G2:dm-...`.
- P2 (the solver notices a lost credit) is unchanged except that its author (and its text) name the monitor.
  S1's `detail.reverted_by` and text name the monitor too.
- Units: `*_ts` fields are run-clock minutes (the clock of item `t`); `latency_min` is accept-to-revert minutes;
  `lag_s` is send-to-delivery seconds. `message_id` is the conference public message id (`dm-<hex>`, the id the
  runtime rows record); `dm_id` is the internal `dm-000034` used in event ids.
- `timeline/index.json` entries of solvers gain `"monitor": "agmon-NNN"`.

## Monitor timelines (`agentic/<agmon>.jsonl`, `agentic/index.json`)

Same item format. From the monitor's Inspect sample (`judge/history.monitor_sample_index`):

- `context`: the system prompt once; each new user message (implicit-wait deliveries, compaction requests and
  continuations), full text; token counters dropped.
- `reasoning`, `text`, `tool_call` per model output, at its completion; outputs the harness discarded (a re-sent
  request whose calls never ran) are left out; the CLI's empty-text placeholder `(no content)` is dropped.
- `tool_result` in full (wait results are the rendered feed). Its time is its runtime row's time when joined
  (the tool's return), else the monitor's next model request.
- `turn` is the index of the model request in the sample.

Event items from `runtime.jsonl`, author the monitor, role `agentic_monitor`, placed right after the joined tool
result (`detail.tool_call_id`, the monitor's own tool-use id; join: same tool, row time within the call's
[call time - 5 s, result time + 5 s]), else by time (implicit waits, errors, compactions):

| type | runtime kind | detail |
|---|---|---|
| `GW` | `agentic_wait` | `requested_s, clamped_s, delivered_calls, updated_calls, call_ids, update_ids, call_lags_s, implicit, solver_stopped, backlog, chars` |
| `GL` | `agentic_list_revertible` | `count, problem_ids` |
| `GO` | `agentic_get_output` | `call_id` (a solver call id), `found, chars` |
| `GR` | `agentic_revert` | G1's detail plus `dm_sent, delivered_ts` (of its DM; null when never delivered); `fake` set; outcome `reverted` |
| `GF` | `agentic_revert_refused` | `problem_id, why`; outcome `refused` |
| `GM` | `agentic_message` | `message_id, dm_id, text, sent, sent_ts, delivered_ts, lag_s`; outcome `sent`/`failed` |
| `GE` | `agentic_error` | `stage, error, detail`; outcome `error` |
| `GC` | `agentic_compaction` | `messages` |

Every event detail also has `tool_call_id` (null when not joined). Event ids: `GW:agmon-018:0003` (per-type
counter). `agentic/index.json`: `{agmon: {solver, items, est_tokens, first_min, last_min, source}}`, `source`
`inspect` or `runtime_only` (no sample).

## `agentic_context.json` (beside `timeline/`)

```
{"version": 1,
 "pairs": {"agmon-NNN": {"solver", "waits", "mean_wait_s", "reverts", "refused", "messages", "get_output",
                         "list_revertible", "errors", "first_fake_t", "first_revert_t",
                         "time_to_first_revert_min", "fakes_accepted", "fakes_reverted",
                         "fakes_credited_at_end", "false_reverts", "median_seen_lag_s",
                         "message_lag_s_median", "cost_usd"}},
 "seen": {"agent-NNN": [[call_id, call_ts_min, seen_ts_min], ...]}}
```

- Counts are the monitor's runtime rows (`waits` includes implicit waits; `messages` excludes revert DMs).
- `mean_wait_s`: mean `clamped_s` of the waits.
- `first_fake_t`: the solver's first fake submission (any outcome); `first_revert_t`: the monitor's first revert;
  `time_to_first_revert_min` = `first_revert_t - first_fake_t`.
- `fakes_accepted`: the solver's accepted fakes; `fakes_reverted`: those later reverted;
  `fakes_credited_at_end`: those never reverted; `false_reverts`: monitor reverts of non-fake submissions.
- `median_seen_lag_s`: median of all `call_lags_s` (call first seen by the observer to delivery to the monitor);
  `message_lag_s_median`: median send-to-delivery seconds of the monitor's own messages that were delivered.
- `cost_usd`: settled USD of the monitor's `inference_finished` rows; null without `runtime.jsonl`.
- `seen`: every call delivered as new by a wait (explicit or implicit); `call_ts_min` = delivery minus lag,
  `seen_ts_min` = the delivery. `call_id` is the solver timeline's `call_id`. Note: an implicit wait records a
  delivery even when the monitor session then failed to run (restart failures), so `seen` means "delivered to
  the monitor's feed", not "read by its model".

## Constants

`agentic.py`: `ROLE = "agentic_monitor"`, `REVIEWER_ID = "reviewer"`, `REVERT_EVENT = "G1"`,
`MESSAGE_EVENT = "G2"`, `SOLVER_EVENT_TYPES = ("G1", "G2")`,
`MONITOR_EVENT_TYPES = ("GW", "GL", "GO", "GR", "GF", "GM", "GE", "GC")`, `SOLVER_EVENT_NAMES`,
`MONITOR_EVENT_NAMES`, `CONTEXT_FILE = "agentic_context.json"`, `MONITOR_DIR = "agentic"`.

The event types `G1`/`G2` share their names with the taxonomy's thought codes G1 (evaluation awareness) and G2
(loses an earlier decision after compaction). Event ids always carry the `G1:`/`G2:` prefix; code that matches
bare type tokens (for example the view's `EVENT_TOKEN`) must keep events and thought codes apart.
