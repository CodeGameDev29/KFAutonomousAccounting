"""PayPal document gathering — generates detailed transaction receipt PDFs.

Fetches full transaction data from PayPal Reporting API including payer info,
item details, tax, shipping, and generates formatted PDF receipts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
from datetime import date, timedelta
from pathlib import Path

import certifi
import httpx

from core.gather.gmail import GatherResult
from core.paypal_api import _get_base_url, get_access_token, get_paypal_credentials

logger = logging.getLogger(__name__)


class PayPalGatherer:
    """Generates detailed PDF receipts for PayPal transactions."""

    def __init__(self, db, output_dir: str | Path) -> None:
        self.db = db
        self.output_dir = Path(output_dir) / "paypal"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def is_connected(self) -> bool:
        return get_paypal_credentials() is not None

    def _get_client(self) -> httpx.Client:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        return httpx.Client(verify=ssl_ctx, timeout=30)

    def _fetch_raw_transactions(
        self, access_token: str, since: date, until: date
    ) -> list[dict]:
        """Fetch raw transaction details from PayPal API in 30-day chunks."""
        base = _get_base_url()
        client = self._get_client()
        all_txns = []

        chunk_start = since
        while chunk_start < until:
            chunk_end = min(chunk_start + timedelta(days=30), until)
            page = 1
            total_pages = 1

            while page <= total_pages:
                try:
                    resp = client.get(
                        f"{base}/v1/reporting/transactions",
                        params={
                            "start_date": f"{chunk_start}T00:00:00-0000",
                            "end_date": f"{chunk_end}T23:59:59-0000",
                            "fields": "all",
                            "page_size": 100,
                            "page": page,
                        },
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    total_pages = data.get("total_pages", 1)
                    all_txns.extend(data.get("transaction_details", []))
                    page += 1
                except Exception as e:
                    logger.error("PayPal API error: %s", e)
                    break

            chunk_start = chunk_end + timedelta(days=1)

        client.close()
        return all_txns

    def gather(self, since: date | None = None) -> GatherResult:
        result = GatherResult(source="paypal")

        creds = get_paypal_credentials()
        if not creds:
            result.errors.append("PayPal not configured")
            return result

        if since is None:
            since = date.today() - timedelta(days=30)

        access_token = get_access_token(creds[0], creds[1])
        if not access_token:
            result.errors.append("Failed to get PayPal access token")
            return result

        try:
            raw_txns = self._fetch_raw_transactions(access_token, since, date.today())
            logger.info("PayPal: fetched %d raw transactions", len(raw_txns))

            # Only generate PDFs for primary transaction types (with counterparty info).
            # Skip internal PayPal operations (currency conversion, funding, general ledger).
            PRIMARY_EVENT_CODES = {
                "T0001",  # Express Checkout
                "T0003",  # Pre-approved Payment (subscriptions)
                "T0006",  # Website Payment
                "T0007",  # Website Payment (merchant)
                "T0400",  # General Withdrawal
                "T0401",  # AutoSweep
                "T0403",  # Temporary Hold / Authorization
                "T1107",  # Payment Refund
                "T1201",  # Chargeback
            }

            primary_txns = [
                t for t in raw_txns
                if t.get("transaction_info", {}).get("transaction_event_code", "") in PRIMARY_EVENT_CODES
                # Also include T0200 if they have a named counterparty
                or (t.get("transaction_info", {}).get("transaction_event_code", "") == "T0200"
                    and t.get("payer_info", {}).get("payer_name", {}).get("alternate_full_name", ""))
            ]
            logger.info("PayPal: %d primary transactions (of %d total)", len(primary_txns), len(raw_txns))

            for i, txn_data in enumerate(primary_txns, start=1):
                txn_info = txn_data.get("transaction_info", {})
                txn_id = txn_info.get("transaction_id", f"unknown_{i}")
                txn_date = txn_info.get("transaction_initiation_date", "")[:10]

                source_id = f"paypal_txn_{txn_id}"
                if self.db.get_gather_log_by_source_id("paypal", source_id):
                    result.documents_duplicate += 1
                    continue

                pdf_data = self._transaction_to_pdf(txn_data, i)
                if not pdf_data:
                    continue

                file_hash = hashlib.sha256(pdf_data).hexdigest()
                if self.db.get_gather_log_by_hash(file_hash) or self.db.is_file_processed(file_hash):
                    result.documents_duplicate += 1
                    continue

                # Build descriptive filename
                payer = self._extract_counterparty(txn_data)
                safe_payer = re.sub(r'[^A-Za-z0-9]', '', payer)[:30] or "Unknown"
                filename = f"PayPal_{txn_date}_{safe_payer}_{txn_id[:8]}.pdf"
                dest = self.output_dir / filename
                counter = 1
                while dest.exists():
                    dest = self.output_dir / f"PayPal_{txn_date}_{safe_payer}_{txn_id[:8]}-{counter}.pdf"
                    counter += 1

                dest.write_bytes(pdf_data)

                # Extract amount for metadata
                amount_obj = txn_info.get("transaction_amount", {})
                amount = amount_obj.get("value", "0")
                currency = amount_obj.get("currency_code", "CAD")

                self.db.insert_gather_log(
                    source="paypal", source_id=source_id,
                    filename=dest.name, file_hash=file_hash, file_size=len(pdf_data),
                    stored_path=str(dest),
                    metadata_json={
                        "transaction_id": txn_id,
                        "date": txn_date,
                        "amount": amount,
                        "currency": currency,
                        "counterparty": payer,
                    },
                )
                result.documents_new += 1
                result.transactions_new += 1

        except Exception as e:
            result.errors.append(f"PayPal gather error: {e}")
            logger.exception("PayPal gather failed")

        return result

    def _extract_counterparty(self, txn_data: dict) -> str:
        """Extract the other party's name from transaction data."""
        payer = txn_data.get("payer_info", {})
        name_obj = payer.get("payer_name", {})

        # Try alternate_full_name first (usually the company name)
        name = name_obj.get("alternate_full_name", "")
        if name:
            return name

        # Try given + surname
        given = name_obj.get("given_name", "")
        surname = name_obj.get("surname", "")
        if given or surname:
            return f"{given} {surname}".strip()

        # Try transaction subject or note (often has vendor name)
        txn_info = txn_data.get("transaction_info", {})
        subject = txn_info.get("transaction_subject", "")
        if subject:
            return subject

        note = txn_info.get("transaction_note", "")
        if note:
            return note

        # Try first cart item name as hint
        cart = txn_data.get("cart_info", {})
        items = cart.get("item_details", [])
        if items:
            item_name = items[0].get("item_name", "")
            if item_name and len(item_name) > 2:
                return item_name

        # Try email domain
        email = payer.get("email_address", "")
        if email:
            domain = email.split("@")[-1].split(".")[0]
            return domain.capitalize()

        return "Unknown"

    def _transaction_to_pdf(self, txn_data: dict, index: int) -> bytes | None:
        """Generate a detailed PDF receipt from raw PayPal transaction data."""
        try:
            import tempfile

            import fitz

            txn_info = txn_data.get("transaction_info", {})
            payer_info = txn_data.get("payer_info", {})
            shipping = txn_data.get("shipping_info", {})
            cart = txn_data.get("cart_info", {})

            # Extract fields
            txn_id = txn_info.get("transaction_id", "N/A")
            txn_date = txn_info.get("transaction_initiation_date", "N/A")
            amount_obj = txn_info.get("transaction_amount", {})
            amount = amount_obj.get("value", "0")
            currency = amount_obj.get("currency_code", "CAD")
            status = txn_info.get("transaction_status", "")
            status_map = {"S": "Completed", "P": "Pending", "V": "Reversed", "D": "Denied"}
            status_text = status_map.get(status, status)
            invoice_id = txn_info.get("invoice_id", "")
            custom = txn_info.get("custom_field", "")
            event_code = txn_info.get("transaction_event_code", "")

            # Tax
            tax_obj = txn_info.get("sales_tax_amount", {})
            tax = tax_obj.get("value", "") if tax_obj else ""

            # Payer
            counterparty = self._extract_counterparty(txn_data)
            payer_email = payer_info.get("email_address", "")
            payer_country = payer_info.get("country_code", "")

            # Shipping
            ship_name = shipping.get("name", "")
            ship_addr = shipping.get("address", {})
            ship_line = ", ".join(filter(None, [
                ship_addr.get("line1", ""),
                ship_addr.get("city", ""),
                ship_addr.get("state", ""),
                ship_addr.get("postal_code", ""),
                ship_addr.get("country_code", ""),
            ]))

            # Cart items
            items = cart.get("item_details", [])

            # Build HTML
            html_parts = [
                f"""
                <h2 style="color:#003087;margin-bottom:4px">PayPal Transaction Receipt</h2>
                <hr>
                <table>
                    <tr><td><b>Transaction ID:</b></td><td>{txn_id}</td></tr>
                    <tr><td><b>Date:</b></td><td>{txn_date}</td></tr>
                    <tr><td><b>Status:</b></td><td>{status_text}</td></tr>
                    <tr><td><b>Amount:</b></td><td>{amount} {currency}</td></tr>
                """,
            ]
            if tax:
                html_parts.append(f'<tr><td><b>Sales Tax:</b></td><td>{tax} {currency}</td></tr>')
            if invoice_id:
                html_parts.append(f'<tr><td><b>Invoice ID:</b></td><td>{invoice_id}</td></tr>')
            if custom:
                html_parts.append(f'<tr><td><b>Platform:</b></td><td>{custom}</td></tr>')

            html_parts.append("</table><br>")

            # Counterparty section
            html_parts.append('<h3 style="color:#003087">Counterparty</h3><table>')
            html_parts.append(f'<tr><td><b>Name:</b></td><td>{counterparty}</td></tr>')
            if payer_email:
                html_parts.append(f'<tr><td><b>Email:</b></td><td>{payer_email}</td></tr>')
            if payer_country:
                html_parts.append(f'<tr><td><b>Country:</b></td><td>{payer_country}</td></tr>')
            html_parts.append("</table><br>")

            # Shipping
            if ship_name or ship_line:
                html_parts.append('<h3 style="color:#003087">Shipping</h3><table>')
                if ship_name:
                    html_parts.append(f'<tr><td><b>Name:</b></td><td>{ship_name}</td></tr>')
                if ship_line:
                    html_parts.append(f'<tr><td><b>Address:</b></td><td>{ship_line}</td></tr>')
                html_parts.append("</table><br>")

            # Line items
            if items:
                html_parts.append('<h3 style="color:#003087">Items</h3>')
                html_parts.append('<table><tr><td><b>Item</b></td><td><b>Qty</b></td><td><b>Price</b></td><td><b>Total</b></td></tr>')
                for item in items:
                    name = item.get("item_name", "")
                    desc = item.get("item_description", "")
                    qty = item.get("item_quantity", "")
                    unit = item.get("item_unit_price", {}).get("value", "")
                    total = item.get("total_item_amount", {}).get("value", "")
                    item_cur = item.get("item_unit_price", {}).get("currency_code", currency)
                    display_name = name or desc
                    html_parts.append(
                        f'<tr><td>{display_name}</td><td>{qty}</td>'
                        f'<td>{unit} {item_cur}</td><td>{total} {item_cur}</td></tr>'
                    )
                html_parts.append("</table><br>")

            # Footer
            html_parts.append(f'<hr><p style="color:#666;font-size:9px">Event code: {event_code} | Source: PayPal Reporting API</p>')

            full_html = f"""
            <div style="font-family:Arial,sans-serif;font-size:11px;color:#333;max-width:540px">
                {"".join(html_parts)}
            </div>
            """

            # Render with PyMuPDF Story
            tmp_path = os.path.join(tempfile.gettempdir(), f"paypal_{os.getpid()}_{index}.pdf")

            # Strip inline styles for Story compatibility but keep structure
            simple = re.sub(r'\sstyle="[^"]*"', '', full_html)
            simple = f'<div style="font-family:Arial,sans-serif;font-size:11px;color:#333">{simple}</div>'

            story = fitz.Story(html=simple)
            writer = fitz.DocumentWriter(tmp_path)
            mediabox = fitz.paper_rect("letter")
            where = mediabox + (36, 36, -36, -36)

            page_count = 0
            more = True
            prev_filled = -1
            while more and page_count < 5:
                device = writer.begin_page(mediabox)
                more, filled = story.place(where)
                story.draw(device)
                writer.end_page()
                page_count += 1
                if filled == prev_filled and page_count > 1:
                    break
                prev_filled = filled
            writer.close()
            del writer, story

            with open(tmp_path, 'rb') as f:
                data = f.read()
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            return data
        except Exception as e:
            logger.error("Failed to create PayPal PDF: %s", e)
            return None
