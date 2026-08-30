"""
Financial Anomaly Detector
---------------------------
Pipeline stages:
  1. Open and process CSV                         <- implemented here
  2. Rule-based hygiene check (malformed/corrupt rows)
  3. Fine filters / logical rule checks
  4. Contextual anomaly detection via Claude API
  5. Independent rule-based re-verification of LLM findings
  6. Report output for human-in-the-loop review
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import anthropic
import pandas as pd
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

EXPECTED_COLUMNS = [
    "Timestamp",
    "TransactionID",
    "AccountID",
    "Amount",
    "Merchant",
    "TransactionType",
    "Location",
    "Category",
]

# Fields hygiene_check requires to be non-blank on every row. Category is
# deliberately excluded: it's descriptive context for stage 4, not yet a
# rule-enforced field, and legitimately blank when TransactionType itself
# is invalid/unknown.
REQUIRED_FIELDS = [c for c in EXPECTED_COLUMNS if c != "Category"]

TIMESTAMP_FORMAT = "%d-%m-%Y %H:%M"
TRANSACTION_ID_PATTERN = re.compile(r"^TXN\d+$")
ACCOUNT_ID_PATTERN = re.compile(r"^ACC\d+$")
AMOUNT_PATTERN = re.compile(r"^\d+(\.\d+)?$")
VALID_TRANSACTION_TYPES = {"Purchase", "Withdrawal", "Transfer"}

# Category gives stage 4 (LLM) business context for each transaction, scoped
# to a global manufacturing company. Not yet rule-enforced against
# TransactionType - that's a later addition once mismatched categories are
# deliberately introduced as a test case.
CATEGORY_BY_TRANSACTION_TYPE = {
    "Purchase": {"Raw Materials", "Components", "Logistics"},
    "Withdrawal": {"Cash", "Credit"},
    "Transfer": {"Account", "Internal"},
}

TIMESTAMP_STRUCTURE = re.compile(
    r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4}) (?P<hour>\d{2}):(?P<minute>\d{2})$"
)
DAYS_IN_MONTH = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
AMOUNT_STD_THRESHOLD = 2
RAPID_SUCCESSION_WINDOW_MINUTES = 10


def load_csv(csv_path: str | Path) -> pd.DataFrame:
    """Stage 1: Open the CSV and load it into a DataFrame, keeping every
    value as a raw string. No type coercion or validation happens here -
    that is stage 2's job. This stage only needs to succeed at reading
    the file and confirming it has the columns later stages expect.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    logger.info("Loading CSV: %s", csv_path)
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

    missing = [col for col in EXPECTED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing expected columns: {missing}")

    df = df.reset_index(names="row_id")

    logger.info("Loaded %d rows, %d columns", len(df), len(df.columns) - 1)
    return df


def hygiene_check(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 2: rule-based hygiene check.

    Flags two categories of bad rows:
      - empty:     the whole row, or one of its required fields, is blank.
      - malformed: a field is present but doesn't match its expected shape
                    (bad timestamp shape, non-numeric amount, unknown ID/type
                    format).

    Timestamp is checked for SHAPE only here (does it look like
    DD-MM-YYYY HH:MM), not calendar validity - a structurally valid but
    logically wrong date (e.g. day 32, month 13, Feb 29 on a non-leap year)
    is not malformed, it's incorrect, and that judgment belongs to stage 3's
    fine_filter().

    Returns (clean_df, flagged_df). `flagged_df` carries a `hygiene_issues`
    column (semicolon-separated reason codes) so stage 6 can report exactly
    why each row was pulled out before it ever reached the LLM.
    """
    working = df.copy()
    text = working[REQUIRED_FIELDS]
    is_blank = text.apply(lambda c: c.str.strip() == "")
    fully_empty = is_blank.all(axis=1)
    checkable = ~fully_empty  # skip format checks on rows already fully empty

    checks: list[tuple[pd.Series, str]] = [(fully_empty, "empty_row")]
    checks += [
        (is_blank[col] & ~fully_empty, f"missing_field:{col}")
        for col in REQUIRED_FIELDS
    ]
    checks += [
        (checkable & ~is_blank["Timestamp"] & ~working["Timestamp"].str.match(TIMESTAMP_STRUCTURE), "malformed_timestamp"),
        (checkable & ~is_blank["Amount"] & ~working["Amount"].str.match(AMOUNT_PATTERN), "malformed_amount"),
        (checkable & ~is_blank["TransactionID"] & ~working["TransactionID"].str.match(TRANSACTION_ID_PATTERN), "malformed_transaction_id"),
        (checkable & ~is_blank["AccountID"] & ~working["AccountID"].str.match(ACCOUNT_ID_PATTERN), "malformed_account_id"),
        (checkable & ~is_blank["TransactionType"] & ~working["TransactionType"].isin(VALID_TRANSACTION_TYPES), "invalid_transaction_type"),
    ]

    reasons = pd.Series([[] for _ in range(len(working))], index=working.index)
    for mask, reason in checks:
        for idx in working.index[mask]:
            reasons.at[idx].append(reason)

    working["hygiene_issues"] = reasons.apply(lambda r: "; ".join(r))
    flagged_mask = working["hygiene_issues"] != ""

    flagged_df = working[flagged_mask].copy()
    clean_df = working[~flagged_mask].drop(columns=["hygiene_issues"]).copy()

    logger.info(
        "Hygiene check: %d clean rows, %d flagged rows",
        len(clean_df), len(flagged_df),
    )
    return clean_df, flagged_df


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def check_timestamp_calendar(ts: str) -> str | None:
    """Validate a DD-MM-YYYY HH:MM timestamp against calendar rules, computed
    directly (not delegated to a date-parsing library) so the logic stays
    auditable. The timestamp is assumed to already be structurally valid
    (stage 2's job) - this only judges whether the field values it holds
    make calendar sense. Returns "incorrect_timestamp" if not, else None.
    """
    match = TIMESTAMP_STRUCTURE.match(ts)
    if not match:
        return "incorrect_timestamp"  # defensive: shouldn't occur post stage-2

    day, month, year, hour, minute = (
        int(match.group(g)) for g in ("day", "month", "year", "hour", "minute")
    )

    if month < 1 or month > 12:
        return "incorrect_timestamp"

    max_day = 29 if (month == 2 and _is_leap_year(year)) else DAYS_IN_MONTH[month]
    if day < 1 or day > max_day:
        return "incorrect_timestamp"

    if hour < 0 or hour > 23:
        return "incorrect_timestamp"

    if minute < 0 or minute > 59:
        return "incorrect_timestamp"

    return None


def check_timestamp_not_future(ts: str) -> str | None:
    """Flag a timestamp that's later than the current moment - a transaction
    can't have happened in the future. Uses the datetime API directly to
    parse and compare, rather than manual field arithmetic.
    """
    try:
        parsed = datetime.strptime(ts, TIMESTAMP_FORMAT)
    except ValueError:
        return None  # structural/calendar validity is check_timestamp_calendar's job

    if parsed > datetime.now():
        return "future_timestamp"
    return None


def detect_duplicates(df: pd.DataFrame) -> pd.Series:
    """True for every row that has at least one other row with identical
    values across every transaction field - an exact duplicate. `row_id` is
    excluded from the comparison since it's unique by construction.
    """
    return df.duplicated(subset=EXPECTED_COLUMNS, keep=False)


def fine_filter(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 3: fine filters / logical rule checks.

    - Timestamp: calendar-logic validation (month/day/leap-year/hour/minute
      ranges) flagged as "incorrect_timestamp" - the value is structurally
      fine (stage 2 already confirmed that) but not a real calendar date -
      plus a datetime-API check that rejects any timestamp later than now.
    - Amount: flag values more than AMOUNT_STD_THRESHOLD standard deviations
      from the column mean.
    - Duplicate: flag every row that's an exact match (every field) of
      another row - definitively an anomaly, no contextual judgment needed.

    TransactionID reuse across otherwise-different rows, and rapid-succession
    purchase clusters (see tag_rapid_succession), are contextual patterns
    deferred to stage 4 (LLM) rather than flagged outright here.

    Expects `df` to be stage 2's clean output (syntactically valid timestamp
    and numeric amount already guaranteed).
    """
    working = df.copy()

    calendar_reason = working["Timestamp"].apply(check_timestamp_calendar)
    future_reason = working["Timestamp"].apply(check_timestamp_not_future)
    duplicate_mask = detect_duplicates(working)

    amounts = working["Amount"].astype(float)
    mean, std = amounts.mean(), amounts.std()
    lower_bound, upper_bound = mean - AMOUNT_STD_THRESHOLD * std, mean + AMOUNT_STD_THRESHOLD * std
    amount_outlier = (amounts < lower_bound) | (amounts > upper_bound)

    reasons = pd.Series([[] for _ in range(len(working))], index=working.index)
    for idx in working.index[calendar_reason.notna()]:
        reasons.at[idx].append(calendar_reason.at[idx])
    for idx in working.index[future_reason.notna()]:
        reasons.at[idx].append(future_reason.at[idx])
    for idx in working.index[amount_outlier]:
        reasons.at[idx].append("amount_outlier")
    for idx in working.index[duplicate_mask]:
        reasons.at[idx].append("duplicate")

    working["fine_filter_issues"] = reasons.apply(lambda r: "; ".join(r))
    flagged_mask = working["fine_filter_issues"] != ""

    flagged_df = working[flagged_mask].copy()
    clean_df = working[~flagged_mask].drop(columns=["fine_filter_issues"]).copy()

    logger.info(
        "Fine filter: %d clean rows, %d flagged rows (amount mean=%.2f, std=%.2f, bounds=[%.2f, %.2f])",
        len(clean_df), len(flagged_df), mean, std, lower_bound, upper_bound,
    )
    return clean_df, flagged_df


def tag_rapid_succession(df: pd.DataFrame) -> pd.DataFrame:
    """Detect rapid-succession purchase clusters: chains of Purchase
    transactions from the same AccountID where each one follows the
    previous within RAPID_SUCCESSION_WINDOW_MINUTES. A gap longer than the
    window breaks the chain, so a group can span more than the window in
    total as long as every consecutive pair is within it (e.g. three
    purchases ten minutes apart each are one group spanning twenty minutes).

    Groups of 2+ get a shared integer id in a new `rapid_succession_group`
    column; everything else gets <NA>. This only detects and tags candidates
    - it does NOT decide they're anomalies. Whether a cluster is reasonable
    (e.g. routine restocking) or suspicious (e.g. card testing) is a
    contextual judgment for stage 4 (LLM), not yet implemented, so those
    rows stay in the pipeline rather than being pulled out here.
    """
    working = df.copy()
    parsed_ts = pd.to_datetime(working["Timestamp"], format=TIMESTAMP_FORMAT)

    group_id = pd.Series(pd.NA, index=working.index, dtype="Int64")
    next_group_id = 0

    purchase_mask = working["TransactionType"] == "Purchase"
    for _, account_rows in working[purchase_mask].groupby("AccountID"):
        ordered_idx = parsed_ts.loc[account_rows.index].sort_values().index.to_list()
        ordered_ts = parsed_ts.loc[ordered_idx].to_list()

        current_group = [ordered_idx[0]]
        for i in range(1, len(ordered_idx)):
            gap_minutes = (ordered_ts[i] - ordered_ts[i - 1]).total_seconds() / 60
            if gap_minutes <= RAPID_SUCCESSION_WINDOW_MINUTES:
                current_group.append(ordered_idx[i])
            else:
                if len(current_group) >= 2:
                    group_id.loc[current_group] = next_group_id
                    next_group_id += 1
                current_group = [ordered_idx[i]]
        if len(current_group) >= 2:
            group_id.loc[current_group] = next_group_id
            next_group_id += 1

    working["rapid_succession_group"] = group_id

    logger.info(
        "Rapid succession tagging: %d rows across %d groups flagged as stage 4 review candidates",
        group_id.notna().sum(), next_group_id,
    )
    return working


# ---------------------------------------------------------------------------
# Stage 4 (LLM) - prompt construction only. The Claude API call itself isn't
# wired up yet; this builds the exact text each candidate row will be sent
# as, once that integration exists.
# ---------------------------------------------------------------------------

LLM_PROMPT_FIELDS = ["Timestamp", "AccountID", "Amount", "Merchant", "TransactionType", "Location", "Category"]

# Every question is phrased so "yes" always means an anomaly signal was
# found, matching its key name - keeps stage 5's parsing simple (any "yes"
# in the response is a flag, regardless of which key it's under).
LLM_QUESTIONS = [
    ("suspicious_name", "Merchant: Does the merchant name look suspicious - e.g. a jumble of letters/numbers rather than a genuine attempt at a business name?"),
    ("not_real", "Location: Is this location not a real place (fake, fictional, or nonexistent)?"),
    ("too_vague", "Location: Is the location too vague - e.g. just a country, not a specific city/town?"),
    ("suspicious_purchase_amount", "Amount: Does the amount seem unreasonable or suspicious for the stated category?"),
    ("suspicious_category", "Category: Does the category seem like it doesn't make sense for a global manufacturing company?"),
    ("transaction_type_mismatch", "TransactionType: Does the transaction type mismatch the category?"),
]

LLM_RESPONSE_KEYS = [key for key, _ in LLM_QUESTIONS]


def build_llm_prompt(row: pd.Series) -> str:
    """Build the stage 4 prompt for a single candidate row: embeds the
    row's business-facing fields and asks the six suspicion questions. The
    response format itself is enforced server-side (see TransactionReview /
    CLAUDE_MODEL calls below), not by instructing the model in text.
    """
    data_lines = "\n".join(f"{field}: {row[field]}" for field in LLM_PROMPT_FIELDS)
    questions = "\n".join(f"{i}. {question}" for i, (_, question) in enumerate(LLM_QUESTIONS, start=1))

    return (
        "You are reviewing a single transaction record for a global manufacturing "
        "company, checking for signs it may be anomalous or fraudulent.\n\n"
        "Transaction:\n"
        f"{data_lines}\n\n"
        "Answer the following questions about this transaction with \"yes\" or \"no\":\n"
        f"{questions}"
    )


def build_rapid_succession_prompt(group_df: pd.DataFrame) -> str:
    """Build the stage 4 prompt for a rapid-succession purchase group: lists
    every purchase in the cluster and asks a single question about the
    pattern as a whole, rather than the per-row six-question review.
    """
    purchases = "\n\n".join(
        f"Purchase {i}:\n" + "\n".join(f"{field}: {row[field]}" for field in LLM_PROMPT_FIELDS)
        for i, (_, row) in enumerate(group_df.iterrows(), start=1)
    )

    return (
        "You are reviewing a cluster of purchases for a global manufacturing "
        "company, all made from the same account within a short time window.\n\n"
        f"{purchases}\n\n"
        "Given these purchases came from the same account within a short time "
        "window, does this pattern look suspicious?"
    )


# ---------------------------------------------------------------------------
# Claude API calls. Uses structured outputs (client.messages.parse with a
# Pydantic schema) so the response is guaranteed to be valid JSON with
# exactly the expected keys - no manual parsing/validation needed downstream.
# Reads the API key from the ANTHROPIC_API_KEY environment variable (never
# hardcoded here).
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-haiku-4-5"

YesNo = Literal["yes", "no"]


class TransactionReview(BaseModel):
    suspicious_name: YesNo
    not_real: YesNo
    too_vague: YesNo
    suspicious_purchase_amount: YesNo
    suspicious_category: YesNo
    transaction_type_mismatch: YesNo


class RapidSuccessionReview(BaseModel):
    potential_fraud: YesNo


def get_claude_client() -> anthropic.Anthropic:
    """Reads credentials from the ANTHROPIC_API_KEY environment variable
    (or another SDK-recognized source) - never pass a key literal here.
    """
    return anthropic.Anthropic()


def review_transaction(row: pd.Series, client: anthropic.Anthropic | None = None) -> dict:
    """Stage 4: send a single non-rapid-succession candidate row through the
    six-question review. Returns a dict with the six fixed "yes"/"no" keys.
    """
    client = client or get_claude_client()
    response = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": build_llm_prompt(row)}],
        output_format=TransactionReview,
    )
    return response.parsed_output.model_dump()


def review_rapid_succession_group(group_df: pd.DataFrame, client: anthropic.Anthropic | None = None) -> dict:
    """Stage 4: send one rapid-succession purchase cluster through the
    single-question pattern review. Returns {"potential_fraud": "yes"/"no"},
    applying to every row in the group.
    """
    client = client or get_claude_client()
    response = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": build_rapid_succession_prompt(group_df)}],
        output_format=RapidSuccessionReview,
    )
    return response.parsed_output.model_dump()


def _call_llm(fn, *args, **kwargs) -> tuple[dict | None, str | None]:
    """Run a stage 4 review call, translating SDK exceptions into a result
    so one failed row/group doesn't abort the whole batch. Most-specific
    exception first, per the SDK's recommended error-handling chain.
    """
    try:
        return fn(*args, **kwargs), None
    except anthropic.RateLimitError as e:
        return None, f"rate_limited: {e}"
    except anthropic.APIStatusError as e:
        return None, f"api_error({e.status_code}): {e.message}"
    except anthropic.APIConnectionError as e:
        return None, f"connection_error: {e}"
    except TypeError as e:
        # The SDK raises a plain TypeError (not an AnthropicError subclass)
        # when no credentials are resolvable at all - this happens client-side
        # before any network request, so it costs nothing, but every
        # subsequent row will fail identically until credentials are set.
        return None, f"client_error: {e}"


def run_stage4(tagged_df: pd.DataFrame, client: anthropic.Anthropic | None = None) -> pd.DataFrame:
    """Stage 4: send every candidate row through Claude for contextual
    review. Rows in a rapid_succession_group (see tag_rapid_succession) are
    reviewed once per group with the pattern-level prompt, not individually;
    every other row gets the six-question individual review.

    Expects `tagged_df` to already carry a `rapid_succession_group` column
    (i.e. it's the output of tag_rapid_succession on stage 3's clean rows).

    Adds two columns to a copy of `tagged_df`:
      - llm_review_type: "individual" or "rapid_succession"
      - llm_review: the parsed response dict (shared across every row in the
        same rapid_succession_group); None if the API call failed
      - llm_review_error: the error string if the call failed, else None

    This makes real, billed API calls - it is not wired into main()'s
    default run.
    """
    client = client or get_claude_client()
    working = tagged_df.copy()

    review_type = pd.Series(pd.NA, index=working.index, dtype="object")
    review_result = pd.Series([None] * len(working), index=working.index, dtype="object")
    review_error = pd.Series([None] * len(working), index=working.index, dtype="object")

    individual_mask = working["rapid_succession_group"].isna()
    for idx in working.index[individual_mask]:
        result, error = _call_llm(review_transaction, working.loc[idx], client=client)
        review_type.at[idx] = "individual"
        review_result.at[idx] = result
        review_error.at[idx] = error
        if error:
            logger.error("Stage 4 individual review failed for row_id=%s: %s", working.at[idx, "row_id"], error)

    for group_id, group_rows in working[~individual_mask].groupby("rapid_succession_group"):
        result, error = _call_llm(review_rapid_succession_group, group_rows, client=client)
        if error:
            logger.error("Stage 4 rapid-succession review failed for group=%s: %s", group_id, error)
        for idx in group_rows.index:
            review_type.at[idx] = "rapid_succession"
            review_result.at[idx] = result
            review_error.at[idx] = error

    working["llm_review_type"] = review_type
    working["llm_review"] = review_result
    working["llm_review_error"] = review_error

    n_failed = review_error.notna().sum()
    logger.info(
        "Stage 4: %d rows reviewed (%d individual, %d via rapid-succession groups), %d failed",
        len(working), individual_mask.sum(), (~individual_mask).sum(), n_failed,
    )
    return working


# ---------------------------------------------------------------------------
# Stage 6 (lightweight): consolidate every flagged row across every stage
# into one report for human-in-the-loop review.
# ---------------------------------------------------------------------------

REPORT_COLUMNS = (
    ["row_id"] + EXPECTED_COLUMNS
    + ["hygiene_issues", "fine_filter_issues", "rapid_succession_group",
       "llm_review_type", "llm_review", "llm_review_error"]
)


def _humanize_reason(code: str) -> str:
    """"malformed_transaction_id" -> "Malformed Transaction Id";
    "missing_field:Category" -> "Missing Field: Category" (the suffix after
    a colon is already a proper column name, so it's left as-is).
    """
    if ":" in code:
        prefix, _, suffix = code.partition(":")
        return f"{prefix.replace('_', ' ').title()}: {suffix}"
    return code.replace("_", " ").title()


def _humanize_issue_list(issues: str) -> str:
    """Reformat a "; "-separated reason-code string (hygiene_issues /
    fine_filter_issues) into human-readable Title Case."""
    if not issues:
        return issues
    return "; ".join(_humanize_reason(code) for code in issues.split("; "))


def _humanize_llm_review(review: dict | None) -> str:
    """Reduce a full LLM answer dict down to just the questions that came
    back "yes", humanized - e.g. {"suspicious_name": "yes", "not_real": "no",
    ...} -> "Suspicious Name". Multiple flags join with "; "."""
    if not isinstance(review, dict):
        return review
    return "; ".join(_humanize_reason(key) for key, value in review.items() if value == "yes")


def build_report(hygiene_flagged_df: pd.DataFrame, fine_flagged_df: pd.DataFrame, stage4_df: pd.DataFrame) -> pd.DataFrame:
    """Combine every flagged row - from stage 2 (hygiene), stage 3 (fine
    filter), or stage 4 (LLM review, any "yes" answer) - into a single
    report DataFrame, keeping each stage's own issue-tracking columns so a
    reviewer can see exactly why and where each row was pulled out.
    `hygiene_issues`/`fine_filter_issues`/`llm_review` are all rendered as
    human-readable Title Case reason lists rather than raw codes/JSON.

    Rows whose stage 4 call failed (llm_review_error set) are NOT included
    here as "flagged" - their status is undetermined, not confirmed
    anomalous, and they'd need a retry rather than human review.
    """
    stage4_flagged = stage4_df[
        stage4_df["llm_review"].apply(lambda r: isinstance(r, dict) and any(v == "yes" for v in r.values()))
    ]

    report = pd.concat([hygiene_flagged_df, fine_flagged_df, stage4_flagged], ignore_index=True, sort=False)
    report = report.drop_duplicates(subset="row_id").sort_values("row_id").reset_index(drop=True)

    for col in REPORT_COLUMNS:
        if col not in report.columns:
            report[col] = pd.NA
    report = report[REPORT_COLUMNS]

    report["hygiene_issues"] = report["hygiene_issues"].apply(lambda v: _humanize_issue_list(v) if isinstance(v, str) else v)
    report["fine_filter_issues"] = report["fine_filter_issues"].apply(lambda v: _humanize_issue_list(v) if isinstance(v, str) else v)
    report["llm_review"] = report["llm_review"].apply(_humanize_llm_review)

    logger.info(
        "Report: %d flagged rows total (%d hygiene, %d fine filter, %d stage 4)",
        len(report), len(hygiene_flagged_df), len(fine_flagged_df), len(stage4_flagged),
    )
    return report


def main():
    #csv_path = Path(__file__).parent / "financial_anomaly_data.csv"
    csv_path = Path(__file__).parent / "test.csv"
    df = load_csv(csv_path)
    print(f"Total rows loaded: {len(df)}")

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)

    hygiene_clean_df, hygiene_flagged_df = hygiene_check(df)

    print(f"\nClean rows (passed to stage 3): {len(hygiene_clean_df)}")
    print(f"Flagged rows (empty/malformed): {len(hygiene_flagged_df)}")
    if not hygiene_flagged_df.empty:
        print("\nFlagged rows by reason:")
        print(
            hygiene_flagged_df["hygiene_issues"]
            .str.split("; ")
            .explode()
            .value_counts()
        )
        print("\nAll flagged rows:")
        print(hygiene_flagged_df)

    fine_clean_df, fine_flagged_df = fine_filter(hygiene_clean_df)

    print(f"\nClean rows (passed to stage 4): {len(fine_clean_df)}")
    print(f"Flagged rows (calendar/amount rule violations): {len(fine_flagged_df)}")
    if not fine_flagged_df.empty:
        print("\nFlagged rows by reason:")
        print(
            fine_flagged_df["fine_filter_issues"]
            .str.split("; ")
            .explode()
            .value_counts()
        )
        print("\nAll flagged rows:")
        print(fine_flagged_df)

    tagged_df = tag_rapid_succession(fine_clean_df)
    rapid_succession_df = tagged_df[tagged_df["rapid_succession_group"].notna()]

    print(f"\nRapid succession candidates (pending stage 4 review): {len(rapid_succession_df)}")
    if not rapid_succession_df.empty:
        print("\nAll rapid succession groups:")
        print(
            rapid_succession_df[["row_id", "Timestamp", "AccountID", "Amount", "rapid_succession_group"]]
            .sort_values(["rapid_succession_group", "Timestamp"])
        )

    print(f"\nRunning stage 4 (Claude API, model={CLAUDE_MODEL}) on {len(tagged_df)} rows...")
    stage4_df = run_stage4(tagged_df)

    failed_df = stage4_df[stage4_df["llm_review_error"].notna()]
    reviewed_df = stage4_df[stage4_df["llm_review_error"].isna()]
    any_flag = reviewed_df["llm_review"].apply(lambda r: any(v == "yes" for v in r.values()))
    flagged_df = reviewed_df[any_flag]

    print(f"\nStage 4 results: {len(reviewed_df)} reviewed, {len(failed_df)} failed, {len(flagged_df)} flagged (at least one \"yes\")")
    if not failed_df.empty:
        print("\nFailed reviews:")
        print(failed_df[["row_id", "llm_review_type", "llm_review_error"]])
    if not flagged_df.empty:
        print("\nFlagged by stage 4:")
        print(flagged_df[["row_id", "Timestamp", "Merchant", "Location", "Category", "TransactionType", "llm_review_type", "llm_review"]])

    report_df = build_report(hygiene_flagged_df, fine_flagged_df, stage4_df)
    output_path = Path(__file__).parent / "output.csv"
    report_df.to_csv(output_path, index=False)
    print(f"\nWrote {len(report_df)} flagged rows to {output_path}")


if __name__ == "__main__":
    main()
