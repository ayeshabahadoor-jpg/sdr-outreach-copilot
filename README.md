# SDR Outreach Copilot

Turn one prospect's details into a complete, personalized, ready-to-send
**4-touch outreach sequence** for Intercom SDRs — in seconds.

An SDR spends 5–15 hours/week manually researching prospects and writing
outreach. This tool automates the drafting: give it a prospect, get back a
cold email, a LinkedIn connection note, a follow-up "bump", and a cold-call
opener — each one referencing the prospect's intent signal, tied to a pain
specific to their role, and backed by exactly one real Intercom/Fin proof point.

Built for the **AI Fluency Level 1** certification.

## Highlights

- **Zero dependencies** for the default offline engine — pure Python 3 stdlib.
- **Two input modes:** interactive prompts, or batch from CSV/JSON (`--file`).
- **Two generation modes:** deterministic offline engine (always works), or
  `--ai` drafts via the Anthropic API with automatic, crash-proof fallback.
- **Never crashes on bad data:** any missing field gets a sensible default.
- Prints each sequence to the terminal **and** saves one Markdown file per
  prospect under `./output/`, plus a summary table at the end.

## Quick start

```bash
# Interactive mode (offline engine, no setup required)
python3 sdr_outreach_copilot.py

# Batch mode from the sample CSV
python3 sdr_outreach_copilot.py --file prospects.csv

# Batch mode from JSON
python3 sdr_outreach_copilot.py --file prospects.sample.json

# Use the Anthropic API (falls back to offline if key missing / call fails)
export ANTHROPIC_API_KEY=sk-ant-...
python3 sdr_outreach_copilot.py --file prospects.csv --ai

# Full help
python3 sdr_outreach_copilot.py --help
```

## Input format

Each prospect has these fields (all optional — missing ones are filled in):

| Field | Example | Default |
| --- | --- | --- |
| `name` | Maya Chen | `there` |
| `company` | Brightloop | `your company` |
| `role` / `title` | Head of Customer Support | `your team` |
| `industry` | E-commerce SaaS | `your industry` |
| `intent_signal` | requested a Fin demo | `showed interest in Intercom` |
| `product_focus` | Fin / Helpdesk / Copilot | `Fin` |

Common column aliases are recognized (e.g. `title` → `role`, `org` → `company`).

**CSV** — see [`prospects.csv`](prospects.csv):

```csv
name,company,role,industry,intent_signal,product_focus
Maya Chen,Brightloop,Head of Customer Support,E-commerce SaaS,requested a Fin demo,Fin
```

**JSON** — a list of objects, or `{"prospects": [...]}`, or a single object.
See [`prospects.sample.json`](prospects.sample.json).

## Output

For every prospect the tool produces a 4-touch sequence:

1. **Cold email** — subject line + body (< 90 words)
2. **LinkedIn connection note** — < 300 characters
3. **Follow-up "bump"** — < 70 words
4. **Cold-call opener** — 3–4 conversational sentences

Each touch opens by referencing the intent signal, ties to the role-specific
pain, and includes exactly one proof point. `{{Your Name}}` is the sender
placeholder — replace it before sending.

Sequences print to the terminal and save to `./output/<name>_<company>.md`.

## How personalization works (offline engine)

- **Role → pain** mapping: e.g. Support/CX lead → *ticket volume outpacing
  headcount*; Ops → *repetitive manual work*; VP/Director/Head → *efficiency
  targets without hurting CSAT*; Founder/CEO → *scaling support without scaling
  headcount*.
- **Proof-point selection**: `product_focus` (Fin/Helpdesk/Copilot) picks the
  most relevant proof point; otherwise it's derived from the role.

## AI mode

With `--ai`, the tool calls the Anthropic API. The prompt lives in a clearly
named `build_llm_prompt()` function with full role context, a strict JSON output
schema, and the per-touch word-count constraints. Requires the `anthropic`
package and `ANTHROPIC_API_KEY`. If either is missing, or the call/parse fails,
the tool prints a notice and falls back to the offline engine — it never crashes.

## Files

| File | Purpose |
| --- | --- |
| `sdr_outreach_copilot.py` | The CLI tool |
| `prospects.csv` | Sample CSV (2 prospects) |
| `prospects.sample.json` | Sample JSON (2 prospects, incl. a partial record) |
| `output/` | Generated Markdown sequences (git-ignored) |
