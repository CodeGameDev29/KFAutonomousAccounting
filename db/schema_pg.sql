-- Autonomous Accounting — PostgreSQL baseline schema.
--
-- This is the whole schema in one file: every table, index, constraint,
-- trigger and row-level-security policy the application uses. There is no
-- migration history to replay; `db/migrations/` starts empty and a change
-- made after this baseline is a new numbered file there.
--
-- Apply order (see README.md — it is not interchangeable):
--   1. db/local_auth_schema.sql   creates schema `auth`, auth.users and auth.uid()
--   2. db/schema_pg.sql           this file
--   3. python -m db.migrate       applies anything added after the baseline
--   4. db/local_grants.sql        privileges for the `authenticated` role
--
-- Two conventions hold everywhere below.
--
-- **Tenancy.** Every application table carries `user_id UUID NOT NULL` and one
-- policy, `USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid())`.
-- `db/database_pg.py` also filters by user_id in SQL, but that is belt and
-- braces: it sets the JWT claims as transaction-local settings and runs
-- `SET LOCAL ROLE authenticated`, so the policies are what actually enforce
-- isolation. WITH CHECK is present on every policy, not only USING, because
-- USING alone restricts which rows are *visible* and lets an INSERT or UPDATE
-- write a row owned by somebody else.
--
-- **Money is text.** Amounts are stored as TEXT, not as a float, so a decimal
-- survives the round trip byte for byte. Callers parse to Decimal.
--
-- Idempotent: every statement is guarded, so re-applying the file is a no-op.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- uuid_generate_v4(), used in db/database_pg.py

-- Migration bookkeeping. `db/migrate.py` creates this table too; the baseline
-- creates it so it can record itself, and so a fresh install has an explicit
-- starting point rather than an empty ledger. It is the one table with no
-- row-level security: it holds no user data, and `db/local_grants.sql` revokes
-- it from `authenticated` entirely.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ═══════════════════════════════════════════════════════════════════════
-- Statements and transactions
-- ═══════════════════════════════════════════════════════════════════════

-- One uploaded bank, card or payment-platform statement. The upload endpoint
-- inserts the row and answers 202; the background parser fills in the rest and
-- moves `status` to 'imported' or 'failed'. Everything the parser learns about
-- the document is nullable, because none of it is known when the row appears.
CREATE TABLE IF NOT EXISTS statements (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    source_file             TEXT NOT NULL,
    source_file_hash        TEXT,                  -- SHA-256 of the bytes; the duplicate check
    institution             TEXT,
    account_type            TEXT,
    account_number          TEXT,                  -- last digits, when the statement shows them
    currency                TEXT,
    statement_period_start  DATE,
    statement_period_end    DATE,
    opening_balance         NUMERIC(14,2),
    closing_balance         NUMERIC(14,2),
    -- Set by the import check that adds the parsed rows to the opening balance
    -- and compares the result with the closing balance.
    balance_matches         BOOLEAN,
    balance_difference      NUMERIC(14,2),
    page_count              INTEGER,
    row_count               INTEGER,
    confidence_score        NUMERIC(4,3),          -- mean per-row parser confidence, 0..1
    import_confidence       TEXT CHECK (import_confidence IN ('HIGH','MEDIUM','LOW','LEGACY')),
    import_flags            JSONB DEFAULT '[]'::jsonb,   -- parser warnings for the statement
    parse_method            TEXT CHECK (parse_method IN ('local_text','local_vision','gemini_text','gemini_vision','openai_fallback','legacy_pdf_to_csv')),
    llm_model               TEXT,
    llm_tokens_in           INTEGER DEFAULT 0,
    llm_tokens_out          INTEGER DEFAULT 0,
    llm_cost_usd            NUMERIC(10,6) DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'imported'
                              CHECK (status IN ('processing','imported','failed')),
    status_message          TEXT,                  -- user-facing reason when failed
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT statements_unique_hash UNIQUE (user_id, source_file_hash)
);

CREATE INDEX IF NOT EXISTS idx_statements_user_period
    ON statements (user_id, statement_period_end DESC);
CREATE INDEX IF NOT EXISTS idx_statements_institution
    ON statements (user_id, institution, account_type);
CREATE INDEX IF NOT EXISTS idx_statements_confidence
    ON statements (user_id, import_confidence)
    WHERE import_confidence IN ('MEDIUM','LOW');
-- Feeds the startup sweep that re-queues parses interrupted by a restart.
CREATE INDEX IF NOT EXISTS idx_statements_status_created
    ON statements (status, created_at)
    WHERE status = 'processing';

-- One row of a statement.
CREATE TABLE IF NOT EXISTS transactions (
    id               SERIAL PRIMARY KEY,
    user_id          UUID    NOT NULL,
    -- Free-form: any institution name an importer produces is valid.
    account          TEXT    NOT NULL CHECK (LENGTH(account) > 0),
    -- The authoritative direction. Never infer it from the sign of `amount`:
    -- each parser preserves what its source prints, so the signs differ.
    transaction_type TEXT    NOT NULL CHECK(transaction_type IN ('DEBIT','CREDIT')),
    date_posted      TEXT    NOT NULL,  -- ISO date YYYY-MM-DD
    amount           TEXT    NOT NULL,
    currency         TEXT    NOT NULL CHECK(currency IN ('CAD','USD','CNY','EUR','GBP')),
    description      TEXT    NOT NULL,
    source_file      TEXT    NOT NULL,
    source_row       BIGINT  NOT NULL,
    -- IGNORED = self-evident, needs no receipt; its money STILL COUNTS.
    -- EXCLUDED = row isn't real (rejected at import); its money must NOT count.
    status           TEXT    NOT NULL DEFAULT 'UNMATCHED'
                         CHECK(status IN ('UNMATCHED','MATCHED','IGNORED','LINKED',
                                          'PENDING_REVIEW','EXCLUDED')),
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Institution-agnostic import path.
    institution      TEXT,
    account_type     TEXT,
    external_id      TEXT,                 -- the reference the statement printed, if any
    import_confidence TEXT CHECK(import_confidence IS NULL OR import_confidence IN ('HIGH','MEDIUM','LOW','LEGACY')),
    import_flags     JSONB DEFAULT '[]'::jsonb,
    statement_id     UUID REFERENCES statements(id) ON DELETE SET NULL,
    -- Categorization. `category` holds a canonical name from
    -- config/ised_categories.py, or NULL; the literal 'Uncategorized' is a
    -- display label and is never written here.
    category         TEXT,
    category_source  TEXT,
    category_confidence TEXT,
    category_reasoning  TEXT,
    category_confirmed_by_user BOOLEAN DEFAULT FALSE,
    -- Normalized merchant key, the join into vendor_category_cache.
    vendor_key       TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(user_id, date_posted);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(user_id, account);
CREATE INDEX IF NOT EXISTS idx_transactions_statement_id
    ON transactions (statement_id) WHERE statement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transactions_institution
    ON transactions (user_id, institution, account_type) WHERE institution IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transactions_pending_review
    ON transactions (user_id, status) WHERE status = 'PENDING_REVIEW';
CREATE INDEX IF NOT EXISTS idx_txn_vendor_key ON transactions(user_id, vendor_key);
CREATE INDEX IF NOT EXISTS idx_txn_user_category_date
    ON transactions (user_id, category, date_posted) WHERE category IS NOT NULL;

-- Import dedup. `source_row` is part of the key so two genuinely distinct
-- charges for the same amount on the same day with the same description stay
-- distinct; `source_file` is not, so re-uploading the same statement exported
-- in another format is still caught as a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_dedup
    ON transactions(user_id, account, date_posted, amount, description, source_row);


-- ═══════════════════════════════════════════════════════════════════════
-- Documents, matches and the ledger
-- ═══════════════════════════════════════════════════════════════════════

-- One receipt, invoice or bill, as extracted. Money fields are text for the
-- same reason transactions.amount is.
CREATE TABLE IF NOT EXISTS documents (
    id                    SERIAL PRIMARY KEY,
    user_id               UUID    NOT NULL,
    original_filename     TEXT    NOT NULL,
    stored_path           TEXT,
    file_hash             TEXT    NOT NULL,
    vendor                TEXT,
    document_date         TEXT,   -- ISO date YYYY-MM-DD
    currency              TEXT,
    -- How `currency` was arrived at: 'stated' = the document says so;
    -- 'inferred' = read off its Canadian tax line; 'assumed' = nothing said,
    -- and `currency` is then NULL.
    currency_source       TEXT    CHECK(currency_source IN ('stated','inferred','assumed')),
    subtotal              TEXT,
    -- Taxes are kept apart and never merged into one number: an input tax
    -- credit may be claimed only on GST or HST, so a PST or other-tax amount
    -- must not be mistakable for one.
    tax_gst               TEXT,
    tax_hst               TEXT,
    tax_pst               TEXT,
    tax_other             TEXT,
    total                 TEXT,
    payment_method        TEXT,
    invoice_number        TEXT,
    line_items_json       TEXT,
    extraction_confidence TEXT    CHECK(extraction_confidence IN ('high','medium','low')),
    extraction_confidence_numeric NUMERIC
                              CHECK(extraction_confidence_numeric IS NULL
                                    OR (extraction_confidence_numeric >= 0
                                        AND extraction_confidence_numeric <= 1)),
    extraction_model      TEXT,   -- which model produced the accepted extraction
    extraction_raw_json   TEXT,   -- the raw model output, kept for audit
    status                TEXT    NOT NULL DEFAULT 'EXTRACTED'
                              CHECK(status IN ('EXTRACTED','MATCHED','FLAGGED')),
    field_confidence      TEXT,   -- per-field grades, as JSON text
    review_status         TEXT    DEFAULT 'PENDING',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents(user_id, file_hash);

-- One transaction paired with one document, plus the scores behind it.
CREATE TABLE IF NOT EXISTS reconciliation_matches (
    id               SERIAL PRIMARY KEY,
    user_id          UUID    NOT NULL,
    transaction_id   INTEGER NOT NULL REFERENCES transactions(id),
    document_id      INTEGER NOT NULL REFERENCES documents(id),
    confidence_score REAL    NOT NULL,
    amount_score     REAL    NOT NULL,
    date_score       REAL    NOT NULL,
    vendor_score     REAL    NOT NULL,
    match_type       TEXT    NOT NULL DEFAULT 'ONE_TO_ONE'
                         CHECK(match_type IN ('ONE_TO_ONE','MANY_TO_ONE','ONE_TO_MANY')),
    -- An uncertain pair is PENDING_REVIEW, never guessed into an approval.
    status           TEXT    NOT NULL DEFAULT 'PENDING_REVIEW'
                         CHECK(status IN ('AUTO_APPROVED','PENDING_REVIEW',
                                          'USER_APPROVED','USER_REJECTED')),
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    user_action      TEXT,                 -- 'approved' | 'rejected' | 'unmatched'
    actioned_at      TIMESTAMPTZ,
    match_source     TEXT    NOT NULL DEFAULT 'AUTO'
                         CHECK(match_source IN ('AUTO', 'MANUAL')),
    explanation      TEXT,                 -- plain-language reason, shown to the user
    -- Groups the several proofs that together cover one transaction.
    group_id         UUID    NOT NULL DEFAULT uuid_generate_v4(),
    covered_amount   TEXT,                 -- NULL = this proof covers the whole amount
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_matches_user ON reconciliation_matches(user_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON reconciliation_matches(user_id, status);
CREATE INDEX IF NOT EXISTS idx_matches_txn_status
    ON reconciliation_matches(user_id, transaction_id, status);
CREATE INDEX IF NOT EXISTS idx_matches_group_id
    ON reconciliation_matches(user_id, group_id);

-- Re-running reconciliation must not be able to pair the same transaction and
-- document twice, whichever code path inserts the row. A rejected pair is
-- outside the index so the same pair can be proposed again later.
CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_dedup
    ON reconciliation_matches(user_id, transaction_id, document_id)
    WHERE status != 'USER_REJECTED';

-- A document that fully proves one transaction cannot also fully prove
-- another. Partial proofs (MANY_TO_ONE / ONE_TO_MANY) are outside the index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_doc_one_to_one
    ON reconciliation_matches(user_id, document_id)
    WHERE status NOT IN ('USER_REJECTED') AND match_type = 'ONE_TO_ONE';

-- A flattened, exportable row: a transaction plus, when one exists, the match
-- that substantiates it. It has no status of its own.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id               SERIAL PRIMARY KEY,
    user_id          UUID    NOT NULL,
    account          TEXT    NOT NULL,
    month            TEXT    NOT NULL,  -- e.g. 'Jan2026'
    transaction_type TEXT    NOT NULL,
    date_posted      TEXT    NOT NULL,
    amount           TEXT    NOT NULL,
    currency         TEXT    NOT NULL,
    description      TEXT    NOT NULL,
    category         TEXT,
    document_link    TEXT,               -- path to the proof file
    note             TEXT,
    match_id         INTEGER REFERENCES reconciliation_matches(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_user ON ledger_entries(user_id);

-- Supplementary evidence attached to a transaction's note — a PDF or a
-- screenshot that explains a row without claiming a slot in
-- reconciliation_matches.
CREATE TABLE IF NOT EXISTS note_attachments (
    id              SERIAL PRIMARY KEY,
    user_id         UUID    NOT NULL,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    stored_path     TEXT    NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    file_hash       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_note_attachments_txn
    ON note_attachments(user_id, transaction_id, created_at DESC);


-- ═══════════════════════════════════════════════════════════════════════
-- Cross-statement transaction links
-- ═══════════════════════════════════════════════════════════════════════

-- Two rows that are the same movement of money seen from two accounts: a card
-- payment, an internal transfer, an FX conversion, a refund. A link is proof
-- of the same kind a receipt is, which is why a linked transaction is not
-- reported as missing a document.
CREATE TABLE IF NOT EXISTS transaction_links (
    id                    SERIAL PRIMARY KEY,
    user_id               UUID    NOT NULL,
    source_transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    target_transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    link_type             TEXT    NOT NULL CHECK(link_type IN (
        'CREDIT_CARD_PAYMENT','INTERNAL_TRANSFER','FX_CONVERSION',
        'WISE_TRANSFER','REFUND','OTHER'
    )),
    -- A link group is the set of rows sharing one chain_id: one anchor
    -- transaction fanned out to N counterparts. That is what makes a split
    -- refund representable — one charge against several separate credits.
    chain_id              UUID    NOT NULL DEFAULT uuid_generate_v4(),
    chain_position        INTEGER NOT NULL DEFAULT 0,
    amount_variance       TEXT,       -- e.g. "2.35 CAD wire fee"
    exchange_rate         TEXT,       -- e.g. "1.3500" for CAD/USD
    explanation           TEXT,
    match_source          TEXT    NOT NULL DEFAULT 'AUTO'
                              CHECK(match_source IN ('AUTO','MANUAL')),
    confidence_score      REAL,
    status                TEXT    NOT NULL DEFAULT 'PENDING_REVIEW'
                              CHECK(status IN (
                                  'AUTO_APPROVED','PENDING_REVIEW',
                                  'USER_APPROVED','USER_REJECTED'
                              )),
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_no_self_link
        CHECK (source_transaction_id != target_transaction_id)
);

-- One active link per PAIR, in either direction, so a fan-out group can never
-- duplicate one of its legs. Same-account pairs are allowed: a refund and the
-- charge it reverses land in the same account. The automated linkers require
-- cross-account pairing in code; a manual link does not.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_links_dedup
    ON transaction_links(
        user_id,
        LEAST(source_transaction_id, target_transaction_id),
        GREATEST(source_transaction_id, target_transaction_id)
    )
    WHERE status != 'USER_REJECTED';

CREATE INDEX IF NOT EXISTS idx_transaction_links_source
    ON transaction_links(user_id, source_transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_links_target
    ON transaction_links(user_id, target_transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_links_chain
    ON transaction_links(user_id, chain_id);
CREATE INDEX IF NOT EXISTS idx_transaction_links_chain_status
    ON transaction_links(user_id, chain_id)
    WHERE status != 'USER_REJECTED';
CREATE INDEX IF NOT EXISTS idx_transaction_links_status
    ON transaction_links(user_id, status);

-- Refuse a link that would close a cycle.
--
-- The walk is undirected, because a link is symmetric. It therefore has to
-- carry the path it has taken in `visited` and refuse to step onto a
-- transaction already on it: without that, a fan-out group makes the walk
-- bounce anchor -> leg -> anchor and re-branch once per leg on every return,
-- which is exponential in the group size and hangs the INSERT. Cycle detection
-- is unaffected, because a real cycle reaches NEW.source before any node
-- repeats.
CREATE OR REPLACE FUNCTION fn_prevent_link_cycle()
RETURNS TRIGGER AS $$
DECLARE
    cycle_found BOOLEAN;
BEGIN
    WITH RECURSIVE link_walk AS (
        SELECT NEW.target_transaction_id AS txn_id,
               1 AS depth,
               ARRAY[NEW.target_transaction_id] AS visited
        UNION ALL
        SELECT
            CASE WHEN tl.source_transaction_id = lw.txn_id
                 THEN tl.target_transaction_id
                 ELSE tl.source_transaction_id END AS txn_id,
            lw.depth + 1,
            lw.visited || CASE WHEN tl.source_transaction_id = lw.txn_id
                 THEN tl.target_transaction_id
                 ELSE tl.source_transaction_id END
        FROM transaction_links tl
        JOIN link_walk lw ON (
            tl.source_transaction_id = lw.txn_id
            OR tl.target_transaction_id = lw.txn_id
        )
        WHERE tl.user_id = NEW.user_id
          AND tl.status != 'USER_REJECTED'
          AND tl.id != NEW.id AND lw.depth < 20
          AND NOT (CASE WHEN tl.source_transaction_id = lw.txn_id
                        THEN tl.target_transaction_id
                        ELSE tl.source_transaction_id END = ANY(lw.visited))
    )
    SELECT EXISTS (
        SELECT 1 FROM link_walk WHERE txn_id = NEW.source_transaction_id
    ) INTO cycle_found;
    IF cycle_found THEN
        RAISE EXCEPTION 'Transaction link would create a cycle';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'public.transaction_links'::regclass
          AND tgname = 'trg_prevent_link_cycle'
    ) THEN
        CREATE TRIGGER trg_prevent_link_cycle
            BEFORE INSERT OR UPDATE ON transaction_links
            FOR EACH ROW EXECUTE FUNCTION fn_prevent_link_cycle();
    END IF;
END $$;


-- ═══════════════════════════════════════════════════════════════════════
-- The processing queue and its record of each attempt
-- ═══════════════════════════════════════════════════════════════════════

-- Every uploaded document is processed here, not in the request that carried
-- it: the upload endpoint validates, stores the file, inserts a row and
-- answers 202, and one worker in the server process drains the table in
-- arrival order. A restart re-queues whatever was `running` instead of losing
-- it, which is why the durable copy of the file is recorded on the row.
CREATE TABLE IF NOT EXISTS processing_jobs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('receipt', 'statement')),
    filename         TEXT NOT NULL,
    stored_path      TEXT,                  -- durable copy under STORAGE_ROOT
    file_hash        TEXT,
    -- 'waiting_for_model' is not a failure: the LLM endpoint was unreachable
    -- and the job backs off and retries rather than burning an attempt.
    status           TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'waiting_for_model',
                                         'done', 'failed', 'cancelled')),
    position         BIGINT NOT NULL DEFAULT 0,   -- monotonic per user, so the queue is FIFO
    attempts         INTEGER NOT NULL DEFAULT 0,  -- only hard failures count
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    transient_waits  INTEGER NOT NULL DEFAULT 0,  -- steps taken up the backoff ladder
    next_attempt_at  TIMESTAMPTZ,
    pages_total      INTEGER,
    pages_done       INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT,
    error_message    TEXT,
    result           JSONB,                 -- document_id / statement_id + summary
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_processing_jobs_user_status_position
    ON processing_jobs (user_id, status, position);
-- The claim query, which is not user-scoped: the worker takes the oldest
-- runnable job in the whole queue.
CREATE INDEX IF NOT EXISTS idx_processing_jobs_claim
    ON processing_jobs (created_at, position)
    WHERE status IN ('queued', 'waiting_for_model');
CREATE INDEX IF NOT EXISTS idx_processing_jobs_finished
    ON processing_jobs (user_id, kind, finished_at DESC)
    WHERE status = 'done';

-- One row per extraction attempt, including the ones that failed or fell back
-- to another model. The documents row keeps only the accepted result, so this
-- is where a silent failover stays visible.
CREATE TABLE IF NOT EXISTS extraction_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    model         TEXT NOT NULL,
    confidence    NUMERIC NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    latency_ms    INTEGER NOT NULL CHECK (latency_ms >= 0),
    tokens_in     INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out    INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
    cost_usd      NUMERIC CHECK (cost_usd IS NULL OR cost_usd >= 0),
    fallback_from TEXT,    -- the model that was tried first, when this was a failover
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_extraction_log_user ON extraction_log (user_id);
CREATE INDEX IF NOT EXISTS idx_extraction_log_receipt ON extraction_log (receipt_id);
CREATE INDEX IF NOT EXISTS idx_extraction_log_created ON extraction_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_log_model_created
    ON extraction_log (model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_log_fallback_created
    ON extraction_log (fallback_from, created_at DESC) WHERE fallback_from IS NOT NULL;

-- What an LLM adjudicator decided about one question, keyed on a hash of the
-- inputs. The same question asked twice in a run is answered from here instead
-- of being put to the model again, so a re-run of a month is deterministic and
-- costs nothing the first run already paid.
CREATE TABLE IF NOT EXISTS reconciliation_adjudications (
    id             SERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    agent          TEXT NOT NULL CHECK (agent IN ('amount','date','final')),
    inputs_hash    TEXT NOT NULL,
    verdict        JSONB NOT NULL,
    confidence     REAL,
    reasoning      TEXT,
    model          TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_adjudications_question
    ON reconciliation_adjudications (user_id, document_id, agent, inputs_hash);
CREATE INDEX IF NOT EXISTS idx_adjudications_user
    ON reconciliation_adjudications (user_id, used_at DESC);


-- ═══════════════════════════════════════════════════════════════════════
-- Categorization
-- ═══════════════════════════════════════════════════════════════════════

-- The category list as stored per user. It is seeded from the fixed taxonomy
-- in config/ised_categories.py the first time an account needs it
-- (DatabasePg.seed_fixed_taxonomy) — there is no seed data in this file, so a
-- new install starts with no rows at all.
CREATE TABLE IF NOT EXISTS user_categories (
    id              SERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    name            TEXT NOT NULL,
    definition      TEXT,       -- injected into the classifier prompt
    umbrella_for    TEXT,
    example_vendors TEXT,
    -- income|cogs|cogs_contra|expense|transfer|equity|tax|contra. Deliberately
    -- unconstrained here: config/ised_categories.py is the source of truth and
    -- code never reads `kind` back out of this column to make a money decision.
    kind            TEXT,
    is_system       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_user_categories_user ON user_categories(user_id);

-- One decision per normalized merchant, so a vendor seen again is categorized
-- without another model call.
CREATE TABLE IF NOT EXISTS vendor_category_cache (
    id          SERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    vendor_key  TEXT NOT NULL,
    category_id INTEGER REFERENCES user_categories(id) ON DELETE SET NULL,
    confidence  TEXT,
    reasoning   TEXT,
    sample_desc TEXT,    -- one description this key was derived from, for display
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, vendor_key)
);

CREATE INDEX IF NOT EXISTS idx_vendor_cache_user ON vendor_category_cache(user_id);

-- One categorization run, for the progress display and for the record of what
-- the last run did.
CREATE TABLE IF NOT EXISTS categorization_run (
    id           SERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    scope        TEXT NOT NULL,
    total        INTEGER,
    categorized  INTEGER,
    rule_locked  INTEGER,   -- decided by a deterministic rule, with no model call
    llm_calls    INTEGER,
    status       TEXT,
    error        TEXT,
    summary_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_cat_run_user ON categorization_run(user_id, started_at DESC);

-- User-written pattern -> category rules, applied before the classifier.
CREATE TABLE IF NOT EXISTS vendor_rules (
    id             SERIAL PRIMARY KEY,
    user_id        UUID    NOT NULL,
    vendor_pattern TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 50,
    source         TEXT    NOT NULL DEFAULT 'manual'
                       CHECK(source IN ('manual','learned')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ═══════════════════════════════════════════════════════════════════════
-- Profile, periods and the audit trail
-- ═══════════════════════════════════════════════════════════════════════

-- One row per user: what the business is, which is what the classifier and the
-- peer-benchmark engine need in order to judge a transaction.
CREATE TABLE IF NOT EXISTS business_profile (
    id               SERIAL PRIMARY KEY,
    user_id          UUID NOT NULL,
    company_name     TEXT,
    fiscal_year      INTEGER,
    base_currency    TEXT DEFAULT 'CAD',
    tax_jurisdiction TEXT DEFAULT 'CA',
    province         TEXT,
    business_number  TEXT,
    onboarding_complete INTEGER DEFAULT 0,
    setup_checklist_state TEXT,
    config_json      TEXT,
    onboarding_state_json TEXT,
    industry         TEXT,
    business_summary TEXT,
    naics_code       TEXT,       -- selects the peer-benchmark row
    -- Per-user opt-in/opt-out for features that are gated (server/deps.py).
    feature_flags    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_business_profile_user
    ON business_profile(user_id);
CREATE INDEX IF NOT EXISTS idx_business_profile_feature_flags
    ON business_profile USING GIN (feature_flags);

-- Whether a calendar month has been closed. A closed month is read-only.
CREATE TABLE IF NOT EXISTS month_status (
    id          SERIAL PRIMARY KEY,
    user_id     UUID        NOT NULL,
    year        INTEGER     NOT NULL CHECK (year >= 2020 AND year <= 2099),
    month       INTEGER     NOT NULL CHECK (month >= 1 AND month <= 12),
    status      TEXT        NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN', 'CLOSED')),
    closed_at   TIMESTAMPTZ,
    closed_by   TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_month_status_user_period
    ON month_status(user_id, year, month);
CREATE INDEX IF NOT EXISTS idx_month_status_year
    ON month_status(user_id, year);

-- Who changed what, and when. The enums are closed on purpose: an action or an
-- entity type that is not listed is a bug, not a new kind of record, and the
-- constraint is what surfaces it.
CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    entity_type  TEXT NOT NULL CHECK(entity_type IN (
        'match','document','transaction','onboarding','connection',
        'transaction_link','export','report','settings','session',
        'system','proof_bundle'
    )),
    entity_id    INTEGER NOT NULL,
    action       TEXT NOT NULL CHECK(action IN (
        'APPROVE','REJECT','UNMATCH','CORRECT','SKIP_STEP','RECONNECT',
        'AUTO_APPROVE','RUN_RECONCILIATION','MANUAL_MATCH','REASSIGN',
        'LINK','UNLINK','UPLOAD_RECEIPT','UPLOAD_STATEMENT',
        'REMOVE_PROOF','ADD_PROOF',
        'GROUP_APPROVE','GROUP_REJECT','RESET_ALL',
        'EXPORT_PDF','EXPORT_ZIP','EXPORT_CSV',
        'SHARE_LINK_CREATE','SHARE_LINK_REVOKE',
        'SETTINGS_UPDATE','LOGIN',
        'DELETE_RECEIPT',
        'RESET_TO_REVIEW',
        'RECATEGORIZE',
        'EDIT_NOTE',
        'DELETE_STATEMENT'
    )),
    old_value    TEXT,
    new_value    TEXT,
    performed_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(user_id, entity_type, entity_id);

-- Progress through the first-run checklist.
CREATE TABLE IF NOT EXISTS onboarding_steps (
    id           SERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    step         TEXT NOT NULL CHECK(step IN (
        'profile','bank_connect','data_sources','receipt_upload',
        'first_upload','first_reconciliation','export'
    )),
    status       TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK(status IN ('PENDING','COMPLETED','SKIPPED')),
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_onboarding_steps_user_step
    ON onboarding_steps(user_id, step);

-- Files already ingested, so the same upload is not processed twice.
CREATE TABLE IF NOT EXISTS processed_files (
    id                 SERIAL PRIMARY KEY,
    user_id            UUID    NOT NULL,
    file_hash          TEXT    NOT NULL,
    original_filename  TEXT    NOT NULL,
    stored_path        TEXT,
    status             TEXT    NOT NULL DEFAULT 'PENDING'
                           CHECK(status IN ('PENDING','PROCESSING','DONE','FAILED')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_processed_files_user ON processed_files(user_id);
CREATE INDEX IF NOT EXISTS idx_processed_files_hash ON processed_files(user_id, file_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_files_dedup
    ON processed_files(user_id, file_hash);

-- Token count and estimated cost per LLM call, so the load the engine puts on
-- an inference endpoint is visible.
CREATE TABLE IF NOT EXISTS api_calls (
    id             SERIAL PRIMARY KEY,
    user_id        UUID    NOT NULL,
    endpoint       TEXT    NOT NULL,
    model          TEXT,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL    NOT NULL DEFAULT 0.0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_calls_user_date ON api_calls(user_id, created_at);

-- Peer-benchmark flags the user has dismissed, so a judgement they have
-- already answered is not raised again.
CREATE TABLE IF NOT EXISTS reasonableness_dismissed_flags (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    flag_key       TEXT NOT NULL,
    note           TEXT,
    naics_code     TEXT,      -- the benchmark row the flag was raised against
    dismissed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reasonableness_dismissed_flags_user_flag
    ON reasonableness_dismissed_flags (user_id, flag_key);


-- ═══════════════════════════════════════════════════════════════════════
-- Document sources and sharing
-- ═══════════════════════════════════════════════════════════════════════

-- One configuration row per source a user can pull documents from. The
-- permitted values are core/integrations.py's ALL_SOURCE_TYPES.
CREATE TABLE IF NOT EXISTS gather_sources (
    id              SERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    source_type     TEXT NOT NULL CHECK(source_type IN (
        'gmail','wise','paypal','manual','email_forwarding','amazon'
    )),
    enabled         INTEGER NOT NULL DEFAULT 1,
    config_json     TEXT,
    account_email   TEXT,          -- which account the source is connected as
    last_gathered   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gather_sources_type
    ON gather_sources(user_id, source_type);

-- Every document a source produced, for dedup and for the audit trail.
CREATE TABLE IF NOT EXISTS gather_log (
    id              SERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    source          TEXT NOT NULL,
    source_id       TEXT,          -- the source's own id for the item
    filename        TEXT NOT NULL,
    file_hash       TEXT,
    file_size       INTEGER,
    stored_path     TEXT,
    gathered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL DEFAULT 'GATHERED'
                        CHECK(status IN ('GATHERED','RENAMED','PROCESSED','DUPLICATE','SKIPPED')),
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_gather_log_user ON gather_log(user_id);
CREATE INDEX IF NOT EXISTS idx_gather_source_id ON gather_log(user_id, source, source_id);
CREATE INDEX IF NOT EXISTS idx_gather_hash ON gather_log(user_id, file_hash);

-- One mailbox scan: how far it got, and whether it is still running.
CREATE TABLE IF NOT EXISTS scan_runs (
    id                SERIAL PRIMARY KEY,
    user_id           UUID NOT NULL,
    source            TEXT NOT NULL DEFAULT 'gmail',
    status            TEXT NOT NULL DEFAULT 'RUNNING'
                          CHECK (status IN ('RUNNING','COMPLETED','FAILED','CANCELLED')),
    since_date        TIMESTAMPTZ,
    until_date        TIMESTAMPTZ,
    emails_scanned    INTEGER DEFAULT 0,
    documents_found   INTEGER DEFAULT 0,
    documents_added   INTEGER DEFAULT 0,
    documents_skipped INTEGER DEFAULT 0,
    error_message     TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_user ON scan_runs(user_id, created_at DESC);

-- The one-use `state` value of an in-flight Gmail OAuth exchange, checked when
-- the provider redirects back.
CREATE TABLE IF NOT EXISTS gmail_oauth_states (
    id         SERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    state      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_oauth_states_state ON gmail_oauth_states(state);

-- Whether a user currently grants Gmail access, one row per user. Kept
-- separately from the stored credential so the grant can be recorded and
-- revoked without reading or writing anyone's tokens.
CREATE TABLE IF NOT EXISTS gmail_oauth_consents (
    id         SERIAL PRIMARY KEY,
    user_id    UUID NOT NULL UNIQUE,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gmail_consents_active
    ON gmail_oauth_consents(user_id) WHERE revoked_at IS NULL;

-- The health of each connected source, as of its last check.
CREATE TABLE IF NOT EXISTS connection_health (
    id            SERIAL PRIMARY KEY,
    user_id       UUID NOT NULL,
    source        TEXT NOT NULL CHECK(source IN (
        'gmail','wise','paypal','email_forwarding','amazon'
    )),
    status        TEXT NOT NULL DEFAULT 'UNKNOWN'
                      CHECK(status IN ('HEALTHY','DEGRADED','DISCONNECTED','UNKNOWN')),
    last_checked  TIMESTAMPTZ,
    last_success  TIMESTAMPTZ,
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_health_source
    ON connection_health(user_id, source);

-- An expiring, unguessable link to one month's report. The token is the only
-- credential the reader has, so it is unique and the row carries its expiry.
CREATE TABLE IF NOT EXISTS shared_reports (
    id          SERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shared_reports_token ON shared_reports(token);
CREATE INDEX IF NOT EXISTS idx_shared_reports_user ON shared_reports(user_id);


-- ═══════════════════════════════════════════════════════════════════════
-- Row-level security
-- ═══════════════════════════════════════════════════════════════════════
--
-- One policy per table, identical in shape: a row is reachable, and writable,
-- only by the user whose id it carries. Both halves are needed — USING decides
-- which rows are visible to SELECT, UPDATE and DELETE, WITH CHECK decides
-- which rows INSERT and UPDATE are allowed to leave behind. A policy with only
-- USING lets a caller insert a row owned by somebody else.
--
-- `authenticated` holds no BYPASSRLS, so these are unconditional for it. The
-- pool connects as the cluster owner, which does bypass them; that is why
-- db/database_pg.py switches role for every transaction and refuses to run a
-- query if the switch did not take effect.

DO $$
DECLARE t text;
DECLARE p text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'statements', 'transactions', 'documents', 'reconciliation_matches',
        'ledger_entries', 'note_attachments', 'transaction_links',
        'processing_jobs', 'extraction_log', 'reconciliation_adjudications',
        'user_categories', 'vendor_category_cache', 'categorization_run',
        'vendor_rules', 'business_profile', 'month_status', 'audit_log',
        'onboarding_steps', 'processed_files',
        'api_calls', 'reasonableness_dismissed_flags', 'gather_sources',
        'gather_log', 'scan_runs', 'gmail_oauth_states', 'gmail_oauth_consents',
        'connection_health', 'shared_reports'
    ] LOOP
        p := t || '_user_policy';
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        -- Checked rather than DROP … IF EXISTS, which is idempotent but emits
        -- a notice per table on a first install.
        IF EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t AND policyname = p
        ) THEN
            EXECUTE format('DROP POLICY %I ON public.%I', p, t);
        END IF;
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL '
            'USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid())',
            p, t
        );
    END LOOP;
END $$;

-- No views are defined here, deliberately. A view runs with the privileges and
-- the row-level-security context of its OWNER unless it is created
-- `WITH (security_invoker = true)`, so a view over these tables that omits the
-- option hands every tenant's rows to any caller that can select from it.
-- db/database_pg.py assembles the equivalent result from the base tables, where
-- the policies apply directly. Any view added later must set security_invoker;
-- tests/test_rls_enforcement.py enumerates the catalogue and fails if one does
-- not.

-- Record the baseline, so `python -m db.migrate` on a fresh install reports a
-- schema that is up to date rather than an empty ledger.
INSERT INTO schema_migrations (filename)
VALUES ('000_baseline_schema_pg.sql')
ON CONFLICT (filename) DO NOTHING;
