"""LLM prompt templates for the categorization agent.

Every template is kept here so that (a) no phase module has inline
multi-paragraph strings, and (b) prompt wording is easy to tune in one
place. All templates are raw Python format-strings — callers do
``TEMPLATE.format(**kwargs)`` after JSON-encoding structured inputs.
"""

from __future__ import annotations

VENDOR_NORMALIZATION_FALLBACK = """\
You are normalizing bank/card transaction descriptions into stable
vendor keys for categorization. A vendor key must be:
- lowercase snake_case
- the business name only (strip reference numbers, city codes,
  transaction IDs, and trailing star-codes like *AB2)
- stable across spelling variations (e.g. EXMPL MKTP CA and EXAMPLE RETAILER
  both become "example_retailer")

DESCRIPTIONS TO NORMALIZE
{descriptions_json}

Return ONE JSON OBJECT — not a bare array — whose "vendor_keys" value is an
array with one entry per input, in the same order:
{{"vendor_keys": [{{"index": 0, "vendor_key": "example_retailer"}}, ...]}}
Return ONLY that JSON object, no extra text.
"""


CLOSED_LIST_RULES = """\
CLOSED TAXONOMY — HARD RULES (accuracy is the #1 priority):
- You may ONLY choose a category whose name appears VERBATIM in TAXONOMY below.
- You may NOT invent, rename, merge, abbreviate, or reword any category name.
- If no listed category is a confident fit, return exactly "Uncategorized".
  Do NOT guess. A wrong bucket is worse than "Uncategorized".
- "Opening inventory", "Closing inventory", and "Amortization and depletion"
  are JOURNAL-ONLY period entries — they are NOT in the list below and must
  never be assigned to a bank transaction.

CONTRACTOR TIE-BREAK:
- Contractor-platform payouts, Wise / PayPal transfers and outgoing-wire
  payments that deliver CLIENT work are "Purchases, materials and
  sub-contracts" (cost of sales).
- "Professional and business fees" is reserved for legal / accounting / notary
  / consulting for the business itself.

LABOUR TIE-BREAK (both are labour, choose deliberately):
- "Wages and benefits" = DIRECT/production labour that is a cost of the goods or
  services actually sold (cost of sales).
- "Labour and commissions" = INDIRECT administrative or sales labour (operating
  expense). For a service or micro business where the split is unclear, DEFAULT
  to "Labour and commissions" unless the pay is clearly a production cost tied to
  delivering a specific sold unit.
"""


VENDOR_DECISIONS = """\
You are categorizing vendors for a Canadian small business. For each
vendor below, pick exactly ONE category from the taxonomy that best
captures the nature of the business's activity with that vendor.

{closed_list_rules}

HOW TO DECIDE — priority order (STRICT):
0. **User notes are the HIGHEST-PRIORITY, AUTHORITATIVE signal.** If
   ``user_notes`` is non-empty, the user has explicitly told you what these
   transactions are — you MUST read and comprehend each note before deciding,
   and categorize according to it even when the bank description is cryptic
   (e.g. a note "Credit Card payoff" on an "Online Transfer, TF …" row means
   "Credit card payment"; a note "office rent" means "Rent"; "paid the
   contractor" means "Purchases, materials and sub-contracts"). A user note
   overrides an ambiguous description. BUT if a note itself expresses genuine
   uncertainty ("not sure what this is", "unknown", "need to check"), do NOT
   guess — return "Uncategorized".
1. **Matched-receipt proof** is the next PRIMARY signal. If ``proof_examples``
   is non-empty, read the ``proof_vendor``, ``proof_total``, and
   ``proof_line_items`` of those receipts and categorize based on WHAT
   WAS PURCHASED, not on the bank description. A row whose bank
   description is "General Currency Conversion", "Bank Deposit to PP
   Account", "Express Checkout Payment ...", or "PreApproved Payment
   Bill User ..." is a payment-processor wrapper — the underlying merchant
   is the ``proof_vendor`` and the purpose is in ``proof_line_items``.
   Never classify such rows as "Bank & Payment Processing Fees" just
   because the description mentions a payment processor — do that ONLY
   when the proof itself is literally a processor fee (e.g. a card-processor
   fee, a PayPal surcharge, a Wise FX spread) or when there is no proof AND the
   description is unambiguously a fee.
2. **proof_coverage** tells you how many txns in this cluster have proof.
   High coverage means trust the proof; low coverage means lean more on
   sample_descriptions and the business profile.
3. If there is NO proof, pick the category that fits the sample
   descriptions given the business profile. Don't default to "Others"
   unless the activity genuinely doesn't fit any more specific bucket.
4. Consistency: if vendor activity strongly resembles a vendor already
   categorized in "DECISIONS SO FAR", use the same category unless the
   proof says otherwise.

BUSINESS PROFILE
{business_profile}

TAXONOMY (choose one category.name per vendor)
{taxonomy_json}

DECISIONS SO FAR (vendor_key -> category.name from prior groups in this run)
{decisions_so_far_json}

VENDORS TO CATEGORIZE (20 max per call)
{vendors_json}

OUTPUT — READ THIS TWICE:
Return ONE JSON OBJECT. Not a bare array: a single object whose "decisions"
value is an array carrying ONE entry for EVERY vendor_key listed above, in the
same order, with the vendor_key copied VERBATIM. Do not stop after the first
vendor. Do not omit a vendor because it looks like another one. Keep each
"reasoning" under 200 characters so the object is complete.
``needs_refinement`` must be true when proof coverage is mixed and different
txns in the cluster likely belong to different categories.

{{
  "decisions": [
    {{
      "vendor_key": "example_retailer",
      "category": "Office supplies",
      "confidence": "medium",
      "reasoning": "Mixed-use vendor; most txns are consumables.",
      "needs_refinement": true
    }}
  ]
}}
If none of the listed categories fit, use "category": "Uncategorized".
Return ONLY that JSON object.
"""


REFINEMENT = """\
You are refining categorization for transactions one at a time. For each
transaction below, pick the category from the taxonomy that best captures
its specific purpose.

{closed_list_rules}

HOW TO DECIDE — priority order (STRICT):
0. **The user's own ``user_note`` is the HIGHEST-PRIORITY, AUTHORITATIVE
   signal.** When present, read and comprehend it and categorize according to
   it — the user is telling you exactly what this transaction is, which
   overrides an ambiguous or cryptic bank description (e.g. "Credit Card
   payoff" -> "Credit card payment"; "office rent" -> "Rent"). BUT if the note
   expresses genuine uncertainty ("not sure what this is", "unknown"), do NOT
   guess — return "Uncategorized".
1. If the transaction has a ``matched_proof`` block, that is the next PRIMARY
   signal. Read ``proof_vendor``, ``proof_total``, ``proof_invoice_number``
   and especially ``proof_line_items`` — they describe WHAT WAS ACTUALLY
   BOUGHT. Categorize based on that. The bank ``description`` is often
   just a payment-processor wrapper ("General Currency Conversion",
   "Bank Deposit to PP Account", "Express Checkout Payment ...",
   "PreApproved Payment Bill User ...", "User Initiated Withdrawal") —
   ignore it when matched_proof is present. NEVER return "Bank & Payment
   Processing Fees" for a row whose matched_proof describes a merchant
   purchase.
2. ``matched_proof.source`` can be "direct" (a receipt matched directly
   to this txn) or "linked" (inherited from a linked sibling transaction
   via the reconciliation link chain). Both are authoritative.
3. If there is NO matched_proof, fall back to the ``description`` and
   judge it against the business profile. Prefer the most specific
   category that fits — do not default to "Others" unless nothing fits.

BUSINESS PROFILE
{business_profile}

TAXONOMY
{taxonomy_json}

VENDOR CONTEXT
Vendor: {vendor_key}
Total txns: {count}, date range: {date_range}
Sibling descriptions (for context):
{sibling_samples}

TRANSACTIONS TO REFINE (10 max)
{transactions_json}

OUTPUT — READ THIS TWICE:
Return ONE JSON OBJECT. Not a bare array: a single object whose "refinements"
value is an array carrying ONE entry for EVERY transaction id listed above, in
the same order. Only those ids — never an id that is not in the list. Keep each
"reasoning" under 200 characters so the object is complete.

{{
  "refinements": [
    {{"id": 1234, "category": "Office supplies", "confidence": "high",
     "reasoning": "Receipt line items: 1x monitor stand, 1x USB-C hub."}}
  ]
}}
If none of the listed categories fit, use "category": "Uncategorized".
Return ONLY that JSON object.
"""


SELF_CONSISTENCY = """\
Review this categorization run for VENDOR-LEVEL consistency only. The taxonomy
is a FIXED, closed list — you may NOT merge, rename, add, or remove any category.
Your only output is reassignments: move a vendor to a different EXISTING category
when its current assignment clearly contradicts that category's definition. If a
vendor fits nothing, reassign it to "Uncategorized".

TAXONOMY (fixed — for reference only, do not modify)
{taxonomy_json}

VENDOR ASSIGNMENTS (grouped by category)
{assignments_by_category_json}

Return a JSON object with ONLY reassignments (merges and renames must be empty):
{{
  "merges": [],
  "renames": [],
  "reassignments": [
    {{"vendor_key": "foo", "from": "Office supplies", "to": "Software and IT",
     "reason": "SaaS subscription, not a consumable."}}
  ]
}}
Return ONLY the JSON object.
"""
