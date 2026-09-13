# 02 — Arsitektur & Alur Data

## 1. Pembagian tanggung jawab

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  LAPISAN 1 — PERSEPSI (read-only, tidak boleh menyentuh order)               │
│                                                                              │
│  n8n: Market Snapshot (tiap 1m)                                              │
│    GET /api/v1/market/klines       → OHLCV                                   │
│    GET /api/v1/market/indexes      → mark price, next funding rate           │
│    GET /api/v1/market/depth        → kedalaman buku (cek slippage)           │
│    GET /api/v1/market/fundingRates → riwayat funding                         │
│    GET /api/v1/common/riskTable    → maintMarginRatio, maxLeverage (segar)   │
│    GET /api/v1/common/symbols      → baseStep, minNotional, maxSizeLimit     │
│    → tulis ke Postgres: market_snapshot                                      │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  LAPISAN 2 — PENILAIAN (lambat, mahal, opsional tapi memveto)                │
│                                                                              │
│  mirofish_runner (HTTP) ← n8n: MiroFish Sweep (tiap 4 jam / saat ada berita) │
│    1. kumpulkan seed: berita, rilis makro, catatan posisi                    │
│    2. jalankan `mirofish run --files ... --requirement ... --json`           │
│    3. baca uploads/runs/<id>/report/verdict.json + summary.json              │
│    4. adapter → envelope terstandar (docs/03)                                │
│    → tulis ke Postgres: mirofish_verdict (immutable, ada run_id)             │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  LAPISAN 3 — GERBANG (satu-satunya jalan ke bursa; DEFAULT DENY)             │
│                                                                              │
│  n8n: Trading Loop (tiap 1m)                                                 │
│    a. baca risk_config + aturan instrumen + verdict terbaru + posisi         │
│    b. hitung sinyal teknis (arah, stop, TP)                                  │
│    c. validate_signal()  ← risk_engine, 5 gerbang (docs/04 §4)               │
│    d. kalau ditolak: catat alasan, berhenti. Tidak ada "coba lagi longgar".  │
│    e. kalau disetujui: verifikasi liquidationPrice bursa vs estimator lokal  │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │  (hanya bila e lolos)
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  LAPISAN 4 — EKSEKUSI (idempoten, terukur, langsung dipagari)                │
│                                                                              │
│    POST /uapi/v1/trade/order   (clientOrderId deterministik)                 │
│    → konfirmasi fill lewat GET /uapi/v1/trade/fillsByOrderId                 │
│    → tulis trade_order + trade_fill (audit)                                  │
│    → segera daftarkan rencana exit ke posisi (stop/TP/time stop)             │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  LAPISAN 5 — PENGAWASAN (berjalan lebih sering dari lapisan lain)            │
│                                                                              │
│  n8n: Position Monitor (tiap 15 detik)                                       │
│    GET /uapi/v1/account/positions → markPrice, liquidationPrice, unrealizedPnL│
│    trailing stop (ratchet searah profit), stop/TP/time stop                  │
│    deteksi anomali: posisi yang tidak dikenal, leverage ≠ 50, mode ≠ ISOLATED│
│                                                                              │
│  n8n: Preflight & Watchdog (tiap 30 detik, host terpisah untuk watchdog)     │
│    GET /uapi/v1/account/leverage | positionMode | /trade/isolatedMode        │
│    GET /uapi/v1/account/balances → equity, day_start_equity, peak_equity     │
│    evaluasi circuit breaker → bila trip: flatten + cooldown + notifikasi     │
│    bila heartbeat Trading Loop basi > 90 detik: flatten (docs/09)            │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 2. Mengapa MiroFish dipisah dari loop trading

Simulasi MiroFish memakan menit sampai puluhan menit dan menghabiskan token LLM. Loop
trading berjalan tiap menit. Mencampur keduanya membuat loop trading bergantung pada
komponen paling lambat dan paling mahal.

Karena itu:

- MiroFish berjalan **asinkron** dan menulis verdict ke database.
- Loop trading hanya **membaca** verdict terbaru.
- Verdict punya masa berlaku (`max_verdict_age_sec = 21600` = 6 jam). Lewat itu →
  `MIROFISH_STALE` → tidak ada trade. Sistem lebih memilih tidak trading daripada
  trading dengan penilaian basi.
- Bila `mirofish_runner` mati total, verdict menjadi basi dan bot berhenti sendiri.
  **Kegagalan MiroFish = bot berhenti, bukan bot jalan tanpa rem.**

## 3. Kontrak data antar komponen

### 3.1 `market_snapshot` (diproduksi lapis 1)

```json
{
  "symbol": "BTC_USDT_PERP",
  "ts": "2026-09-13T12:00:00Z",
  "mark_price": 100000.0,
  "index_price": 100002.5,
  "next_funding_rate": 0.0001,
  "klines_1m_atr": 82.4,
  "depth_bid_usdt_10bp": 410000,
  "risk_table": { "max_leverage": 100, "maint_margin_ratio": 0.005, "fetched_at": "..." },
  "instrument": { "base_step": 0.0001, "quote_step": 0.01, "min_notional": 5, "max_size": 1000 }
}
```

`risk_table` dan `instrument` **disegarkan setiap siklus**, bukan di-cache harian
(ancaman A9).

### 3.2 `mirofish_verdict` (diproduksi lapis 2, dikonsumsi lapis 3)

```json
{
  "run_id": "20260913-114500-a91f",
  "verdict_ts": "2026-09-13T11:47:12Z",
  "schema_ok": true,
  "bias": "NEUTRAL",
  "confidence": 0.72,
  "event_risk": "LOW",
  "horizon_hours": 24,
  "evidence": ["..."],
  "adapter_version": "1.0.0",
  "raw_path": "/data/runs/20260913-114500-a91f/report/verdict.json"
}
```

`schema_ok=false` → veto (`MIROFISH_SCHEMA_INVALID`). Detail di `docs/03`.

### 3.3 `signal` (diproduksi lapis 3, dikonsumsi `validate_signal`)

Persis seperti yang didokumentasikan di docstring `validate_signal()`:

```json
{
  "direction": "LONG",
  "entry": 100000.0,
  "stop": 99200.0,
  "take_profit": 100970.0,
  "ts": "2026-09-13T12:00:03Z",
  "mirofish": { "schema_ok": true, "verdict_ts": "...", "confidence": 0.72,
                "event_risk": "LOW", "bias": "NEUTRAL", "run_id": "..." },
  "stats": { "win_rate": 0.62, "avg_win_pct": 0.0097, "avg_loss_pct": 0.0080 }
}
```

`stats.avg_win_pct` / `avg_loss_pct` adalah **pergerakan harga (fraksi)**, konsisten
dengan jarak TP dan stop. Ada gerbang `STATS_INCONSISTENT` yang menolak bila
`avg_loss` lebih besar dari jarak stop — yaitu bila angka backtest berasal dari bentuk
trade yang berbeda.

## 4. Status mesin (state machine)

```
        ┌─────────┐  preflight OK   ┌────────┐
        │  IDLE   ├────────────────►│  ARMED │
        └────┬────┘                 └───┬────┘
             │                          │ validate_signal approved
             │                          ▼
             │                     ┌─────────┐   fill    ┌──────────┐
             │                     │ PENDING ├──────────►│ IN_MARKET│
             │                     └────┬────┘           └────┬─────┘
             │              timeout 30s │                     │ stop/TP/time/reverse
             │                          ▼                     ▼
             │                     ┌─────────┐           ┌─────────┐
             └─────────────────────┤  FLAT   ├───────────┤  FLAT   │
                                   └────┬────┘           └────┬────┘
                                        │ breaker trip / heartbeat basi / KILL
                                        ▼
                                   ┌──────────┐  cooldown 240m  ┌──────┐
                                   │ HALTED   ├────────────────►│ IDLE │
                                   └──────────┘                 └──────┘
```

Aturan keras:

- `PENDING` **harus** diselesaikan dalam 30 detik: konfirmasi fill atau batalkan.
  Order yang menggantung tanpa status adalah sumber posisi hantu.
- Transisi ke `IN_MARKET` **wajib** diikuti pendaftaran rencana exit pada tick yang sama.
- `HALTED` hanya bisa keluar lewat cooldown **dan** konfirmasi manual bila alasannya
  `LIQ_MISMATCH` atau `ANOMALY_POSITION`.

## 5. Penempatan komponen

| Komponen | Host | Alasan |
|---|---|---|
| n8n + Postgres + Redis | VPS A | orkestrasi & state |
| mirofish_runner + MiroFish | VPS B | beban CPU/token besar, tidak boleh mengganggu loop |
| watchdog eksternal | VPS C atau GitHub Actions cron | harus hidup saat VPS A mati |
| Pionex API key whitelist | IP VPS A saja | mempersempit dampak kebocoran key |

Watchdog di host terpisah itu penting: watchdog yang tinggal di mesin yang sama dengan
bot tidak bisa melaporkan bot yang mati.

## 6. Latensi anggaran

| Langkah | Anggaran | Bila lewat |
|---|---|---|
| Snapshot pasar → sinyal | < 2 detik | sinyal ditandai basi (`SIGNAL_STALE`) |
| Sinyal → order terkirim | < 1 detik | batalkan, jangan kejar harga |
| Order → konfirmasi fill | < 5 detik | query ulang; bila masih `OPEN` > 30s → cancel |
| Monitor posisi | tiap 15 detik | heartbeat basi > 90s → flatten |
| Verdict MiroFish | < 6 jam | veto |

Semua angka ini ada di `config/risk_config.json` atau sebagai konstanta workflow, dan
semuanya bisa diaudit lewat `docs/06`.
