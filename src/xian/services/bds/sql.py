SCHEMA_VERSION = 7


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
    DROP TABLE IF EXISTS shielded_output_tags CASCADE;
    DROP TABLE IF EXISTS shielded_outputs CASCADE;
    DROP TABLE IF EXISTS rewards CASCADE;
    DROP TABLE IF EXISTS events CASCADE;
    DROP TABLE IF EXISTS state_patches CASCADE;
    DROP TABLE IF EXISTS contracts CASCADE;
    DROP TABLE IF EXISTS state CASCADE;
    DROP TABLE IF EXISTS state_changes CASCADE;
    DROP TABLE IF EXISTS addresses CASCADE;
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
        chi_used BIGINT NOT NULL,
        result JSONB,
        payload JSONB NOT NULL,
        envelope JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (block_height, tx_index)
    );
    CREATE INDEX IF NOT EXISTS idx_transactions_block_hash ON transactions(block_hash);
    CREATE INDEX IF NOT EXISTS idx_transactions_sender_nonce ON transactions(sender, nonce);
    CREATE INDEX IF NOT EXISTS idx_transactions_sender_height_index
        ON transactions(sender, block_height DESC, tx_index DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_contract_height_index
        ON transactions(contract, block_height DESC, tx_index DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_contract_function_height ON transactions(contract, function, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_success_height ON transactions(success, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at DESC);
    """


def create_addresses():
    return """
    CREATE TABLE IF NOT EXISTS addresses (
        address TEXT PRIMARY KEY,
        tx_count BIGINT NOT NULL DEFAULT 0,
        first_block_height BIGINT NOT NULL,
        first_seen TIMESTAMPTZ NOT NULL,
        last_block_height BIGINT NOT NULL,
        last_tx_index INTEGER NOT NULL,
        last_seen TIMESTAMPTZ NOT NULL,
        last_tx_hash TEXT REFERENCES transactions(hash) ON DELETE SET NULL,
        last_contract TEXT NOT NULL,
        last_function TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_addresses_last_activity
        ON addresses(last_block_height DESC, last_tx_index DESC, address ASC);
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
    CREATE INDEX IF NOT EXISTS idx_state_key_prefix ON state(key text_pattern_ops);
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
    CREATE INDEX IF NOT EXISTS idx_events_contract_event_order
        ON events(contract, event, block_height DESC, tx_index DESC, event_index DESC);
    CREATE INDEX IF NOT EXISTS idx_events_contract_event_id
        ON events(contract, event, id);
    CREATE INDEX IF NOT EXISTS idx_events_event_id
        ON events(event, id);
    CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_events_created_id ON events(created_at DESC, id DESC);
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
        source_contract TEXT,
        value NUMERIC NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (block_height, tx_index, reward_index)
    );
    CREATE INDEX IF NOT EXISTS idx_rewards_tx_hash ON rewards(tx_hash, reward_index);
    CREATE INDEX IF NOT EXISTS idx_rewards_block_height ON rewards(block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_rewards_type_recipient_height
        ON rewards(type, recipient_key, block_height DESC);
    CREATE INDEX IF NOT EXISTS idx_rewards_type_source_contract_height
        ON rewards(type, source_contract, block_height DESC);
    """


def create_shielded_outputs():
    return """
    CREATE TABLE IF NOT EXISTS shielded_outputs (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        tx_hash TEXT NOT NULL REFERENCES transactions(hash) ON DELETE CASCADE,
        tx_index INTEGER NOT NULL,
        contract TEXT NOT NULL,
        function TEXT NOT NULL,
        action TEXT NOT NULL,
        output_index INTEGER NOT NULL,
        note_index INTEGER,
        commitment TEXT NOT NULL,
        new_root TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (tx_hash, output_index)
    );
    CREATE INDEX IF NOT EXISTS idx_shielded_outputs_note_index
        ON shielded_outputs(note_index ASC, id ASC)
        WHERE note_index IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_shielded_outputs_tx_hash
        ON shielded_outputs(tx_hash, output_index);
    CREATE INDEX IF NOT EXISTS idx_shielded_outputs_commitment
        ON shielded_outputs(commitment);
    """


def create_shielded_output_tags():
    return """
    CREATE TABLE IF NOT EXISTS shielded_output_tags (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        block_height BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        tx_hash TEXT NOT NULL REFERENCES transactions(hash) ON DELETE CASCADE,
        tx_index INTEGER NOT NULL,
        contract TEXT NOT NULL,
        function TEXT NOT NULL,
        action TEXT NOT NULL,
        output_index INTEGER NOT NULL,
        note_index INTEGER,
        commitment TEXT NOT NULL,
        new_root TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        tag_kind TEXT NOT NULL,
        tag_value TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (tx_hash, output_index, tag_kind, tag_value)
    );
    CREATE INDEX IF NOT EXISTS idx_shielded_output_tags_kind_value_height
        ON shielded_output_tags(tag_kind, tag_value, block_height DESC, tx_index DESC, output_index DESC);
    CREATE INDEX IF NOT EXISTS idx_shielded_output_tags_kind_value_id
        ON shielded_output_tags(tag_kind, tag_value, id);
    CREATE INDEX IF NOT EXISTS idx_shielded_output_tags_kind_value_note
        ON shielded_output_tags(tag_kind, tag_value, note_index ASC, id ASC)
        WHERE note_index IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_shielded_output_tags_tx_hash
        ON shielded_output_tags(tx_hash, output_index);
    CREATE INDEX IF NOT EXISTS idx_shielded_output_tags_commitment
        ON shielded_output_tags(commitment);
    """


def create_contracts():
    return """
    CREATE TABLE IF NOT EXISTS contracts (
        name TEXT PRIMARY KEY,
        last_tx_hash TEXT NOT NULL REFERENCES transactions(hash) ON DELETE CASCADE,
        submitted_at_block BIGINT NOT NULL REFERENCES blocks(height) ON DELETE CASCADE,
        submitted_at TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        xsc001 BOOLEAN NOT NULL DEFAULT FALSE
    );
    CREATE INDEX IF NOT EXISTS idx_contracts_submitted_at_block ON contracts(submitted_at_block DESC);
    CREATE INDEX IF NOT EXISTS idx_contracts_xsc001_submitted_at_block
        ON contracts(submitted_at_block DESC, name ASC)
        WHERE xsc001 = TRUE;
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
        contract, function, success, status_code, chi_used, result, payload,
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
        source_contract, value, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (block_height, tx_index, reward_index) DO NOTHING;
    """


def upsert_address_activity():
    return """
    INSERT INTO addresses(
        address,
        tx_count,
        first_block_height,
        first_seen,
        last_block_height,
        last_tx_index,
        last_seen,
        last_tx_hash,
        last_contract,
        last_function,
        updated_at
    )
    VALUES ($1, 1, $2, $3, $2, $4, $3, $5, $6, $7, $8)
    ON CONFLICT (address) DO UPDATE SET
        tx_count = addresses.tx_count + 1,
        first_block_height = LEAST(addresses.first_block_height, EXCLUDED.first_block_height),
        first_seen = LEAST(addresses.first_seen, EXCLUDED.first_seen),
        last_block_height = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_block_height
            ELSE addresses.last_block_height
        END,
        last_tx_index = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_tx_index
            ELSE addresses.last_tx_index
        END,
        last_seen = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_seen
            ELSE addresses.last_seen
        END,
        last_tx_hash = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_tx_hash
            ELSE addresses.last_tx_hash
        END,
        last_contract = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_contract
            ELSE addresses.last_contract
        END,
        last_function = CASE
            WHEN (EXCLUDED.last_block_height, EXCLUDED.last_tx_index)
                >= (addresses.last_block_height, addresses.last_tx_index)
                THEN EXCLUDED.last_function
            ELSE addresses.last_function
        END,
        updated_at = EXCLUDED.updated_at;
    """


def insert_shielded_output():
    return """
    INSERT INTO shielded_outputs(
        block_height,
        tx_hash,
        tx_index,
        contract,
        function,
        action,
        output_index,
        note_index,
        commitment,
        new_root,
        payload_hash,
        created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (tx_hash, output_index) DO NOTHING;
    """


def insert_shielded_output_tag():
    return """
    INSERT INTO shielded_output_tags(
        block_height,
        tx_hash,
        tx_index,
        contract,
        function,
        action,
        output_index,
        note_index,
        commitment,
        new_root,
        payload_hash,
        tag_kind,
        tag_value,
        created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    ON CONFLICT (tx_hash, output_index, tag_kind, tag_value) DO NOTHING;
    """


def upsert_contract():
    return """
    INSERT INTO contracts(
        name, last_tx_hash, submitted_at_block, submitted_at, source, xsc001
    )
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (name) DO UPDATE SET
        last_tx_hash = EXCLUDED.last_tx_hash,
        submitted_at_block = EXCLUDED.submitted_at_block,
        submitted_at = EXCLUDED.submitted_at,
        source = EXCLUDED.source,
        xsc001 = EXCLUDED.xsc001;
    """


def insert_state_patch_record():
    return """
    INSERT INTO state_patches(
        hash, block_height, block_hash, block_time, patch_count, patches, created_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (hash) DO NOTHING;
    """


def select_block_identity():
    return """
    SELECT block_hash, app_hash
    FROM blocks
    WHERE height = $1;
    """


def select_contracts():
    return """
    SELECT
        name,
        last_tx_hash,
        submitted_at_block,
        submitted_at,
        source,
        xsc001
    FROM contracts
    ORDER BY submitted_at_block ASC, name ASC
    LIMIT $1 OFFSET $2;
    """


def select_token_contracts():
    return """
    SELECT
        c.name AS contract,
        c.last_tx_hash,
        c.submitted_at_block,
        c.submitted_at,
        name_state.value AS token_name,
        symbol_state.value AS token_symbol,
        logo_state.value AS token_logo_url,
        COUNT(*) OVER() AS total_count
    FROM contracts AS c
    LEFT JOIN state AS name_state
        ON name_state.key = c.name || '.metadata:token_name'
    LEFT JOIN state AS symbol_state
        ON symbol_state.key = c.name || '.metadata:token_symbol'
    LEFT JOIN state AS logo_state
        ON logo_state.key = c.name || '.metadata:token_logo_url'
    WHERE c.xsc001 = TRUE
    ORDER BY c.submitted_at_block DESC, c.name ASC
    LIMIT $1 OFFSET $2;
    """


def select_contract_summary():
    return """
    SELECT
        c.name,
        c.last_tx_hash,
        c.submitted_at_block,
        c.submitted_at,
        c.xsc001,
        submitter.sender AS creator,
        COALESCE(tx_stats.tx_count, 0) AS tx_count,
        COALESCE(reward_stats.total_rewards, 0) AS total_rewards,
        COALESCE(reward_stats.reward_count, 0) AS reward_count,
        reward_stats.first_block_height,
        reward_stats.last_block_height,
        reward_stats.first_reward_at,
        reward_stats.last_reward_at
    FROM contracts AS c
    LEFT JOIN transactions AS submitter
        ON submitter.hash = c.last_tx_hash
    LEFT JOIN (
        SELECT contract, COUNT(*) AS tx_count
        FROM transactions
        WHERE contract = $1
        GROUP BY contract
    ) AS tx_stats
        ON tx_stats.contract = c.name
    LEFT JOIN (
        SELECT
            source_contract,
            SUM(value) AS total_rewards,
            COUNT(*) AS reward_count,
            MIN(block_height) AS first_block_height,
            MAX(block_height) AS last_block_height,
            MIN(created_at) AS first_reward_at,
            MAX(created_at) AS last_reward_at
        FROM rewards
        WHERE type = 'developer_reward'
          AND source_contract = $1
        GROUP BY source_contract
    ) AS reward_stats
        ON reward_stats.source_contract = c.name
    WHERE c.name = $1;
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
        (SELECT COUNT(*) FROM transactions) AS indexed_tx_count,
        (SELECT app_hash FROM blocks ORDER BY height DESC LIMIT 1) AS indexed_app_hash;
    """


def select_transaction_by_hash():
    return """
    SELECT
        hash AS tx_hash,
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
        chi_used,
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
        hash AS tx_hash,
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
        chi_used,
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
        hash AS tx_hash,
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
        chi_used,
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
        hash AS tx_hash,
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
        chi_used,
        result,
        payload,
        envelope,
        created_at
    FROM transactions
    WHERE sender = $1
    ORDER BY block_height DESC, tx_index DESC
    LIMIT $2 OFFSET $3;
    """


def select_recent_addresses():
    return """
    SELECT
        address,
        tx_count,
        first_block_height,
        first_seen,
        last_block_height,
        last_tx_index,
        last_seen,
        last_tx_hash,
        last_contract,
        last_function,
        updated_at
    FROM addresses
    ORDER BY last_block_height DESC, last_tx_index DESC, address ASC
    LIMIT $1 OFFSET $2;
    """


def select_transactions_by_contract():
    return """
    SELECT
        hash AS tx_hash,
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
        chi_used,
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


def select_shielded_output_tags():
    return """
    SELECT
        id,
        block_height,
        tx_hash,
        tx_index,
        contract,
        function,
        action,
        output_index,
        note_index,
        commitment,
        new_root,
        payload_hash,
        tag_kind,
        tag_value,
        created_at
    FROM shielded_output_tags
    WHERE tag_kind = $1 AND tag_value = $2
    ORDER BY block_height DESC, tx_index DESC, output_index DESC
    LIMIT $3 OFFSET $4;
    """


def select_shielded_output_tags_after_id():
    return """
    SELECT
        id,
        block_height,
        tx_hash,
        tx_index,
        contract,
        function,
        action,
        output_index,
        note_index,
        commitment,
        new_root,
        payload_hash,
        tag_kind,
        tag_value,
        created_at
    FROM shielded_output_tags
    WHERE tag_kind = $1 AND tag_value = $2 AND id > $3
    ORDER BY id ASC
    LIMIT $4;
    """


def select_events_by_event_after_id():
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
    WHERE event = $1 AND id > $2
    ORDER BY id ASC
    LIMIT $3;
    """


def select_shielded_wallet_history():
    return """
    SELECT
        so.id AS output_id,
        NULL::BIGINT AS event_id,
        so.tx_hash,
        so.block_height,
        so.tx_index,
        so.contract,
        so.function,
        so.action,
        so.output_index,
        so.note_index,
        so.commitment,
        so.new_root,
        CASE WHEN sot.tx_hash IS NULL THEN NULL ELSE so.payload_hash END AS payload_hash,
        sot.tag_kind,
        sot.tag_value,
        CASE
            WHEN sot.tx_hash IS NOT NULL
             AND jsonb_typeof(t.payload -> 'kwargs' -> 'output_payloads') = 'array'
                THEN (t.payload -> 'kwargs' -> 'output_payloads') ->> so.output_index
            ELSE NULL
        END AS output_payload,
        so.created_at
    FROM shielded_outputs AS so
    JOIN transactions AS t
        ON t.hash = so.tx_hash
    LEFT JOIN shielded_output_tags AS sot
        ON sot.tx_hash = so.tx_hash
       AND sot.output_index = so.output_index
       AND sot.tag_kind = $1
       AND sot.tag_value = $2
    WHERE so.note_index IS NOT NULL
      AND so.note_index >= $3
    ORDER BY so.note_index ASC, so.id ASC
    LIMIT $4;
    """


def select_transactions_payloads_for_hashes():
    return """
    SELECT hash, payload
    FROM transactions
    WHERE hash = ANY($1::TEXT[]);
    """


def select_shielded_output_tags_for_transactions():
    return """
    SELECT
        tx_hash,
        output_index,
        payload_hash,
        tag_kind,
        tag_value
    FROM shielded_output_tags
    WHERE tag_kind = $1
      AND tag_value = $2
      AND tx_hash = ANY($3::TEXT[]);
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


def select_events_by_contract_event_after_id():
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
    WHERE contract = $1 AND event = $2 AND id > $3
    ORDER BY id ASC
    LIMIT $4;
    """


def select_dex_candles():
    amount0_in = _numeric_projection("jsonb_extract_path(e.data, $12::TEXT)")
    amount1_in = _numeric_projection("jsonb_extract_path(e.data, $13::TEXT)")
    amount0_out = _numeric_projection("jsonb_extract_path(e.data, $14::TEXT)")
    amount1_out = _numeric_projection("jsonb_extract_path(e.data, $15::TEXT)")
    return f"""
    WITH raw_swaps AS (
        SELECT
            e.id AS event_id,
            e.block_height,
            e.tx_hash,
            e.tx_index,
            e.event_index,
            e.created_at,
            {amount0_in} AS amount0_in,
            {amount1_in} AS amount1_in,
            {amount0_out} AS amount0_out,
            {amount1_out} AS amount1_out
        FROM events AS e
        WHERE e.contract = $1
          AND e.event = $2
          AND (
              e.data_indexed @> jsonb_build_object($4::TEXT, $5::TEXT)
              OR (
                  $6::BIGINT IS NOT NULL
                  AND e.data_indexed @> jsonb_build_object($4::TEXT, $6::BIGINT)
              )
          )
          AND ($7::TIMESTAMPTZ IS NULL OR e.created_at >= $7::TIMESTAMPTZ)
          AND ($8::TIMESTAMPTZ IS NULL OR e.created_at < $8::TIMESTAMPTZ)
    ),
    trades AS (
        SELECT
            *,
            CASE
                WHEN amount0_in > 0 AND amount1_out > 0 THEN amount1_out / amount0_in
                WHEN amount1_in > 0 AND amount0_out > 0 THEN amount1_in / amount0_out
                ELSE NULL
            END AS price_token1_per_token0,
            COALESCE(NULLIF(amount0_in, 0), NULLIF(amount0_out, 0), 0) AS volume_token0,
            COALESCE(NULLIF(amount1_in, 0), NULLIF(amount1_out, 0), 0) AS volume_token1
        FROM raw_swaps
    ),
    bucketed AS (
        SELECT
            *,
            (FLOOR(EXTRACT(EPOCH FROM created_at) / $9::NUMERIC) * $9::NUMERIC)::BIGINT
                AS bucket_epoch
        FROM trades
        WHERE price_token1_per_token0 IS NOT NULL
    ),
    candles AS (
        SELECT
            $3::TEXT AS source,
            $5::TEXT AS market_id,
            $6::BIGINT AS pair_id,
            to_timestamp(bucket_epoch) AS bucket_start,
            to_timestamp(bucket_epoch + $9::BIGINT) AS bucket_end,
            (ARRAY_AGG(price_token1_per_token0 ORDER BY created_at ASC, event_id ASC))[1]
                AS open,
            MAX(price_token1_per_token0) AS high,
            MIN(price_token1_per_token0) AS low,
            (ARRAY_AGG(price_token1_per_token0 ORDER BY created_at DESC, event_id DESC))[1]
                AS close,
            SUM(volume_token0) AS volume_token0,
            SUM(volume_token1) AS volume_token1,
            COUNT(*) AS trade_count,
            MIN(block_height) AS first_block_height,
            MAX(block_height) AS last_block_height,
            (ARRAY_AGG(event_id ORDER BY created_at ASC, event_id ASC))[1] AS first_event_id,
            (ARRAY_AGG(event_id ORDER BY created_at DESC, event_id DESC))[1] AS last_event_id
        FROM bucketed
        GROUP BY bucket_epoch
    )
    SELECT *
    FROM (
        SELECT *
        FROM candles
        ORDER BY bucket_start DESC
        LIMIT $10 OFFSET $11
    ) AS page
    ORDER BY bucket_start ASC;
    """


def select_recent_events():
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
    ORDER BY created_at DESC, id DESC
    LIMIT $1 OFFSET $2;
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


def select_token_balances():
    return """
    WITH portfolio AS (
        SELECT
            c.name AS contract,
            s.value AS balance,
            s.value_numeric AS balance_numeric,
            s.last_tx_hash,
            s.last_block_height,
            s.updated_at,
            name_state.value AS token_name,
            symbol_state.value AS token_symbol,
            logo_state.value AS token_logo_url
        FROM contracts AS c
        JOIN state AS s
            ON s.key = c.name || '.balances:' || $1
        LEFT JOIN state AS name_state
            ON name_state.key = c.name || '.metadata:token_name'
        LEFT JOIN state AS symbol_state
            ON symbol_state.key = c.name || '.metadata:token_symbol'
        LEFT JOIN state AS logo_state
            ON logo_state.key = c.name || '.metadata:token_logo_url'
        WHERE c.xsc001 = TRUE
          AND ($2 OR s.value_numeric IS DISTINCT FROM 0)
    )
    SELECT
        contract,
        balance,
        balance_numeric,
        last_tx_hash,
        last_block_height,
        updated_at,
        token_name,
        token_symbol,
        token_logo_url,
        COUNT(*) OVER() AS total_count
    FROM portfolio
    ORDER BY last_block_height DESC, contract ASC
    LIMIT $3 OFFSET $4;
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


def select_state_previous():
    return """
    SELECT
        s.key,
        s.value AS current_value,
        s.last_change_id,
        s.last_tx_hash,
        s.last_block_height,
        s.updated_at,
        latest.block_hash AS current_block_hash,
        latest.block_time AS current_block_time,
        latest.tx_index AS current_tx_index,
        latest.write_index AS current_write_index,
        latest.origin_type AS current_origin_type,
        prev.change_id AS previous_change_id,
        prev.new_value AS previous_value,
        prev.tx_hash AS previous_tx_hash,
        prev.block_height AS previous_block_height,
        prev.block_hash AS previous_block_hash,
        prev.block_time AS previous_block_time,
        prev.tx_index AS previous_tx_index,
        prev.write_index AS previous_write_index,
        prev.origin_type AS previous_origin_type,
        prev.created_at AS previous_created_at
    FROM state AS s
    JOIN state_changes AS latest
        ON latest.change_id = s.last_change_id
    LEFT JOIN state_changes AS prev
        ON prev.change_id = latest.previous_change_id
    WHERE s.key = $1;
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


def select_developer_rewards_summary():
    return """
    SELECT
        $1 AS recipient_key,
        COALESCE(SUM(r.value), 0) AS total_rewards,
        COUNT(*) AS reward_count,
        COUNT(DISTINCT r.tx_hash) AS tx_count,
        COUNT(DISTINCT r.source_contract) AS contract_count,
        MIN(r.block_height) AS first_block_height,
        MAX(r.block_height) AS last_block_height,
        MIN(r.created_at) AS first_reward_at,
        MAX(r.created_at) AS last_reward_at
    FROM rewards AS r
    WHERE r.type = 'developer_reward'
      AND r.recipient_key = $1;
    """
