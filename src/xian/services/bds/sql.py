SCHEMA_VERSION = 2


def _numeric_projection(column: str) -> str:
    return f"""
        CASE
            WHEN jsonb_typeof({column}) = 'number'
                THEN ({column}::text)::numeric
            WHEN jsonb_typeof({column}) = 'string'
                AND trim(both '"' from {column}::text) ~ '^-?[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?$'
                THEN (trim(both '"' from {column}::text))::numeric
            WHEN jsonb_typeof({column}) = 'object'
                AND {column} ? '__fixed__'
                THEN ({column} ->> '__fixed__')::numeric
            WHEN jsonb_typeof({column}) = 'object'
                AND {column} ? '__big_int__'
                THEN ({column} ->> '__big_int__')::numeric
            ELSE NULL
        END
    """.strip()


def drop_all_tables():
    return """
    DROP TABLE IF EXISTS rewards CASCADE;
    DROP TABLE IF EXISTS events CASCADE;
    DROP TABLE IF EXISTS state_patches CASCADE;
    DROP TABLE IF EXISTS contracts CASCADE;
    DROP TABLE IF EXISTS state CASCADE;
    DROP TABLE IF EXISTS state_changes CASCADE;
    DROP TABLE IF EXISTS transactions CASCADE;
    DROP TABLE IF EXISTS blocks CASCADE;
    DROP TABLE IF EXISTS bds_meta CASCADE;
    """


def create_meta():
    return """
    CREATE TABLE IF NOT EXISTS bds_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    );
    """


def select_schema_version():
    return """
    SELECT value
    FROM bds_meta
    WHERE key = 'schema_version';
    """


def upsert_schema_version():
    return """
    INSERT INTO bds_meta(key, value, updated_at)
    VALUES ('schema_version', $1, $2)
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = EXCLUDED.updated_at;
    """


def create_blocks():
    return """
    CREATE TABLE IF NOT EXISTS blocks (
        height BIGINT PRIMARY KEY,
        block_hash TEXT NOT NULL UNIQUE,
        block_time BIGINT NOT NULL,
        block_time_iso TIMESTAMPTZ NOT NULL,
        tx_count INTEGER NOT NULL,
        app_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_blocks_time_iso ON blocks(block_time_iso DESC);
    """


def create_transactions():
    return """
    CREATE TABLE IF NOT EXISTS transactions (
        hash TEXT PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        block_hash TEXT NOT NULL,
        block_time BIGINT NOT NULL,
        tx_index INTEGER NOT NULL,
        sender TEXT NOT NULL,
        nonce BIGINT NOT NULL,
        contract TEXT NOT NULL,
        function TEXT NOT NULL,
        success BOOLEAN NOT NULL,
        status_code INTEGER NOT NULL,
        stamps_used BIGINT NOT NULL,
        result JSONB,
        payload JSONB NOT NULL,
        envelope JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (block_height, tx_index)
    );
    CREATE INDEX IF NOT EXISTS idx_transactions_block_hash ON transactions(block_hash);
    CREATE INDEX IF NOT EXISTS idx_transactions_sender_nonce ON transactions(sender, nonce);
    CREATE INDEX IF NOT EXISTS idx_transactions_contract_function_height ON transactions(contract, function, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_success_height ON transactions(success, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at DESC);
    """


def create_state_changes():
    return f"""
    CREATE TABLE IF NOT EXISTS state_changes (
        change_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        block_hash TEXT NOT NULL,
        block_time BIGINT NOT NULL,
        tx_hash TEXT REFERENCES transactions(hash) ON DELETE CASCADE,
        tx_index INTEGER NOT NULL,
        write_index INTEGER NOT NULL,
        key TEXT NOT NULL,
        new_value JSONB,
        new_value_numeric NUMERIC GENERATED ALWAYS AS ({_numeric_projection("new_value")}) STORED,
        previous_change_id BIGINT,
        previous_tx_hash TEXT,
        origin_type TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (block_height, tx_index, write_index)
    );
    CREATE INDEX IF NOT EXISTS idx_state_changes_key_history
        ON state_changes(key, block_height DESC, tx_index DESC, write_index DESC);
    CREATE INDEX IF NOT EXISTS idx_state_changes_tx_hash ON state_changes(tx_hash, write_index);
    CREATE INDEX IF NOT EXISTS idx_state_changes_block_order ON state_changes(block_height, tx_index, write_index);
    CREATE INDEX IF NOT EXISTS idx_state_changes_previous_change ON state_changes(previous_change_id);
    """


def create_state():
    return f"""
    CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value JSONB,
        value_numeric NUMERIC GENERATED ALWAYS AS ({_numeric_projection("value")}) STORED,
        last_change_id BIGINT NOT NULL,
        last_tx_hash TEXT,
        last_block_height BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_state_value_numeric ON state(value_numeric);
    CREATE INDEX IF NOT EXISTS idx_state_last_block_height ON state(last_block_height DESC);
    """


def create_events():
    return """
    CREATE TABLE IF NOT EXISTS events (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        tx_hash TEXT NOT NULL REFERENCES transactions(hash) ON DELETE CASCADE,
        tx_index INTEGER NOT NULL,
        event_index INTEGER NOT NULL,
        contract TEXT NOT NULL,
        event TEXT NOT NULL,
        signer TEXT NOT NULL,
        caller TEXT NOT NULL,
        data_indexed JSONB NOT NULL,
        data JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (tx_hash, event_index)
    );
    CREATE INDEX IF NOT EXISTS idx_events_contract_event_height ON events(contract, event, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_events_tx_hash ON events(tx_hash);
    CREATE INDEX IF NOT EXISTS idx_events_data_indexed ON events USING GIN (data_indexed);
    """


def create_rewards():
    return """
    CREATE TABLE IF NOT EXISTS rewards (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        tx_hash TEXT REFERENCES transactions(hash) ON DELETE CASCADE,
        tx_index INTEGER NOT NULL,
        reward_index INTEGER NOT NULL,
        type TEXT NOT NULL,
        recipient_key TEXT,
        value NUMERIC NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (block_height, tx_index, reward_index)
    );
    CREATE INDEX IF NOT EXISTS idx_rewards_tx_hash ON rewards(tx_hash, reward_index);
    CREATE INDEX IF NOT EXISTS idx_rewards_block_height ON rewards(block_height DESC);
    """


def create_contracts():
    return """
    CREATE TABLE IF NOT EXISTS contracts (
        name TEXT PRIMARY KEY,
        last_tx_hash TEXT NOT NULL REFERENCES transactions(hash) ON DELETE CASCADE,
        submitted_at_block BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        submitted_at TIMESTAMPTZ NOT NULL,
        code TEXT NOT NULL,
        xsc0001 BOOLEAN NOT NULL DEFAULT FALSE
    );
    CREATE INDEX IF NOT EXISTS idx_contracts_submitted_at_block ON contracts(submitted_at_block DESC);
    """


def create_state_patches():
    return """
    CREATE TABLE IF NOT EXISTS state_patches (
        hash TEXT PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        block_hash TEXT NOT NULL,
        block_time BIGINT NOT NULL,
        patch_count INTEGER NOT NULL,
        patches JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_state_patches_block_height ON state_patches(block_height DESC);
    """


def insert_block():
    return """
    INSERT INTO blocks(
        height, block_hash, block_time, block_time_iso, tx_count, app_hash, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (height) DO NOTHING;
    """


def insert_transaction():
    return """
    INSERT INTO transactions(
        hash, block_height, block_hash, block_time, tx_index, sender, nonce,
        contract, function, success, status_code, stamps_used, result, payload,
        envelope, created_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7,
        $8, $9, $10, $11, $12, $13, $14,
        $15, $16
    )
    ON CONFLICT (hash) DO NOTHING;
    """


def insert_state_change():
    return """
    INSERT INTO state_changes(
        block_height, block_hash, block_time, tx_hash, tx_index, write_index,
        key, new_value, previous_change_id, previous_tx_hash, origin_type,
        created_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6,
        $7, $8, $9, $10, $11,
        $12
    )
    RETURNING change_id;
    """


def upsert_state():
    return """
    INSERT INTO state(
        key, value, last_change_id, last_tx_hash, last_block_height, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        last_change_id = EXCLUDED.last_change_id,
        last_tx_hash = EXCLUDED.last_tx_hash,
        last_block_height = EXCLUDED.last_block_height,
        updated_at = EXCLUDED.updated_at;
    """


def insert_event():
    return """
    INSERT INTO events(
        block_height, tx_hash, tx_index, event_index, contract, event, signer,
        caller, data_indexed, data, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (tx_hash, event_index) DO NOTHING;
    """


def insert_reward():
    return """
    INSERT INTO rewards(
        block_height, tx_hash, tx_index, reward_index, type, recipient_key,
        value, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (block_height, tx_index, reward_index) DO NOTHING;
    """


def upsert_contract():
    return """
    INSERT INTO contracts(
        name, last_tx_hash, submitted_at_block, submitted_at, code, xsc0001
    )
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (name) DO UPDATE SET
        last_tx_hash = EXCLUDED.last_tx_hash,
        submitted_at_block = EXCLUDED.submitted_at_block,
        submitted_at = EXCLUDED.submitted_at,
        code = EXCLUDED.code,
        xsc0001 = EXCLUDED.xsc0001;
    """


def insert_state_patch_record():
    return """
    INSERT INTO state_patches(
        hash, block_height, block_hash, block_time, patch_count, patches, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (hash) DO NOTHING;
    """


def select_contracts():
    return """
    SELECT
        name,
        last_tx_hash,
        submitted_at_block,
        submitted_at,
        code,
        xsc0001
    FROM contracts
    ORDER BY submitted_at_block ASC, name ASC
    LIMIT $1 OFFSET $2;
    """


def select_blocks():
    return """
    SELECT
        height,
        block_hash,
        block_time,
        block_time_iso,
        tx_count,
        app_hash,
        created_at
    FROM blocks
    ORDER BY height DESC
    LIMIT $1 OFFSET $2;
    """


def select_block_by_height():
    return """
    SELECT
        height,
        block_hash,
        block_time,
        block_time_iso,
        tx_count,
        app_hash,
        created_at
    FROM blocks
    WHERE height = $1;
    """


def select_block_by_hash():
    return """
    SELECT
        height,
        block_hash,
        block_time,
        block_time_iso,
        tx_count,
        app_hash,
        created_at
    FROM blocks
    WHERE block_hash = $1;
    """


def select_index_status():
    return """
    SELECT
        (SELECT COUNT(*) FROM blocks) AS indexed_block_count,
        (SELECT height FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_height,
        (SELECT block_hash FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_block_hash,
        (SELECT block_time FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_block_time,
        (SELECT block_time_iso FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_block_time_iso,
        (SELECT tx_count FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_tx_count,
        (SELECT app_hash FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_app_hash;
    """


def select_transaction_by_hash():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        sender,
        nonce,
        contract,
        function,
        success,
        status_code,
        stamps_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE hash = $1;
    """


def select_transactions_for_block_height():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        sender,
        nonce,
        contract,
        function,
        success,
        status_code,
        stamps_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE block_height = $1
    ORDER BY tx_index ASC;
    """


def select_transactions_for_block_hash():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        sender,
        nonce,
        contract,
        function,
        success,
        status_code,
        stamps_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE block_hash = $1
    ORDER BY tx_index ASC;
    """


def select_transactions_by_sender():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        sender,
        nonce,
        contract,
        function,
        success,
        status_code,
        stamps_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE sender = $1
    ORDER BY block_height DESC, tx_index DESC
    LIMIT $2 OFFSET $3;
    """


def select_transactions_by_contract():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        sender,
        nonce,
        contract,
        function,
        success,
        status_code,
        stamps_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE contract = $1
    ORDER BY block_height DESC, tx_index DESC
    LIMIT $2 OFFSET $3;
    """


def select_events_for_tx():
    return """
    SELECT
        id,
        block_height,
        tx_hash,
        tx_index,
        event_index,
        contract,
        event,
        signer,
        caller,
        data_indexed,
        data,
        created_at
    FROM events
    WHERE tx_hash = $1
    ORDER BY event_index ASC;
    """


def select_events_by_contract_event():
    return """
    SELECT
        id,
        block_height,
        tx_hash,
        tx_index,
        event_index,
        contract,
        event,
        signer,
        caller,
        data_indexed,
        data,
        created_at
    FROM events
    WHERE contract = $1 AND event = $2
    ORDER BY block_height DESC, tx_index DESC, event_index DESC
    LIMIT $3 OFFSET $4;
    """


def select_state():
    return """
    SELECT
        key,
        value,
        last_tx_hash,
        last_block_height,
        updated_at
    FROM state
    WHERE key LIKE $1 || '%'
    ORDER BY key ASC
    LIMIT $2 OFFSET $3;
    """


def select_state_history():
    return """
    SELECT
        key,
        new_value AS value,
        tx_hash,
        block_height,
        block_hash,
        block_time,
        tx_index,
        write_index,
        previous_change_id,
        previous_tx_hash,
        origin_type,
        created_at
    FROM state_changes
    WHERE key = $1
    ORDER BY block_height DESC, tx_index DESC, write_index DESC
    LIMIT $2 OFFSET $3;
    """


def select_state_tx():
    return """
    SELECT
        key,
        new_value AS value,
        block_height,
        write_index,
        origin_type,
        created_at
    FROM state_changes
    WHERE tx_hash = $1
    ORDER BY write_index ASC;
    """


def select_state_block_height():
    return """
    SELECT
        key,
        new_value AS value,
        tx_hash,
        tx_index,
        write_index,
        origin_type
    FROM state_changes
    WHERE block_height = $1
    ORDER BY tx_index ASC, write_index ASC;
    """


def select_state_block_hash():
    return """
    SELECT
        key,
        new_value AS value,
        tx_hash,
        tx_index,
        write_index,
        origin_type
    FROM state_changes
    WHERE block_hash = $1
    ORDER BY tx_index ASC, write_index ASC;
    """


def select_state_patches():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        patch_count,
        patches,
        created_at
    FROM state_patches
    ORDER BY block_height DESC, created_at DESC
    LIMIT $1 OFFSET $2;
    """


def select_state_patches_for_block():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        patch_count,
        patches,
        created_at
    FROM state_patches
    WHERE block_height = $1
    ORDER BY created_at ASC;
    """


def select_state_patch_by_hash():
    return """
    SELECT
        hash,
        block_height,
        block_hash,
        block_time,
        patch_count,
        patches,
        created_at
    FROM state_patches
    WHERE hash = $1;
    """


def select_state_changes_for_patch():
    return """
    SELECT
        key,
        new_value AS value,
        block_height,
        write_index,
        created_at
    FROM state_changes
    WHERE tx_hash = $1 AND origin_type = 'state_patch'
    ORDER BY write_index ASC;
    """
