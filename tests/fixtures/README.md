# Test fixtures — synthetic, generated, seed 42, no real records

Every file in this directory is **written by `scripts/gen_fixtures.py`**, never
collected. There is no real bank statement, receipt, invoice, client,
contractor, merchant, account number or amount anywhere in it. The company is
`Example Corp Inc.`, the people are invented, the addresses are
`123 Example St, Anytown, MB R0A 0A0`, the account numbers are
`000000-1234`-style, and every email address is at `example.com`.

`test_bank_statement.pdf` and `test_bank_cad.csv` are **a synthetic statement in
the BMO export layout**: they carry the product strings and the descriptor
grammar the built-in BMO parser keys on, because format detection is what they
exist to exercise, and every other value in them is invented. Each generated PDF
prints `This document is a synthetic test fixture` on its face.

Regenerate with:

```bash
python scripts/gen_fixtures.py            # seed 42
python scripts/gen_fixtures.py --rows 500 # same shapes, bigger CSV/XLSX
```

`gen_fixtures.py` writes **every file listed below**, with no exceptions — this
directory has no hand-kept file. It is deterministic: run it twice, or run it
on a fresh clone, and `git status` stays clean — the PDFs use reportlab's
invariant mode (fixed document id and creation date) and the XLSX has its
member timestamps rewritten, because both formats otherwise stamp the wall
clock into their bytes. It uses only packages already in `requirements.txt`
(reportlab, Pillow, openpyxl), so there is nothing extra to install.

The company is in **Manitoba**, and that is load-bearing rather than
decoration: the retail receipt charges 5% GST and a separate 7% provincial
sales tax, which is a real combination in Manitoba and an invalid one in an
HST province. The whole invented cast shares the province, so a supplier's
invoice, the company's own bank statement and a receipt shipped to the company
cannot disagree about where the company is.

## What is here, and which code path each one keeps honest

| File | Rows / pages | Exercises |
|---|---|---|
| `test_bank_statement.pdf` | 2 pages, 4 transactions | A synthetic statement in the BMO export layout, read by `core/pdf_statement_parser.py` and `core/statement_detector.py`. The quirks are the point: the table crosses a page break, pdfplumber returns the columns run together (`Openingbalance`, `Closingtotals`), the FX line carries an `AT1.3500` token that must **not** read as a second amount, the wire line carries a counterparty memo, an interest line is a credit with no receipt behind it, and the opening/closing rows have a transaction's shape without being transactions. The account is detected as USD from `Account Type: USD`. |
| `test_invoice_contractor.pdf` | 1 page, 2 line items | `core/statement_detector.py` — an invoice with dates, a subtotal, GST and a total must fire **zero** statement signals. Its invoice date and due date sit on separate lines on purpose: together on one line they would read as a date *range*, which is a signal. It charges GST only, with no provincial line, because that is what professional services billed between two Manitoba businesses carry. |
| `test_receipt_retailer.pdf` | 1 page, 1 line item | The same false-positive guard for a retail receipt, and a document with **separate** GST and provincial-tax lines — `core/gst_tax_code.py` may only claim an input tax credit on evidence of GST/HST, never on a provincial sales tax. The provincial line is labelled `RST`, Manitoba's own name for it, which the engine treats like any other provincial tax: it reads the GST and HST amounts and nothing else. |
| `test_receipt_retailer.png` | 620×460 | The image ingest path: a photographed receipt. |
| `test_receipt_scan.pdf` | 1 page, image only | A scan has no text layer; the detector must answer `no usable text layer` rather than raise. |
| `test_bank_cad.csv` | 4 rows | A synthetic statement in the BMO export layout, read by `core/bank_parser.py` `detect_csv_format` → `parse_bmo_csv`: the chequing header signature, `YYYYMMDD` dates, signed amounts, and a wire row that names a counterparty. The account key comes from the filename stem ending in `cad`. |
| `test_wise.csv` | 5 rows (3 COMPLETED) | `parse_wise_csv`: the COMPLETED-only filter, `Direction` → transaction type, the fee column, and an FX leg whose rate and target currency land in the description. The `PENDING` and `CANCELLED` rows are short on purpose — dropping them must not depend on reading the columns that are missing. |
| `test_paypal.csv` | 5 rows, 3 imported | `detect_csv_format` → `parse_paypal_csv`: the PayPal export signature (`TimeZone` / `Gross` / `Net`), the Completed-only filter, an internal currency-conversion row that `_PAYPAL_SKIP_TYPES` must drop, a fee that lands in the description, and one credit among the debits. Every row's `Net` is `Gross + Fee`, so the arithmetic closes. The `TimeZone` column says `UTC` on purpose: the column otherwise names the account holder's local zone, which is not something a checked-in fixture should carry. |
| `test_unsupported.txt` | 1 line | A file whose first line matches no CSV signature — `detect_csv_format` must raise. One line of prose; it contains no data at all. |
| `ledger-2026-01.xlsx` | 2 sheets, 8 rows | `scripts/qa/xlsx_parser.py`: a hand-kept ground-truth workbook with a CAD and a USD section in **one** sheet separated by a SUM row, a metadata line above the credit-card header, and a credit-card sheet whose sign convention is the opposite of the chequing one. Its category column uses canonical names from `config/ised_categories.py`. |
| `ised_benchmarks_sample.json` | 4 cells | `core/reasonableness.py` `load_benchmarks`. The same JSON shape as `config/ised_benchmarks.json` (gitignored, far too large to check in, built by `scripts/ingest_ised_benchmarks.py`), cut to four cells. **The ratios are invented**: NAICS codes are the public Canadian taxonomy, but no number in this file is real industry data. `tests/conftest.py` points `BENCHMARK_DATA_PATH` here so the suite never needs the large file. |
| `ised_benchmarks_cohorts_sample.json` | 10 cells | The same shape, one industry, carried across every cohort the reasonableness engine selects between: the whole-band `all` cell with per-line `{q1, median, q3}` bands, four by-revenue cohorts and four by-profit-margin cohorts as point cells, and one cell too thin to read so the density gate has something to suppress. **Every figure is invented** — it is the benchmark file's shape, not a sample of any published release, and no Open Government Licence data file ships in this repository. Two shape choices are deliberate: the revenue cohorts leave the $180K–$450K window uncovered, so the fallback from a size cohort to the whole-band cell has a case; and spending falls as profitability rises across `pm_q1`..`pm_q4`, so a comparison against the most-profitable cohort has a direction. It passes `scripts/ingest_ised_benchmarks.py`'s own validator, which `tests/test_ised_ingest.py` asserts. |

## What the synthetic set does not exercise

- **Scale.** Four transactions is not a month and a month is not a year. The
  generator takes `--rows` for volume, but nothing checked in here will surface
  an O(n²) matcher.
- **Distribution skew.** Invented amounts are evenly spread; a working set of
  books is not, because recurring vendors dominate, FX rates cluster, and one
  merchant turns up under several spellings. The vendor-clustering and
  rule-prepass paths are tested on hand-written cases, not on a long tail.
- **Published peer benchmarks.** The two benchmark samples have the shape of the
  published table and none of its numbers, so they prove the engine reads and
  suppresses correctly — never that a verdict about a business is right.
- **Scanned quality.** `test_receipt_scan.pdf` is a clean render of text, not a
  faded thermal receipt photographed at an angle. The retry-at-higher-DPI path
  in `core/extraction.py` is tested with mocks, not with genuinely bad input.
- **Anything an LLM must read.** No test in this repository sends a fixture to a
  model. Extraction and statement parsing are tested against recorded-shape
  responses; the fixtures prove the deterministic halves.

See `docs/data-schema.md` for the record shapes, and the "Bring your own data"
section there for how to point the engine at your own statements instead.
