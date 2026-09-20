# Domain — Canadian small-business bookkeeping

The facts the engine has to be right about. Getting these wrong produces a filing error, which is
the one class of bug this project cannot ship. Record shapes, status vocabularies, the category
taxonomy and the accepted CSV format are specified in [`docs/data-schema.md`](../../docs/data-schema.md);
this file is the *why* behind them.

## Tax

Canada, CRA. GST/HST/PST, with the ISED/GIFI taxonomy wired in throughout. VAT, US sales tax and
every other regime are not modelled — do not half-add one.

**CRA Information Circular IC05-1R1** governs digital records: keep them at least six years, and
keep them readable. That is why the audit binder ZIP is **unencrypted** on purpose
(`core/export_binder.py`): a reviewer must be able to open it without a password, and an encrypted
archive nobody can open preserves nothing. Do not "harden" it by adding a password.

This software files nothing and is not accounting advice. It produces a ledger and reports that a
human checks.

## Accounts

A typical install has a CAD chequing account, a USD chequing account, a multi-currency wallet
(Wise) and a credit card; `config/accounts.yaml` names them and orders report sections.
Movements *between* a user's own accounts — transfers, card payments, currency conversions — are
internal, not income or expense. Reconciliation excludes them, along with bank fees and interest,
rather than force-matching them to a document (`core/transfer_exclusion.py`,
`config/transfer_keywords.yaml`).

Amounts in different currencies are never summed into one total. A cross-currency match is only
allowed inside the configured FX band (`FX_CAD_USD_MIN` / `FX_CAD_USD_MAX`), and an implied rate
outside it is a non-match, not a rounding problem.

## Documents

Gathered and renamed documents follow `YYYYMMDD-VendorName-Type.ext`
(`core/gather/DOCUMENT_RULES.md`). Every ledger entry links back to its source document — that
link is what makes the books auditable, so an entry that cannot produce its source is incomplete
regardless of whether the number is right.

Dates legitimately disagree: an invoice date precedes the bank clearing date, one bank charge can
cover several invoices, and one invoice can be paid in installments. The matcher treats these as
the domain, inside `RECONCILIATION_DATE_WINDOW_DAYS`, not as errors.

## Ledger

Monthly sheets per account, an index sheet and a net-income summary in the XLSX workbook; a
GST/HST/PST summary; QuickBooks- and Xero-style CSVs. Categories are a fixed taxonomy (see
`docs/data-schema.md`, *The category taxonomy*) mapped to GIFI buckets by
`config/aa_category_gifi_map.yaml` for the peer-benchmark pass.

## What belongs in config, not code

A user's own vendors, clients, aliases and account names live in the six YAML files under
`config/`. The engine must stay general: a fix that only works for one person's merchants belongs
in their YAML, and a fix in `core/` has to hold for books it has never seen.
