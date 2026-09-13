# 01 — Ringkasan Eksekutif, Model Ancaman, dan Temuan Angka

## 1. Apa yang diminta dan apa yang dibangun

**Permintaan:** bot trading "sangat aman" dengan leverage 50x, memakai n8n + MiroFish di Pionex.

**Yang dibangun:** rancangan lengkap plus kernel risiko yang bisa dites, di mana
"sangat aman" diterjemahkan menjadi **sepuluh lapisan pertahanan yang masing-masing
bisa gagal sendiri-sendiri tanpa menghancurkan akun** — bukan menjadi jaminan tidak rugi.

Satu kalimat yang perlu dipegang: **leverage 50x di sini adalah alat efisiensi modal,
bukan pengali keuntungan.** Notional dibatasi maksimal 3× ekuitas dan margin per posisi
maksimal 5% ekuitas, sehingga posisi 50x berperilaku seperti posisi 3x yang margin-nya
kecil.

## 2. Empat angka yang menentukan seluruh rancangan

Semua angka di bawah dihasilkan oleh `risk_engine/` dan ditegakkan oleh `tests/`
(`python -m pytest -q` → 69 passed). Asumsi: `maintMarginRatio` 0.5%, taker fee 0.05%
— **keduanya wajib diambil ulang dari akun Anda saat runtime**, lihat §6.

| # | Besaran | Nilai | Sumber |
|---|---|---|---|
| 1 | Jarak harga entry → likuidasi, 50x | **1.49925%** (long) / 1.50075% (short) | `liquidation_distance_pct(50, 0.005, 0.0005)` |
| 2 | Plafon jarak stop (buffer 33% dari likuidasi) | **1.00450%** | `d × (1 − 0.33)` |
| 3 | RR maksimum yang masih masuk akal di 50x | **1.687** | `max_usable_rr(50, 0.008, ...)` |
| 4 | Biaya round-trip relatif terhadap margin di 50x | **5.0%** margin (0.10% notional) | `cost_of_trade` ÷ margin |

Pembanding untuk leverage 10x: jarak likuidasi **9.495%**, plafon stop 6.33%, RR maksimum
**10.68**. Selisih ruang gerak ini — bukan selisih keuntungan — adalah inti masalahnya.

### Konsekuensi #1: di 50x, target profit tidak punya tempat

Stop maksimal 1.00%. Target profit paling jauh yang masih waras adalah 90% dari jarak
likuidasi = 1.35%. Maka **RR paling banter 1.687**. Bandingkan dengan 10x yang memberi
RR sampai 10.68. Strategi yang butuh RR 3:1 atau 4:1 **secara geometris tidak mungkin
dijalankan di 50x** — bukan karena biayanya, tapi karena harga likuidasi berada terlalu
dekat untuk memberi ruang pada target.

Karena itu `config/risk_config.json` memakai `min_rr_ratio = 1.1`, dan ada unit test
(`test_config_min_rr_is_achievable_at_50x`) yang **menggagalkan build** bila seseorang
menaikkan angka itu melewati batas yang bisa dicapai.

### Konsekuensi #2: win rate impas naik, dan kenaikannya bisa dihitung

Biaya round-trip 0.10% dari notional tidak berubah saat leverage berubah. Yang berubah
adalah besarnya relatif terhadap margin: 1% di 10x, 5% di 50x. Win rate impas:

```
wr_min = (avg_loss + biaya) / (avg_win + avg_loss)     # semua dalam satuan yang sama
```

| RR | wr impas di 10x | wr impas di 50x |
|---|---|---|
| 1.1 | 47.62% | **52.38%** |
| 1.6 | 38.46% | **42.31%** |
| 2.0 | 33.33% | **36.67%** |

Angka-angka ini keluar dari `min_win_rate_for_profit()` dan `breakeven_win_rate()`.
Di 50x dengan RR 1.1 (satu-satunya RR yang tersedia, lihat Konsekuensi #1), Anda butuh
**win rate > 52.38% hanya untuk balik modal**. Margin keuntungannya tipis sekali.

### Konsekuensi #3: margin per posisi harus dibatasi oleh *anggaran rugi*, bukan oleh leverage

Inilah kunci "aman"-nya. Jika margin per posisi dibatasi 5% ekuitas, maka **satu
likuidasi total — stop gagal, harga gap, bursa lambat — menghapus maksimal 5% ekuitas**,
bukan 100%.

| Ekuitas | Leverage bursa | Notional | Margin | Rugi bila terlikuidasi |
|---|---|---|---|---|
| 1.000 USDT | 50x | 2.500 USDT | 50 USDT | **50 USDT (5%)** |
| 1.000 USDT | 10x | 2.500 USDT | 250 USDT | 250 USDT (25%) |

Perhatikan baris kedua: leverage *rendah* justru lebih berbahaya **bila margin tidak
dibatasi**, karena posisi yang sama memakan margin 5× lebih besar. Aman atau tidaknya
ditentukan oleh `max_margin_per_trade_pct`, bukan oleh angka leverage di layar bursa.

### Konsekuensi #4: frekuensi adalah musuh di 50x

Biaya 5% margin per putaran berarti 20 trade = 100% margin terkikis biaya bila semua
trade impas. Strategi 50x harus **jarang dan selektif**: maksimal 1 posisi terbuka
(`max_open_positions = 1`), maksimal 3 kerugian beruntun lalu mesin berhenti
(`max_consecutive_losses = 3`), time stop 24 jam.

## 3. Arsitektur dalam satu paragraf

n8n adalah orkestrator (jadwal, HTTP, notifikasi, audit). MiroFish dijalankan terpisah
sebagai mesin simulasi multi-agent yang menghasilkan penilaian risiko peristiwa — ia
**memveto**, bukan menyuruh beli. Di antara keduanya dan bursa berdiri satu-satunya
gerbang: `risk_engine.validate_signal()`, yang **default-nya menolak**. Tidak ada jalur
kode dari MiroFish ke Pionex yang tidak melewati gerbang itu. Posisi selalu dipasang
dengan margin terisolasi, stop di dalam buffer likuidasi, dan `clientOrderId` deterministik
agar retry tidak pernah membuka posisi ganda.

Detail: `docs/02`.

## 4. Model ancaman

| # | Ancaman | Dampak | Lapisan pertahanan |
|---|---|---|---|
| A1 | Wick/gap menembus stop sebelum order keluar | Likuidasi | Margin terisolasi ≤ 5% ekuitas; buffer likuidasi 33%; time stop |
| A2 | n8n macet/restart saat posisi terbuka | Posisi tanpa pengawasan | Watchdog eksternal + heartbeat; `Preflight` meratakan posisi bila heartbeat basi |
| A3 | Retry n8n mengirim order dua kali | Posisi ganda | `clientOrderId` deterministik (dedup 2 jam di sisi Pionex) |
| A4 | Verdict MiroFish basi/halusinasi | Sinyal palsu | Skema divalidasi; umur maks 6 jam; `event_risk=HIGH` = veto keras |
| A5 | API key bocor | Kehilangan dana | Key tanpa izin transfer; whitelist IP; key di credential store n8n, bukan di JSON workflow |
| A6 | Strategi rugi terus tanpa terlihat | Drawdown diam-diam | Circuit breaker harian 3%, drawdown 10%, 3 loss beruntun |
| A7 | Estimasi likuidasi kita salah | Stop di sisi salah likuidasi | `assert_local_liq_matches_exchange()` menolak entry bila selisih > 0.2% |
| A8 | Fee tier berbeda dari asumsi | EV berubah jadi negatif | Fee dibaca dari konfigurasi per-akun; EV dihitung ulang tiap sinyal; `HIGH_TAKER_FEE` warning |
| A9 | Perubahan `maintMarginRatio` oleh bursa | Jarak likuidasi bergeser | `riskTable` disegarkan tiap siklus preflight; nilai basi = veto |
| A10 | Jam server melenceng | Semua request 401 | NTP wajib; jendela timestamp Pionex ±20 detik |
| A11 | Seseorang (termasuk Anda) menaikkan angka risiko | Rencana runtuh | `risk_config.json` divalidasi terhadap field `RiskConfig`; test menolak RR mustahil |
| A12 | Overfitting backtest | Live jauh lebih buruk | Rollout bertahap paper → 1% → 5% modal; `docs/08` |

## 5. Yang secara jujur TIDAK dijamin rancangan ini

1. **Profit tidak dijamin.** Simulator di `simulator/` memakai win rate sebagai *input*.
   Pada win rate 45% dengan RR 1.25, hasil 500 trade adalah **−33.88%** dengan drawdown
   36.51% (mode stres, 30% trade rugi berakhir likuidasi). Angka itu nyata dan bisa
   direproduksi:
   ```bash
   python simulator/monte_carlo.py --win-rate 0.45 --stop-pct 0.008 --tp-pct 0.010 \
          --leverage 50 --liq-prob 0.30 --seed 7
   ```
2. **MiroFish bukan peramal harga.** Ia mensimulasikan reaksi agen terhadap materi yang
   Anda masukkan. Laporan upstream MiroFish sendiri menyatakan ReportAgent menghasilkan
   laporan Markdown kualitatif dan **tidak menjamin** kontrak `verdict.json`, `p_yes`,
   `confidence`, atau `action` yang terkalibrasi untuk perdagangan otomatis. Karena itu
   perannya di sini adalah **veto risiko peristiwa**, dan setiap verdict harus lolos
   validasi skema (`docs/03`).
3. **Stop-loss bukan jaminan.** Ia adalah order biasa. Pada pergerakan sangat cepat ia
   bisa terisi jauh dari harga stop. Itu sebabnya margin per posisi dibatasi.
4. **Tidak ada testnet Pionex yang terverifikasi di dokumen ini.** Saya tidak menemukan
   testnet futures resmi Pionex; karena itu `docs/08` memakai **mode paper** (sinyal
   dicatat, order tidak dikirim) sebagai pengganti, dan menyarankan konfirmasi langsung
   ke dukungan Pionex sebelum live.

## 6. Yang harus Anda cek sendiri sebelum live (tidak bisa saya verifikasi di sini)

Sandbox tempat rancangan ini ditulis tidak punya akses keluar ke `api.pionex.com`
(`curl` mengembalikan kode 000). Jadi nilai-nilai berikut **wajib** diambil dari akun Anda:

```bash
curl "https://api.pionex.com/api/v1/common/riskTable?symbol=BTC_USDT_PERP"
#   -> rows[].maintMarginRatio, rows[].maxLeverage, rows[].notionalLimit
curl "https://api.pionex.com/api/v1/common/symbols?symbols=BTC_USDT_PERP"
#   -> baseStep, quoteStep, minNotional, minSizeLimit, maxSizeLimit, liquidationFeeRate
curl "https://api.pionex.com/api/v1/market/fundingRates?symbol=BTC_USDT_PERP"
#   -> fundingRate aktual (bukan asumsi 0.01%)
```

Dan dari aplikasi Pionex: **fee maker/taker tier akun Anda**. Berbagai sumber pihak
ketiga menyebut 0.02% maker / 0.05% taker untuk futures, tetapi tier VIP mengubahnya dan
saya tidak dapat memverifikasinya dari sini. Seluruh perhitungan EV di rancangan ini
memakai 0.05% taker sebagai asumsi; bila angka Anda berbeda, ubah di
`ExchangeRules.taker_fee` dan jalankan ulang test + simulator.

## 7. Keputusan rancangan yang paling penting

Jika Anda hanya mengingat tiga hal:

1. **Batasi margin per posisi (5% ekuitas), bukan leverage-nya.** Itu yang membuat
   likuidasi total menjadi kejadian 5%, bukan kejadian 100%.
2. **Pasang stop di dalam 67% jarak likuidasi, dan verifikasi terhadap `liquidationPrice`
   dari bursa sebelum entry.** Estimasi lokal bukan otoritas.
3. **MiroFish memveto, tidak memerintah.** Sinyal arah tetap harus datang dari aturan
   yang bisa di-backtest; MiroFish hanya menjawab "apakah ada peristiwa yang membuat
   trade ini berbahaya sekarang".
