from xian.services.bds.sql import _numeric_projection


def _pair_key(value: str) -> str:
    return f"COALESCE({value}->>'__big_int__', {value}#>>'{{}}')"


def create_market_views() -> str:
    swap_pair = _pair_key("(data_indexed->'pair')")
    created_pair = _pair_key("(data->'pair')")
    reserve0 = _numeric_projection("e.data->'reserve0'")
    reserve1 = _numeric_projection("e.data->'reserve1'")
    amounts = ",\n        ".join(
        f"""{_numeric_projection(f"data->'{event_field}'")} AS {column}"""
        for event_field, column in (
            ("amount0In", "amount0_in"),
            ("amount1In", "amount1_in"),
            ("amount0Out", "amount0_out"),
            ("amount1Out", "amount1_out"),
        )
    )
    return f"""
    CREATE INDEX IF NOT EXISTS idx_events_dex_pair_time
        ON events(contract, event, ({swap_pair}), created_at DESC, id DESC)
        WHERE (data_indexed ? 'pair') AND event IN ('Swap', 'Sync');
    CREATE INDEX IF NOT EXISTS idx_events_dex_market_tokens
        ON events((data_indexed->>'token0'), (data_indexed->>'token1'))
        WHERE contract = 'con_pairs' AND event = 'PairCreated';

    CREATE OR REPLACE VIEW bds_dex_swaps AS
    SELECT id AS event_id, contract, {swap_pair} AS pair_id, block_height,
        tx_hash, tx_index, event_index, signer, caller, data_indexed->'to' AS recipient,
        created_at AS block_time,
        {amounts}
    FROM events
    WHERE event = 'Swap' AND data_indexed ? 'pair';
    COMMENT ON VIEW bds_dex_swaps IS '@behavior -*';

    CREATE OR REPLACE VIEW dex_trades AS
    SELECT event_id, pair_id, block_height, tx_hash, tx_index, event_index, signer, caller,
        recipient, block_time, amount0_in, amount1_in, amount0_out, amount1_out
    FROM bds_dex_swaps WHERE contract = 'con_pairs';
    COMMENT ON VIEW dex_trades IS E'@primaryKey event_id\n@foreignKey (tx_hash) references transactions (hash)\n@behavior -query:resource:connection -query:resource:list\nOne execution per pool fill.';
    COMMENT ON COLUMN dex_trades.block_time IS '@behavior +attribute:filterBy +attribute:orderBy';

    CREATE OR REPLACE VIEW dex_markets AS
    SELECT {created_pair} AS pair_id, data_indexed->>'token0' AS token0,
        data_indexed->>'token1' AS token1, block_height AS created_at_height, tx_hash
    FROM events
    WHERE contract = 'con_pairs' AND event = 'PairCreated';
    COMMENT ON VIEW dex_markets IS E'@primaryKey pair_id\n@behavior -type:node\nIndexed canonical DEX markets.';
    COMMENT ON COLUMN dex_markets.token0 IS '@behavior +attribute:filterBy';
    COMMENT ON COLUMN dex_markets.token1 IS '@behavior +attribute:filterBy';

    CREATE OR REPLACE FUNCTION dex_market(token_a text, token_b text)
    RETURNS dex_markets LANGUAGE sql STABLE STRICT
    RETURN (
        SELECT m FROM dex_markets m
        WHERE m.token0 = least(token_a COLLATE "C", token_b COLLATE "C")
          AND m.token1 = greatest(token_a COLLATE "C", token_b COLLATE "C")
    );

    CREATE OR REPLACE FUNCTION dex_markets_trades(
        market dex_markets, start_time timestamptz DEFAULT NULL, end_time timestamptz DEFAULT NULL
    )
    RETURNS SETOF dex_trades LANGUAGE sql STABLE
    BEGIN ATOMIC
        SELECT t.* FROM dex_trades t WHERE t.pair_id = market.pair_id
            AND (start_time IS NULL OR t.block_time >= start_time)
            AND (end_time IS NULL OR t.block_time < end_time)
        ORDER BY t.block_time DESC, t.event_id DESC;
    END;
    COMMENT ON FUNCTION dex_markets_trades(dex_markets, timestamptz, timestamptz)
        IS '@behavior +connection';

    CREATE OR REPLACE FUNCTION dex_markets_indexed_height(market dex_markets)
    RETURNS bigint LANGUAGE sql STABLE
    RETURN (SELECT height FROM blocks ORDER BY height DESC LIMIT 1);

    CREATE OR REPLACE FUNCTION dex_markets_latest_reserves(
        market dex_markets,
        OUT reserve0 numeric, OUT reserve1 numeric, OUT block_height bigint, OUT block_time timestamptz
    )
    LANGUAGE sql STABLE
    BEGIN ATOMIC
        SELECT {reserve0}, {reserve1}, e.block_height, e.created_at
        FROM events e WHERE e.contract = 'con_pairs' AND e.event = 'Sync'
            AND e.data_indexed ? 'pair' AND {swap_pair} = market.pair_id
        ORDER BY e.created_at DESC, e.id DESC LIMIT 1;
    END;

    CREATE OR REPLACE FUNCTION dex_markets_stats_24h(
        market dex_markets,
        OUT as_of timestamptz, OUT trade_count bigint, OUT volume_token0 numeric, OUT volume_token1 numeric
    )
    LANGUAGE sql STABLE
    BEGIN ATOMIC
        WITH tip AS (SELECT block_time_iso FROM blocks ORDER BY height DESC LIMIT 1)
        SELECT tip.block_time_iso, count(t.event_id),
            coalesce(sum(coalesce(nullif(t.amount0_in, 0), nullif(t.amount0_out, 0), 0)), 0),
            coalesce(sum(coalesce(nullif(t.amount1_in, 0), nullif(t.amount1_out, 0), 0)), 0)
        FROM tip LEFT JOIN dex_trades t ON t.pair_id = market.pair_id
            AND t.block_time > tip.block_time_iso - interval '24 hours'
            AND t.block_time <= tip.block_time_iso
        GROUP BY tip.block_time_iso;
    END;
    """


def create_candle_api() -> str:
    return """
    DO $$
    BEGIN
        IF to_regtype('dex_candle') IS NULL THEN
            CREATE TYPE dex_candle AS (
                bucket_start timestamptz, bucket_end timestamptz, open numeric, high numeric,
                low numeric, close numeric, volume_token0 numeric, volume_token1 numeric,
                trade_count bigint, first_block_height bigint, last_block_height bigint,
                first_event_id bigint, last_event_id bigint
            );
            CREATE TYPE dex_candle_page AS (
                indexed_height bigint, next_before timestamptz, items dex_candle[]
            );
        END IF;
    END $$;
    COMMENT ON TYPE dex_candle IS 'OHLCV based on fills, quoted in token1 per token0.';
    COMMENT ON TYPE dex_candle_page IS 'Chronological nonempty candles. Pass nextBefore as before for older history.';

    CREATE OR REPLACE FUNCTION bds_dex_candle_page(
        market_contract text, market_pair text, interval_seconds integer,
        page_size integer, start_time timestamptz, before timestamptz
    )
    RETURNS dex_candle_page LANGUAGE sql STABLE
    BEGIN ATOMIC
        WITH settings AS (
            SELECT make_interval(secs => greatest(1, least(interval_seconds, 604800))) AS stride,
                '1970-01-01T00:00:00Z'::timestamptz AS epoch,
                greatest(0, least(page_size, 1000)) AS page_limit
        ), anchor AS MATERIALIZED (
            SELECT s.*, (
                SELECT block_time FROM bds_dex_swaps
                WHERE contract = market_contract AND pair_id = market_pair
                  AND (before IS NULL OR block_time < before)
                  AND (start_time IS NULL OR block_time >= start_time)
                ORDER BY block_time DESC, event_id DESC LIMIT 1
            ) AS last_fill
            FROM settings s
        ), time_window AS MATERIALIZED (
            SELECT stride, epoch, page_limit,
                greatest(start_time, date_bin(stride, last_fill - greatest(
                    interval '24 hours', least(interval '720 hours', stride * page_limit * 2)
                ), epoch)) AS lower_bound,
                least(before, date_bin(stride, last_fill, epoch) + stride) AS upper_bound
            FROM anchor WHERE last_fill IS NOT NULL AND page_limit > 0
        ), fills AS (
            SELECT w.stride, date_bin(w.stride, t.block_time, w.epoch) AS bucket_start,
                t.block_time, t.event_id, t.block_height,
                CASE
                    WHEN amount0_in > 0 AND amount1_out > 0 THEN amount1_out / amount0_in
                    WHEN amount1_in > 0 AND amount0_out > 0 THEN amount1_in / amount0_out
                END AS price,
                coalesce(nullif(amount0_in, 0), nullif(amount0_out, 0), 0) AS volume0,
                coalesce(nullif(amount1_in, 0), nullif(amount1_out, 0), 0) AS volume1
            FROM time_window w CROSS JOIN bds_dex_swaps t
            WHERE t.contract = market_contract AND t.pair_id = market_pair
                AND t.block_time >= w.lower_bound AND t.block_time < w.upper_bound
        ), buckets AS (
            SELECT bucket_start, bucket_start + stride AS bucket_end,
                (array_agg(price ORDER BY block_time, event_id))[1] AS open,
                max(price) AS high, min(price) AS low,
                (array_agg(price ORDER BY block_time DESC, event_id DESC))[1] AS close,
                sum(volume0) AS volume_token0, sum(volume1) AS volume_token1,
                count(*) AS trade_count, min(block_height) AS first_block_height,
                max(block_height) AS last_block_height,
                (array_agg(event_id ORDER BY block_time, event_id))[1] AS first_event_id,
                (array_agg(event_id ORDER BY block_time DESC, event_id DESC))[1] AS last_event_id
            FROM fills WHERE price IS NOT NULL GROUP BY bucket_start, stride
            ORDER BY bucket_start DESC
            LIMIT (SELECT page_limit FROM settings)
        ), page AS (
            SELECT CASE WHEN count(*) = w.page_limit THEN min(b.bucket_start)
                        ELSE w.lower_bound END AS older_than,
                coalesce(
                    array_agg(ROW(b.*)::dex_candle ORDER BY b.bucket_start)
                        FILTER (WHERE b.bucket_start IS NOT NULL),
                    ARRAY[]::dex_candle[]
                ) AS items
            FROM time_window w LEFT JOIN buckets b ON true
            GROUP BY w.page_limit, w.lower_bound
        )
        SELECT (SELECT height FROM blocks ORDER BY height DESC LIMIT 1),
            CASE WHEN EXISTS (
                SELECT 1 FROM bds_dex_swaps
                WHERE contract = market_contract AND pair_id = market_pair AND block_time < p.older_than
                  AND (start_time IS NULL OR block_time >= start_time)
            ) THEN p.older_than END,
            coalesce(p.items, ARRAY[]::dex_candle[])
        FROM (SELECT 1) AS singleton LEFT JOIN page p ON true;
    END;
    COMMENT ON FUNCTION bds_dex_candle_page(text, text, integer, integer, timestamptz, timestamptz)
        IS '@behavior -*';

    CREATE OR REPLACE FUNCTION dex_markets_candles(
        market dex_markets, interval_seconds integer DEFAULT 60, page_size integer DEFAULT 100,
        start_time timestamptz DEFAULT NULL, before timestamptz DEFAULT NULL
    )
    RETURNS dex_candle_page LANGUAGE sql STABLE
    RETURN bds_dex_candle_page('con_pairs', market.pair_id, interval_seconds, page_size, start_time, before);

    DROP FUNCTION IF EXISTS bds_dex_candle_page(text, text, integer, integer, integer, timestamptz, timestamptz);
    """
