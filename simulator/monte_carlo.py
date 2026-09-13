"""Simulasi Monte Carlo untuk mengukur risiko rencana trading sebelum uang sungguhan dipakai.

Contoh:
    python simulator/monte_carlo.py                 # pakai profil default (config)
    python simulator/monte_carlo.py --leverage 10   # bandingkan leverage
    python simulator/monte_carlo.py --mode ruin     # skenario terburuk: stop gagal semua

Model:
  mode "normal" -> setiap trade berakhir di stop (-stop_pct) atau di TP (+tp_pct),
                   dengan probabilitas menang `win_rate`. Biaya round-trip dipotong.
  mode "ruin"   -> skenario stres: setiap posisi yang melawan kita *terlikuidasi*
                   (kehilangan seluruh margin terisolasi) dengan probabilitas `liq_prob`,
                   sisanya stop biasa. Ini yang harus direncanakan, bukan diabaikan.

Angka keluaran dipakai di docs/04-risiko-dan-guardrail.md. Seed tetap agar bisa
direproduksi oleh reviewer.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "risk_engine"))

from risk_engine import (  # noqa: E402
    DEFAULT_MMR_FALLBACK,
    DEFAULT_TAKER_FEE,
    ExchangeRules,
    RiskConfig,
    liquidation_distance_pct,

    size_position,
)


def load_cfg() -> RiskConfig:
    path = ROOT / "config" / "risk_config.json"
    return RiskConfig(**json.loads(path.read_text(encoding="utf-8")))


def run(
    *,
    equity: float,
    trades: int,
    win_rate: float,
    stop_pct: float,
    tp_pct: float,
    taker_fee: float,
    cfg: RiskConfig,
    mode: str,
    liq_prob: float,
    seed: int,
) -> dict:
    rnd = random.Random(seed)
    eq = equity
    peak = equity
    max_dd = 0.0
    ruin = False
    pnl_hist: list[float] = []

    instrument = ExchangeRules(
        symbol="BTC_USDT_PERP",
        base_step=0.0001,
        quote_step=0.01,
        min_size=0.0,
        max_size=1e12,
        min_notional=5.0,
        max_leverage=100.0,
        mmr=DEFAULT_MMR_FALLBACK,
        taker_fee=taker_fee,
        maker_fee=0.0002,
        liquidation_fee_rate=0.0,
    )

    for _ in range(trades):
        if eq <= 0:
            ruin = True
            break
        sizing = size_position(eq, 100_000.0, stop_pct, instrument, cfg)
        qty = sizing["qty"]
        if qty <= 0:
            break
        notional = qty * 100_000.0
        margin = notional / cfg.leverage
        fee = notional * taker_fee * 2  # biaya round-trip (entry + exit taker)

        won = rnd.random() < win_rate
        if not won and mode == "ruin" and rnd.random() < liq_prob:
            # stop gagal (gap / wick menembus stop) -> likuidasi:
            # seluruh margin terisolasi posisi itu hilang
            pnl = -margin
        elif won:
            pnl = notional * tp_pct - fee
        else:
            pnl = -notional * stop_pct - fee

        eq += pnl
        pnl_hist.append(pnl)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)

    pnl_hist.sort()
    return {
        "mode": mode,
        "trades": len(pnl_hist),
        "final_equity": round(eq, 2),
        "return_pct": round((eq / equity - 1) * 100, 2) if equity else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "ruin": ruin,
        "p5_pnl": round(pnl_hist[int(len(pnl_hist) * 0.05)], 2) if pnl_hist else 0.0,
        "p95_pnl": round(pnl_hist[int(len(pnl_hist) * 0.95)], 2) if pnl_hist else 0.0,
    }


def main() -> int:
    cfg = load_cfg()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--trades", type=int, default=500)
    ap.add_argument("--win-rate", type=float, default=0.52)
    ap.add_argument("--stop-pct", type=float, default=0.008, help="jarak stop (fraksi)")
    ap.add_argument("--tp-pct", type=float, default=0.0164, help="jarak TP (fraksi)")
    ap.add_argument("--mode", choices=("normal", "ruin", "both"), default="both")
    ap.add_argument("--liq-prob", type=float, default=0.10, help="P(stop gagal | trade rugi)")
    ap.add_argument("--leverage", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.leverage:
        cfg = RiskConfig(**{**cfg.__dict__, "leverage": args.leverage})

    liq = liquidation_distance_pct(cfg.leverage, DEFAULT_MMR_FALLBACK, DEFAULT_TAKER_FEE)
    if args.stop_pct >= liq:
        print(
            f"PERINGATAN: stop {args.stop_pct:.3%} >= jarak likuidasi {liq:.3%}. "
            "Mode 'ruin' akan mendominasi.",
            file=sys.stderr,
        )

    modes = ("normal", "ruin") if args.mode == "both" else (args.mode,)
    out = []
    for mode in modes:
        out.append(
            run(
                equity=args.equity,
                trades=args.trades,
                win_rate=args.win_rate,
                stop_pct=args.stop_pct,
                tp_pct=args.tp_pct,
                taker_fee=DEFAULT_TAKER_FEE,
                cfg=cfg,
                mode=mode,
                liq_prob=args.liq_prob,
                seed=args.seed,
            )
        )

    if args.json:
        print(json.dumps({"leverage": cfg.leverage, "liq_distance_pct": liq, "runs": out}, indent=2))
        return 0

    margin_per_pos = min(
        args.equity * cfg.max_effective_leverage,
        args.equity * cfg.max_margin_per_trade_pct / 100.0 * cfg.leverage,
    ) / cfg.leverage
    print(f"Equity awal        : {args.equity:,.2f} USDT")
    print(f"Leverage bursa     : {cfg.leverage}x  (efektif max {cfg.max_effective_leverage}x)")
    print(f"Jarak likuidasi    : {liq:.3%} dari entry")
    print(f"Stop yang diuji    : {args.stop_pct:.3%}   TP: {args.tp_pct:.3%}   win rate: {args.win_rate:.0%}")
    print(f"Margin per posisi  : {margin_per_pos:,.2f} USDT "
          f"({margin_per_pos / args.equity * 100:.1f}% ekuitas)")
    print("-" * 78)
    for r in out:
        label = "Normal (stop selalu kerja)" if r["mode"] == "normal" else \
                f"Stres ({args.liq_prob:.0%} trade rugi jadi likuidasi)"
        print(f"{label}")
        print(f"  trade            : {r['trades']}")
        print(f"  ekuitas akhir    : {r['final_equity']:,.2f} USDT ({r['return_pct']:+.2f}%)")
        print(f"  drawdown max     : {r['max_drawdown_pct']:.2f}%")
        print(f"  bangkrut (<=0)   : {'YA' if r['ruin'] else 'tidak'}")
        print(f"  PnL p5 / p95     : {r['p5_pnl']:+.2f} / {r['p95_pnl']:+.2f} USDT")
        print("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
