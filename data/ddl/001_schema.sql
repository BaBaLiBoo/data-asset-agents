CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE dim_branch (
    branch_id BIGINT PRIMARY KEY,
    branch_name VARCHAR(100) NOT NULL UNIQUE,
    region VARCHAR(50) NOT NULL
);

CREATE TABLE dim_customer (
    customer_id BIGINT PRIMARY KEY,
    customer_type VARCHAR(30) NOT NULL,
    home_branch_id BIGINT NOT NULL REFERENCES dim_branch(branch_id)
);

CREATE TABLE dim_account (
    account_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES dim_customer(customer_id),
    account_type VARCHAR(30) NOT NULL,
    branch_id BIGINT NOT NULL REFERENCES dim_branch(branch_id)
);

CREATE TABLE dim_card (
    card_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES dim_account(account_id),
    card_type VARCHAR(20) NOT NULL CHECK (card_type IN ('CREDIT', 'DEBIT'))
);

CREATE TABLE dim_merchant (
    merchant_id BIGINT PRIMARY KEY,
    merchant_name VARCHAR(100) NOT NULL,
    merchant_category VARCHAR(50) NOT NULL
);

CREATE TABLE dwd_card_transaction (
    transaction_id BIGINT PRIMARY KEY,
    card_id BIGINT NOT NULL REFERENCES dim_card(card_id),
    customer_id BIGINT NOT NULL REFERENCES dim_customer(customer_id),
    branch_id BIGINT NOT NULL REFERENCES dim_branch(branch_id),
    merchant_id BIGINT NOT NULL REFERENCES dim_merchant(merchant_id),
    txn_amount NUMERIC(14, 2) NOT NULL,
    txn_amount_cny NUMERIC(14, 2) NOT NULL,
    posted_amount NUMERIC(14, 2),
    card_type VARCHAR(20) NOT NULL,
    transaction_status VARCHAR(20) NOT NULL,
    transaction_channel VARCHAR(20) NOT NULL,
    transaction_date DATE NOT NULL
);
CREATE INDEX idx_card_txn_date ON dwd_card_transaction(transaction_date);
CREATE INDEX idx_card_txn_branch ON dwd_card_transaction(branch_id);

CREATE TABLE dwd_account_transaction (
    transaction_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES dim_account(account_id),
    txn_amount_cny NUMERIC(14, 2) NOT NULL,
    transaction_status VARCHAR(20) NOT NULL,
    transaction_date DATE NOT NULL
);

CREATE TABLE dwd_transfer_transaction (
    transfer_id BIGINT PRIMARY KEY,
    source_account_id BIGINT NOT NULL REFERENCES dim_account(account_id),
    target_account_id BIGINT NOT NULL REFERENCES dim_account(account_id),
    txn_amount_cny NUMERIC(14, 2) NOT NULL,
    transaction_status VARCHAR(20) NOT NULL,
    transaction_date DATE NOT NULL
);

CREATE TABLE dws_branch_transaction_day (
    branch_id BIGINT NOT NULL REFERENCES dim_branch(branch_id),
    card_type VARCHAR(20) NOT NULL,
    transaction_date DATE NOT NULL,
    transaction_amount_cny NUMERIC(16, 2) NOT NULL,
    transaction_count BIGINT NOT NULL,
    PRIMARY KEY (branch_id, card_type, transaction_date)
);

CREATE TABLE dws_customer_transaction_month (
    customer_id BIGINT NOT NULL REFERENCES dim_customer(customer_id),
    transaction_month DATE NOT NULL,
    transaction_amount_cny NUMERIC(16, 2) NOT NULL,
    transaction_count BIGINT NOT NULL,
    PRIMARY KEY (customer_id, transaction_month)
);

-- Deliberate distractor assets. They exist physically but policy forbids selection.
CREATE TABLE legacy_card_transaction (
    transaction_id BIGINT PRIMARY KEY,
    branch_id BIGINT,
    txn_amount NUMERIC(14, 2)
);
CREATE TABLE old_account_transaction (
    transaction_id BIGINT PRIMARY KEY,
    account_id BIGINT,
    posted_amount NUMERIC(14, 2)
);
CREATE TABLE tmp_transaction_result (
    transaction_id BIGINT,
    txn_amount_cny NUMERIC(14, 2)
);
CREATE TABLE test_transaction_copy (
    transaction_id BIGINT,
    txn_amount_cny NUMERIC(14, 2)
);
CREATE TABLE deprecated_branch_summary (
    branch_id BIGINT,
    amount NUMERIC(16, 2),
    count BIGINT
);

-- Runtime semantic repository. YAML remains the reviewed source of truth.
CREATE TABLE semantic_concept (
    concept_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    description TEXT NOT NULL,
    synonyms JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding vector(1024),
    ontology_version VARCHAR(30) NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE certified_sql_asset (
    asset_id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    certified BOOLEAN NOT NULL DEFAULT false,
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

