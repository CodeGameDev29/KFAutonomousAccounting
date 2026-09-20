"""Tests for the HTML Audit Binder template (core/templates/binder.html).

Validates that the sidebar uses <aside> (not <nav>) to avoid classless-CSS
flex/inline-block conflicts, and that the CSS is clean of unnecessary
!important overrides on sidebar rules.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from core.html_binder import generate_html_report
from models.transaction import AccountType, Transaction, TransactionStatus


def _make_transaction(
    txn_id: int,
    account: AccountType = AccountType.CAD,
    txn_type: str = "DEBIT",
    amount: Decimal = Decimal("100.00"),
    currency: str = "CAD",
    description: str = "Test Transaction",
    status: TransactionStatus = TransactionStatus.MATCHED,
    date_posted: date | None = None,
) -> Transaction:
    """Create a minimal Transaction for testing."""
    return Transaction(
        id=txn_id,
        account=account,
        transaction_type=txn_type,
        date_posted=date_posted or date(2026, 1, 15),
        amount=amount,
        currency=currency,
        description=description,
        source_file="test.csv",
        source_row=1,
        status=status,
    )


class SidebarElementFinder(HTMLParser):
    """Simple HTML parser that finds what element has class='sidebar'."""

    def __init__(self):
        super().__init__()
        self.sidebar_tags: list[str] = []
        self.in_sidebar = False
        self.sidebar_children: list[str] = []
        self.sidebar_depth = 0

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes = attr_dict.get("class", "").split()
        if "sidebar" in classes:
            self.sidebar_tags.append(tag)
            self.in_sidebar = True
            self.sidebar_depth = 1
        elif self.in_sidebar:
            self.sidebar_depth += 1
            self.sidebar_children.append(tag)

    def handle_endtag(self, tag):
        if self.in_sidebar:
            self.sidebar_depth -= 1
            if self.sidebar_depth <= 0:
                self.in_sidebar = False


@pytest.fixture
def sample_transactions() -> list[Transaction]:
    """Provide a small set of transactions for template rendering."""
    return [
        _make_transaction(1, AccountType.CAD, "DEBIT", Decimal("50.00"), "CAD", "Office Supplies"),
        _make_transaction(2, AccountType.CAD, "CREDIT", Decimal("1000.00"), "CAD", "Client Payment"),
        _make_transaction(3, AccountType.USD, "DEBIT", Decimal("200.00"), "USD", "Cloud Hosting"),
    ]


@pytest.fixture
def rendered_html(sample_transactions) -> str:
    """Render the binder HTML template with sample data."""
    doc_paths = {1: "receipts/office.pdf", 2: "receipts/payment.pdf"}
    vendors = {1: "Cedarview Supplies", 2: "Northwind Trading", 3: "Bluepeak Software"}
    receipt_name_map = {
        1: "20260115-CedarviewSupplies-50.00-CAD.pdf",
        2: "20260115-NorthwindTrading-1000.00-CAD.pdf",
    }

    html = asyncio.get_event_loop().run_until_complete(
        generate_html_report(
            company_name="Test Corp",
            year=2026,
            month=1,
            label="Jan2026",
            transactions=sample_transactions,
            doc_paths=doc_paths,
            vendors=vendors,
            receipt_name_map=receipt_name_map,
        )
    )
    return html


class TestSidebarElement:
    """Verify the sidebar uses <aside> instead of <nav> to avoid classless-CSS conflicts."""

    def test_sidebar_is_aside_not_nav(self, rendered_html: str):
        """The sidebar element must be <aside>, not <nav>.

        A classless CSS framework applies aggressive horizontal-layout rules to <nav>
        (display: flex, inline-block list items). Using <aside> avoids this.
        """
        parser = SidebarElementFinder()
        parser.feed(rendered_html)

        assert len(parser.sidebar_tags) == 1, (
            f"Expected exactly one element with class='sidebar', found {len(parser.sidebar_tags)}"
        )
        assert parser.sidebar_tags[0] == "aside", (
            f"Sidebar element must be <aside>, not <{parser.sidebar_tags[0]}>. "
            "Using <nav> causes classless-CSS flex/inline-block conflicts."
        )

    def test_sidebar_contains_ul_with_li_children(self, rendered_html: str):
        """The sidebar must contain a <ul> with <li> children for navigation links."""
        parser = SidebarElementFinder()
        parser.feed(rendered_html)

        assert "ul" in parser.sidebar_children, "Sidebar must contain a <ul> element"
        assert "li" in parser.sidebar_children, "Sidebar <ul> must contain <li> elements"

    def test_sidebar_has_summary_and_reconciliation_links(self, rendered_html: str):
        """The first two sidebar links must point to #summary and #reconciliation."""
        assert 'href="#summary"' in rendered_html, "Sidebar must contain a link to #summary"
        assert 'href="#reconciliation"' in rendered_html, "Sidebar must contain a link to #reconciliation"


class TestSidebarCSS:
    """Verify the sidebar CSS does not use unnecessary !important overrides."""

    def test_no_important_in_desktop_sidebar_rules(self, rendered_html: str):
        """Desktop sidebar CSS must not use !important (not needed with <aside>).

        The only acceptable !important is in @media print rules (standard practice).
        """
        # Extract CSS between <style> tags
        style_start = rendered_html.find("<style>")
        style_end = rendered_html.find("</style>")
        assert style_start != -1 and style_end != -1, "HTML must contain a <style> block"

        full_css = rendered_html[style_start + 7 : style_end]

        # Split CSS into media-query blocks and non-media-query blocks, so the
        # check below sees only the .sidebar rules outside @media print
        import re

        # Remove @media print block (where !important is acceptable)
        css_no_print = re.sub(
            r"@media\s+print\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}",
            "",
            full_css,
            flags=re.DOTALL,
        )

        # Remove @media (max-width: 768px) block - check separately
        css_desktop = re.sub(
            r"@media\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}",
            "",
            css_no_print,
            flags=re.DOTALL,
        )

        # Find all .sidebar rules in desktop CSS
        sidebar_rules = re.findall(r"\.sidebar[^{]*\{[^}]*\}", css_desktop)
        for rule in sidebar_rules:
            assert "!important" not in rule, (
                f"Desktop .sidebar CSS must not use !important (not needed with <aside>). "
                f"Found in: {rule[:120]}"
            )

    def test_no_important_in_mobile_sidebar_rules(self, rendered_html: str):
        """Mobile sidebar CSS must not use !important (not needed with <aside>)."""
        import re

        style_start = rendered_html.find("<style>")
        style_end = rendered_html.find("</style>")
        full_css = rendered_html[style_start + 7 : style_end]

        # Extract @media (max-width: 768px) block
        mobile_match = re.search(
            r"@media\s*\(max-width:\s*768px\)\s*\{((?:[^{}]*\{[^}]*\})*[^{}]*)\}",
            full_css,
            flags=re.DOTALL,
        )

        if mobile_match:
            mobile_css = mobile_match.group(1)
            sidebar_rules = re.findall(r"\.sidebar[^{]*\{[^}]*\}", mobile_css)
            for rule in sidebar_rules:
                assert "!important" not in rule, (
                    f"Mobile .sidebar CSS must not use !important (not needed with <aside>). "
                    f"Found in: {rule[:120]}"
                )


class TestHTMLStructure:
    """General HTML structure validation."""

    def test_html_renders_without_error(self, rendered_html: str):
        """The template must render to valid non-empty HTML."""
        assert len(rendered_html) > 500, "Rendered HTML is suspiciously short"
        assert "<!DOCTYPE html>" in rendered_html
        assert "</html>" in rendered_html

    def test_summary_section_present(self, rendered_html: str):
        """The HTML must contain an executive summary section."""
        assert 'id="summary"' in rendered_html
        assert "Executive Summary" in rendered_html

    def test_reconciliation_section_present(self, rendered_html: str):
        """The HTML must contain a reconciliation status section."""
        assert 'id="reconciliation"' in rendered_html
        assert "Reconciliation Status" in rendered_html

    def test_print_css_hides_sidebar(self, rendered_html: str):
        """Print CSS must hide the sidebar using display: none !important."""
        assert ".sidebar" in rendered_html
        # Print CSS should contain: .sidebar, .back-top, .skip-link, .print-link { display: none !important; }
        assert "display: none !important" in rendered_html, (
            "Print CSS must use display: none !important to hide sidebar during printing"
        )
