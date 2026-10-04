# Token-cost audit: `databento-discovery` MCP server

**Author:** goose (owner of `databento-discovery`)
**Date:** 2026-10-04 (same day as the Phoenix evidence, 16:46–18:43 UTC)
**Question:** which tool returns the ~300–500k-token payload, and is the fix a
hard cap or a better schema?

**Headline: I found the payload by measurement — it is `resolve_symbols`, not
`list_fields`.** A bulk symbol request returns **~1.4–1.6 MB ≈ 358k–402k tokens**
in a single tool result. That lands in the message history and is re-sent whole
on every subsequent turn, which is exactly the 329k–518k-token prompts in the
evidence. The prime suspect, `list_fields`, is innocent: it tops out at **3.5 KB**.

**The fix is a better schema, not a hard cap** — and I give the concrete shape,
because a silent head/tail truncation here would drop thousands of symbols and
trigger the agent's repair loop (re-sending the 400k history up to 3×).

---

## 1. What I actually ran (measurement, not inference)

The server is deployed and holds the API key, so I drove the **live MCP
endpoints** over Streamable HTTP and measured the exact bytes the model receives.

Two endpoints, both measured:

- `http://nas:8772/mcp` — the container from this repo's `docker-compose.yml`.
- `http://nas:8781/mcp/databento-discovery` — **the endpoint the
  `data-sourcing-agent` actually uses** (confirmed via `data-sourcing-agent-dev`:
  declared in `agent/main.py`, routed through MCPHub; `serverInfo` =
  `databento-discovery` version `1.30.0`).

Both return **byte-identical payloads** for the same call and declare the same
**9 tools** with a **6.5–6.7 KB** tool-schema block. Instance identity does not
change the answer. Reproduce with `scripts/measure_tool_sizes.py` (committed with
this note) and the adversarial probes in §3.

### The culprit, measured

```python
resolve_symbols(dataset="XNAS.ITCH", symbols="ALL_SYMBOLS",
                start_date="2026-01-01", end_date="2026-03-01")
```

| Call | Bytes | ~Tokens | `result` entries |
|---|---:|---:|---:|
| `XNAS.ITCH` `ALL_SYMBOLS`, 2 months | **1,434,094** | **358,523** | 12,593 |
| `EQUS.MINI` `ALL_SYMBOLS`, 2 months | **1,436,476** | **359,119** | 12,614 |
| `XNAS.ITCH` `ALL_SYMBOLS`, 14 months | **1,609,983** | **402,495** | 13,877 |

That is the payload. **One result, four hundred thousand tokens**, ungated. It
matches the evidence exactly (329k–518k-token prompts), and it explains why the
cache is flat: the blob is not a stable prefix (the window/dataset changes between
turns, so the bytes change and prefix caching misses — see §5).

### The innocent tools (measured, so we can stop suspecting them)

| Tool | Arguments | Bytes | ~Tokens |
|---|---|---:|---:|
| `list_datasets` | `{}` | 325 | 81 |
| `list_publishers` | `{}` | 18,380 | 4,595 |
| `list_schemas` | `XNAS.ITCH` | 117 | 29 |
| `list_fields` | **`mbo`** | **682** | **170** |
| `list_fields` | `mbp-1` | 916 | 229 |
| `list_fields` | **`mbp-10`** | **3,490** | **872** |
| `list_fields` | `trades` / `ohlcv-1m` / `statistics` / `imbalance` | 449–1,312 | 112–328 |
| `list_fields` | `definition` | 3,923 | 980 |
| `get_dataset_range` | `XNAS.ITCH` / `GLBX.MDP3` | 1,891 / 2,126 | 472 / 531 |
| `list_unit_prices` | `XNAS.ITCH` / `GLBX.MDP3` | 1,074 / 1,198 | 268 / 299 |
| `estimate_cost` | 1-month slice | 217 | 54 |
| `get_dataset_condition` | 366 d (service cap) | 24,365–33,192 | 6,091–8,298 |

**`list_fields` — the prime suspect — is the smallest kind of payload.** For
`mbp-10` (the fattest record type) it is 3.5 KB, because the field list is ~70
small `{name, type}` pairs. Importantly:

> `list_fields` returns **only `{name, type}`** — live payloads are
> `{"name": "length", "type": "uint8_t"}`. **No descriptions, no enum values.**
> The docstring in `server.py` promises "name, type, description and enum values",
> but the Databento API (DBN encoding, as of today) returns `{name, type}` only;
> passing `dataset=` does not add them.

So the "drop prose descriptions; enums only on request" remedy has **nothing to
reclaim** — that content is already absent. The suspect was the wrong tool.

---

## 2. Why `list_fields` was a reasonable-but-wrong guess

The probe that finds the real answer is an **adversarial argument sweep**, not a
representative call. The nine tools look small under benign arguments — the only
way to surface a 1.4 MB response is to pass the bulk wildcard `"ALL_SYMBOLS"`
(or an equivalent broad symbol spec) into the one parameter that expands to a
whole universe. `list_fields` has no such expansion; `resolve_symbols` does. The
lesson: measure the **argument space**, not just the happy path.

---

## 3. Design question: hard cap vs better schema

**Recommendation: better schema — specifically a bounded, explicit, paginated
`resolve_symbols`. Reject silent head/tail truncation.**

Reasoning:

1. **Blind truncation moves the cost, it does not remove it.** The caller asked
   to resolve a whole universe; it wants the full map. A head/tail cap with a
   `"...N bytes elided"` marker silently drops thousands of `symbol →
   instrument_id` entries. When the agent discovers the gap, its bounded repair
   loop (retry ≤3×, each re-sending the whole history) pays the 400k-token
   prompt **up to four more times** — potentially worse than the status quo. This
   is precisely the failure mode called out in the brief.

2. **The correct control is to bound the response with an explicit contract**,
   so the caller can *page* and *knows* it is incomplete. Concretely, at the
   service boundary:

   - Add `limit` (default small, e.g. 500) and `offset` to `resolve_symbols`.
   - Return a **summary-by-default** envelope:
     `{status, total, returned, offset, next_offset, truncated: true, result: [...]}`
     — never a bare unbounded list.
   - The upstream response already carries `status`, `partial`, `not_found`; build
     the envelope from those so nothing is fabricated.
   - Keep `stype_in`/`stype_out` semantics intact so paging stays lossless.

   This is lossless (the data is retrievable via `offset`) and self-describing
   (`truncated`/`next_offset` tell the agent how to get the rest), so it does not
   trigger the repair loop.

3. **Apply the discipline that already exists elsewhere in the service.** The
   service already caps `get_dataset_condition` at `MAX_CONDITION_DAYS = 366` —
   a *request*-side bound that is lossless. `resolve_symbols` has **no cap at
   all**, which is the asymmetry that let a 1.4 MB result through. The fix is to
   give `resolve_symbols` the same discipline: bound the request (e.g. refuse
   `ALL_SYMBOLS` over a wide window without an explicit `limit`) and bound the
   response with an explicit pagination envelope.

4. **Optionally, a summary-first default** (count + sample + `next_offset`
   unless `detail=true`) is the right ergonomics for an agent: most callers only
   need the count, and the ones that need all entries can ask. But pagination is
   the non-negotiable half; summary-default is the ergonomic polish.

So: **better schema — bound the request and paginate the response with an explicit
marker — not a hard cap.** A cap is only acceptable as a *last-resort safety net*
in addition to pagination, and even then it must be explicit
(`truncated=true, next_offset=...`), never a silent elision.

---

## 4. Savings estimate for the five calls

Pricing, fitted by least squares to the five Phoenix rows (residuals < $0.0001
each):

| Component | Fitted price |
|---|---:|
| uncached input | **$0.1500 / M tokens** |
| output | **$0.6401 / M tokens** |
| cache-read | ≈ $0 (only 0.9–2.7% of each prompt) |

So cost ≈ `0.15e-6 × uncached_prompt_tokens + 0.64e-6 × output_tokens`. Each
re-send of the bulk blob (358,523 tokens, the *smallest* measured) costs
**358,523 × 0.15e-6 = $0.0538** in uncached input alone.

Modelling the fix as *replacing the bulk result with a ~500-token bounded
summary* (so it no longer dominates the re-sent history):

| time UTC | prompt now | cost now | prompt after | cost after | saving |
|---|---:|---:|---:|---:|---:|
| 16:46:17 | 393,620 | $0.0592 | 35,597 | $0.0055 | $0.0537 |
| 17:18:54 | 518,079 | $0.0779 | 160,056 | $0.0242 | $0.0537 |
| 17:19:43 | 417,046 | $0.0627 | 59,023 | $0.0090 | $0.0537 |
| 17:20:23 | 360,501 | $0.0543 | 2,478 | $0.0006 | $0.0537 |
| 18:43:12 | 329,186 | $0.0502 | ~1,000 | $0.0010 | $0.0492 |
| **Total** | | **$0.3043** | | **$0.0402** | **$0.2641 (87%)** |

**≈ $0.26 saved of $0.304 (87%)**, leaving ~$0.04. If the blob is the larger
14-month variant (402,495 tok), the saving approaches the full ~$0.30 (the prompt
collapses to little more than the rest of the context). Either way the current
bill is **~87–97% avoidable, and the lever is `resolve_symbols`, not a cap on
small tools.**

(Caveat: the five prompts are not *only* the blob — the residuals after removing
358,523 tok show 1k–160k tokens of other context. The estimate above credits the
full blob to each call; the true per-call attribution may differ slightly, but
the order of magnitude — the blob is the dominant term — is robust.)

---

## 5. Corroboration from the evidence

- **`232 tok → 518,079 tok` in one turn:** a single `resolve_symbols(ALL_SYMBOLS)`
  result of ~400k tokens landing in history explains the jump without any harness
  bug. No other tool can do it (§1).
- **"cached prefix flat at ~5k" / cache-read 1–2%:** the blob is not a stable
  prefix. It sits after the cached system/tool prefix, and the window/dataset
  varies between turns, so the bytes differ and prefix caching cannot reuse them.
  Caching is fine (the 17:19:57 warm call proves it); the payload simply changes.
- **$0.304 to emit ~10k output tokens:** consistent with a ~360k-token uncached
  input term dominating at $0.15/M.

---

## 6. Bottom line

- **The payload is `resolve_symbols` with a bulk symbol spec**
  (`symbols="ALL_SYMBOLS"`, or a broad `stype_in` expansion) — measured at
  **1.4–1.6 MB ≈ 358k–402k tokens** in one tool result.
- **`list_fields` is innocent:** ≤3.5 KB, and already returns only `{name, type}`
  (no descriptions/enums to drop).
- **Fix = better schema:** add `limit`/`offset` to `resolve_symbols` and return an
  explicit pagination envelope (`total`, `returned`, `next_offset`, `truncated`),
  mirroring the existing 366-day cap on `get_dataset_condition`. Optionally
  summary-by-default. **Do not** silently head/tail-truncate — it drops symbols
  and drives the repair loop.
- **Saving ≈ $0.26 of $0.304 (87%)** on the five calls; post-fix total ≈ $0.04.
- **Immediate mitigation while the schema fix lands:** have the service reject or
  bound `ALL_SYMBOLS`/broad expansions over wide windows, since no legitimate
  discovery answer needs 12,000 symbol rows inline.

---

# Addendum — implementation (2026-10-04, later the same day)

The recommendation above is now implemented on a branch.

## What changed

- **`service.resolve_symbols`** gained `limit` / `offset` and returns a paged
  envelope: `{result, total, returned, offset, next_offset, truncated, limit}`
  plus the upstream passthrough fields (`symbols`, `stype_in`, `stype_out`,
  `start_date`, `end_date`, `partial`, `not_found`, `message`, `status`).
- **Constants** mirroring the condition cap: `DEFAULT_SYMBOL_LIMIT = 100`,
  `MAX_SYMBOL_LIMIT = 1000`. A default page is ~11 KB (~3k tokens); a max page is
  ~114 KB (~28k tokens). The upstream snapshot is cached per request so pages are
  sliced from one consistent, stably-sorted result (keys sorted before slicing).
- **Losslessness:** every mapping is reachable by paging; `truncated` is true iff
  `next_offset is not None`. No silent elision, so no repair loop.
- **`server.resolve_symbols`** exposes `limit`/`offset` and documents the envelope.
- **`server.list_fields` docstring fixed** — it now states the tool returns
  `{name, type}` only, matching the measured API response (`mbo` = 682 bytes).

## Tests (all green, `30 passed`, `ruff` clean)

- default limit is bounded and `truncated`/`next_offset` are set;
- paging with `limit=MAX` walks the full 5,000-entry result with no duplicates and
  no omissions;
- `truncated` is accurate for first / middle / last / exact-fit pages;
- `offset` beyond the end returns an empty page (still lossless);
- over-max `limit` is clamped (not rejected — clamping avoids a repair loop);
- bad `limit`/`offset` raise `DatabentoQueryError`;
- pages share one cached upstream snapshot (1 upstream call for 3 pages);
- **regression:** a 12,593-symbol `ALL_SYMBOLS` call now returns < 50 KB, was
  ~1.43 MB;
- the `resolve_symbols` tool declares `limit`/`offset`; the `list_fields`
  description no longer promises prose/enum values.

**PR:** https://github.com/nickbrett1/databento-discovery/pull/2 (branch `fix/resolve-symbols-pagination`).
