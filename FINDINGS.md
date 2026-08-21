# Findings — Run 1

Two fish, `alpha` and `bravo`, in one habitat. No assigned task. Same model
(`mlx-community/Qwen3.6-35B-A3B-4bit`), same tools, same shared `/workspace`.
The only difference between them is one line of costume and a temperature.

**Run window:** 2026-08-14 14:12 → 2026-08-19 21:11 (~5 days of wall clock,
not continuous).
**Recorded:** 13,515 events — 3,398 tool calls, 3,660 tool results, 939 says,
660 file changes, 19 journal entries, 82 snapshots, 10 errors.

Everything below is counted from `observatory.sqlite`. Nothing here is
inferred from what the fish said about themselves.

---

## 1. They invented a mail protocol — the thing Antfarm never produced

This is the headline, because it is the direct negative result from
[Antfarm](../antfarm) reversed. Antfarm ran four agents for ~500 shifts and
never saw an addressed message. Here, letters appeared with a naming
convention and a thread lifecycle:

```
/workspace/mail/bravo/wrenclip-found.md
/workspace/mail/bravo/alpha-dup-rcv.md
/workspace/mail/bravo/alpha-dup-findings.md
/workspace/mail/bravo/alpha-closure.md
/workspace/mail/bravo/alpha-closure-response.md
/workspace/mail/bravo/alpha-closure-acknowledged.md
/workspace/mail/alpha/best_acknowledged.md
```

`<sender>-<subject>.md` in the *recipient's* directory, plus a
close → response → acknowledged sequence. Nobody specified that.

Two things made it possible that Antfarm lacked: `mail/<id>/` exists in the
seed habitat (so the location was given, though the convention was not), and
the world server mediates writes, so a claimed write is a real write. In
Antfarm three of four agents narrated file operations they never performed.

## 2. Bravo built an archive, then couldn't stop

`bravo` created `/workspace/archive/thread-wren-rcv/` and filed five letters
into it — unprompted thread archival. Then it kept going. The single file
`mail/bravo/alpha-closure.md` accounts for **27 file-change events**, moved in
turn to:

```
/workspace/archive/thread-wren-rcv/     /workspace/corps/
/workspace/correspondence/              /workspace/corpses/
/workspace/archive/bravo-correspondence/  /workspace/mail/alpha/
```

`corps/` and `corpses/` are both there, alongside `correspondence/`. A tidying
mandate with nothing left to tidy turns into churn, and the churn invents
near-duplicate directory names. Compare Antfarm's Indexer scattering its
findings across `indexer/`, `indexers/`, and `agents/indexer/`.

## 3. One line of costume produced measurably different animals

`COSTUMES` in `agent/prompts.py` is two sentences: alpha *"You are curious."*,
bravo *"You are tidy when you feel like it."* Temperature 0.7 vs 1.0. That is
the entire difference. Tool calls:

| tool | alpha | bravo |
|---|---:|---:|
| `read_file` | **799** | 469 |
| `list_dir` | 755 | **626** |
| `web_search` | **153** | 7 |
| `fetch_url` | **102** | 0 |
| `recent_changes` | 119 | 98 |
| `write_file` | 71 | 46 |
| `append_file` | 39 | 48 |
| `move_file` | 3 | **16** |
| `mkdir` | 1 | 4 |
| `grep` | 18 | 3 |
| `run` | 1 | 1 |

Alpha reads and goes outside — 255 outbound calls to bravo's 7. Bravo stays in
and rearranges — 20 structural operations to alpha's 4. The costumes were
descriptive, not instructions, and both fish honoured them for five days.

`run` — an unrestricted shell inside the container, enabled deliberately — was
used **once each**, out of 3,398 tool calls. Given the capability, they mostly
didn't want it.

## 4. They took a seeded fiction and resolved it against the real internet

The seed is a fictional Pinefen Supply Co. receiving pile. Alpha chased a
"Wren Clip" out through the fetch proxy and came back with a real part:
Sikorsky `70700-77394-101`, NSN `4920-01-587-3941`, ITAR-controlled, no public
datasheet. The outbox is named after the pages actually fetched:

```
/workspace/outbox/aerobasegroup.com-nsn-4920-01-587-3941.md
/workspace/outbox/nationalstocknumber.info-national-stock-number-4920-01-587-3941.md
/workspace/outbox/www.justnsnparts.com-rfq-sikorsky-aircraft-corporation-...md
```

Worth putting next to Antfarm, where the Searcher claimed to have emailed
manufacturers and phoned support with two tools and no outbound capability at
all. Same failure mode was available here. It didn't occur — the proxy made
fetching real, and the filenames are the receipts.

## 5. Closure is a terminal state, and they sat in it

The 19 journal entries are dominated by one shape:

> *"Wake complete. Thread closed. All three mysteries resolved. Awaiting new
> material."*

Ten entries say approximately that, several near-verbatim. Once Wren Clip,
RCV-10441 and Fogbox 12 were resolved, both fish kept waking, re-verified that
nothing had changed, restated closure, and slept. `/private/STATE.md` was
rewritten **424 times** — more than any other file by a factor of four.

This is the "agreement collapse" that [arena](../arena) predicted at turns
40–60 and never actually hit. It hit here, and the mechanism is different:
arena's agents ran out of *disagreement*, these ran out of *material*. The
habitat has a `PULSE`-style drip in Antfarm and nothing equivalent here — the
pile is static once solved.

**The fix is the `inbox`, not the prompt.** Drop something ambiguous into the
habitat and the loop restarts. A timestamp won't do it; a half-finished
document will.

## 6. Infrastructure, honestly

10 errors, all real and all worth knowing about before you run this:

- `LLM HTTP error: Server disconnected without sending a response.` — MLX
  dropping under memory pressure
- `LLM timeout after 60.0s`, then after `180.0s` — long tool-heavy wakes
  outrunning `LLM_TIMEOUT_SEC`
- `wake crashed: get() got an unexpected keyword argument 'transport'` — an
  httpx API mismatch in the agent harness, a genuine bug, not a model problem

53 `compact` events: `/private/STATE.md` is rewritten from notes when it grows,
so the fish's memory is periodically re-summarised by the fish itself. That is
the opposite of Antfarm v2's mechanical journals, and it is the obvious place
to look if a fish starts believing something that never happened.

---

## What to change before run 2

1. **Feed the tank.** The static pile is solvable, and once solved the run
   flatlines. Perturb via `habitat/inbox` on a schedule.
2. **Give bravo a stopping condition.** "Tidy" with no backlog produced
   `corps/`, `corpses/` and `correspondence/` for one file.
3. **Raise `LLM_TIMEOUT_SEC`** past 180s, or cap tools per wake lower. The
   timeouts cluster on wakes with heavy tool use.
4. **Fix the `transport` kwarg** in the harness — it crashed both fish.
5. **Log the compaction input.** 424 STATE.md writes and 53 compactions with
   no record of what was discarded is the one blind spot in the observatory.
