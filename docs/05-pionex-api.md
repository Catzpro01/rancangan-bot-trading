# 05 — Peta API Pionex Futures, Penandatanganan, dan Idempotensi

Base URL produksi: `https://api.pionex.com`

## 1. Endpoint yang dipakai rancangan ini

### Publik (tanpa tanda tangan)

| Method | Path | Dipakai untuk |
|---|---|---|
| GET | `/api/v1/common/symbols?type=PERP&status=TRADING` | `baseStep`, `quoteStep`, `minNotional`, `minSizeLimit`, `maxSizeLimit`, `liquidationFeeRate` |
| GET | `/api/v1/common/riskTable?symbol=BTC_USDT_PERP` | `rows[].maxLeverage`, `rows[].maintMarginRatio`, `rows[].notionalLimit` |
| GET | `/api/v1/market/klines?symbol=..&interval=..` | OHLCV → ATR, arah |
| GET | `/api/v1/market/tickers?symbol=..` | ticker 24 jam |
| GET | `/api/v1/market/depth?symbol=..&limit=..` | kedalaman buku → estimasi slippage |
| GET | `/api/v1/market/indexes?symbol=..` | index price, mark price, next funding rate |
| GET | `/api/v1/market/indexKlines` / `markKlines` | kline index/mark (perp saja) |
| GET | `/api/v1/market/fundingRates?symbol=..` | riwayat funding |
| GET | `/api/v1/market/trades?symbol=..` | trade terakhir (taker side) |

### Privat — butuh izin **Enable reading**

| Method | Path | Dipakai untuk |
|---|---|---|
| GET | `/uapi/v1/account/balances` | `balances[]` (cross) + `isolates[]` (margin terisolasi per simbol) |
| GET | `/uapi/v1/account/positions?symbol=..` | `markPrice`, `avgPrice`, `netSize`, `unrealizedPnL`, **`liquidationPrice`**, `leverage`, `isolatedMode`, `riskState` |
| GET | `/uapi/v1/account/detail` | ringkasan akun |
| GET | `/uapi/v1/account/leverage` | verifikasi leverage benar-benar 50 |
| GET | `/uapi/v1/account/positionMode` | verifikasi one-way / hedge |
| GET | `/uapi/v1/account/historyPositions` | rekonstruksi PnL untuk statistik |
| GET | `/uapi/v1/trade/isolatedMode` | verifikasi mode margin ISOLATED |
| GET | `/uapi/v1/trade/order` / `openOrders` / `historyOrders` | status order |
| GET | `/uapi/v1/trade/orderByClientOrderId` | cek idempotensi setelah retry |
| GET | `/uapi/v1/trade/fills` / `fillsByOrderId` | konfirmasi fill & harga rata-rata |
| GET | `/uapi/v1/trade/fundingFee` | biaya funding aktual |

### Privat — butuh izin **Enable trading**

| Method | Path | Dipakai untuk |
|---|---|---|
| POST | `/uapi/v1/trade/order` | **satu-satunya** endpoint pembuka/penutup posisi di rancangan ini |
| DELETE | `/uapi/v1/trade/order` | batalkan satu order |
| POST | `/uapi/v1/trade/massOrder` | batch limit (maks 20 order) — hanya untuk exit ladder |
| DELETE | `/uapi/v1/trade/allOrders` | **kill switch** |
| POST | `/uapi/v1/trade/isolateMargin` | tambah/kurangi margin terisolasi (dipakai preflight, jarang) |
| POST | `/uapi/v1/account/leverage` | set leverage — **lihat §5** |
| POST | `/uapi/v1/account/positionMode` | set mode posisi — **lihat §5** |
| POST | `/uapi/v1/trade/isolatedMode` | set mode margin — **lihat §5** |

### Privat — butuh izin **Enable transfer** (TIDAK dipakai)

`POST /api/v1/assets/transfer`. **Key bot tidak boleh punya izin ini.** Bila bot hanya
bisa trading, key yang bocor tidak bisa menarik dana keluar.

## 2. Penandatanganan

Header: `PIONEX-KEY`, `PIONEX-SIGNATURE` (HMAC SHA256 hex). Query `timestamp` dalam
milidetik, **jendela valid ±20 detik** → NTP wajib (ancaman A10).

Proses (sesuai dokumentasi resmi):

1. Susun parameter query (termasuk `timestamp`), **tanpa URL-encoding** untuk nilai yang
   ditandatangani.
2. Urutkan alfabetis berdasarkan key, gabung dengan `&`.
3. `PATH_URL = path + "?" + query_sorted`.
4. Awali dengan METHOD: `payload = METHOD + PATH_URL`.
5. Untuk POST/DELETE, **tempelkan body**; untuk GET, jangan.
6. HMAC SHA256 dengan API secret → hex → header `PIONEX-SIGNATURE`.

Implementasi referensi ada di `risk_engine.sign_payload()` dan diuji:

- `test_query_sorted_alphabetically` — urutan `symbol` sebelum `timestamp`.
- `test_post_signature_includes_body` — body ikut ditandatangani.
- `test_signature_is_stable_hex` — GET **tidak** menyertakan body.

Contoh payload yang ditandatangani:

```
GET/uapi/v1/account/positions?symbol=BTC_USDT_PERP&timestamp=1789246800000
POST/uapi/v1/trade/order?timestamp=1789246800000{"symbol":"BTC_USDT_PERP",...}
```

## 3. Body order

```json
POST /uapi/v1/trade/order?timestamp=<ms>
{
  "clientOrderId": "pg-ENTRY-3f9c1a2b7d4e5f60718293a4",
  "symbol": "BTC_USDT_PERP",
  "positionSide": "BOTH",
  "side": "BUY",
  "type": "LIMIT",
  "size": "0.0058",
  "price": "100000.00",
  "reduceOnly": false
}
```

| Field | Aturan |
|---|---|
| `clientOrderId` | opsional, **maks 64 karakter, hanya alfanumerik dan tanda hubung**, dedup di sisi server **dalam 2 jam** |
| `type` | `LIMIT`, `MARKET_QTY`, `IOC`, `FOK`, `POSTONLY` |
| `size` / `price` | string; wajib untuk limit. **Bulatkan ke `baseStep`/`quoteStep`** |
| `positionSide` | `BOTH` untuk one-way; `LONG`/`SHORT` untuk hedge mode |
| `reduceOnly` | hanya berlaku di one-way (BUYSELL); **wajib `false` di hedge mode** |

Respons: `{ "result": true, "timestamp": ..., "data": { "orderId": 1 } }`. Order bersifat
**asinkron** — `orderId` belum berarti terisi. Konfirmasi lewat `fillsByOrderId`.

### Tidak ada stop-market native

Daftar `type` di atas **tidak** memuat stop/trigger order. Karena itu stop-loss di
rancangan ini adalah **stop yang dikelola klien**: `Position Monitor` membandingkan
`markPrice` dengan `stop` tiap 15 detik dan mengirim order penutup.

Konsekuensinya harus diterima secara jujur:

- Ada jeda hingga ~15 detik antara harga menyentuh stop dan order terkirim.
- Karena itu **buffer likuidasi 33%** dan **margin ≤ 5% ekuitas** bukan pilihan estetika —
  keduanya adalah kompensasi atas tidak adanya stop di sisi bursa.
- Jangan pernah mengandalkan "bursa akan menghentikan saya".

Alternatif yang memperpendek jeda: kurangi interval monitor menjadi 5 detik (perhatikan
rate limit), atau gunakan `massOrder` untuk memasang ladder exit limit di beberapa tingkat
harga saat entry.

## 4. Idempotensi (ancaman A3)

`clientOrderId` dibangun deterministik:

```python
client_order_id(signal_id, symbol, action)  # -> "pg-ENTRY-<sha256[:24]>"
```

Sifat yang diuji (`test_client_order_id_deterministic_and_legal`):

- signal + symbol + action sama → ID sama (retry aman).
- signal berbeda → ID berbeda.
- `ENTRY` vs `EXIT` → ID berbeda.
- Panjang ≤ 64, hanya alfanumerik + `-`.

Alur wajib saat mengirim order:

```
1. INSERT ke trade_order (status=SENDING, client_order_id, request_hash)
2. POST /uapi/v1/trade/order
3. bila HTTP error / timeout:
     GET /uapi/v1/trade/orderByClientOrderId
       -> ada  => order sebenarnya masuk; lanjut ke langkah 4
       -> 404  => aman untuk mengirim ulang dengan clientOrderId yang SAMA
4. poll fillsByOrderId sampai filled / expired (maks 30 detik)
5. UPDATE trade_order (status, filled_size, avg_price)
```

Langkah 3 adalah yang paling sering dilewati orang: timeout **bukan** berarti gagal.

## 5. Setelan akun: manual, bukan otomatis

Rancangan ini **membaca** leverage/positionMode/isolatedMode tiap preflight dan menolak
trading bila tidak sesuai harapan — tetapi **tidak menulis** setelan itu secara otomatis.

Alasan: endpoint `POST /uapi/v1/account/leverage`, `positionMode`, `isolatedMode` mampu
mengubah profil risiko seluruh akun. Bug kecil di workflow bisa mengubah akun dari
isolated 50x menjadi cross 5x tanpa Anda sadari. Lebih aman:

1. Set sekali lewat aplikasi Pionex: **Isolated**, **One-way (BUYSELL)**, **50x**.
2. Preflight memverifikasi tiap 30 detik.
3. Bila tidak sesuai → `HALTED` + notifikasi, bukan "perbaiki sendiri".

## 6. Presisi & batas instrumen

Ambil dari `/api/v1/common/symbols` dan patuhi:

| Field | Efek bila dilanggar |
|---|---|
| `baseStep` | order ditolak |
| `quoteStep` | order ditolak |
| `minNotional` | order ditolak |
| `minSizeLimit` / `maxSizeLimit` | order ditolak |
| `maxOrderNum` | terlalu banyak order terbuka |
| `riskTable.notionalLimit` | melewati batas → **naik tier risiko → `maxLeverage` turun dan `maintMarginRatio` naik** |

Baris terakhir penting: menambah ukuran posisi bisa **menurunkan leverage maksimum yang
diizinkan** dan **mendekatkan harga likuidasi**. Preflight membaca `riskTable` tiap siklus
dan menolak sinyal yang notional-nya akan mendorong posisi ke tier berikutnya
(`LEVERAGE_CONFIG_INVALID` bila `cfg.leverage > maxLeverage` tier aktif).

## 7. Rate limit & backoff

Dokumentasi menandai bobot per endpoint (`Weight: 5` untuk mayoritas). Rancangan ini:

- Token bucket sederhana di Code node `pionex_sign.js`: maksimum N request/detik per
  workflow, antre sisanya.
- HTTP 429 → backoff eksponensial dengan jitter (1s, 2s, 4s, maksimum 3 percobaan),
  hormati `Retry-After` bila ada.
- HTTP 5xx → **jangan** retry untuk POST order tanpa langkah idempotensi §4.
- Semua kegagalan HTTP dicatat ke `api_call_log` bersama `request_hash` (tanpa rahasia).

## 8. Yang belum terverifikasi dari lingkungan ini

Sandbox penulisan rancangan tidak punya akses keluar ke `api.pionex.com`
(`curl -m 15` mengembalikan kode HTTP `000`, tanpa body). Akibatnya nilai berikut adalah
**asumsi bertanda**, bukan fakta terukur:

| Item | Status |
|---|---|
| `maintMarginRatio` per simbol/tier | ambil dari `/api/v1/common/riskTable` saat runtime |
| `maxLeverage` per simbol/tier | idem |
| Fee maker/taker tier akun Anda | cek di aplikasi; berbagai sumber pihak ketiga menyebut 0.02% / 0.05% untuk futures |
| Ada/tidaknya testnet futures Pionex | **tidak ditemukan**; `docs/08` memakai mode paper sebagai gantinya |
| Interval funding pasti (8 jam?) | ambil dari `/api/v1/market/fundingRates` |

Semua asumsi ini dikumpulkan di satu tempat (`config/risk_config.json` +
`ExchangeRules`) supaya menggantinya adalah satu suntingan, bukan perburuan.
