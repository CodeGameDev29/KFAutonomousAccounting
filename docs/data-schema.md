# Data schema

What this engine stores, and which parts of it you are expected to edit.

Two different things live under `config/`:

* **Code** — `config/ised_categories.py` (the fixed category taxonomy) and
  `config/settings.py` (environment wiring). Do not edit these to describe your
  business.
* **Data** — six YAML files that ship with generic defaults and are meant to be
  edited.

| File | Holds | Loaded by | Read by |
|---|---|---|---|
| `config/accounts.yaml` | account display names and their order in reports | `config/data_files.py` | `core/export_binder.py`, `core/html_binder.py`, `core/report_pdf.py`, `server/api/accounts.py`, `server/api/reports.py`, `server/api/shared.py` |
| `config/category_rules.yaml` | the deterministic regex pre-pass | `config/data_files.py` | `core/categorization_agent/rule_prepass.py` |
| `config/vendor_aliases.yaml` | description regex → stable vendor key | `config/data_files.py` | `core/categorization_agent/vendor_clustering.py`, `core/reconciliation.py` |
| `config/vendor_rules.yaml` | your own clients and vendors | **not** `data_files.py` — `core/reconciliation.py` opens it directly | `core/reconciliation.py` |
| `config/transfer_keywords.yaml` | which statement wordings mean "this is one leg of a transfer" | **not** `data_files.py` — `core/transaction_linker.py` opens it directly | `core/transaction_linker.py` |
| `config/aa_category_gifi_map.yaml` | fallback category → GIFI bucket rules for the reasonableness engine | **not** `data_files.py` — `core/reasonableness.py` opens it directly | `core/reasonableness.py`, `server/api/reasonableness.py` |

The first three are loaded by `config/data_files.py`, which reads each file once
per process and caches it. The last three are opened by their own consumers —
see each file's section below for exactly which function does it.

A missing or unparseable file never stops the server: the loader logs a warning
and hands back an empty table, and each loader skips individual malformed
entries rather than dropping the whole file. What "empty" costs you is described
per file below.

---

## `config/accounts.yaml`

Maps an account key to the name printed above that account's section in the XLSX
binder, the HTML report and the PDF report, and fixes the order those sections
appear in.

```yaml
display:
  CAD: "CAD Chequing"

order:
  - "CAD"
```

* `display:` — a mapping of **account key → human-readable name**. The keys are
  the exact strings stored in `transactions.account`, so they are matched, not
  displayed, by the rest of the code: rename a key and it simply stops being
  found. The values are yours to change.
* `order:` — a list of account keys, in the order you want their sections to
  appear.

The shipped file activates two example accounts, `CAD` and `USD` — the keys the
built-in BMO chequing parser writes — and carries four more commented out:
`CreditCard`, `Wise`, `PayPal`, `Amazon`, the keys the built-in parsers write
when they recognise a card statement, a Wise export, a PayPal export or a
marketplace order file. Uncomment the ones you use, in both blocks.

**When it is missing or wrong.** No `display:` entry for an account (or no file
at all) means the account is printed using its raw key — `ACCOUNT_DISPLAY.get(key, key)`
at every call site. An account absent from `order:` is appended **alphabetically
after** the ones that are listed, by `_ordered_account_keys()`; nothing is ever
dropped from a report for being unknown. That is exactly what a commented-out
account costs you: a nicer name and a chosen position, never a missing section.
A `display:` that is not a mapping, or an `order:` that is not a list, is ignored
with a warning.

## `config/category_rules.yaml`

The Phase 1 deterministic pre-pass. Every transaction description is matched
against this list before any LLM phase runs; a hit assigns the category and locks
the row so the LLM phases never see it.

```yaml
rules:
  - category: "Interest and bank charges"
    non_reconcilable: true
    patterns:
      - "MONTHLY\\s*FEE"
```

* `category:` — **must** be a canonical name from `config/ised_categories.py`
  (see [The category taxonomy](#the-category-taxonomy)).
* `patterns:` — a list of regexes, matched with `re.search(..., re.IGNORECASE)`
  against the raw `description`.
* `non_reconcilable:` — optional, default `false`. `true` means "internal or
  no-receipt flow": the row is excluded from the reconciliation match loop as
  well as locked. It is keyed on the matched **pattern**, not on the emitted
  category name, because one canonical name is shared by reconcilable and
  non-reconcilable flows (client income and a payroll direct deposit both emit
  `Sales of goods and services`).

Order is meaningful — the list is first-match-wins, top to bottom, and patterns
within a rule are tried in order.

**Escaping.** These are double-quoted YAML scalars, so a backslash must be
doubled: write `"MONTHLY\\s*FEE"` to get the regex `MONTHLY\s*FEE`. A single
`\s` is a YAML parse error, and a single `\b` silently becomes a backspace
character rather than a word boundary. Single-quoted YAML (`'MONTHLY\s*FEE'`)
is literal and also works.

**When it is missing or wrong.** An empty table means nothing is rule-locked and
every transaction goes to the LLM categorization phases — slower, still correct.
A pattern that will not compile is dropped with a warning and the rest of the
table keeps working; a rule with no `category` or no `patterns` list is skipped.

## `config/vendor_aliases.yaml`

Collapses the many ways one merchant appears on a statement into one stable key,
so their rows cluster together and get one categorization decision.

```yaml
aliases:
  - pattern: "^(examplecorp|example[_\\s]*corp)\\b"
    key: "example_corp"
```

* `pattern:` — a regex, compiled case-insensitively, matched against the
  **normalized** description, not the raw one. `normalize_description()` first
  strips payment-processor action prefixes, reference numbers of six digits or
  more, `*CODE` and `#123` fragments, dates, and a list of noise tokens (legal
  forms, country codes, city names), then keeps the first three remaining tokens
  and lowercases them. That is why patterns here are lowercase, anchored with
  `^`, and use `[_\s]*` where the raw text may have had a space, an underscore
  or nothing.
* `key:` — a short lowercase snake_case identifier, not a display name. Two
  patterns may deliberately share one key so several merchants cluster together.

First match wins.

**When it is missing or wrong.** A description that matches nothing still
clusters: it falls back to a slug of its first one or two normalized tokens. So
an empty table costs accuracy in clustering, not correctness. An entry whose
pattern will not compile, or which is missing `pattern` or `key`, is skipped
with a warning.

## `config/vendor_rules.yaml`

Where **your own** clients and vendors go — the names no generic rule could
know. The shipped file is entirely made-up examples and is safe to leave as-is:
nothing here is required, and these rules only add determinism.

```yaml
rules:
  - vendor_pattern: "EXAMPLE CLIENT CO"
    category: "Sales of goods and services"
    priority: 100
    source: manual
```

Fields are the `VendorRule` model in `models/vendor_rule.py`:

| Field | Type | Meaning |
|---|---|---|
| `vendor_pattern` | string | Regex, matched case-insensitively against both the transaction description and the document vendor. A plain name is a valid regex. |
| `category` | string | A canonical name from `config/ised_categories.py`. |
| `priority` | int, default 50 | Higher wins when two patterns match. |
| `source` | `manual` \| `learned` | Who wrote the rule. |

**How it is loaded.** Not through `config/data_files.py`. The one place that
opens this file by name is `core/reconciliation.py` `_known_vendor_patterns()`,
which reads `PROJECT_ROOT / "config" / "vendor_rules.yaml"` itself, `yaml.safe_load`s
it, and compiles every `vendor_pattern` — alongside the compiled
`vendor_aliases.yaml` table — so the matcher can tell "this bank line names a
merchant I have a rule about" from "this bank line names nobody". It is loaded
lazily and cached, because reconciliation must not pay a YAML parse per candidate
pair.

`core/categorization.py` also offers `load_vendor_rules(path)` (building
`VendorRule` objects from `models/vendor_rule.py`, sorted by `priority`
descending) and `match_vendor_rule()`, which returns the first hit's category.
That pair takes any path you hand it; nothing in the shipped engine hands it this
file, so today the `priority` and `category` fields are inert and only
`vendor_pattern` has an effect.

The file's schema also has `category_patterns:` (label → list of regexes),
`usd_billing_vendors:` (list of names) and `vendor_aliases:` (word → list of
synonyms). No module reads those three today; they are notes to yourself.

**When it is missing or wrong.** A missing file is tolerated silently — the
reconciliation loader catches the error and carries on with no vendor rules, so
every bank line is simply "names nobody". An unparseable file logs a warning and
yields none. An individual `vendor_pattern` that will not compile is skipped with
a warning. A `category` that is not canonical will be rejected downstream — it is
not a category.

## `config/transfer_keywords.yaml`

Which statement wordings mean "this row is one leg of a transfer between two of
your own accounts", so the linker can pair the two legs instead of treating each
as an unmatched expense.

```yaml
entries:
  - pattern: "PAYMENT\\s*-?\\s*THANK\\s*YOU"
    transfer_type: CREDIT_CARD_PAYMENT
    side: TARGET
    account_types: [CreditCard]
    priority: 95
```

| Field | Type | Meaning |
|---|---|---|
| `pattern` | string | Regex, compiled with `re.IGNORECASE`, searched against the raw description. |
| `transfer_type` | string | `CREDIT_CARD_PAYMENT`, `FX_CONVERSION`, `WISE_TRANSFER`, `INTERNAL_TRANSFER`, `REFUND`. |
| `side` | `SOURCE` \| `TARGET` | The account the money leaves, or the one it arrives in. |
| `account_types` | list | Account keys (the `config/accounts.yaml` keys) this entry applies to. |
| `priority` | int, default 50 | Breaks ties when several entries match one description. |

A pair only becomes a link candidate when **both** legs match: one `SOURCE`
entry scoped to the paying account and one `TARGET` entry scoped to the
receiving account. A leg that matches nothing is never paired, which is why an
institution whose wording is not in this table leaves its transfers unlinked.

**How it is loaded.** Not through `config/data_files.py`.
`core/transaction_linker.py` `_load_keywords()` resolves
`config/transfer_keywords.yaml` relative to the repository root (or takes an
explicit `keyword_config_path`) and `yaml.safe_load`s it.

**When it is missing or wrong.** A missing file logs a warning and yields an
empty keyword list: no transfer is linked by keyword at all. An entry whose
pattern will not compile, or that is missing `transfer_type`, `side` or
`account_types`, is skipped with a warning and the rest of the table keeps
working.

## `config/aa_category_gifi_map.yaml`

The free-text safety net for the CRA reasonableness / peer-benchmark engine:
substring rules that assign a GIFI rollup bucket to a category string that is
**not** in the fixed taxonomy (an imported or hand-edited legacy label).

```yaml
version: 1
fallback_rules:
  - {contains: ["insurance"], gifi_bucket: insurance}
default: {gifi_bucket: other_operating, is_generic: true}
```

Only `fallback_rules:` and `default:` are read. There is deliberately no
per-category block in this file: every canonical name's bucket comes from
`config/ised_categories.py` `as_gifi_map_dict()`, an exact identity map, so the
two can never disagree. Resolution order is exact canonical name → first
`contains:` substring hit, in file order → `default`. Needles are matched against
the label with whitespace and punctuation stripped, so keep them specific enough
not to collide: a bare `"ai"` would swallow `"airfare"`.

**How it is loaded.** Not through `config/data_files.py`.
`core/reasonableness.py` `load_category_gifi_map()` calls `_load_legacy_gifi_yaml()`,
which opens the path in `GIFI_MAPPING_PATH` (`config/settings.py`, overridable by
the environment variable of the same name) and `yaml.safe_load`s it, then caches
the result for the process.

**When it is missing or wrong.** `_load_legacy_gifi_yaml()` opens the path
directly, so a missing or unreadable file raises rather than degrading — set
`GIFI_MAPPING_PATH` or leave the shipped file in place. An empty file parses to
no fallback rules and a built-in `other_operating` default, which means every
non-canonical label lands in the generic bucket. A rule with no `contains:` list
is simply never matched, and the rules after it still work.

---

## The transaction shape

One row of a bank, card or payment-platform statement.
`models/transaction.py` (`Transaction`), table `transactions` in
`db/schema_pg.sql`.

| Field | Type | Notes |
|---|---|---|
| `id` | int | Assigned on insert. |
| `account` | str | **Free-form text.** See below. |
| `transaction_type` | `DEBIT` \| `CREDIT` | Enforced by a regex on the model and a `CHECK` in the schema. This, not the sign of `amount`, is the authoritative direction. |
| `date_posted` | date | Stored as ISO `YYYY-MM-DD` text. |
| `amount` | Decimal | Stored as **text** so decimal precision survives the round trip. Sign varies — see below. |
| `currency` | str | One of `CAD`, `USD`, `CNY`, `EUR`, `GBP`, enforced in both model and schema. Amounts in different currencies are never summed. |
| `description` | str | The raw statement line. Everything the categorization engine does starts here. |
| `source_file`, `source_row` | str, int | Provenance back to the imported file and line. |
| `status` | enum | `UNMATCHED`, `MATCHED`, `IGNORED`, `LINKED`, `PENDING_REVIEW`, `EXCLUDED`. |
| `category` | str \| None | A canonical category name, or `NULL`. The literal string `"Uncategorized"` is never written — it is a display label only. |
| `note` | str \| None | User note; read by the categorization agent as an authoritative signal. |
| `institution`, `account_type`, `external_id` | str \| None | Set by the institution-agnostic import path. |
| `import_confidence` | `HIGH` \| `MEDIUM` \| `LOW` \| `LEGACY` \| None | How sure the parser was. |
| `import_flags` | list[str] | JSONB; parser warnings for this row. |
| `statement_id` | UUID \| None | The statement import this row came from. |

**`account` is free-form.** It was an enum once (`AccountType` in
`models/transaction.py` still exists for the legacy rows and the built-in CSV
parsers), but the column is `TEXT NOT NULL` with only a non-empty check: any
institution name an importer produces is valid. `account_str()` accepts either a
legacy enum instance or a plain string and returns the string. The keys in
`config/accounts.yaml` are exactly these strings; an account key with no entry
there is displayed as-is.

**Amount signs vary by source, on purpose.** The parsers preserve what each
source prints: a chequing CSV is stored as printed; the card parser treats a
positive amount as a `DEBIT` (a charge) and a negative one as a `CREDIT` (a
payment or refund); the transfer-platform parser negates the amount on a `DEBIT`
row. So **never infer direction from the sign**. Reporting code reads
`transaction_type` for direction and `abs(amount)` for magnitude, per currency.

**`IGNORED` and `EXCLUDED` mean opposite things about money.** `IGNORED` is a
self-evident row — a bank fee, interest, an internal transfer — that needs no
receipt: its money **still counts** in every total. `EXCLUDED` is a row that is
not real (a mis-parse, a duplicate, not yours), rejected at import review: its
money **must never count**.

## The document shape

One receipt, invoice or bill. `models/document.py` (`Document`), table
`documents`.

| Field | Type | Notes |
|---|---|---|
| `id` | int | Assigned on insert. |
| `original_filename` | str | As uploaded. |
| `stored_path` | str \| None | Where the file landed in the file store. |
| `file_hash` | str | SHA-256 of the bytes; the duplicate check. |
| `vendor` | str \| None | Extracted merchant name — the main signal for vendor scoring at reconciliation. |
| `document_date` | date \| None | ISO `YYYY-MM-DD`. |
| `currency` | str \| None | `None` when the document said nothing. |
| `currency_source` | `stated` \| `inferred` \| `assumed` \| None | Provenance for `currency`: the document says so; read off its Canadian tax line; or nothing said (and `currency` is then `NULL`). `NULL` on rows written before provenance existed — those are read as `stated`. |
| `subtotal` | Decimal \| None | Pre-tax amount. |
| `tax_gst`, `tax_hst`, `tax_pst`, `tax_other` | Decimal \| None | Taxes kept **separate**, never merged into one number: `core/gst_tax_code.py` asserts a recoverable input tax credit only on positive evidence of GST or HST on the linked document, so a PST or other-tax amount must not be mistakable for one. `TaxBreakdown` in `models/document.py` also carries a `tax_label`. |
| `total` | Decimal \| None | What the amount match compares against. |
| `payment_method`, `invoice_number` | str \| None | As printed. |
| `line_items` | list[LineItem] \| None | `description`, `quantity`, `unit_price`, `amount` each. Stored as JSON text in `documents.line_items_json`. |
| `extraction_confidence` | `high` \| `medium` \| `low` \| None | Overall extraction confidence. |
| `field_confidence` | dict \| None | Per-field confidence, e.g. `{"vendor": "high", "date": "medium"}`. |
| `extraction_raw_json` | str \| None | The raw model output, kept for audit. |
| `status` | `EXTRACTED` \| `MATCHED` \| `FLAGGED` | Extraction/reconciliation state. |
| `review_status` | str | `pending`, `auto_accepted`, `flagged`, `corrected`. Not constrained by the schema; the model defaults to `pending` and the column defaults to `PENDING`. |

All money fields are stored as text for the same precision reason as
`transactions.amount`.

**There are no views.** Proof is assembled from the base tables in
`db/database_pg.py`, not read out of a view, and that is deliberate: a view over these
tables runs with its owner's privileges and row-level-security context unless it is
created `WITH (security_invoker = true)`, so one written without the option returns
every tenant's rows to any caller that can select from it. The schema defines none;
`tests/test_rls_enforcement.py` enumerates the catalogue and fails if one appears
without the option.

## The reconciliation match, and the ledger entry

### `ReconciliationMatch`

`models/match.py`, table `reconciliation_matches`. One transaction paired with
one document, plus why.

| Field | Type | Notes |
|---|---|---|
| `transaction_id`, `document_id` | int | The pair. |
| `confidence_score` | float 0–1 | Overall. |
| `amount_score`, `date_score`, `vendor_score` | float 0–1 | The three sub-scores behind it. |
| `match_type` | `ONE_TO_ONE` \| `MANY_TO_ONE` \| `ONE_TO_MANY` | |
| `status` | `AUTO_APPROVED` \| `PENDING_REVIEW` \| `USER_APPROVED` \| `USER_REJECTED` | An uncertain pair is flagged for review, never guessed. |
| `match_source` | `AUTO` \| `MANUAL` | |
| `reviewed_by`, `reviewed_at`, `user_action`, `actioned_at` | | The review trail. `user_action` is `approved`, `rejected` or `unmatched`. |
| `explanation` | str \| None | Plain-language reason, shown in the UI and the binder. |
| `group_id` | UUID | Groups several proofs covering one transaction. |
| `covered_amount` | str \| None | Decimal text; `None` means this proof covers the full amount. |
| `adjudication` | `agents` \| `deterministic` | Run-local, not a database column: whether the LLM pipeline decided the pair or it was unambiguous. |

Proof coverage for one transaction is reported by `ProofItem` /
`ProofCoverageStats` in the same module. A `ProofItem` has
`proof_type` of `document` or `transaction_link` — the second is the other side
of an internal transfer, which is a link to a transaction, **not** a receipt.

### `LedgerEntry`

`models/ledger_entry.py`, table `ledger_entries`. A flattened, exportable row:
`account`, `month` (e.g. `Jan2026`), `transaction_type`, `date_posted`,
`amount`, `currency`, `description`, `category`, `document_link`, `note`,
`match_id`.

A ledger entry has **no status of its own**. It is derived from a transaction
and, when one exists, the match that substantiates it: `match_id` points at the
`reconciliation_matches` row and `document_link` at the proof file. Its status,
if you need one, is the status of those two.

## The category taxonomy

`config/ised_categories.py` is a **closed** list of canonical Canada ISED/GIFI
category names — 27 of them, in four sections (`revenue`, `cost_of_sales`,
`operating`, `structural`). It is code, not config, and the one source of truth:
the categorization agent classifies **into** this list and never invents,
renames, merges or deletes a name.

Every `category:` you write in `config/category_rules.yaml` or
`config/vendor_rules.yaml`, and every category name anywhere else in the engine,
must be one of these exact strings. `CATEGORY_NAME_SET` is the set of valid
stored values; `is_valid_category()` and `coerce_to_valid()` are how a name is
checked on the way in, and `remap_legacy()` is how a stored name is normalised on
the way out — it returns the name if it is still in the taxonomy and `None` if it
is not, so a value that has drifted off the list can never reach a report as if it
were a category.

Each entry carries more than a name: `kind`
(`income|cogs|cogs_contra|expense|transfer|equity|tax|contra`), `section`,
`ised_bucket` (the benchmarking rollup), `gifi` (the CRA GIFI line code for the
audit binder and T2), and a one-line `definition` that is injected into every
LLM prompt. Only `kind='transfer'` removes a row from raw money totals
(`core/transfer_exclusion.py`), which is why wages, owner's draw and tax
remittance are deliberately **not** transfers.

Two names behave specially:

* `"Uncategorized"` (`UNCATEGORIZED_LABEL`) is a **display label only**. The
  write path stores `NULL` in `transactions.category`; the UI renders it with
  `COALESCE(category, 'Uncategorized')`.
* Journal-only categories (`is_assignable()` is false) are period journal
  entries and are never assignable to a bank transaction.

Adding a category is a taxonomy change, not a config change. Do not add rows
without deciding you mean it.

## Bring your own data

Two ingest routes, both through the web UI.

### Bank / card statements

`POST /api/transactions/upload-statement`, one file, 50 MB maximum, extension
one of `.csv`, `.pdf`, `.xlsx`, `.xls`. The extension, the size and the file's
magic bytes are checked before anything else happens, and the same bytes
uploaded twice are recognised as a duplicate rather than imported again.

* **CSV / XLSX** parse deterministically in well under a second and answer
  `200`. An XLSX is converted to CSV first, then treated as one.
* **PDF** is parsed by the LLM, which takes tens of seconds to minutes, so it is
  stored, queued as a job and answered `202`; the browser follows it over SSE or
  by polling `GET /api/jobs`. This path is institution-agnostic — it reads
  whatever the statement says.

`core/bank_parser.py` `detect_csv_format()` recognises a CSV by its **header
row**, skipping leading blank and `#` comment lines, and raises
`ValueError("Unknown or unrecognized CSV format")` if no signature matches. The
built-in signatures are combinations of column names: a header containing
`Transaction Type`, `Date Posted` and `Transaction Amount` is the BMO chequing
export (`bmo_cad`, parsed by `parse_bmo_csv`); one containing `Posting Date` and
`Card Number` is the BMO credit-card export (`bmo_credit_card`); one containing
`ID`, `Status`, `Direction` and `Exchange rate` is Wise; `TimeZone` / `Gross` /
`Net` is PayPal; and `ASIN` with `Total Owed` or `Item Net Total` is Amazon.

**The minimum a CSV needs**, if you are producing one yourself, is the first of
those signatures: a header row containing the strings `Transaction Type`,
`Date Posted` and `Transaction Amount`, then one row per transaction with at
least five columns, in this order:

| # | Column | Value |
|---|---|---|
| 1 | account number | Ignored by the parser; may be anything, including blank. |
| 2 | transaction type | `DEBIT` or `CREDIT`. |
| 3 | date posted | `YYYYMMDD`, no separators. |
| 4 | amount | Decimal, e.g. `-42.50`. |
| 5 | description | The statement line. |

Rows with fewer than five columns are skipped with a warning. The account key
those rows land under comes from `_infer_account_from_filename()`, which
lowercases the filename stem and looks for `creditcard`, `credit_card` or a `cc`
segment; then `_usd`, `usd_` or a stem ending `usd`; then `_cad`, `cad_` or a
stem ending `cad`. Anything it cannot recognise defaults to `CAD` — so name the
file after the account you mean, then add that key to `config/accounts.yaml` if
you want a nicer name in reports.

### Receipts

`POST /api/receipts/upload`, 50 MB maximum, extension one of `.pdf`, `.jpg`,
`.jpeg`, `.png`, `.jfif`, `.webp`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.xlsx`,
`.xls`, `.csv`, `.docx`, `.doc`. The request stores the file, queues an
extraction job and answers `202`; the worker fills in the document fields above.
Duplicate bytes return the existing document or the job already running for it.

With `?auto_detect=1`, a PDF that `core/statement_detector.py` recognises as a
bank statement is routed to the statement pipeline instead. It is off by
default: a file dropped in the explicit receipts zone is a receipt because you
said so.

### After ingest

Reconciliation pairs transactions with documents, the categorization agent
assigns canonical categories (starting with the deterministic pre-pass above),
and the binder exports an unencrypted ZIP: an XLSX ledger workbook, a
`proof_of_transaction/` folder of the matched source documents, and a
self-contained HTML report. Amounts in different currencies are never summed
into one total, at any stage.
