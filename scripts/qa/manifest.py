"""Which files belong to which accounting month.

This is the index the QA harness walks: for every month it knows about, the bank
statement PDFs, the folders of receipts, and the hand-reconciled XLSX that acts
as ground truth. ``scripts/qa/test_pdf_parsing.py`` parses the PDFs with the same
pipeline the app uses and compares the result against the workbook, so the
question "does the parser agree with the workbook?" can be answered without a
browser.

**It indexes your own files, and there are none in this repository.** Put them
under ``data/`` (which is gitignored) in this layout, or point ``AA_QA_DATA_DIR``
somewhere else::

    data/books/
        2026/
            statements/
                cad-2026-01.pdf         # one PDF per account per month. The stem
                usd-2026-01.pdf         # names the account (see
                creditcard-2026-01.pdf  # ACCOUNT_FILENAME_PATTERNS) and the
                                        # month, as YYYY-MM or "January 2026".
            receipts/
                2026-01/                # one folder per month; subfolders are walked
            ledger-2026-01.xlsx         # the ground-truth workbook

The accounts a month can hold are the account keys in ``config/accounts.yaml``,
the same keys the reports use, plus any other account a filename names — those
are appended rather than dropped, so an account missing from the config costs a
column position and nothing else. There is no fixed set of accounts here.

Nothing here is required for the app to run, and an absent directory is not an
error: ``build_manifest()`` simply returns a manifest with nothing in it.

The one file set that *does* ship is the synthetic fixture corpus in
``tests/fixtures`` — ``fixture_manifest()`` indexes that, so

    python -m scripts.qa.manifest

prints something meaningful with no data directory configured.

Usage::

    from scripts.qa.manifest import build_manifest, MonthData
    manifest = build_manifest()
    jan = manifest["2026-01"]
    print(jan.statements, jan.xlsx)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from config.data_files import account_display, account_order

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: Where your own statements, receipts and workbooks live. Gitignored.
QA_DATA_DIR = Path(os.getenv("AA_QA_DATA_DIR", str(PROJECT_ROOT / "data" / "books")))

#: The synthetic corpus that ships with the repository.
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

MONTH_FULL = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}
MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

SUPPORTED_RECEIPT_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp"}

#: Which account a statement filename is about: account key → the spellings a
#: filename may use for it. The keys are the ones stored in
#: ``transactions.account`` and named in ``config/accounts.yaml``; the patterns
#: are searched case-insensitively against the filename stem. Edit this table,
#: or pass ``account_patterns=`` to :func:`account_of`, to match how your own
#: files are named.
#:
#: Order matters and is the reason this is a tuple of pairs rather than a plain
#: mapping: a stem containing "credit card" also contains "card", so the card
#: entry has to be tried before an entry whose pattern it would satisfy.
ACCOUNT_FILENAME_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CreditCard", (r"credit[_\-\s]?card", r"\bcc\b", r"mastercard", r"visa")),
    ("USD", (r"\busd\b", r"us[_\-\s]?dollar", r"\bus\$")),
    ("CAD", (r"\bcad\b", r"chequing", r"checking")),
    ("Wise", (r"\bwise\b",)),
    ("PayPal", (r"\bpaypal\b",)),
    ("Amazon", (r"\bamazon\b",)),
)


def configured_accounts() -> list[str]:
    """The account keys ``config/accounts.yaml`` names, in its own order.

    An empty or missing config yields an empty list, which costs an ordering
    preference and nothing else: :func:`build_manifest` indexes whatever
    accounts the filenames name either way.
    """
    ordered = list(account_order())
    for key in account_display():
        if key not in ordered:
            ordered.append(key)
    return ordered


@dataclass
class MonthData:
    """All file paths for a single accounting month."""
    year: int
    month: int
    #: account key → the statement PDF for that account this month.
    statements: dict[str, Path] = field(default_factory=dict)
    receipts_dirs: list[Path] = field(default_factory=list)
    xlsx: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.year}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{MONTH_ABBR[self.month]} {self.year}"

    def statement_for(self, account: str) -> Path | None:
        """The statement PDF for one account key, if this month has one."""
        return self.statements.get(account)

    def receipt_files(self) -> list[Path]:
        """Every receipt under every receipt directory, deduped, sorted."""
        seen: set[Path] = set()
        files = []
        for d in self.receipts_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED_RECEIPT_EXTS:
                    resolved = f.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        files.append(f)
        return files

    def has_bank_pdfs(self) -> bool:
        return bool(self.statements)


# ── Filename reading ────────────────────────────────────────────────────────

def account_of(
    name: str,
    account_patterns: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> str | None:
    """Which account key a statement filename names, or None if it names none.

    The patterns table is consulted first, in its own order; a configured
    account with no entry in it still matches on its own key appearing in the
    stem, so adding an account to ``config/accounts.yaml`` is enough for files
    named after it to be indexed.
    """
    stem = name.lower()
    for account, patterns in (account_patterns or ACCOUNT_FILENAME_PATTERNS):
        if any(re.search(p, stem) for p in patterns):
            return account
    matched = [key for key in configured_accounts() if key.lower() in stem]
    if matched:
        return max(matched, key=len)
    return None


def month_of(name: str) -> tuple[int, int] | None:
    """The (year, month) a filename names, from ``2026-01``, ``202601`` or ``January 2026``."""
    lowered = name.lower()

    iso = re.search(r"(20\d{2})[-_]?(0[1-9]|1[0-2])\b", lowered)
    if iso:
        return int(iso.group(1)), int(iso.group(2))

    year_match = re.search(r"\b(20\d{2})\b", lowered)
    if not year_match:
        return None
    year = int(year_match.group(1))
    for number, full in MONTH_FULL.items():
        if full.lower() in lowered or re.search(rf"\b{MONTH_ABBR[number].lower()}\b", lowered):
            return year, number
    return None


# ── Building ────────────────────────────────────────────────────────────────

def _index_statements(year_dir: Path) -> dict[tuple[int, int], dict[str, Path]]:
    """``{(year, month): {account_key: path}}`` for one year's statement folder."""
    found: dict[tuple[int, int], dict[str, Path]] = {}
    statements = year_dir / "statements"
    if not statements.is_dir():
        return found
    for pdf in sorted(statements.rglob("*.pdf")):
        when = month_of(pdf.stem)
        account = account_of(pdf.stem)
        if when is None or account is None:
            logger.debug("skipping %s: no month and/or account in the filename", pdf.name)
            continue
        found.setdefault(when, {}).setdefault(account, pdf)
    return found


def _receipt_dirs_for(year_dir: Path, year: int, month: int) -> list[Path]:
    """Every folder of receipts that belongs to one month."""
    receipts = year_dir / "receipts"
    if not receipts.is_dir():
        return []
    out = []
    for candidate in sorted(receipts.iterdir()):
        if candidate.is_dir() and month_of(candidate.name) == (year, month):
            out.append(candidate)
    return out


def _xlsx_for(year_dir: Path, year: int, month: int) -> Path | None:
    """The ground-truth workbook for one month, if one is there."""
    exact = year_dir / f"ledger-{year}-{month:02d}.xlsx"
    if exact.is_file():
        return exact
    for candidate in sorted(year_dir.glob("*.xlsx")):
        if candidate.name.startswith("~$"):
            continue  # an open-workbook lock file
        if month_of(candidate.stem) == (year, month):
            return candidate
    return None


def available_years(data_dir: Path | None = None) -> list[int]:
    """The years that have a folder under the data directory."""
    root = Path(data_dir) if data_dir else QA_DATA_DIR
    if not root.is_dir():
        return []
    years = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and re.fullmatch(r"20\d{2}", child.name):
            years.append(int(child.name))
    return years


def build_manifest(
    years: list[int] | None = None,
    data_dir: Path | None = None,
) -> dict[str, MonthData]:
    """Index every month that has at least one file, keyed ``"YYYY-MM"``.

    Returns an empty manifest when the data directory does not exist, which is
    the normal state of a checkout that nobody has pointed at any books.
    """
    root = Path(data_dir) if data_dir else QA_DATA_DIR
    manifest: dict[str, MonthData] = {}

    for year in (years if years is not None else available_years(root)):
        year_dir = root / str(year)
        if not year_dir.is_dir():
            logger.info("no folder for %d under %s", year, root)
            continue

        statements = _index_statements(year_dir)
        for month in range(1, 13):
            md = MonthData(year=year, month=month)
            md.statements = dict(statements.get((year, month), {}))
            md.receipts_dirs = _receipt_dirs_for(year_dir, year, month)
            md.xlsx = _xlsx_for(year_dir, year, month)
            if md.has_bank_pdfs() or md.receipts_dirs or md.xlsx:
                manifest[md.key] = md

    return manifest


def fixture_manifest() -> dict[str, MonthData]:
    """The synthetic corpus in ``tests/fixtures``, indexed the same way.

    One month, with the generated statement PDF standing in for the USD account
    and the generated workbook as ground truth. It is here so the harness has
    something to run against with no data directory configured, and so the shape
    of a manifest is demonstrable without anybody's books.
    """
    statement = FIXTURES_DIR / "test_bank_statement.pdf"
    ledger = FIXTURES_DIR / "ledger-2026-01.xlsx"
    md = MonthData(
        year=2026,
        month=1,
        statements={"USD": statement} if statement.is_file() else {},
        receipts_dirs=[FIXTURES_DIR] if FIXTURES_DIR.is_dir() else [],
        xlsx=ledger if ledger.is_file() else None,
    )
    return {md.key: md}


def manifest_accounts(manifest: dict[str, MonthData]) -> list[str]:
    """Every account key in a manifest, configured ones first, then the rest.

    An account nobody configured still gets a column, because a report that
    silently drops a statement it found is worse than an unexpected column.
    """
    present = {account for md in manifest.values() for account in md.statements}
    ordered = [key for key in configured_accounts() if key in present]
    ordered += sorted(present - set(ordered))
    return ordered


def print_manifest_summary(manifest: dict[str, MonthData] | None = None) -> None:
    """Print a summary table of the manifest: one column per account found."""
    if manifest is None:
        manifest = build_manifest()

    accounts = manifest_accounts(manifest)
    header = f"{'Month':<10} " + "".join(f"{a[:11]:<12}" for a in accounts)
    header += f"{'Receipts':<10} {'XLSX':<6}"
    print(header)
    print("-" * max(len(header), 40))

    total_receipts = 0
    for key in sorted(manifest.keys()):
        md = manifest[key]
        n_receipts = len(md.receipt_files())
        total_receipts += n_receipts
        line = f"{md.label:<10} "
        line += "".join(f"{('YES' if md.statement_for(a) else '---'):<12}" for a in accounts)
        line += f"{n_receipts:<10} {'YES' if md.xlsx else '---':<6}"
        print(line)

    print("-" * max(len(header), 40))
    print(f"Total receipts: {total_receipts}")
    months_with_xlsx = sum(1 for md in manifest.values() if md.xlsx)
    print(f"Months with XLSX ground truth: {months_with_xlsx}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    own = build_manifest()
    if own:
        print(f"Your books, from {QA_DATA_DIR}:\n")
        print_manifest_summary(own)
    else:
        print(f"No books found under {QA_DATA_DIR} — see this module's docstring "
              "for the layout, or set AA_QA_DATA_DIR.\n")
        print("The synthetic fixture corpus that ships with the repository:\n")
        print_manifest_summary(fixture_manifest())
