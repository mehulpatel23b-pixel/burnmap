# burnmap

**Map exactly where your AI coding agent burns tokens and money.**

`burnmap` reads the session logs your agent already writes and turns them into a
per-turn spend map: which model cost what, which tool ate the budget, and how much
of the bill was avoidable (loops, cache misses, context creep). One command, zero
dependencies, no API access required.

```text
burnmap - session spend map
==============================================================
turns           15
tokens          75,540  (in 69,380 / out 6,160 / cache read 44,800 / cache write 4,160)
total cost      $0.3249
cost per turn   $0.0217
cache hit rate  39.2%

COST BY TOOL
  Read                        $0.1026     4 calls       5,862 tok/call  ########################
  Bash                        $0.0813     5 calls       3,466 tok/call  ###################.....
  Edit                        $0.0736     3 calls       5,377 tok/call  #################.......
  Write                       $0.0621     1 calls      17,100 tok/call  ###############.........

WASTE
  [context_creep] 6 late turns averaged 11,550 input tokens vs a 210 baseline
      ~$0.1822 over 61,232 tokens  (turns 8, 9, 12, 13, 14, 15)
      fix: Compact the transcript or start a fresh session per task instead of
           carrying the whole history.
  [cache_misses] turn 14: 16,200 uncached input tokens
      ~$0.0437 over 14,580 tokens  (turns 14)
      fix: Keep the system prompt and file reads byte-stable, then re-mark the
           cache breakpoint.
  [loops] Bash(pytest -q tests/test_api.py) called 4x in a row
      ~$0.0059 over 640 tokens  (turns 3, 4, 5, 6)
      fix: Read the file once and patch it, or widen the glob so one call covers
           every match.

estimated avoidable spend: $0.1881 (57.9% of this session)
```

That is real output from `sample/session.jsonl` in this repo, not a mock-up.

---

## Why this exists

Agent bills do not fail loudly. They drift. A session that should cost $0.08 costs
$0.31 because the agent re-read the same file four times, because a cache breakpoint
was invalidated after turn 6, and because the transcript grew to 16k input tokens
while the agent was still grepping for one bug. You find out at the end of the month,
on an invoice with a single number and no explanation.

Dashboards give you totals. `burnmap` gives you **attribution**: this dollar, that
turn, that tool, that root cause, that one-line fix.

| Question | Answer source |
| --- | --- |
| Which tool is actually expensive? | `COST BY TOOL` ranks every tool by attributed spend and tokens per call |
| Is caching working? | `cache hit rate` plus a per-turn `cache_misses` list |
| Why is this session 4x the budget? | `WASTE` buckets with turn numbers and dollar estimates |
| Is my cheaper model swap helping? | `COST BY MODEL` and `--pricing` lets you price hypotheticals |
| Can this regress in CI? | `--fail-over` turns avoidable spend into an exit code |

---

## Installation

### Run from source, no install

```bash
git clone https://github.com/mehulpatel23b-pixel/burnmap.git
cd burnmap
./burnmap sample/session.jsonl
```

The `./burnmap` launcher adds `src/` to `sys.path`, so it works on a bare Python
3.9+ install with nothing else present.

### Install as a command

```bash
pip install .
burnmap --help
```

### Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

### Requirements

Python 3.9 or newer. That is the entire list. No API keys, no network calls, no
telemetry, no vendor SDK. Your session logs never leave the machine.

---

## Usage

```bash
# one session
burnmap ~/.claude/projects/-Users-me-app/9c1f2e3d.jsonl

# every session in a directory tree, summed into one map
burnmap ~/.claude/projects/**/*.jsonl

# pipe a log straight in
cat session.jsonl | burnmap -

# CI gate: fail the job when avoidable spend passes 10 cents
burnmap session.jsonl --fail-over 0.10

# price a model swap properly
burnmap session.jsonl --pricing rates.json
```

### Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--json` | off | Machine-readable output with a stable key order |
| `--top N` | 8 | Rows shown in the tool ranking |
| `--loop-threshold N` | 3 | Consecutive identical calls before it counts as a loop |
| `--fail-over USD` | off | Exit `2` when estimated avoidable spend exceeds this |
| `--pricing FILE` | built-in table | JSON rate overrides, including a `default` catch-all |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Analysed successfully |
| `1` | File unreadable, or no usable usage records found |
| `2` | `--fail-over` budget exceeded (CI gate) |

### Where sessions live

| Agent | Typical path |
| --- | --- |
| Claude Code | `~/.claude/projects/<project-slug>/*.jsonl` |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` |
| Anything else | point `burnmap` at any JSONL that carries token counts |

---

## Architecture

```text
                 ┌──────────────┐
  session JSONL  │   parser.py  │  shape detection, one Turn per response
  (any format)   └──────┬───────┘
                        │  Turn(index, model, tokens, tools, targets)
                        ▼
                 ┌──────────────┐
                 │  pricing.py  │  model name -> Rate, override aware
                 └──────┬───────┘       input / output / cache_read / cache_write
                        │
                        ▼
                 ┌──────────────┐
                 │  analyze.py  │  per-turn USD, model + tool attribution,
                 └──────┬───────┘  three waste rules, de-duplicated
                        │
                        ▼
              ┌───────────────────┐
              │     report.py     │  text map or stable JSON
              └─────────┬─────────┘
                        ▼
                 ┌──────────────┐
                 │    cli.py    │  argv, exit codes, CI gate
                 └──────────────┘
```

Each stage is a pure function over data, which is why the whole suite runs in under
a second and every rule is unit-testable without a fixture filesystem.

### Stage 1 — parse (`src/burnmap/parser.py`)

Reads JSONL line by line and normalises three real-world shapes into one `Turn`:

* **Anthropic Messages / Claude Code** — `message.usage` with `input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and
  `content[].type == "tool_use"` blocks.
* **OpenAI / Codex** — `usage.prompt_tokens`, `completion_tokens`,
  `prompt_tokens_details.cached_tokens`, and `choices[0].message.tool_calls`.
* **Flat** — top-level token counters and a `tools` array.

Mixed-format logs are fine: each line is detected independently. Junk lines, comment
lines, user turns, and records without usage numbers are skipped rather than fatal,
so you can feed a whole messy directory in. Malformed tool-call JSON falls back to a
raw target instead of raising. Model names are cleaned (`claude-sonnet-4-5[1m]`
becomes `claude-sonnet-4-5`) so pricing keys match.

### Stage 2 — price (`src/burnmap/pricing.py`)

A `PriceBook` maps a model string to a `Rate` of four numbers per million tokens.
Matching is a case-insensitive substring test with **longest key first**, so
`gpt-4o-mini` never accidentally resolves to `gpt-4o`. User overrides from
`--pricing` are always tested before the built-in table, so a short override like
`"claude"` genuinely wins over the longer built-in `"claude-sonnet"`. Unknown models
use a documented fallback rate and are **listed in the report** so a guessed rate is
never silent.

### Stage 3 — attribute and score (`src/burnmap/analyze.py`)

Every turn gets a cost, then that cost is attributed to each tool it invoked (split
evenly across a batch, so the tool totals reconcile with the grand total). Tool
ranking uses that attribution plus tokens per call, which is the number that actually
explains cost: `Write` is often expensive on a single call, `Bash` on volume.

Then three waste rules run.

**Loops.** Three or more consecutive calls with an identical tool signature
(name plus target, e.g. `Bash:pytest -q`). The first call is legitimate work, so only
the repeats are charged. Distinct targets never merge, so a legitimate sweep over
`file1.py`, `file2.py`, `file3.py` is not a loop.

**Cache misses.** In a session that clearly uses prompt caching, a turn with 2,000+
input tokens and zero cache reads is priced at the input rate but *charged* at the
input-minus-cache-read delta, which is the real money lost. If a session never caches
at all, this rule stays silent: you cannot miss a cache you never had.

**Context creep.** Splits the session in half and compares medians. The second half
must be at least 1.8x the first half by median and at least 2,000 tokens fatter
before anything is flagged, then only the excess above the baseline is charged. Using
medians and a first-half baseline means one genuinely huge file read does not
masquerade as creep.

**Honesty invariant.** Rules overlap: a loop turn can also be a cache miss. Naively
summing them produced a "waste" figure of 116% of the session bill in development.
So each rule records per-turn amounts, and the total takes the **largest single
attribution per turn**. Avoidable spend is therefore mathematically capped at the
session's actual cost:

```python
self.waste_cost_usd = min(sum(worst_per_turn.values()), self.total_cost_usd)
```

A test asserts this invariant, because a spend tool that can quote more waste than
you spent is a spend tool nobody trusts.

### Stage 4 — report (`src/burnmap/report.py`)

Text output is deliberately plain: no ANSI colour, no cursor control, no Unicode
box drawing. It renders identically in a terminal, a CI log, a Slack paste, and a
GitHub issue. JSON output has a stable key order so downstream diffing works.

---

## Project structure

```text
burnmap/
├── burnmap                  # dependency-free launcher (./burnmap ...)
├── pyproject.toml           # packaging + console script
├── pytest.ini
├── requirements.txt         # points at the dev set; runtime needs nothing
├── requirements-dev.txt     # pytest
├── LICENSE                  # MIT
├── .gitignore
├── .github/workflows/ci.yml # tests on 3.9 - 3.13
├── sample/
│   └── session.jsonl        # realistic mixed-format 15-turn session
├── src/burnmap/
│   ├── __init__.py
│   ├── __main__.py          # python -m burnmap
│   ├── parser.py            # JSONL -> Turn objects
│   ├── pricing.py           # model -> Rate, overrides
│   ├── analyze.py           # attribution + waste rules
│   ├── report.py            # text / JSON renderers
│   └── cli.py               # argparse, exit codes
└── tests/
    ├── test_parser.py
    ├── test_analyze.py
    └── test_report_cli.py   # includes subprocess CLI contract tests
```

---

## Real-world use cases

### 1. Stop the monthly invoice surprise

Run it over last week before anyone asks. `COST BY MODEL` shows whether the Opus
default is the reason, `COST BY TOOL` shows whether it is a specific workflow (test
running, web fetching, file writes), and the `WASTE` section usually names one
concrete habit to change. Teams routinely find 40-60% of a session bill sitting in
one of the three buckets.

### 2. CI budget gate for agent-authored changes

Agents that run in CI can quietly spend real money. Give the job a ceiling:

```yaml
- name: Agent session budget
  run: burnmap agent-session.jsonl --fail-over 0.25
```

Exit code `2` fails the build with the offending turns printed to stderr. The gate
is about *avoidable* spend, so an expensive but efficient session still passes.

### 3. Model swap decisions with real numbers

You think a cheaper model will cut the bill. Re-price the exact same session:

```bash
burnmap session.jsonl --pricing swap.json --json
```

```json
{ "claude-sonnet-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08} }
```

Same tokens, new rates, directly comparable totals. No re-running the agent, no
guessing, no benchmark vibes.

### 4. Find the prompt that broke caching

A cache hit rate that collapses mid-session usually means the prefix changed: a
timestamp injected into the system prompt, a reordered tool list, a file re-read
with different whitespace. `cache_misses` points at the exact turns where caching
stopped paying, and the fix line names the usual culprits.

### 5. Teach the habit, not the invoice

The per-turn attribution is a teaching tool. "Your `Write` calls cost 3x your `Read`
calls because each one re-sends the full context" is actionable in a way that a
dashboard total never is. Good pairing for a team retro or an agent-instructions
tune-up.

### 6. Regression-test an agent prompt change

Keep a golden session log. Change the system prompt, replay a comparable session,
diff the totals. A prompt edit that improves quality but doubles input tokens is a
trade-off you should be choosing deliberately, not discovering at renewal.

---

## Design decisions

**Zero dependencies.** Not minimal dependencies: zero. Standard library only, so it
runs on a locked-down CI runner, an air-gapped machine, or a laptop with a broken
`pip`. The whole tool is a few hundred lines of readable Python.

**Conservative estimates with visible assumptions.** Every waste number prints the
rule that produced it and the exact basis of the charge. A reviewer can disagree with
the estimate, which is far better than a number with no provenance.

**Never invent data.** Unknown model prices are flagged, never silently guessed.
Missing usage fields are skipped, never fabricated. Empty input is an error, not a
zero-dollar success.

**Read-only.** `burnmap` opens your logs, reads them, and prints. It never writes to
them, never sends them anywhere, and never modifies your agent's state.

---

## Limitations

* Pricing tables are list prices and drift over time. Use `--pricing` for precision.
* Waste estimates cover three common patterns, not every inefficiency. Retrieval
  loops across tool *types*, over-long system prompts, and redundant subagent
  fan-out are not modelled yet.
* Attribution splits a turn's cost evenly across the tools it called. A batched call
  with one dominant tool will look cheaper than it was.
* Session formats are inferred, not declared. An unusual schema will parse as zero
  turns and exit `1` rather than silently producing nonsense.

---

## Contributing

Issues and PRs welcome, especially new session formats and new waste rules. The bar
is simple: a rule needs a test that proves it fires on real waste and stays silent on
legitimate work. Run `python -m pytest` before opening a PR.

## License

MIT. See [LICENSE](LICENSE).
