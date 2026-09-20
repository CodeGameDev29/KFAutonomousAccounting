# Document Output Invariants

Rigid, non-negotiable rules that every document must satisfy at each pipeline stage.
Used by the gather and rename validation passes to reject non-conforming files.

---

## Gather Phase Rules (Rules 1–11)

Every file in `gather/` must pass ALL of these:

### Content Rules
1. **Monetary amount required** — Must contain at least one recognizable monetary amount (`$X.XX`, `CAD`/`USD` + digits, or `total/amount/subtotal: $X`)
2. **Not blank** — Must have ≥50 characters of extractable text
3. **Max 5 pages** — No PDF may exceed 5 pages
4. **No blank pages** — Every page in the PDF must contain ≥20 characters of text
5. **No personal vendors** — Must not be a personal (non-business) purchase: groceries, restaurants, gyms, streaming, clothing, personal healthcare, etc.
6. **Business relevant** — Must be at least marginally related to the business purpose of the company configured in the business profile
7. **Not marketing** — Must not be promotional content (payment buttons, newsletters, "get up to $X", referral offers)
8. **Financial document** — Must be a receipt, invoice, statement, payment confirmation, wire transfer, or bill

### Format Rules (email body PDFs)
9. **Email header present** — Email-sourced PDFs must have From, Date, Subject at the top
10. **No raw HTML** — No visible HTML tags (`<div>`, `<td>`, `&nbsp;`, etc.) in rendered text
11. **No broken characters** — No encoding artifacts (`?????`, `â€™`, `Â`, `Ã©`, etc.)

---

## Rename Phase Rules (Rules 12–19)

Every file output by the rename phase must pass ALL of these:

### Filename Format: `YYYYMMDD-VendorName-Type.ext`
12. **Date format** — Must be exactly 8 digits (`YYYYMMDD`) or `00000000` if unknown. Never partial, never `0000MMDD`
13. **Vendor name** — Must be CamelCase with no spaces or special characters. Must be the actual transaction counterparty — never `Gmail`, never `NonFinancial`, never `Unknown`
14. **Document type** — Must be one of: `Invoice`, `Receipt`, `Statement`, `Bill`, `Contract`, `Report`, `Letter`, `Other`
15. **NonFinancial exclusion** — Files classified as `NonFinancial` by the AI must be excluded entirely, not renamed and saved

### Hard Exclusions
16. **No NonFinancial files** — No file named `*-NonFinancial-*` in the output
17. **No Unknown-Other files** — No file named `*-Unknown-Other*` in the output
18. **No empty extractions** — No file with `00000000` date AND `Unknown` vendor (means nothing was extractable — file should be excluded)
19. **No duplicate filenames** — Collisions must be resolved with `-2`, `-3` suffix. No two files may share the same name

### Payment Confirmation / Notification Rule
20. **Email body notifications must pass the 2-of-3 substantive proof test** — Email-body PDFs (not attachments) are only kept if the email text itself contains at least 2 of these 3 elements:
    - **(a) Specific dollar amount** — `$X.XX` or `CAD/USD X.XX` present in the body
    - **(b) Named counterparty** — vendor/merchant name clearly stated (not just "your payment")
    - **(c) Transaction identifier** — order #, transaction ID, invoice #, reference number

    Emails with ≥2 of 3 are substantive proof (keep). Emails with 0–1 are vague notifications (discard).

    Examples KEPT: "Your subscription renewed — $41.25 charged by Bluepeak Software Inc" (amount + vendor), "You sent $104.55 to Cedarview Supplies Ltd, reference 4XY9A2" (amount + vendor + identifier)

    Examples DISCARDED: "Your utility bill is now ready" (0 of 3 — just a link), "Your transfer was successful" (0 of 3 — no amount), "Your order has been shipped" (0 of 3 — shipping, not financial)
