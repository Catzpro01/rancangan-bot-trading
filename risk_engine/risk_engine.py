"""
risk_engine.py -- Kernel risiko untuk "Pionex Guard 50x" (n8n + MiroFish).

Prinsip desain
--------------
1. **Default DENY.** Tidak ada order yang boleh keluar dari sistem tanpa melewati
   `validate_signal()`. Setiap alasan penolakan dicatat dan diaudit.
2. **Leverage 50x tidak pernah berarti "efektif 50x".** Leverage di setelan bursa
   hanya menentukan *margin awal*. Notional efektif dibatasi oleh `max_effective_leverage`
   dan margin per posisi dibatasi oleh *anggaran rugi harian*, sehingga satu likuidasi
   total tidak bisa menghancurkan akun.
3. **Likuidasi bukan exit plan.** Stop-loss selalu dipasang di *dalam* jarak likuidasi,
   dengan buffer `liq_buffer_pct`.
4. **Sumber kebenaran likuidasi adalah bursa.** Angka lokal di sini adalah estimator;
   `assert_local_liq_matches_exchange()` memaksa sistem membandingkan estimasi lokal
   dengan `liquidationPrice` dari `GET /uapi/v1/account/positions` sebelum entry.

Modul ini sengaja tanpa dependensi eksternal agar bisa dijalankan di n8n Code node
(port JS ada di `n8n/code/`) dan di unit test.

Semua harga/ukuran memakai `Decimal`-like float; pembulatan ke presisi bursa ada di
`round_step()`. Jangan pernah mengirim angka mentah ke Pionex.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------------------
# Konstanta default (nilai produksi ada di config/risk_config.json)
# --------------------------------------------------------------------------------------

#: Fee taker futures Pionex yang umum dipublikasikan (0.05%). Sumber pihak ketiga --
#: WAJIB dicek ulang di aplikasi Pionex Anda (tier VIP mengubah angka ini).
DEFAULT_TAKER_FEE = 0.0005
#: Fee maker futures Pionex yang umum dipublikasikan (0.02%).
DEFAULT_MAKER_FEE = 0.0002
#: Fallback conservatif maintenance margin ratio. Angka sesungguhnya per-simbol
#: diambil dari GET /api/v1/common/riskTable -> maintMarginRatio.
DEFAULT_MMR_FALLBACK = 0.005
#: Toleransi selisih estimasi likuidasi lokal vs bursa (fraksi dari harga entry).
LIQ_PRICE_TOLERANCE = 0.002  # 0.2%


class RiskReject(Exception):
    """Dilempar bila sebuah order lolos paksa tanpa persetujuan guard."""


# --------------------------------------------------------------------------------------
# Struktur data
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExchangeRules:
    """Aturan instrumen dari GET /api/v1/common/symbols (+ riskTable)."""

    symbol: str
    base_step: float = 0.001          # step qty (baseStep)
    quote_step: float = 0.01          # step harga (quoteStep)
    min_size: float = 0.0             # minSizeLimit
    max_size: float = 1e12            # maxSizeLimit
    min_notional: float = 5.0         # minNotional (USDT)
    max_leverage: float = 50.0        # maxLeverage dari riskTable tier aktif
    mmr: float = DEFAULT_MMR_FALLBACK # maintMarginRatio tier aktif
    taker_fee: float = DEFAULT_TAKER_FEE
    maker_fee: float = DEFAULT_MAKER_FEE
    liquidation_fee_rate: float = 0.0 # liquidationFeeRate dari /common/symbols


@dataclass(frozen=True)
class RiskConfig:
    """Kebijakan risiko. Ini satu-satunya tempat angka risiko boleh diubah."""

    # --- sizing ---
    risk_per_trade_pct: float = 0.5           # % ekuitas yang hilang bila stop kena
    max_effective_leverage: float = 3.0       # notional / ekuitas (BUKAN leverage bursa)
    max_margin_per_trade_pct: float = 5.0     # margin terisolasi max, % dari ekuitas
    max_open_positions: int = 1
    slippage_buffer_pct: float = 0.05         # buffer slippage utk memperlebar jarak stop

    # --- likuidasi ---
    leverage: int = 50                        # leverage yang di-set di bursa
    liq_buffer_pct: float = 33.0              # stop harus >= 33% lebih dekat dari likuidasi

    # --- gate sinyal ---
    max_signal_age_sec: int = 900             # sinyal eksekusi basi = veto
    max_verdict_age_sec: int = 6 * 3600       # verdict MiroFish basi = veto
    min_gate_score: float = 0.55
    min_confidence: float = 0.60
    veto_if_high_event_risk: bool = True
    veto_on_negative_ev: bool = True
    min_rr_ratio: float = 1.1                 # reward:risk minimum (lihat max_usable_rr)

    # --- circuit breaker ---
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_consecutive_losses: int = 3
    cooldown_minutes_after_trip: int = 240

    # --- biaya ---
    funding_rate_per_interval: float = 0.0001  # 0.01% per 8 jam (periksa fundingRates)
    funding_intervals_per_day: int = 3
    max_hold_hours: float = 24.0


@dataclass
class Position:
    symbol: str
    side: str                  # "LONG" | "SHORT"
    entry: float
    size: float                # qty base
    stop: float
    take_profit: float
    highest: float             # utk trailing (LONG)
    lowest: float              # utk trailing (SHORT)
    opened_at: datetime
    liq_price: float | None = None


@dataclass
class Decision:
    approved: bool
    reasons: list[str]
    size: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    stop_distance_pct: float = 0.0
    liq_distance_pct: float = 0.0
    risk_amount: float = 0.0
    rr_ratio: float = 0.0
    ev_per_trade_pct: float = 0.0
    effective_leverage: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reasons": self.reasons,
            "size": round(self.size, 12),
            "notional": round(self.notional, 8),
            "margin": round(self.margin, 8),
            "stop_distance_pct": round(self.stop_distance_pct, 6),
            "liq_distance_pct": round(self.liq_distance_pct, 6),
            "risk_amount": round(self.risk_amount, 8),
            "rr_ratio": round(self.rr_ratio, 4),
            "ev_per_trade_pct": round(self.ev_per_trade_pct, 6),
            "effective_leverage": round(self.effective_leverage, 6),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------------------
# Matematika likuidasi (estimasi lokal, margin terisolasi)
# --------------------------------------------------------------------------------------


def liquidation_distance_pct(
    leverage: float,
    mmr: float = DEFAULT_MMR_FALLBACK,
    taker_fee: float = DEFAULT_TAKER_FEE,
    side: str = "LONG",
) -> float:
    """Jarak harga (fraksi) dari ENTRY ke harga likuidasi, margin ISOLATED.

        room = 1/L - MMR - fee     (1.45% di 50x; kerugian thd NOTIONAL = 72.5% margin)
        LONG : d = (room + fee) / (1 + fee)  -> 1.49925%   (liq di BAWAH entry)
        SHORT: d = (room + fee) / (1 - fee)  -> 1.50075%   (liq di ATAS entry)
    Perhatikan pembilangnya `room + fee`, bukan `room`: fee likuidasi yang dikenakan
    pada notional di HARGA LIKUIDASI ikut menambah jarak dari entry. Menghilangkan
    `+ fee` membuat estimator ~0.05% terlalu optimis -- cukup untuk menaruh stop di
    sisi yang salah dari harga likuidasi.

    `room` adalah kerugian terhadap NOTIONAL saat likuidasi (= 72.5% margin di 50x),
    BUKAN jarak dari entry. Jarak dari entry sedikit lebih besar karena fee likuidasi
    dikenakan pada notional di HARGA LIKUIDASI. Dengan definisi ini berlaku persis:

        liquidation_price(side, entry, ...) == entry * (1 -/+ d)

    dan keduanya sama dengan bentuk aljabar dari persamaan margin:
        LONG : entry * (1 - 1/L + MMR + fee) / (1 + fee)
        SHORT: entry * (1 + 1/L - MMR - fee) / (1 - fee)

    Peringatan: (1 - room)/(1 + fee) TIDAK sama dengan 1 - room/(1 + fee); selisihnya
    ~0.05% harga -- cukup untuk menaruh stop di sisi yang salah dari harga likuidasi.
    """
    if leverage <= 0:
        raise ValueError("leverage harus > 0")
    if side.upper() not in ("LONG", "SHORT"):
        raise ValueError("side harus LONG atau SHORT")
    room = 1.0 / leverage - mmr - taker_fee
    if room <= 0:
        raise ValueError(
            f"leverage {leverage}x dengan mmr={mmr} fee={taker_fee} "
            "tidak menyisakan ruang likuidasi positif"
        )
    # Identitas yang HARUS holds (ada unit test yang menegakkannya):
    #   LONG : (room+fee)/(1+fee) == 1 - (1-room)/(1+fee)
    #   SHORT: (room+fee)/(1-fee) == (1+room)/(1-fee) - 1
    if side.upper() == "SHORT":
        return (room + taker_fee) / (1.0 - taker_fee)
    return (room + taker_fee) / (1.0 + taker_fee)


def liquidation_room_pct(
    leverage: float,
    mmr: float = DEFAULT_MMR_FALLBACK,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """`room = 1/L - MMR - fee` tanpa penyesuaian arah (dipakai untuk plafon stop)."""
    return 1.0 / leverage - mmr - taker_fee


def min_win_rate_for_profit(
    rr_ratio: float,
    leverage: float,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """Win rate minimum agar EV > 0 pada RR tertentu (satuan: pergerakan harga).

    wr_min = (1 + biaya) / (1 + RR), dengan `biaya` = 2*fee dinyatakan dalam SATUAN
    YANG SAMA dengan avg_win/avg_loss (di sini: persen, jadi 0.10 bukan 0.001).
    Tidak bergantung leverage. Fee taker 0.05% -> RR 1.1: 52.42%, RR 1.6: 42.31%,
    RR 2: 36.67%, RR 4: 22.00%.

    Hati-hati satuan: inilah bug yang paling mudah terjadi di kode trading.
    """
    cost = breakeven_move_pct(taker_fee) * 100.0  # fraksi -> persen
    return (1.0 + cost) / (1.0 + rr_ratio)


def max_usable_rr(
    leverage: float,
    stop_distance_pct: float,
    mmr: float = DEFAULT_MMR_FALLBACK,
    taker_fee: float = DEFAULT_TAKER_FEE,
    liq_buffer_pct: float = 33.0,
    target_frac_of_room: float = 0.9,
) -> float:
    """RR maksimum yang MASUK AKAL pada leverage tertentu.

    Di 50x jarak likuidasi hanya 1.449%, dan stop wajib >= 33% lebih dekat dari
    likuidasi -> plafon stop 0.971%. Target profit paling jauh yang masih waras
    adalah 90% dari jarak likuidasi (1.304%), sehingga RR maksimum ~1.63.
    Bandingkan dengan 10x: plafon stop 6.33%, RR maksimum ~14.

    Inilah batasan sesungguhnya dari leverage tinggi: bukan biaya, tapi tidak
    adanya ruang untuk target profit. `min_rr_ratio` di config tidak boleh
    melebihi angka ini (ada test yang menegakkannya).
    """
    room = liquidation_distance_pct(leverage, mmr, taker_fee)
    max_stop = room * (1.0 - liq_buffer_pct / 100.0)
    if stop_distance_pct <= 0 or stop_distance_pct > max_stop:
        return 0.0
    return (room * target_frac_of_room) / stop_distance_pct


def liquidation_price(
    side: str,
    entry: float,
    leverage: float,
    mmr: float = DEFAULT_MMR_FALLBACK,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """Estimasi harga likuidasi (margin terisolasi).

    Dihitung lewat `liquidation_distance_pct()` sehingga IDENTIK dengannya:
        liq == entry * (1 - d)   untuk LONG
        liq == entry * (1 + d)   untuk SHORT
    (Bentuk aljabar setara: LONG entry*(1 - 1/L + MMR + fee)/(1 + fee).)

    SELALU bandingkan dengan field `liquidationPrice` dari
    GET /uapi/v1/account/positions sebelum entry.
    """
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side harus LONG atau SHORT")
    # Dihitung dari bentuk aljabar persamaan margin, yang secara identitas sama dengan
    # entry * (1 -/+ liquidation_distance_pct(...)). Uji silang ada di unit test.
    imr = 1.0 / leverage
    if side == "LONG":
        return entry * (1.0 - imr + mmr + taker_fee) / (1.0 + taker_fee)
    return entry * (1.0 + imr - mmr - taker_fee) / (1.0 - taker_fee)


def assert_local_liq_matches_exchange(
    side: str,
    entry: float,
    local_liq: float,
    exchange_liq: float,
    tolerance: float = LIQ_PRICE_TOLERANCE,
) -> tuple[bool, str]:
    """Guard: jangan entry bila estimator likuidasi kita melenceng dari bursa.

    Untuk LONG kita menuntut estimasi lokal TIDAK lebih optimis dari bursa
    (local_liq >= exchange_liq - toleransi). Untuk SHORT sebaliknya.
    """
    if exchange_liq <= 0:
        return False, "EXCHANGE_LIQ_MISSING"
    drift = abs(local_liq - exchange_liq) / entry
    if drift > tolerance:
        return False, f"LIQ_MISMATCH drift={drift:.5f} tol={tolerance}"
    side = side.upper()
    if side == "LONG" and local_liq < exchange_liq * (1 - tolerance):
        return False, "LOCAL_LIQ_TOO_OPTIMISTIC_LONG"
    if side == "SHORT" and local_liq > exchange_liq * (1 + tolerance):
        return False, "LOCAL_LIQ_TOO_OPTIMISTIC_SHORT"
    return True, "OK"


# --------------------------------------------------------------------------------------
# Presisi instrumen
# --------------------------------------------------------------------------------------


def round_step(value: float, step: float, mode: str = "down") -> float:
    """Bulatkan ke kelipatan step. Default 'down' -- tidak pernah melebihkan ukuran."""
    if step <= 0:
        return float(value)
    n = math.floor(value / step + 1e-12) if mode == "down" else math.ceil(value / step - 1e-12)
    return round(n * step, 12)


def cost_of_trade(notional: float, taker_fee: float = DEFAULT_TAKER_FEE) -> float:
    """Biaya satu putaran penuh (entry taker + exit taker)."""
    return notional * taker_fee * 2.0


def breakeven_move_pct(taker_fee: float = DEFAULT_TAKER_FEE) -> float:
    """Pergerakan harga minimum (fraksi) untuk balik modal biaya round-trip.

    Tidak bergantung leverage: biaya proporsional terhadap notional.
    """
    return taker_fee * 2.0


# --------------------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------------------


def size_position(
    equity: float,
    entry: float,
    stop_distance_pct: float,
    rules: ExchangeRules,
    cfg: RiskConfig,
) -> dict[str, Any]:
    """Hitung qty dari anggaran risiko, lalu jepit dengan semua batas.

    Mengembalikan dict: qty, notional, margin, risk_amount, binding_constraint.
    """
    if equity <= 0 or entry <= 0:
        return {"qty": 0.0, "reason": "EQUITY_OR_ENTRY_INVALID"}
    if stop_distance_pct <= 0:
        return {"qty": 0.0, "reason": "STOP_DISTANCE_INVALID"}

    eff_stop = stop_distance_pct + cfg.slippage_buffer_pct / 100.0
    risk_amount = equity * cfg.risk_per_trade_pct / 100.0

    # 1) Batas risiko: qty = risk / (entry * jarak stop efektif)
    qty_risk = risk_amount / (entry * eff_stop)
    # 2) Batas leverage efektif: notional <= equity * max_effective_leverage
    qty_lev = equity * cfg.max_effective_leverage / entry
    # 3) Batas margin per trade: margin = notional / leverage_bursa
    qty_margin = (equity * cfg.max_margin_per_trade_pct / 100.0) * cfg.leverage / entry

    qty = min(qty_risk, qty_lev, qty_margin)
    binding = min(
        (("RISK_BUDGET", qty_risk), ("EFFECTIVE_LEVERAGE", qty_lev), ("MARGIN_CAP", qty_margin)),
        key=lambda kv: kv[1],
    )[0]

    # Jaring pengaman terakhir: apa pun yang dihitung di atas, notional tidak boleh
    # melewati plafon leverage efektif.
    qty_ceiling = equity * cfg.max_effective_leverage / entry
    if qty >= qty_ceiling:
        qty = qty_ceiling
        binding = "EFFECTIVE_LEVERAGE_CEILING"

    qty = round_step(qty, rules.base_step, "down")
    qty = min(qty, rules.max_size)
    notional = qty * entry
    margin = notional / cfg.leverage
    return {
        "qty": qty,
        "notional": notional,
        "margin": margin,
        "risk_amount": qty * entry * eff_stop,
        "stop_distance_effective_pct": eff_stop,
        "binding_constraint": binding,
    }


# --------------------------------------------------------------------------------------
# Gate sinyal
# --------------------------------------------------------------------------------------


def _age_sec(iso_ts: str | None, now: datetime) -> float:
    if not iso_ts:
        return float("inf")
    ts = iso_ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds())


def expected_value_pct(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    cost_pct: float,
) -> float:
    """EV per trade dalam satuan PERGERAKAN HARGA (fraksi), dipotong biaya round-trip.

    `avg_win_pct` / `avg_loss_pct` adalah jarak harga (mis. 0.008 = 0.8% dari entry),
    BUKAN persentase margin. Karena itu `cost_pct` juga harus pergerakan harga
    (= 2 * taker_fee), tanpa dikalikan leverage.

    Catatan penting: EV dalam satuan harga TIDAK bergantung leverage. Yang leverage
    ubah adalah (a) berapa % margin yang dipertaruhkan dan (b) seberapa dekat harga
    likuidasi -- bukan apakah strateginya profit.
    """
    return (win_rate * avg_win_pct - (1.0 - win_rate) * avg_loss_pct) - cost_pct


def breakeven_win_rate(
    avg_win_pct: float,
    avg_loss_pct: float,
    leverage: float,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """Win rate minimum agar EV >= 0 setelah biaya round-trip.

    Semua argumen jarak dalam satuan pergerakan harga. `leverage` diterima agar
    pemanggil sadar konteksnya, tetapi TIDAK memengaruhi hasil: EV per satuan harga
    bebas leverage. wr_min = (avg_loss + 2*fee) / (avg_win + avg_loss).
    """
    cost = breakeven_move_pct(taker_fee)
    denom = avg_win_pct + avg_loss_pct
    if denom <= 0:
        raise ValueError("avg_win_pct + avg_loss_pct harus > 0")
    return (avg_loss_pct + cost) / denom


def validate_signal(
    signal: dict[str, Any],
    equity: float,
    entry: float,
    rules: ExchangeRules,
    cfg: RiskConfig,
    *,
    open_positions: int = 0,
    breaker: dict[str, Any] | None = None,
    now: datetime | None = None,
    stats: dict[str, float] | None = None,
) -> Decision:
    """Satu-satunya gerbang menuju order. Default DENY.

    `signal` yang diharapkan (lihat docs/03 & n8n/code/mirofish_adapter.js):
      {
        "direction": "LONG"|"SHORT"|"NONE",
        "entry": 65000, "stop": 64480, "take_profit": 66040,
        "ts": "2026-09-13T10:00:00Z",
        "mirofish": {
            "verdict_ts": "...", "confidence": 0.0-1.0, "event_risk": "LOW|MEDIUM|HIGH",
            "bias": "LONG|SHORT|NEUTRAL", "run_id": "...", "schema_ok": true
        },
        "stats": {"win_rate": 0.52, "avg_win_pct": 2.0, "avg_loss_pct": 1.0}
      }
    """
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    warnings: list[str] = []
    stats = stats or signal.get("stats") or {}

    direction = str(signal.get("direction", "NONE")).upper()
    if direction not in ("LONG", "SHORT"):
        reasons.append("DIRECTION_NONE")

    # --- Gate 1: MiroFish (risiko peristiwa / veto) -----------------------------------
    miro = signal.get("mirofish") or {}
    if not miro.get("schema_ok"):
        reasons.append("MIROFISH_SCHEMA_INVALID")
    verdict_age = _age_sec(miro.get("verdict_ts"), now)
    if verdict_age > cfg.max_verdict_age_sec:
        reasons.append(f"MIROFISH_STALE age={verdict_age:.0f}s")
    conf = float(miro.get("confidence", 0.0) or 0.0)
    if conf < cfg.min_confidence:
        reasons.append(f"MIROFISH_LOW_CONFIDENCE {conf:.2f}<{cfg.min_confidence}")
    event_risk = str(miro.get("event_risk", "UNKNOWN")).upper()
    if cfg.veto_if_high_event_risk and event_risk == "HIGH":
        reasons.append("MIROFISH_HIGH_EVENT_RISK_VETO")
    bias = str(miro.get("bias", "NEUTRAL")).upper()
    if bias not in ("NEUTRAL", direction) and direction in ("LONG", "SHORT"):
        reasons.append(f"MIROFISH_BIAS_CONFLICT {bias} vs {direction}")

    # --- Gate 2: eksekusi / struktur trade -------------------------------------------
    try:
        stop = float(signal["stop"])
        tp = float(signal["take_profit"])
    except (KeyError, TypeError, ValueError):
        stop = tp = float("nan")
    if not (math.isfinite(stop) and math.isfinite(tp)) or stop <= 0 or tp <= 0:
        reasons.append("MISSING_STOP_OR_TP")
        stop_distance = float("nan")
        rr = 0.0
    else:
        if direction == "LONG":
            stop_distance = (entry - stop) / entry
            reward = (tp - entry) / entry
        else:
            stop_distance = (stop - entry) / entry
            reward = (entry - tp) / entry
        if stop_distance <= 0:
            reasons.append("STOP_ON_WRONG_SIDE")
        if reward <= 0:
            reasons.append("TP_ON_WRONG_SIDE")
        rr = (reward / stop_distance) if stop_distance > 0 else 0.0
        if rr < cfg.min_rr_ratio:
            reasons.append(f"RR_TOO_LOW {rr:.2f}<{cfg.min_rr_ratio}")

    sig_age = _age_sec(signal.get("ts"), now)
    if sig_age > cfg.max_signal_age_sec:
        reasons.append(f"SIGNAL_STALE age={sig_age:.0f}s")

    # --- Gate 3: likuidasi & jarak stop ----------------------------------------------
    try:
        liq_dist = liquidation_distance_pct(
            cfg.leverage, rules.mmr, rules.taker_fee, side=direction
        )
    except ValueError as exc:
        reasons.append(f"LEVERAGE_CONFIG_INVALID {exc}")
        liq_dist = float("nan")
    if math.isfinite(liq_dist) and math.isfinite(stop_distance):
        max_allowed_stop = liq_dist * (1.0 - cfg.liq_buffer_pct / 100.0)
        if stop_distance >= max_allowed_stop:
            reasons.append(
                f"STOP_TOO_FAR {stop_distance:.4%}>={max_allowed_stop:.4%} "
                f"(liq={liq_dist:.4%}, buffer={cfg.liq_buffer_pct}%)"
            )

    # --- Gate 4: statistik & EV -------------------------------------------------------
    win_rate = float(stats.get("win_rate", 0.0) or 0.0)
    avg_win = float(stats.get("avg_win_pct", 0.0) or 0.0)
    avg_loss = float(stats.get("avg_loss_pct", 1.0) or 1.0)
    # Biaya round-trip dalam satuan pergerakan harga: 2 * taker_fee = 0.10%.
    # (Dalam satuan margin angka ini = 0.10% * leverage, yaitu 5% margin di 50x --
    #  besar, tetapi itu soal berapa margin yang dipertaruhkan, bukan soal EV.)
    cost_pct = breakeven_move_pct(rules.taker_fee)
    ev = expected_value_pct(win_rate, avg_win, avg_loss, cost_pct)
    if win_rate <= 0:
        reasons.append("NO_TRACK_RECORD")
    elif cfg.veto_on_negative_ev and ev <= 0:
        reasons.append(f"NEGATIVE_EV {ev:.5f}")
    # Statistik backtest harus merepresentasikan BENTUK trade ini. avg_loss yang lebih
    # besar dari jarak stop berarti angka itu berasal dari trade lain -> EV-nya bohong.
    if math.isfinite(stop_distance) and stop_distance > 0 and avg_loss > stop_distance * 1.05:
        reasons.append(
            f"STATS_INCONSISTENT avg_loss={avg_loss:.5f} > stop_distance={stop_distance:.5f}"
        )
    if avg_win > 0 and reward > 0 and avg_win > reward * 1.05:
        reasons.append(
            f"STATS_INCONSISTENT avg_win={avg_win:.5f} > reward={reward:.5f}"
        )

    # --- Gate 5: kapasitas & circuit breaker -----------------------------------------
    if open_positions >= cfg.max_open_positions:
        reasons.append(f"MAX_OPEN_POSITIONS {open_positions}>={cfg.max_open_positions}")
    breaker = breaker or {}
    if breaker.get("tripped"):
        reasons.append(f"CIRCUIT_BREAKER_TRIPPED {breaker.get('reason', '')}")

    # --- Sizing -----------------------------------------------------------------------
    sizing: dict[str, Any] = {}
    if math.isfinite(stop_distance) and stop_distance > 0:
        sizing = size_position(equity, entry, stop_distance, rules, cfg)
    qty = float(sizing.get("qty", 0.0))
    notional = float(sizing.get("notional", 0.0))
    if qty <= 0:
        reasons.append("SIZE_ZERO")
    else:
        if qty < rules.min_size:
            reasons.append(f"BELOW_MIN_SIZE {qty}<{rules.min_size}")
        if notional < rules.min_notional:
            reasons.append(f"BELOW_MIN_NOTIONAL {notional}<{rules.min_notional}")
        if notional > equity * cfg.max_effective_leverage:
            reasons.append("EFFECTIVE_LEVERAGE_BREACH")

    # --- Peringatan (tidak memblokir) -------------------------------------------------
    if math.isfinite(liq_dist) and liq_dist < 0.02:
        warnings.append(
            f"LIQ_DISTANCE_TINY {liq_dist:.3%} -- satu candle news bisa menembus stop"
        )
    if conf < 0.75 and direction in ("LONG", "SHORT"):
        warnings.append("MIROFISH_CONFIDENCE_MARGINAL")
    if rules.taker_fee > 0.0005:
        warnings.append("HIGH_TAKER_FEE")

    approved = not reasons
    return Decision(
        approved=approved,
        reasons=reasons,
        size=qty if approved else 0.0,
        notional=notional if approved else 0.0,
        margin=float(sizing.get("margin", 0.0)) if approved else 0.0,
        stop_distance_pct=stop_distance if math.isfinite(stop_distance) else 0.0,
        liq_distance_pct=liq_dist if math.isfinite(liq_dist) else 0.0,
        risk_amount=float(sizing.get("risk_amount", 0.0)) if approved else 0.0,
        rr_ratio=rr,
        ev_per_trade_pct=ev,
        effective_leverage=(notional / equity) if (approved and equity > 0) else 0.0,
        warnings=warnings,
    )


# --------------------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------------------


def evaluate_breaker(
    day_start_equity: float,
    equity: float,
    peak_equity: float,
    consecutive_losses: int,
    cfg: RiskConfig,
    last_trip_ts: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Circuit breaker berlapis: rugi harian, drawdown total, loss beruntun."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"tripped": False, "reason": "", "cooldown_until": None}

    if day_start_equity <= 0:
        out.update(tripped=True, reason="DAY_START_EQUITY_INVALID")
        return out

    day_pnl_pct = (equity - day_start_equity) / day_start_equity * 100.0
    dd_pct = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
    out.update(
        day_pnl_pct=round(day_pnl_pct, 4),
        drawdown_pct=round(dd_pct, 4),
        consecutive_losses=consecutive_losses,
    )

    if day_pnl_pct <= -cfg.max_daily_loss_pct:
        out.update(tripped=True, reason=f"DAILY_LOSS {day_pnl_pct:.2f}%")
    elif dd_pct >= cfg.max_drawdown_pct:
        out.update(tripped=True, reason=f"DRAWDOWN {dd_pct:.2f}%")
    elif consecutive_losses >= cfg.max_consecutive_losses:
        out.update(tripped=True, reason=f"CONSEC_LOSSES {consecutive_losses}")

    if out["tripped"]:
        cooldown = cfg.cooldown_minutes_after_trip * 60
        until = now.timestamp() + cooldown
        out["cooldown_until"] = until
        if last_trip_ts is not None and now.timestamp() < until:
            out["hard_stop"] = True
    return out


# --------------------------------------------------------------------------------------
# Manajemen posisi
# --------------------------------------------------------------------------------------


def atr_stop(entry: float, side: str, atr: float, multiplier: float = 1.5) -> float:
    """Stop awal berbasis ATR (dipakai saat signal engine memberi ATR, bukan stop eksplisit)."""
    if atr <= 0 or multiplier <= 0:
        raise ValueError("atr dan multiplier harus > 0")
    return entry - atr * multiplier if side.upper() == "LONG" else entry + atr * multiplier


def trailing_stop(
    pos: Position,
    mark_price: float,
    atr: float,
    atr_multiplier: float = 2.0,
) -> Position:
    """Trailing stop berbasis ATR. Hanya bergerak searah profit (ratchet)."""
    if pos.side.upper() == "LONG":
        pos.highest = max(pos.highest, mark_price)
        candidate = pos.highest - atr * atr_multiplier
        if candidate > pos.stop:
            pos.stop = candidate
    else:
        pos.lowest = min(pos.lowest, mark_price) if pos.lowest > 0 else mark_price
        candidate = pos.lowest + atr * atr_multiplier
        if 0 < candidate < pos.stop:
            pos.stop = candidate
    return pos


def should_exit(pos: Position, mark_price: float, now: datetime, cfg: RiskConfig) -> tuple[bool, str]:
    """Exit rules: stop, take profit, time stop. Likuidasi TIDAK pernah jadi rencana."""
    if pos.side.upper() == "LONG":
        if mark_price <= pos.stop:
            return True, "STOP_LOSS"
        if mark_price >= pos.take_profit:
            return True, "TAKE_PROFIT"
    else:
        if mark_price >= pos.stop:
            return True, "STOP_LOSS"
        if mark_price <= pos.take_profit:
            return True, "TAKE_PROFIT"
    held_h = (now - pos.opened_at).total_seconds() / 3600.0
    if held_h >= cfg.max_hold_hours:
        return True, "TIME_STOP"
    return False, "HOLD"


def funding_cost_estimate(
    notional: float,
    side: str,
    funding_rate: float,
    hours_held: float,
    intervals_per_day: int = 3,
) -> float:
    """Perkiraan biaya/penerimaan funding. LONG membayar saat funding positif."""
    intervals = hours_held / 24.0 * intervals_per_day
    signed = funding_rate if side.upper() == "LONG" else -funding_rate
    return notional * signed * intervals


# --------------------------------------------------------------------------------------
# Idempotensi order
# --------------------------------------------------------------------------------------


def client_order_id(signal_id: str, symbol: str, action: str) -> str:
    """clientOrderId deterministik (Pionex dedup 2 jam, max 64 char, alfanumerik + '-').

    Deterministik = retry n8n tidak akan pernah membuka posisi ganda.
    """
    raw = f"{signal_id}|{symbol}|{action}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    safe_action = "".join(ch for ch in action.upper() if ch.isalnum())[:8] or "ACT"
    return f"pg-{safe_action}-{digest}"[:64]


def sign_payload(method: str, path: str, query: dict[str, Any], body: str, secret: str) -> str:
    """Implementasi referensi penandatanganan Pionex (docs: futures-api/authentication).

    1. query (termasuk timestamp) diurutkan alfabetis, digabung '&', tanpa URL-encoding
    2. PATH_URL = path + '?' + query_sorted
    3. payload = METHOD + PATH_URL (+ body untuk POST/DELETE)
    4. HMAC-SHA256 hex dengan API secret
    """
    items = sorted((str(k), str(v)) for k, v in query.items())
    joined = "&".join(f"{k}={v}" for k, v in items)
    path_url = f"{path}?{joined}" if joined else path
    payload = f"{method.upper()}{path_url}"
    if method.upper() in ("POST", "DELETE"):
        payload += body or ""
    return _hmac_sha256_hex(secret, payload)


def _hmac_sha256_hex(secret: str, payload: str) -> str:
    import hmac

    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------------------
# Util kecil
# --------------------------------------------------------------------------------------


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def first_reasons(decision: Decision, n: int = 3) -> str:
    return "; ".join(decision.reasons[:n]) if decision.reasons else "OK"


def describe_leverage_plan(equity: float, cfg: RiskConfig) -> dict[str, Any]:
    """Ringkasan rencana: notional, margin, dan jarak likuidasi pada setelan ini.

    Margin per posisi dibatasi oleh DUA plafon sekaligus: plafon leverage efektif dan
    plafon margin per trade. Yang paling ketat yang menang.
    """
    notional_by_leverage = equity * cfg.max_effective_leverage
    notional_by_margin_cap = equity * cfg.max_margin_per_trade_pct / 100.0 * cfg.leverage
    notional = min(notional_by_leverage, notional_by_margin_cap)
    margin = notional / cfg.leverage
    return {
        "equity": equity,
        "leverage_bursa": cfg.leverage,
        "leverage_efektif": cfg.max_effective_leverage,
        "notional_max": notional,
        "margin_per_posisi": margin,
        "margin_pct_equity": margin / equity * 100.0 if equity else 0.0,
        "binding": (
            "EFFECTIVE_LEVERAGE" if notional_by_leverage <= notional_by_margin_cap else "MARGIN_CAP"
        ),
    }


def all_symbols_rules(rules: Iterable[ExchangeRules]) -> dict[str, ExchangeRules]:
    return {r.symbol: r for r in rules}
