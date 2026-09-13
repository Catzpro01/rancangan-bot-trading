# 09 — Runbook Insiden

Dibaca **saat** ada masalah, bukan sebelumnya. Setiap bagian berdiri sendiri: gejala →
periksa → tindakan. Urut dari yang paling berbahaya.

Aturan pertama: **bila ragu, hentikan.** Biaya berhenti adalah kehilangan peluang.
Biaya tidak berhenti pada leverage 50x adalah kehilangan modal. Keduanya tidak
sebanding.

```bash
# Perintah henti darurat — hafalkan ini, jangan mencari-cari saat panik:
curl -X POST https://n8n.example.com/webhook/pionex-guard/kill \
     -H "Authorization: Bearer <token-webhook>"
```

---

## 1. Posisi terbuka yang tidak dikenali

**Gejala:** ada posisi di aplikasi Pionex yang tidak ada di `open_position`.

```sql
SELECT position_id, symbol, side, qty, opened_at FROM v_posisi_terbuka;
```

Bandingkan dengan `GET /uapi/v1/account/positions`.

**Tindakan (urutan wajib):**
1. **Kill switch sekarang.** Jangan menganalisis dulu.
2. Ratakan posisi itu manual di aplikasi bila webhook gagal.
3. Cari penyebabnya di `trade_order` — apakah ada baris `SENDING` tanpa `FILLED`?
   Itu berarti order terkirim tetapi responsnya tidak pernah dicatat.
4. **Jangan lanjutkan trading sebelum penyebabnya jelas.** Posisi yatim berarti
   catatan Anda tidak bisa dipercaya, dan semua gerbang berbasis catatan.

---

## 2. Bot tidak mengirim order sama sekali

**Gejala:** `v_ringkasan_harian` menunjukkan 100% penolakan.

```sql
SELECT jsonb_array_elements_text(reasons) AS kode, count(*)
  FROM trade_decision
 WHERE created_at > now() - interval '6 jam' AND NOT approved
 GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

| Kode dominan | Artinya | Tindakan |
|---|---|---|
| `MIROFISH_STALE` | runner tidak menghasilkan verdict baru | lihat §5 |
| `MIROFISH_SCHEMA_INVALID` | bentuk verdict berubah | periksa `mirofish_verdict.raw`, lalu `docs/03` |
| `CIRCUIT_BREAKER_TRIPPED` | breaker aktif | **ini benar.** periksa `halt_reason`, jangan reset terburu-buru |
| `HALTED` | Anda (atau preflight) menghentikannya | cari penyebab awal di email/log |
| `STOP_TOO_FAR` | stop melebihi plafon likuidasi | strategi menghasilkan stop terlalu lebar untuk 50x |
| `NO_TRACK_RECORD` | `stats.win_rate` kosong | sumber statistik mati |
| `BELOW_MIN_NOTIONAL` | ukuran di bawah minimum bursa | ekuitas terlalu kecil untuk config ini |

**Ini biasanya bukan kerusakan — ini gerbang bekerja.** Tingkat penolakan tinggi itu
normal. Yang harus dicurigai adalah tingkat penolakan yang tiba-tiba **rendah**.

---

## 3. Bot mengirim order terlalu banyak

**Gejala:** frekuensi order melonjak; `trade_decision` menunjukkan hampir semua
disetujui.

**Tindakan:**
1. Kill switch.
2. Periksa apakah `config/risk_config.json` berubah: `git log -p config/risk_config.json`.
3. Periksa `bot_state.cfg` — apakah cocok dengan berkas config?
4. **Asumsikan ada gerbang yang mati** sampai terbukti sebaliknya. Ini skenario paling
   berbahaya karena terasa seperti keberhasilan.

---

## 4. Circuit breaker trip

**Gejala:** email `Pionex Guard: HALT`, `bot_state.halted = true`.

```sql
SELECT halt_reason, halted_at, day_start_equity, peak_equity FROM bot_state
 ORDER BY id DESC LIMIT 1;
```

| `halt_reason` | Tindakan |
|---|---|
| `DAILY_LOSS …` | **Berhenti hari itu.** Jangan reset. Itu persis fungsinya. |
| `DRAWDOWN …` | Tinjau `docs/08` §4 — kemungkinan ini aturan berhenti permanen. |
| `CONSEC_LOSSES …` | Periksa apakah strategi cocok dengan rezim pasar saat ini. |
| `DAY_START_EQUITY_INVALID` | `bot_state.day_start_equity` = 0. Isi dengan ekuitas nyata, jangan dibiarkan. |
| `MANUAL_KILL` | Anda sendiri. Selesai. |

Mereset breaker = menghapus satu-satunya mekanisme yang berdiri antara Anda dan
kerugian berulang. Reset hanya setelah penyebabnya diperbaiki **dan** dicatat.

---

## 5. MiroFish mati / verdict basi

**Gejala:** `MIROFISH_STALE` mendominasi, atau `mirofish_verdict.verdict_ts` > 6 jam.

```bash
curl -s -H "Authorization: Bearer $MIROFISH_RUNNER_TOKEN" \
     "$MIROFISH_RUNNER_URL/health"
docker compose logs --tail=100 mirofish-runner
```

**Tindakan:** tidak ada yang perlu diperbaiki di sisi trading. Gerbang akan menolak
semua sinyal (`MIROFISH_STALE`) dan itu **benar** — veto yang tidak punya data bukan
veto yang boleh diabaikan. Perbaiki runner-nya; jangan melonggarkan `max_verdict_age_sec`.

Bila verdict ada tapi selalu `schema_ok = false`:

```sql
SELECT run_id, notes, left(raw::text, 300) FROM (
  SELECT run_id, raw->'notes' AS notes, raw FROM mirofish_verdict
  ORDER BY verdict_ts DESC LIMIT 5) t;
```

`CONFIDENCE_MISSING` berarti keluaran MiroFish berubah bentuk. Perbaiki adapter
(`n8n/code/mirofish_adapter.js` + `mirofish_runner/runner.py` **bersamaan** — keduanya
diuji terhadap `tests/verdict_cases.json`), jangan matikan pemeriksaannya.

---

## 6. n8n mati / tidak responsif

```bash
docker compose ps
docker compose logs --tail=200 n8n
```

**Yang harus disadari:** selama n8n mati, **stop tidak dikelola**. Pionex tidak punya
order stop bawaan, jadi posisi terbuka tidak punya pengaman.

**Tindakan:**
1. Bila ada posisi terbuka dan n8n tidak bisa dipulihkan dalam ~1 menit: **ratakan
   manual dari aplikasi.**
2. Pulihkan n8n, lalu jalankan preflight manual sebelum mengaktifkan apa pun.
3. Preflight akan HALT sendiri karena `HEARTBEAT_STALE` — itu benar. Reset setelah
   Anda yakin tidak ada posisi yang tidak terpantau.

---

## 7. Postgres mati

Semua workflow gagal. **Bot berhenti dengan sendirinya**, dan itu aman: tanpa state,
gerbang tidak bisa menyetujui apa pun.

```bash
docker compose logs --tail=100 postgres
docker compose restart postgres
psql "$DATABASE_URL" -c "SELECT count(*) FROM bot_state;"
```

Jangan menulis ulang skema dengan tangan saat panik. `db/schema.sql` aman diulang
(`IF NOT EXISTS` di mana-mana).

---

## 8. Kunci API bocor

1. **Cabut kunci di aplikasi Pionex sekarang**, sebelum membaca lebih lanjut.
2. Ratakan semua posisi manual.
3. Buat kunci baru — tetap hanya reading + trading, tetap dibatasi IP.
4. Putar juga `N8N_ENCRYPTION_KEY` dan `MIROFISH_RUNNER_TOKEN`.
5. Periksa riwayat order untuk order yang tidak Anda kenali.
6. Karena kunci tidak pernah punya izin transfer, saldo Anda seharusnya utuh. Itu
   bukan kebetulan — itu alasan izin transfer tidak pernah diaktifkan (`docs/07` §4).

---

## 9. Setelah insiden apa pun

1. Tulis tiga baris: apa yang terjadi, kapan, apa yang Anda lakukan. Simpan di
   `docs/` atau issue tracker.
2. Jalankan `make test` sebelum mengaktifkan kembali apa pun.
3. Mulai dari Tahap yang lebih rendah di `docs/08` bila insidennya melibatkan uang
   sungguhan. Menurunkan tahap bukan hukuman; itu satu-satunya cara membuktikan
   perbaikan dengan risiko kecil.
