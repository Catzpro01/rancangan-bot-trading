# 04 — Kerangka Risiko & Guardrail

Ini dokumen inti. Semua angka di sini dihasilkan `risk_engine/` dan ditegakkan
`tests/test_risk_engine.py` (`python -m pytest -q` → **69 passed**).

## 1. Prinsip

1. **Default DENY.** `validate_signal()` mengembalikan `approved=false` kecuali *semua*
   gerbang lolos. Tidak ada jalur "longgarkan sedikit lalu coba lagi".
2. **Leverage bursa ≠ risiko.** Yang menentukan risiko adalah margin per posisi terhadap
   ekuitas dan jarak stop, bukan angka 50 di layar.
3. **Likuidasi bukan exit plan.** Stop selalu di dalam buffer likuidasi, dan estimator
   lokal diverifikasi terhadap angka bursa sebelum entry.
4. **Setiap penolakan dicatat.** `decision.reasons` masuk ke tabel `trade_decision`.
   Sistem yang tidak bisa menjelaskan mengapa ia *tidak* trading tidak bisa diaudit.

## 2. Matematika likuidasi (margin terisolasi)

```
room  = 1/L − MMR − fee_taker                     # kerugian thd NOTIONAL saat likuidasi
LONG  : d = (room + fee) / (1 + fee)              # jarak dari ENTRY
SHORT : d = (room + fee) / (1 − fee)
```

Dengan `L = 50`, `MMR = 0.005`, `fee = 0.0005`:

| Besaran | Nilai |
|---|---|
| `room` | 0.0145 → 1.45% notional = **72.5% margin** |
| `d` LONG | **0.014992503748125937** → 1.49925% |
| `d` SHORT | **0.015007503751875937** → 1.50075% |
| Harga likuidasi LONG dari entry 100.000 | **98.500,7496** |
| Harga likuidasi SHORT dari entry 100.000 | **101.500,7504** |

Perbandingan leverage (LONG, MMR 0.5%, fee 0.05%):

| Leverage | Jarak likuidasi | Plafon stop (buffer 33%) | RR maksimum |
|---|---|---|---|
| 10x | 9.4547% | 6.335% | **10.68** |
| 20x | 4.498% | 3.014% | 5.06 |
| 50x | **1.4993%** | **1.0045%** | **1.687** |
| 100x | 0.4998% | 0.335% | 0.56 ← RR < 1, tidak layak |

> **Catatan implementasi yang mahal harganya.** Bentuk `(1 − room)/(1 + fee)` **tidak
> sama** dengan `1 − room/(1 + fee)`; selisihnya ≈ 0.05% harga. Saat menulis rancangan
> ini, kekeliruan itu muncul beberapa kali dan baru tertangkap karena ada test identitas
> yang memaksa `liquidation_price(...) == entry × (1 − d)`. Di 50x, 0.05% adalah
> sepertiga dari seluruh ruang stop Anda. Jangan hapus test itu.

### Guard wajib sebelum entry

```python
ok, why = assert_local_liq_matches_exchange(side, entry, local_liq, exchange_liq)
```

`exchange_liq` adalah field `liquidationPrice` dari `GET /uapi/v1/account/positions`.
Bila selisih > 0.2% atau estimator lokal lebih optimis dari bursa → **tolak entry**.
Estimator lokal adalah alat bantu; bursa adalah otoritas.

## 3. Ukuran posisi

Tiga plafon, yang paling ketat menang (`size_position()`):

| Plafon | Rumus | Contoh (ekuitas 1.000, entry 100.000, stop 0.8%) |
|---|---|---|
| Anggaran risiko | `qty = (E × risk%) / (entry × (stop + slippage))` | 5 / 850 = **0.005882** |
| Leverage efektif | `qty ≤ E × max_eff_lev / entry` | 3.000/100.000 = 0.03 |
| Plafon margin | `qty ≤ (E × margin%) × L / entry` | 2.500/100.000 = 0.025 |

Hasil: `qty = 0.0058` (dibulatkan **ke bawah** ke `baseStep` 0.0001), notional 580 USDT,
margin 11,6 USDT, kerugian bila stop kena ≈ 5 USDT = 0.5% ekuitas.

Pembulatan selalu **ke bawah** (`round_step(..., "down")`). Tidak pernah melebihkan
ukuran karena sisa pembulatan.

### Kenapa margin dibatasi 5% ekuitas

| Skenario | Tanpa plafon margin | Dengan plafon 5% |
|---|---|---|
| Likuidasi total 1 posisi (margin 50x) | bisa 30–100% ekuitas | **maksimal 5% ekuitas** |
| 3 likuidasi beruntun | akun habis | −15%, lalu circuit breaker menghentikan |

Inilah arti "aman" yang bisa dipertanggungjawabkan: **bukan tidak pernah likuidasi,
melainkan likuidasi tidak pernah fatal.**

## 4. Lima gerbang `validate_signal()`

| Gerbang | Pemeriksaan | Kode alasan |
|---|---|---|
| 1. MiroFish | skema valid, umur ≤ 6 jam, confidence ≥ 0.60, `event_risk ≠ HIGH`, bias tidak berlawanan | `MIROFISH_SCHEMA_INVALID`, `MIROFISH_STALE`, `MIROFISH_LOW_CONFIDENCE`, `MIROFISH_HIGH_EVENT_RISK_VETO`, `MIROFISH_BIAS_CONFLICT` |
| 2. Struktur trade | stop & TP ada dan di sisi benar, RR ≥ `min_rr_ratio`, umur sinyal ≤ 15 menit | `MISSING_STOP_OR_TP`, `STOP_ON_WRONG_SIDE`, `TP_ON_WRONG_SIDE`, `RR_TOO_LOW`, `SIGNAL_STALE` |
| 3. Likuidasi | jarak stop < 67% jarak likuidasi | `STOP_TOO_FAR`, `LEVERAGE_CONFIG_INVALID` |
| 4. Statistik | ada track record, EV > 0, `avg_loss ≤ jarak stop`, `avg_win ≤ jarak TP` | `NO_TRACK_RECORD`, `NEGATIVE_EV`, `STATS_INCONSISTENT` |
| 5. Kapasitas | posisi terbuka < maks, circuit breaker tidak trip, qty & notional ≥ minimum bursa, leverage efektif tidak jebol | `MAX_OPEN_POSITIONS`, `CIRCUIT_BREAKER_TRIPPED`, `SIZE_ZERO`, `BELOW_MIN_SIZE`, `BELOW_MIN_NOTIONAL`, `EFFECTIVE_LEVERAGE_BREACH` |

Plus **peringatan** yang tidak memblokir tetapi wajib masuk log:
`LIQ_DISTANCE_TINY`, `MIROFISH_CONFIDENCE_MARGINAL`, `HIGH_TAKER_FEE`.

### Contoh keluaran nyata

```json
{
  "approved": true, "reasons": [],
  "size": 0.0058, "notional": 580.0, "margin": 11.6,
  "stop_distance_pct": 0.008, "liq_distance_pct": 0.014993,
  "risk_amount": 4.93, "rr_ratio": 1.2125,
  "ev_per_trade_pct": 0.001974, "effective_leverage": 0.58,
  "warnings": ["LIQ_DISTANCE_TINY 1.499% -- satu candle news bisa menembus stop"]
}
```

Perhatikan `warnings`: sistem yang menyetujui trade ini **tetap** memberitahu bahwa
ruangnya sempit. Peringatan yang selalu muncul adalah tanda config perlu ditinjau.

## 5. Biaya: angka yang paling sering diremehkan

Biaya round-trip = 2 × taker fee = 0.10% **dari notional**. Relatif terhadap margin:

| Leverage | Biaya per putaran (% margin) |
|---|---|
| 5x | 0.5% |
| 10x | 1.0% |
| 20x | 2.0% |
| **50x** | **5.0%** |
| 100x | 10.0% |

EV dihitung dalam satuan **pergerakan harga** (bebas leverage), dengan biaya 0.10%:

```
EV = wr × avg_win − (1 − wr) × avg_loss − 2 × fee
```

Win rate impas = `(avg_loss + biaya) / (avg_win + avg_loss)`, semua dalam satuan sama.
Fungsi `min_win_rate_for_profit(rr, leverage)` memberi angka langsung:

| RR | wr impas |
|---|---|
| 1.1 | **52.38%** |
| 1.6 | 42.31% |
| 2.0 | 36.67% |
| 4.0 | 22.00% |

Karena di 50x RR maksimum hanya 1.687 (§2), **ruang win rate Anda sempit sekali**: antara
52.38% (impas) dan 100%. Itu bukan alasan untuk tidak memakai 50x — itu alasan untuk
tidak memakai 50x sebelum win rate Anda *terbukti* di atas 52.4% pada bentuk trade yang
persis sama (stop 0.8%, TP ≤ 1.35%).

### Funding

`funding_cost_estimate(notional, side, rate, hours)` — LONG membayar saat funding positif.
Pada posisi 2.500 USDT dengan funding 0.01% per 8 jam, menahan 24 jam = 0,75 USDT
(0.03% notional, 1.5% margin di 50x). Kecil per trade, tetapi `max_hold_hours = 24`
memastikannya tidak menumpuk.

## 6. Circuit breaker

`evaluate_breaker()` — tiga pemicu, dievaluasi tiap preflight (30 detik):

| Pemicu | Ambang | Efek |
|---|---|---|
| Rugi harian | −3% dari `day_start_equity` | flatten + cooldown 240 menit |
| Drawdown | 10% dari `peak_equity` | flatten + cooldown + **konfirmasi manual** |
| Loss beruntun | 3 | flatten + cooldown 240 menit |
| `day_start_equity` tidak valid | — | trip (jangan trading dengan basis tak jelas) |

`day_start_equity` di-reset pada 00:00 UTC oleh workflow `Preflight`; `peak_equity`
adalah high-water mark yang **tidak pernah turun** kecuali lewat reset manual yang
tercatat di audit.

## 7. Hasil simulasi Monte Carlo

`simulator/monte_carlo.py`, seed tetap, 500 trade, ekuitas 1.000, margin per posisi 5%.
Mode "stres" = sejumlah trade rugi **gagal stop dan berakhir likuidasi** (margin posisi
hilang seluruhnya).

### 7.1 Sensitivitas terhadap win rate (50x, stop 0.8%, TP 1.0%, RR 1.25)

```bash
python simulator/monte_carlo.py --equity 1000 --trades 500 --win-rate <WR> \
       --stop-pct 0.008 --tp-pct 0.010 --leverage 50 --liq-prob 0.30 --seed 7
```

| Win rate | Mode normal: hasil akhir | drawdown | Mode stres (30% likuidasi): hasil | drawdown |
|---|---|---|---|---|
| 45% | 866,71 (**−13,33%**) | 19,44% | 661,16 (**−33,88%**) | **36,51%** |
| 50% | 1.102,69 (+10,27%) | 11,11% | 853,96 (−14,60%) | 19,45% |
| 55% | 1.577,71 (+57,77%) | 6,18% | 1.292,00 (+29,20%) | 8,55% |
| 62% | 1.847,17 (+84,72%) | 5,66% | 1.637,48 (+63,75%) | 5,77% |

Bacaan yang jujur:

- **Tidak ada yang bangkrut** — plafon margin 5% bekerja sesuai rancangan.
- Namun pada win rate 45% dengan 30% kegagalan stop, Anda kehilangan **sepertiga modal**.
  Itu harga dari win rate yang sedikit saja di bawah impas.
- Seluruh rentang hasil ini digerakkan oleh satu asumsi (win rate) yang **belum Anda
  ukur**. Karena itu `docs/08` mensyaratkan pengukuran win rate pada data Anda sendiri
  sebelum modal nyata dipakai.

### 7.2 Perbandingan leverage pada win rate sama (62%)

| Konfigurasi | Mode normal | drawdown | Mode stres | drawdown |
|---|---|---|---|---|
| 50x, stop 0.8% / TP 1.0% | +84,72% | 5,66% | +63,75% | 5,77% |
| 10x, stop 2.0% / TP 5.0% | +1.344,16% | 2,71% | +919,74% | 6,27% |

Angka 10x tampak jauh lebih besar **karena RR-nya 2.5 vs 1.25** — yaitu persis karena 10x
memberi ruang target yang 50x tidak punya. Ini ilustrasi Konsekuensi #1 di `docs/01`,
bukti angka, bukan opini. Sekali lagi: win rate 62% adalah *input*, bukan temuan.

## 8. Manajemen posisi

- **Trailing stop ATR** (`trailing_stop`) hanya bergerak searah profit (ratchet). Untuk
  LONG, stop tidak pernah turun; untuk SHORT, tidak pernah naik. Ada test untuk keduanya.
- **Urutan exit** (`should_exit`): stop → take profit → time stop (24 jam).
- **Tidak ada average-down.** Menambah posisi yang rugi di 50x adalah cara tercepat
  mengubah kerugian 0.5% menjadi likuidasi. `max_open_positions = 1` menegakkannya secara
  struktural.
- **Anomali = flatten.** Posisi dengan `leverage ≠ 50`, `isolatedMode ≠ ISOLATED_*`, atau
  simbol di luar whitelist → ratakan dan masuk `HALTED`.

## 9. Konfigurasi (`config/risk_config.json`)

```json
{
  "risk_per_trade_pct": 0.5, "max_effective_leverage": 3.0,
  "max_margin_per_trade_pct": 5.0, "max_open_positions": 1,
  "slippage_buffer_pct": 0.05, "leverage": 50, "liq_buffer_pct": 33.0,
  "max_signal_age_sec": 900, "max_verdict_age_sec": 21600,
  "min_gate_score": 0.55, "min_confidence": 0.6,
  "veto_if_high_event_risk": true, "veto_on_negative_ev": true,
  "min_rr_ratio": 1.1,
  "max_daily_loss_pct": 3.0, "max_drawdown_pct": 10.0,
  "max_consecutive_losses": 3, "cooldown_minutes_after_trip": 240,
  "funding_rate_per_interval": 0.0001, "funding_intervals_per_day": 3,
  "max_hold_hours": 24.0
}
```

Tiga test menjaga berkas ini:

1. `test_config_file_matches_riskconfig_fields` — tidak ada kunci typo yang diam-diam
   diabaikan.
2. `test_configured_stop_ceiling_is_inside_liquidation` — plafon stop masih di dalam
   jarak likuidasi.
3. `test_config_min_rr_is_achievable_at_50x` — `min_rr_ratio` tidak melebihi RR yang
   secara geometris bisa dicapai pada leverage ini.

Artinya: mengubah `config/risk_config.json` sembarangan akan **membuat test gagal**,
bukan membuat bot diam-diam lebih agresif.

## 10. Yang sengaja TIDAK dilakukan rancangan ini

| Tidak dilakukan | Kenapa |
|---|---|
| Martingale / average-down | di 50x ini mengubah kerugian kecil menjadi likuidasi |
| Grid pada leverage tinggi | setiap lapis grid adalah margin tambahan di ruang 1.5% |
| Copy-trade / sinyal pihak ketiga tanpa gerbang | gerbang tidak bisa memverifikasi asal sinyal |
| Trading 24/7 tanpa jeda | `max_hold_hours` + cooldown memberi waktu sistem mendingin |
| Otomasi penarikan dana | API key **tidak** diberi izin transfer |
| Mengandalkan `liquidationPrice` lokal | bursa adalah otoritas; lokal hanya pembanding |
