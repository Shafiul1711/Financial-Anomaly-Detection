# Financial-Anomaly-Detection

A tiered anomaly-detection pipeline for financial transaction data. Cheap, deterministic rules handle what's mechanically checkable; an LLM (Claude API) is escalated to only for the ambiguous cases that require contextual judgment. Structural rules can't ask "does this business name look real" or "does this purchase amount make sense for this category," so those checks are pushed to a model, and every model output is surfaced as an explicit, labeled flag for human review rather than trusted or acted on automatically.

Built as an exercise in tiered anomaly detection: using cheap deterministic checks wherever they suffice, and escalating only the genuinely ambiguous cases to a model, where outputs are verifiable before anything acts on them.

## Why tiered, not "just ask the LLM"

Not every anomaly is the same kind of problem:

- **Structural.** A field is missing, malformed, or the wrong type. Cheap to catch with a format check.
- **Statistical.** A value is a numeric outlier for its column. Cheap to catch with mean/standard deviation.
- **Contextual.** A value is well-formed and not a numeric outlier, but doesn't make sense given what it represents (a merchant name that isn't a real business, a location that doesn't exist, a category that doesn't fit a manufacturing company). No rule can catch this class of problem; it requires understanding, not math, which is exactly where an LLM adds value that a rule can't.

Running every row through an LLM ignores that most anomalies are actually structural or statistical: cheap, deterministic, and answerable without a model call. The pipeline routes each row through cheap checks first and only escalates the genuinely ambiguous remainder, keeping cost and latency proportional to actual difficulty.

## Pipeline

1. **Ingest.** Load and parse the CSV.
2. **Hygiene check.** Catch structurally invalid rows (empty fields, unparseable timestamps, wrong types). Cheap, deterministic.
3. **Fine filters.** Catch logically invalid but structurally well-formed data: impossible calendar dates (including full leap-year handling, where century years are excluded unless divisible by 400), future-dated transactions, statistical amount outliers, exact duplicates.
4. **Rapid-succession detection.** A cross-row rule flags clusters of transactions from the same account occurring within a short time window (chained transitively, so a 3-transaction sequence each 10 minutes apart from its neighbor is grouped as one cluster even though the first and last are 20 minutes apart). This is a relational anomaly; it doesn't exist in any single row.
5. **LLM contextual review.** Every row that survives stages 2 to 4 is evaluated by Claude against six binary questions (does the merchant name look like a real business, is the location real, is it specific enough, does the amount fit the category, does the category fit a manufacturing company, does the transaction type match the category), returned as structured JSON so it's reliably parseable rather than free-text. Rapid-succession clusters are evaluated separately: the same set of transactions is passed as shared context and the model is asked whether the pattern, not any single row, looks suspicious.
6. **Report.** Every flag from every stage is combined into one human-readable reason per row, filtered to flagged rows only, with the original file line number so a reviewer can find the source row directly. No hierarchy or auto-resolution is applied; flags are surfaced for a human to weigh, consistent with a human-in-the-loop design.

## What this deliberately does not do

A few decisions were made explicitly, not by omission:

- **No rule-based re-verification of the LLM's contextual judgments.** Most of the six checks (is this a real business name, does this category fit) can't be independently re-verified by a rule without reimplementing the judgment the LLM was invoked for in the first place, which would defeat the purpose. Instead, every LLM flag is surfaced transparently, with the specific check that triggered it, so a human reviewer applies their own judgment rather than trusting an opaque verdict.
- **No flag-severity hierarchy.** Any single flag routes a row to the report. A production system would likely weight flags differently based on observed false-positive rates (a fake location is a stronger signal alone than an unusual merchant name), but that requires real data to tune against. This version treats all signals as worth a human's attention.
- **No cost controls beyond the tiered routing.** At the scale of a real enterprise dataset (100k+ rows), a large fraction would still survive cheap filtering and reach the LLM stage, since most of what makes data suspicious is contextual by nature, not something a rule can pre-filter for. A production deployment would likely add cheap heuristic pre-screening (for example, pattern-based name scoring, or a gazetteer lookup for locations) or risk-based sampling (prioritizing high-value transactions) to control LLM call volume, at the cost of some coverage on lower-priority rows.

These aren't gaps discovered after the fact. They're the actual shape of the tradeoff between what a rule-based system can do and what requires a model, made explicit rather than hidden.

## Tech

- Python, pandas
- Anthropic Claude API, structured (JSON) output
- Synthetic transaction data for testing, with a hand-built answer key for known seeded anomalies across every check category

## Development process

Built with Claude Code, directing the architecture, edge cases, and test design throughout.
