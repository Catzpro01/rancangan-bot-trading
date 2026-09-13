# 08 — Rencana Rollout: Paper → Live

Rancangan ini tidak boleh langsung menyentuh uang. Urutan berikut ada karena satu
alasan: **setiap tahap dirancang agar bisa gagal tanpa biaya.**

## 0. Prasyarat (semua wajib)

- [ ] `make test` lulus (Python + Node + validator struktur + validator kontrak)
- [ ] `docs/10-checklist-pra-live.md` tercentang seluruhnya
- [ ] Kunci API Pionex: hanya reading + trading, **tanpa** transfer, dibatasi IP
- [ ] Uang yang dipakai adalah uang yang bila habis total tidak mengubah hidup Anda

## 1. Tahap A — Paper trading (minimum 4 minggu)

Pionex tidak punya testnet futures, jadi paper trading dilakukan dengan ekuitas
simulasi: gerbang risiko berjalan penuh, order **tidak** dikirim. Cara paling aman
melakukannya adalah menjalankan `01`, `02`, `04`, `06` secara normal tetapi menahan
`03` tetap nonaktif, lalu membandingkan keputusan gerbang dengan harga yang benar-benar
terjadi.

Yang diukur, bukan "berapa untung":

| Metrik | Target minimum | Kenapa ini yang diukur |
|---|---|---|
| Jumlah keputusan | ≥ 200 | di bawah ini statistik tidak berarti apa-apa |
| Tingkat penolakan | 60–95% | terlalu rendah = gerbang tidak bekerja; 100% = ada yang mati |
| Distribusi kode penolakan | tidak ada satu kode > 80% | satu kode mendominasi biasanya berarti config salah, bukan pasar |
| `MIROFISH_STALE` | < 5% | runner tertinggal → seluruh veto berbasis verdict jadi tidak berarti |
| Drawdown maksimum simulasi | < 15% | lihat `simulator/monte_carlo.py` |
| Selisih harga isi vs harga sinyal | < 0,05% | slippage yang lebih besar merusak seluruh perhitungan EV |

**Kriteria gagal Tahap A** (berhenti, perbaiki, ulangi): drawdown > 20%, atau tingkat
penolakan < 40%, atau ada satu kode penolakan > 80% tanpa penjelasan.

Baseline untuk pembanding — jalankan dan simpan keluarannya:

```bash
python simulator/monte_carlo.py --equity 1000 --trades 500 \
  --win-rate 0.50 --stop-pct 0.008 --tp-pct 0.010 \
  --leverage 50 --liq-prob 0.30 --seed 7
```

Bandingkan hasil paper dengan angka ini. Kalau paper jauh lebih bagus daripada Monte
Carlo pada win rate yang sama, yang salah biasanya asumsi Anda tentang win rate.

## 2. Tahap B — Live mikro (minimum 4 minggu)

Ekuitas **100–200 USDT**, dan turunkan sementara dua config:

```json
{ "max_margin_per_trade_pct": 2.0, "max_open_positions": 1, "leverage": 50 }
```

Margin 2% dari 150 USDT = 3 USDT per trade, notional 150 USDT. Kerugian maksimum satu
trade ≈ 2–3 USDT. Tujuan tahap ini **bukan** profit; tujuannya membuktikan bahwa:

1. order benar-benar terisi pada harga yang diharapkan;
2. `clientOrderId` mencegah duplikat saat n8n retry;
3. stop klien benar-benar menutup posisi pada 15 detik;
4. tidak ada posisi yatim (baris `open_position` tanpa pasangan di bursa);
5. rekonsiliasi `open_position` vs `GET /uapi/v1/account/positions` selalu cocok.

**Berhenti dan cabut kunci API bila**: ada posisi yang tidak tercatat, ada order ganda,
atau stop meleset lebih dari 0,3% dari harga yang dihitung. Ketiganya berarti ada yang
salah secara struktural, bukan soal pasar.

## 3. Tahap C — Naik bertahap

Hanya bila Tahap B bersih selama 4 minggu berturut-turut:

| Langkah | Ekuitas | `max_margin_per_trade_pct` | `max_open_positions` | Tunggu |
|---|---|---|---|---|
| C1 | 500 | 3% | 1 | 4 minggu |
| C2 | 1.000 | 5% | 1 | 4 minggu |
| C3 | 1.000 | 5% | 2 | 4 minggu |

**`leverage` tidak pernah dinaikkan, dan `max_effective_leverage` tidak pernah
disentuh.** Keduanya adalah rem, bukan setelan performa. Yang boleh naik hanya berapa
banyak margin yang dipertaruhkan — dan itu pun hanya setelah bukti, bukan setelah
keyakinan.

Aturan tambahan: satu perubahan config per minggu, dan setiap perubahan di-commit
bersama alasan singkatnya. `config/risk_config.json` punya riwayat git untuk alasan
ini: enam bulan dari sekarang Anda akan ingin tahu kenapa sebuah angka berubah.

## 4. Aturan berhenti (berlaku di semua tahap)

Berhenti total — bukan "kurangi ukuran" — bila salah satu terjadi:

- drawdown dari puncak ≥ 25%;
- tiga likuidasi dalam 30 hari;
- kerugian bulanan ≥ 15% selama dua bulan berturut-turut;
- satu insiden rekonsiliasi (posisi yang tidak cocok dengan bursa);
- Anda tidak bisa menjelaskan kenapa bot mengambil posisi terakhir.

Poin terakhir terdengar lunak tetapi yang paling penting. Sistem yang tidak bisa
dijelaskan tidak bisa diperbaiki, dan 50x tidak memberi ruang untuk sistem yang tidak
bisa diperbaiki.

## 5. Tinjauan berkala

Setiap minggu, lima menit:

```sql
SELECT * FROM v_ringkasan_harian LIMIT 7;
SELECT close_reason, count(*) FROM open_position
 WHERE closed_at IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

Setiap bulan: jalankan ulang Monte Carlo dengan win rate dan RR **terukur** dari bulan
itu, bukan dari asumsi awal. Bila hasil simulasi dengan angka terukur lebih buruk
daripada yang Anda terima di Tahap A, turunkan margin per trade — jangan menaikkan
target.
