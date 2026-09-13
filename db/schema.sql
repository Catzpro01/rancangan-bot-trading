-- db/schema.sql
-- Skema audit + state untuk bot. Satu-satunya sumber kebenaran struktur DB.
--
-- Prinsip desain:
--   1. Setiap keputusan (disetujui ATAU ditolak) wajib punya baris di trade_decision.
--      Jejak penolakan sama berharganya dengan jejak order -- justru lebih sering.
--   2. trade_order memakai client_order_id sebagai PRIMARY KEY sehingga
--      "INSERT ... ON CONFLICT DO NOTHING" memberi idempotensi: kalau n8n retry,
--      barisnya tidak ganda dan Pionex pun menolak duplikat clientOrderId.
--   3. Semua angka harga/kuantitas memakai NUMERIC, bukan FLOAT. Selisih 1e-12 pada
--      jarak likuidasi 1.49% bukan hal yang boleh dibulatkan oleh tipe data.
--   4. Tabel ini juga dipakai untuk REKONSILIASI: baris di sini dibandingkan dengan
--      jawaban GET /uapi/v1/account/positions. Yang beda = insiden (lihat docs/09).
--
-- Dijalankan:  psql "$DATABASE_URL" -f db/schema.sql      (aman diulang-ulang)

CREATE TABLE IF NOT EXISTS bot_state (
    id                    BIGSERIAL PRIMARY KEY,
    cfg                   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    day_start_equity      NUMERIC(20,8) NOT NULL DEFAULT 0,
    peak_equity           NUMERIC(20,8) NOT NULL DEFAULT 0,
    consecutive_losses    INT          NOT NULL DEFAULT 0,
    halted                BOOLEAN      NOT NULL DEFAULT true,
    halt_reason           TEXT,
    halted_at             TIMESTAMPTZ,
    last_heartbeat        TIMESTAMPTZ,
    last_market_beat      TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE bot_state IS
    'State bot. halted default TRUE: bot lahir dalam keadaan berhenti dan harus '
    'dinyalakan secara sadar oleh preflight, bukan sebaliknya.';

-- Baris awal. Preflight membaca "baris terakhir"; tanpa baris ini SELECT-nya kosong
-- dan seluruh loop berhenti (fail-closed, memang itu yang diinginkan).
INSERT INTO bot_state (cfg, day_start_equity, peak_equity, halted)
SELECT '{}'::jsonb, 0, 0, true
WHERE NOT EXISTS (SELECT 1 FROM bot_state);

CREATE TABLE IF NOT EXISTS market_snapshot (
    id      BIGSERIAL PRIMARY KEY,
    symbol  TEXT        NOT NULL,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_ts ON market_snapshot (ts DESC);

COMMENT ON TABLE market_snapshot IS
    'Snapshot pasar per putaran (klines, depth, ATR). Sumber atr untuk trailing stop; '
    'kalau tabel ini basi, monitor menutup posisi (ANOMALY:MARKET_STALE).';

CREATE TABLE IF NOT EXISTS mirofish_run_log (
    id         BIGSERIAL PRIMARY KEY,
    job_id     TEXT        NOT NULL UNIQUE,
    symbol     TEXT        NOT NULL,
    status     TEXT        NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    detail     JSONB
);
CREATE INDEX IF NOT EXISTS idx_mirofish_run_log_status ON mirofish_run_log (status, started_at);

CREATE TABLE IF NOT EXISTS mirofish_verdict (
    run_id          TEXT PRIMARY KEY,          -- dedup: satu run = satu verdict
    verdict_ts      TIMESTAMPTZ NOT NULL,
    schema_ok       BOOLEAN     NOT NULL DEFAULT false,
    bias            TEXT        NOT NULL DEFAULT 'NONE',
    confidence      NUMERIC(5,4),
    event_risk      TEXT        NOT NULL DEFAULT 'HIGH',
    horizon_hours   INT,
    risk_score      NUMERIC(5,4),
    evidence        JSONB,
    adapter_version TEXT,
    raw             JSONB       NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_event_risk CHECK (event_risk IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
    CONSTRAINT chk_confidence_range CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX IF NOT EXISTS idx_mirofish_verdict_ts ON mirofish_verdict (verdict_ts DESC);

COMMENT ON TABLE mirofish_verdict IS
    'Verdict MiroFish SETELAH dinormalkan adapter. Kolom event_risk default HIGH dan '
    'schema_ok default false: verdict rusak terbaca sebagai veto, bukan sebagai izin.';

CREATE TABLE IF NOT EXISTS trade_decision (
    id         BIGSERIAL PRIMARY KEY,
    signal_id  TEXT        NOT NULL,
    approved   BOOLEAN     NOT NULL,
    reasons    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    warnings   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_decision_created ON trade_decision (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_decision_signal ON trade_decision (signal_id);

COMMENT ON TABLE trade_decision IS
    'Setiap evaluasi sinyal, disetujui maupun ditolak, beserta kode alasannya. '
    'Ini tabel yang dipakai untuk audit "kenapa bot tidak masuk?".';

CREATE TABLE IF NOT EXISTS trade_order (
    client_order_id  TEXT PRIMARY KEY,          -- pg-<ACTION>-<hash>, ≤64 char
    signal_id        TEXT        NOT NULL,
    symbol           TEXT,
    side             TEXT,
    status           TEXT        NOT NULL DEFAULT 'SENDING',
    request_hash     TEXT,
    body             JSONB,
    response         JSONB,
    exchange_order_id TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_status CHECK (status IN ('SENDING', 'FILLED', 'PARTIAL', 'CANCELED', 'REJECTED', 'TIMEOUT'))
);
CREATE INDEX IF NOT EXISTS idx_trade_order_created ON trade_order (created_at DESC);

COMMENT ON TABLE trade_order IS
    'PK = client_order_id supaya retry n8n tidak mengirim dua order. Alur: tulis baris '
    'SENDING lebih dulu, baru POST ke Pionex; kalau timeout, statusnya jadi TIMEOUT dan '
    'kebenaran dipastikan lewat GET /uapi/v1/trade/orderByClientOrderId.';

CREATE TABLE IF NOT EXISTS open_position (
    id           BIGSERIAL PRIMARY KEY,
    position_id  TEXT        NOT NULL UNIQUE,
    signal_id    TEXT,
    client_order_id TEXT,
    symbol       TEXT        NOT NULL,
    side         TEXT        NOT NULL,
    entry_price  NUMERIC(20,8) NOT NULL,
    qty          NUMERIC(20,8) NOT NULL,
    leverage     INT         NOT NULL,
    stop_price   NUMERIC(20,8) NOT NULL,
    take_profit  NUMERIC(20,8) NOT NULL,
    liquidation_price NUMERIC(20,8),
    state        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,
    close_reason TEXT,
    CONSTRAINT chk_side CHECK (side IN ('LONG', 'SHORT'))
);
CREATE INDEX IF NOT EXISTS idx_open_position_open ON open_position (closed_at) WHERE closed_at IS NULL;

COMMENT ON TABLE open_position IS
    'Cermin posisi yang DIKIRIMI bot. Bukan pengganti GET /uapi/v1/account/positions; '
    'dibandingkan dengannya tiap 15 detik oleh 04-position-monitor.';

-- ---------------------------------------------------------------- pandangan bantu

CREATE OR REPLACE VIEW v_posisi_terbuka AS
SELECT position_id, symbol, side, entry_price, qty, stop_price, take_profit,
       liquidation_price, opened_at,
       EXTRACT(EPOCH FROM (now() - opened_at)) / 3600.0 AS umur_jam
FROM open_position
WHERE closed_at IS NULL;

CREATE OR REPLACE VIEW v_ringkasan_harian AS
SELECT date_trunc('day', created_at)                       AS hari,
       count(*)                                            AS total_keputusan,
       count(*) FILTER (WHERE approved)                    AS disetujui,
       count(*) FILTER (WHERE NOT approved)                AS ditolak,
       round(100.0 * count(*) FILTER (WHERE approved) / count(*), 2) AS pct_disetujui
FROM trade_decision
GROUP BY 1
ORDER BY 1 DESC;

-- Tingkat penolakan yang wajar itu TINGGI. Kalau pct_disetujui melonjak, yang berubah
-- biasanya bukan pasar -- biasanya ada gerbang yang diam-diam mati.
