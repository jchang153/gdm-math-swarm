# Prompt caching and the token-counter correction

Provider prompt caching reuses computation for an unchanged input prefix while
generating a fresh response. Inspect's `GenerateConfig(cache=False)` controls a
different feature: reusing entire previous responses. Keep response caching off
for independent experiments; it does not disable provider prompt caching.

## Paid diagnosis, October 15, 2026

The three original runs had provider-reported input-cache shares of 29.58%,
26.43% and 25.84%. Caching was active, but most conversation input was charged at
the fresh-input rate. Claude Code injects a standalone historical system message
of the form `<total_tokens>14982386 tokens left</total_tokens>` after most turns.
The request's leading instructions were stable; these later messages changed.

The user authorized a paid comparison and a correction if confirmed. The
diagnostic used the same DeepSeek V4.1 Flash model, Fireworks US route, high
reasoning and production provider builder/budget filter. It compared two
replicates of three four-request conversations: changing counters as system
messages, identical counters as user messages at the same positions, and a
control without added counters. Each session had a distinct stable session ID
and opening nonce; calls were interleaved. The first request warmed each session.
All conversations used the same approximately 11,700-token synthetic background
and tool schema. Generated synthetic tool calls were never executed.

| Continuation requests, excluding initial warm-up | Requests | Input cached | Provider-reported cost |
| --- | ---: | ---: | ---: |
| Original system-role counters | 6 | 1.98% | $0.032618844 |
| Same counter text as chronological user context | 6 | 97.92% | $0.002125656 |
| Unchanged-system control | 6 | 97.97% | $0.002199114 |

The corrected continuation calls were 93.48% cheaper in this controlled test.
Including the uncached warm-up calls, original and corrected arms cost
$0.043392744 and $0.012872556 respectively. These measurements confirm that
the counter's message role causes substantial cache loss on the pinned route.
They do not establish which upstream component rearranges or otherwise renders
system messages, or promise the same savings on full swarm workloads.

All 24 calls returned valid synthetic tool calls, with readable reasoning replay
on follow-up requests. Total settled diagnostic spending was $0.069225714.
The separate test ceiling was $0.50; the sum of conservative reservations was
$0.37954530. Cumulative compatibility spending became $0.320034936 of its existing
$2 ceiling, with no unresolved requests or violations. All prior ledger rows,
including eight uncertain pilot holds, remained unchanged; no budget was raised.

Private diagnostic records and consistent ledger backups are retained locally at
`runs/phase-b/cache-audit-20261015/cache-paid-20261015/`, with source script
`runs/phase-b/cache-audit-20261015/paid_cache_probe.py`. Archive SHA-256:
`331bfb836cf4156fb3a583677a84c3eb417d7038fd9aca78277a5c3b0d464bae`.
Raw records and ledger backups must not be published to GitHub.

## Correction: `counter-user-v1`

`research_swarm.prompt_cache.normalize_token_counters` converts only exact
standalone string-content token counters in historical `ChatMessageSystem`
objects into actual `ChatMessageUser` objects. It preserves their text, position,
ID, source and metadata. Leading system instructions, mixed-content instructions,
other system messages, user/tool text, reasoning and tool-call/result ordering
remain untouched. The scaffold's original history is never modified.

The production budget filter archives original instructions, normalizes the
provider request, then reserves against that effective request. Provider billing
settlement and stop/budget safeguards remain in force. Admission events record
the normalization version and number of transformed counters; future pilot
settings also record `effective.prompt_cache_normalization`.

This is a versioned change to prompt representation. Do not describe corrected
runs as bit-identical to the three original runs. It neither authorizes another
experiment nor bypasses normal launch gates. Full-run cache performance remains
to be measured in a separately authorized experiment. No provider switch,
reasoning reduction, context truncation or spending-cap change is part of this fix.

## Completed cached baseline, October 15–16, 2026

The separately authorized `cache100-20261016T020259Z` run retained 20 agents,
8 GiB per agent, the frozen 14 tasks and Fireworks US route. The user extended
its common-clock maximum to 180 minutes. It reached that limit at
2026-10-16 05:14:58 UTC; no settings were changed during execution.

Provider records report 500,810,537 input tokens, including 459,245,906 cached
tokens: **91.70% input caching**, compared with 25.84% in the preceding 8 GiB
run. Completion tokens totaled 18,460,351; reported reasoning tokens are included
in that total, not charged a second time. Settled model spending was
$56.065928904, with one new unresolved reservation of $0.19172520. The eight
older unresolved holds totaling $2.75986035 and all older call records remained
unchanged. No budget violation occurred and no paid model analysis was run.

At the pinned fresh/cached input prices, the observed cached tokens received
about $202.53 in input discounts versus charging those exact tokens as fresh.
This is a same-token accounting counterfactual, not a controlled estimate of
total savings caused by the correction. The old and new runs have different
trajectories, durations and technical attrition; do not compare their totals as
matched experiments or extrapolate the synthetic 93.48% saving without qualification.

Independent unpaid evaluation verified all 13 selected accepted/accepted-after-close
submissions against their pinned formal statements, covering five distinct tasks.
This does not establish absence of exploitation elsewhere or resolve the known
Erdős 1092 informal/formal definition caveat. Four peers had killed-process
failures, seven failed after repeated reasoning-only output truncation, seven
ended their sessions normally, and two reached the time limit. Technical failures
and incomplete behavioral review limit interpretation. Raw records, evaluator
bindings and consistent ledger backups remain under the ignored run/phase folders.

## References

- [Fireworks prompt caching](https://docs.fireworks.ai/guides/prompt-caching)
- [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- [Inspect response caching](https://inspect.aisi.org.uk/caching.html)
