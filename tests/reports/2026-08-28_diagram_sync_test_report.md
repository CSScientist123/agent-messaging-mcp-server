# Test report — 2026-08-28 (diagram sync)

**500 pytest tests, all passing** — unchanged by this work, which touched
diagrams, two code comments and the audit script. **53 mutants, all caught by a
named test.** `tests/doc_consistency.py`: **186 checks, up from 146**, green.
Vault `schema_test.py`: 62 passing. Vault `vault_check.py`: 4 pre-existing
problems in files this pass did not touch.

## The audit itself was the test

No new pytest tests. The work was verification-shaped rather than
behaviour-shaped: ten diagrams audited against the code, six mechanical checks
added so the audit does not have to be repeated by hand.

Three parallel Explore agents audited the ten diagrams claim by claim. Every
finding they reported was re-verified against the source before being acted on;
four spot-checks are recorded below because they are the ones that would have
been embarrassing to take on trust.

| Claim | Verified how | Result |
|---|---|---|
| Server names are `messaging-nlm` / `messaging-gemini` | `mcp_server/server.py:869` builds from `source_prefix.rstrip('_')` | confirmed, diagram wrong |
| polling imports neither `db` nor `labels` | read `polling/server.py:33-43` in full | confirmed, two phantom edges |
| `bridge_single_code_partner` runs bridge→code, not code→bridge | the query keys on `science_end["id"]` and excludes `code_end["id"]` | confirmed, cardinality inverted |
| `[ERROR]`/`[RESEARCH]` have non-NULL `reply_behavior` | `schema/schema.sql:113-118` seed rows | confirmed, worst single error |

## The six new checks, and proof each can fail

A check that cannot fail is not a check. Each was broken deliberately once and
observed to fail **by name**, then the tree was restored and re-verified green —
the same standard `tests/mutation_run.py` holds.

| Check | Break applied | Failure observed |
|---|---|---|
| `mmd/labels` | re-added `[IDLE]` to `04` | `[mmd/labels/04-priority-queue.mmd] names [IDLE], which is not a row in label_caps` |
| `mmd/handshake-branches` | renamed `gemini_already_inherited` in `07` | `handshake can raise 'gemini_already_inherited', and 07 draws no branch for it` |
| `mmd/extension-coverage` | renamed `read_remote_result` in `08` | `RemoteExtension.read_remote_result is not drawn in 08-extension-boundary` |
| `mmd/triggers` | deleted `partner_paths_gemini_only` from `03` | `trigger 'partner_paths_gemini_only' is not named in the ER diagram` |
| `dead-ref` (extended) | added an `interrupt_partner` node to `08` | `[dead-ref/08-extension-boundary.mmd] 'interrupt_partner' still described as live` |
| `mmd/rendered` | `touch`ed `03-schema-er.mmd` | `03-schema-er.mmd is newer than its .png -- run ./render-diagrams.sh` |

The last one was then cleared by running `./render-diagrams.sh`, confirming the
remediation the message names actually works.

## One false positive, caught and fixed

Adding the render check made `MMD` glob the new `.png` files, and reading image
bytes as text manufactured label-shaped tokens: `[COOS]`, `[KK]`, `[V]`, `[Q]`.
Four spurious failures. `MMD` is now filtered to `.mmd` sources, with the reason
in a comment. Worth recording because it is the same class of error the new
checks exist to catch — a surface silently growing past what a rule assumed.

## Rendering as a test

`./render-diagrams.sh` renders all ten sources. Because Mermaid refuses to render
a file it cannot parse, a clean run is also a syntax check on every edit made
this pass — all ten rendered on the first attempt after editing. The WSL
`--no-sandbox` risk flagged in the plan did not materialise; puppeteer's cached
Chrome worked unmodified.

## Coverage gap this leaves

`mmd/*` checks that the code's facts all *appear* in the diagrams. It cannot
check that a diagram's prose *explains* them correctly, that an arrow points the
right way, or that branch ordering matches control flow — the `07` ordering
defect would not have been caught by any of these checks, only by the read. That
limit is stated in the script's own MANUAL section, which is where it belongs.
