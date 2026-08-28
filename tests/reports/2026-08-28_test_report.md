# Test report — 2026-08-28

**500 pytest tests, all passing.** Plus two standalone scripts: `tests/test_schema_constraints.py`
(65 assertions against `schema/schema.sql`) and the vault's `schema_test.py` (62 assertions
against its copy). `tests/doc_consistency.py` runs 146 checks with no inconsistency.
`tests/mutation_run.py`: **53 mutants, all 53 caught by a named test.**

## Counts by file

| File | Tests | What it claims |
|---|---|---|
| `test_core_capabilities.py` | 109 | Every capability's parameters, returns and rejections, one at a time |
| `test_adapters.py` | 83 | The three adapters against their real remotes' shapes |
| `test_foundation.py` | 61 | errors, labels, config, db, responses, slots, templates |
| `test_boundaries.py` | 46 | Attacks on stated authorization, isolation and ordering claims |
| `test_extension_points.py` | 27 | The extension interface and its refusing defaults |
| `test_paused_row_invariant.py` | 24 | Seeded property run: at most one paused **work** row per label |
| `test_polling_working_slot.py` | 18 | The drain pass, the slot, and what a failure must not lose |
| `test_interruption_flow.py` | 18 | An agent that cannot continue on its own |
| `test_mcp_surface.py` | 16 | Tool registration and the rendered bodies |
| `test_search_relevance.py` | 15 | The relevance floor and deliberate substrings |
| `test_thread_lifecycle.py` | 12 | Which partners a process will and will not drain |
| `test_partner_replies.py` | 11 | The reverse handshake carries answers, never delegation |
| `test_lifecycle_safety.py` | 10 | Archiving and deletion, and who is owed a word |
| `test_observability.py` | 9 | Logging, the diagnostic report, and `status` |
| `test_inheritance.py` | 8 | One Antigravity conversation continuing another |
| `test_adapter_recovery.py` | 8 | A fresh adapter recovering ids it did not create |
| `test_summary_phase.py` | 6 | A `[RESEARCH]` summary that goes back into the queue |
| `test_schema_migration.py` | 5 | Additive columns on a database that predates them |
| `test_queue_order.py` | 4 | The two-statement head read |
| `test_notebook_query.py` | 4 | A notebook asked in its own terms |
| `test_schema_suite.py` | 3 | Schema vs vault copy vs documentation |
| `test_mcp_config.py` | 3 | Building the stack from the environment |

## What changed this pass

### `test_interruption_flow.py` — 15 → 18

Rewritten for the removal of `[IDLE]`. `test_nothing_displaces_a_waiting_agent`
became `test_only_a_truthful_report_displaces_a_waiting_agent`, because the
corrected design leaves `[QUERY]`/`[ERROR]` at their natural priority of 2 and a
`[TRUTHFUL-REPORT]` at 1 therefore does displace a wait. Three tests added:

- `test_a_stopped_agent_cannot_ask_a_second_question` — both blocking labels
  refused with `already_awaiting_an_answer`, and nothing half-applied: the
  original wait intact, no requeued row, nothing reaching the target.
- `test_a_stopped_agent_may_ask_again_once_it_is_answered` — the refusal is a
  wait, not a ban.
- `test_a_displaced_wait_outranks_ordinary_paused_work_of_its_own_label` — two
  paused `[QUERY]` rows, same label, the work row older, so chronology alone
  picks the wrong one. **This test found a real deadlock** (below).

### The deadlock it found

Written for a tie-break, it failed on its last assertion instead: the
`[MESSAGE-RESPONSE]` never reached the agent. `[MESSAGE-RESPONSE]` sits at
priority 3, below `[QUERY]`/`[ERROR]` at 2 and `[TRUTHFUL-REPORT]` at 1 — so an
agent whose paused work carried one of those labels had a queue head that was
never the answer, and `advance` returned `None` on every pass forever. `advance`
now looks the answer up by label while the slot is awaiting. Mutant
`the-answer-is-ordered-by-priority` covers it.

Every existing test passed the buggy code, because each used a paused
`[RESEARCH]` at priority 4 — which the answer beats.

### `test_boundaries.py` — 46, nine rewritten

`interrupt_partner` was the file's tool for forcing a displacement. A module
helper `evict()` replaces it, doing exactly what `advance`'s swap leaves behind
(release the slot, requeue paused) — the tests are about which row `advance`
picks *next*, and staging a real displacement would put the displacing message
into the slot under test.

Several tests moved from `workerA` to `bridgeA`, the one target two different
callers can hold a standing handshake to at once: an agent that asks a `[QUERY]`
is now stopped by it, so "first" and "second" can no longer come from one caller.
`test_cap_counts_the_working_slot_deterministically` moved from `[QUERY]` (cap 3)
to `[RESEARCH]` (cap 2) for the same reason — `[QUERY]` cannot be sent three
times by one agent any more, and a refusal that has nothing to do with caps would
have masked the one under test.

`test_idle_hold_is_never_requeued_after_being_displaced` became
`test_a_waiting_agents_own_question_is_never_handed_back_to_it_as_work`, and
`test_interrupt_partner_cannot_cross_projects` became
`test_a_partner_in_another_project_cannot_be_reached_or_stopped` — there is no
capability to attack directly, so the attack is the send.

### `test_core_capabilities.py` — 110 → 109

Three `test_interrupt_partner_*` tests and `test_send_rejects_idle_not_sendable`
deleted. Two added in their place, covering what actually stops a partner now:
`test_a_strictly_higher_arrival_displaces_the_working_task_and_stops_the_remote`
and `test_an_equal_priority_arrival_does_not_displace_and_does_not_stop_the_remote`.

`test_a_partner_on_an_uncancellable_remote_can_still_be_displaced` now asserts
**two** recorded uncancelled stops rather than one: the caller's `[QUERY]` stops
the caller too, and that remote refuses as well.

### `test_summary_phase.py` — 5 → 6

`test_a_summary_whose_delivery_fails_still_reports_its_result_to_the_caller`
added. Nothing displaces a summary any more — it runs at `[TRUTHFUL-REPORT]`'s
own priority and `advance` needs a strictly lower number — so the reachable route
back into the queue is a failed delivery through `_requeue`. The
`summary-phase-not-carried` mutant was retargeted from `_swap` to `_requeue` to
match; on `_swap` it had begun surviving, correctly, because that path is now
defensive.

### `test_paused_row_invariant.py` — 24

The property run found two paused `[ERROR]` rows and reported it as a violation.
It was right that the situation is new and wrong that it is a defect: a displaced
wait is stored paused and carries the label of the question asked, so it can
coexist with a paused work row of that label. The counting query is now scoped to
`awaiting_resolution = 0`, with the reason in a comment — a wait is never
rendered, so it is never what a resume prompt names.

### Mutants

Five removed (they named deleted code), seven added:

| Mutant | Caught by |
|---|---|
| `a-wait-is-typed-at-the-agent` | `test_a_waiting_agents_own_question_is_never_handed_back_to_it_as_work` |
| `an-asker-does-not-stop-itself` | same (14 tests total) |
| `a-second-question-while-stopped` | `test_a_stopped_agent_cannot_ask_a_second_question` |
| `a-bare-response-is-the-prompt` | `test_the_answer_is_concatenated_with_paused_work` |
| `the-answer-is-ordered-by-priority` | `test_a_displaced_wait_outranks_ordinary_paused_work_of_its_own_label` |
| `a-displaced-wait-comes-back-as-work` | `test_a_waiting_agents_own_question_is_never_handed_back_to_it_as_work` |
| `a-wait-does-not-outrank-its-own-queue` | `test_a_displaced_wait_outranks_ordinary_paused_work_of_its_own_label` |

Four existing mutants (`priority-inverted`, `in-process-ignored`,
`displaced-not-paused`, `in-process-crosses-labels`) reported "pattern not found"
after the SQL and the insert gained a column — patterns updated. A pattern that
stops matching is a mutant that silently stops testing anything, which is why the
runner distinguishes it from a survivor.

## Failures and skips

No failures. `test_schema_suite.py::test_shipped_schema_matches_the_vault` skips
rather than fails when the vault is not mounted — a missing mount is not a
defect in the schema.
