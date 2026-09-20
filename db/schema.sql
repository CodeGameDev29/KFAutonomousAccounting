-- Autonomous Accounting — the SQLite schema.
--
-- Single-tenant and single-file: no user_id, no row-level security. It backs
-- the local, file-based `Database` in db/database.py, which the gather
-- orchestrator and scripts/batch_import_receipts.py use when they run outside
-- the web application. The web application uses PostgreSQL —
-- db/schema_pg.sql — and the two schemas are not kept column-for-column
-- identical; this one carries only what those callers read and write.

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT    NOT NULL CHECK(account IN ('CAD','USD','Wise','CreditCard')),
    transaction_type TEXT   NOT NULL CHECK(transaction_type IN ('DEBIT','CREDIT')),
    date_posted     TEXT    NOT NULL,  -- ISO date YYYY-MM-DD
    amount          TEXT    NOT NULL,  -- stored as text to preserve decimal precision
    currency        TEXT    NOT NULL CHECK(currency IN ('CAD','USD','CNY','EUR','GBP')),
    description     TEXT    NOT NULL,
    source_file     TEXT    NOT NULL,
    source_row      INTEGER NOT NULL,  -- SQLite INTEGER stores up to 8 bytes (64-bit); no overflow risk here
    status          TEXT    NOT NULL DEFAULT 'UNMATCHED'
                        CHECK(status IN ('UNMATCHED','MATCHED','IGNORED')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    original_filename     TEXT    NOT NULL,
    stored_path           TEXT,
    file_hash             TEXT    NOT NULL,
    vendor                TEXT,
    document_date         TEXT,   -- ISO date YYYY-MM-DD
    currency              TEXT,
    subtotal              TEXT,
    tax_gst               TEXT,
    tax_hst               TEXT,
    tax_pst               TEXT,
    tax_other             TEXT,
    total                 TEXT,
    payment_method        TEXT,
    invoice_number        TEXT,
    line_items_json       TEXT,
    extraction_confidence TEXT    CHECK(extraction_confidence IN ('high','medium','low')),
    extraction_raw_json   TEXT,
    status                TEXT    NOT NULL DEFAULT 'EXTRACTED'
                              CHECK(status IN ('EXTRACTED','MATCHED','FLAGGED')),
    created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconciliation_matches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id   INTEGER NOT NULL REFERENCES transactions(id),
    document_id      INTEGER NOT NULL REFERENCES documents(id),
    confidence_score REAL    NOT NULL,
    amount_score     REAL    NOT NULL,
    date_score       REAL    NOT NULL,
    vendor_score     REAL    NOT NULL,
    match_type       TEXT    NOT NULL DEFAULT 'ONE_TO_ONE'
                         CHECK(match_type IN ('ONE_TO_ONE','MANY_TO_ONE','ONE_TO_MANY')),
    status           TEXT    NOT NULL DEFAULT 'PENDING_REVIEW'
                         CHECK(status IN ('AUTO_APPROVED','PENDING_REVIEW',
                                          'USER_APPROVED','USER_REJECTED')),
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account          TEXT    NOT NULL,
    month            TEXT    NOT NULL,  -- e.g. 'Jan2026'
    transaction_type TEXT    NOT NULL,
    date_posted      TEXT    NOT NULL,
    amount           TEXT    NOT NULL,
    currency         TEXT    NOT NULL,
    description      TEXT    NOT NULL,
    category         TEXT,
    document_link    TEXT,
    note             TEXT,
    match_id         INTEGER REFERENCES reconciliation_matches(id),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendor_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_pattern TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 50,
    source         TEXT    NOT NULL DEFAULT 'manual'
                       CHECK(source IN ('manual','learned')),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash         TEXT    NOT NULL UNIQUE,
    original_filename TEXT    NOT NULL,
    stored_path       TEXT,
    status            TEXT    NOT NULL DEFAULT 'PENDING'
                          CHECK(status IN ('PENDING','PROCESSING','DONE','FAILED')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint       TEXT    NOT NULL,
    model          TEXT,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL    NOT NULL DEFAULT 0.0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for common lookups
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date_posted);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(file_hash);
CREATE INDEX IF NOT EXISTS idx_matches_status ON reconciliation_matches(status);
CREATE INDEX IF NOT EXISTS idx_processed_files_hash ON processed_files(file_hash);

-- Composite uniqueness constraint for transaction dedup.
-- Includes source_row to allow legitimate duplicates (e.g., two PayPal charges
-- for the same amount on the same day with identical descriptions — they have
-- different source_row values within the same file).
-- Excludes source_file so re-uploading the same data from a different file
-- (e.g., PDF export vs CSV export of the same statement) is caught as a dupe.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_dedup
    ON transactions(account, date_posted, amount, description, source_row);

-- ── Gather Phase Tables ─────────────────────────────────────────────

-- Onboarding/gather configuration
CREATE TABLE IF NOT EXISTS gather_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL CHECK(source_type IN (
        'gmail','wise','paypal','manual','email_forwarding','amazon'
    )),
    enabled         INTEGER NOT NULL DEFAULT 1,
    config_json     TEXT,
    last_gathered   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gather_sources_type ON gather_sources(source_type);

-- Tracks every gathered document for dedup + audit
CREATE TABLE IF NOT EXISTS gather_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT,
    filename        TEXT NOT NULL,
    file_hash       TEXT,
    file_size       INTEGER,
    stored_path     TEXT,
    gathered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'GATHERED'
                        CHECK(status IN ('GATHERED','RENAMED','PROCESSED','DUPLICATE','SKIPPED')),
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_gather_source_id ON gather_log(source, source_id);
CREATE INDEX IF NOT EXISTS idx_gather_hash ON gather_log(file_hash);

-- Business profile (onboarding data)
CREATE TABLE IF NOT EXISTS business_profile (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name     TEXT,
    fiscal_year      INTEGER,
    base_currency    TEXT DEFAULT 'CAD',
    tax_jurisdiction TEXT DEFAULT 'CA',
    onboarding_complete INTEGER DEFAULT 0,
    config_json      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
