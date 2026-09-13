"""Regenerasi `tests/parity_vectors.json` dari `risk_engine` (satu-satunya sumber benar).

    python tools/make_parity_vectors.py            # tulis ulang berkas
    python tools/make_parity_vectors.py --check    # bandingkan saja, exit 1 bila beda

Vektor ini lalu dibaca oleh DUA test di dua bahasa:
  - tests/test_parity_js.py  -> memastikan Python cocok dengan vektor
  - tests/test_parity.py     -> memastikan port JS (n8n/code/risk_guard.js) cocok juga

Dengan begitu port JavaScript tidak bisa diam-diam menyimpang dari spesifikasi Python.
"""

from __future__ import annotations

import argparse
import json
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
    liquidation_price,
    max_usable_rr,
    min_win_rate_for_profit,
    size_position,
    validate_signal,
)
from datetime import datetime, timezone  # noqa: E402

VECTORS = ROOT / "tests" / "parity_vectors.json"

HEADER = (
    "Vektor uji bersama: dibaca oleh tests/test_parity_js.py (Python) dan "
    "tests/test_parity.py (Node). Nilai `expected` dihitung oleh risk_engine.py -- "
    "JANGAN ditulis tangan. Regenerasi: python tools/make_parity_vectors.py"
)


def build() -> dict:
    liq_cases = [
        {"leverage": 50, "mmr": 0.005, "fee": 0.0005, "side": "LONG"},
        {"leverage": 50, "mmr": 0.005, "fee": 0.0005, "side": "SHORT"},
        {"leverage": 10, "mmr": 0.005, "fee": 0.0005, "side": "LONG"},
        {"leverage": 100, "mmr": 0.005, "fee": 0.0005, "side": "LONG"},
    ]
    liq_price_cases = [
        {"side": "LONG", "entry": 100000, "leverage": 50, "mmr": 0.005, "fee": 0.0005},
        {"side": "SHORT", "entry": 100000, "leverage": 50, "mmr": 0.005, "fee": 0.0005},
    ]
    rr_cases = [
        {"leverage": 50, "stop": 0.008, "mmr": 0.005, "fee": 0.0005, "buffer": 33.0},
        {"leverage": 10, "stop": 0.008, "mmr": 0.005, "fee": 0.0005, "buffer": 33.0},
    ]
    size_cases = [
        {
            "equity": 1000, "entry": 100000, "stop_distance": 0.008, "base_step": 0.0001,
            "risk_per_trade_pct": 0.5, "max_effective_leverage": 3.0,
            "max_margin_per_trade_pct": 5.0, "leverage": 50, "slippage_buffer_pct": 0.05,
        }
    ]
    wr_cases = [
        {"rr": 1.1, "fee": 0.0005},
        {"rr": 2.0, "fee": 0.0005},
        {"rr": 4.0, "fee": 0.0005},
    ]

    out: dict = {"_komentar": HEADER}

    out["liquidation_distance"] = [
        {"args": a, "expected": liquidation_distance_pct(a["leverage"], a["mmr"], a["fee"], a["side"])}
        for a in liq_cases
    ]
    out["liquidation_price"] = [
        {"args": a, "expected": liquidation_price(a["side"], a["entry"], a["leverage"], a["mmr"], a["fee"])}
        for a in liq_price_cases
    ]
    out["max_usable_rr"] = [
        {"args": a, "expected": max_usable_rr(a["leverage"], a["stop"], a["mmr"], a["fee"], a["buffer"])}
        for a in rr_cases
    ]

    sizes = []
    for a in size_cases:
        rules = ExchangeRules(symbol="BTC_USDT_PERP", base_step=a["base_step"])
        cfg = RiskConfig(
            risk_per_trade_pct=a["risk_per_trade_pct"],
            max_effective_leverage=a["max_effective_leverage"],
            max_margin_per_trade_pct=a["max_margin_per_trade_pct"],
            leverage=a["leverage"],
            slippage_buffer_pct=a["slippage_buffer_pct"],
        )
        s = size_position(a["equity"], a["entry"], a["stop_distance"], rules, cfg)
        sizes.append({
            "args": a,
            "expected": {
                "qty": s["qty"], "notional": s["notional"], "margin": s["margin"],
                "binding": s["binding_constraint"],
            },
        })
    out["size_position"] = sizes

    out["min_win_rate_for_profit"] = [
        {"args": a, "expected": min_win_rate_for_profit(a["rr"], 50, a["fee"])} for a in wr_cases
    ]

    out["validate_signal"] = _signal_cases()
    out["client_order_id"] = [
        {"args": {"signal_id": "sig-1", "symbol": "BTC_USDT_PERP", "action": "ENTRY"},
         "expected_prefix": "pg-ENTRY-"},
        {"args": {"signal_id": "sig-1", "symbol": "BTC_USDT_PERP", "action": "EXIT"},
         "expected_prefix": "pg-EXIT-"},
    ]
    return out


def _signal_cases() -> list[dict]:
    rules = ExchangeRules(symbol="BTC_USDT_PERP", base_step=0.0001)
    cfg = RiskConfig()
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

    base_miro = {
        "schema_ok": True, "verdict_ts": "2026-09-13T08:00:00Z",
        "confidence": 0.78, "event_risk": "LOW", "bias": "NEUTRAL",
    }
    base_stats = {"win_rate": 0.62, "avg_win_pct": 0.0097, "avg_loss_pct": 0.008}

    raw = [
        ("happy_path_long", {
            "direction": "LONG", "entry": 100000, "stop": 99200, "take_profit": 100970,
            "ts": "2026-09-13T11:59:30Z", "mirofish": base_miro, "stats": base_stats,
        }),
        ("high_event_risk_veto", {
            "direction": "LONG", "entry": 100000, "stop": 99200, "take_profit": 100970,
            "ts": "2026-09-13T11:59:30Z",
            "mirofish": {**base_miro, "event_risk": "HIGH"}, "stats": base_stats,
        }),
        ("stop_too_far_for_50x", {
            "direction": "LONG", "entry": 100000, "stop": 98550, "take_profit": 103000,
            "ts": "2026-09-13T11:59:30Z", "mirofish": base_miro,
            "stats": {"win_rate": 0.62, "avg_win_pct": 0.03, "avg_loss_pct": 0.0145},
        }),
        ("stale_verdict", {
            "direction": "SHORT", "entry": 100000, "stop": 100800, "take_profit": 99030,
            "ts": "2026-09-13T11:59:30Z",
            "mirofish": {**base_miro, "verdict_ts": "2026-09-12T01:00:00Z", "confidence": 0.9},
            "stats": base_stats,
        }),
    ]

    cases = []
    for name, sig in raw:
        d = validate_signal(sig, 1000, 100000, rules, cfg, now=now)
        # untuk kasus yang disetujui, simpan seluruh alasan; untuk yang ditolak, simpan
        # alasan pertama agar test kedua bahasa tidak rapuh terhadap urutan
        reasons = d.reasons if d.approved else d.reasons[:1]
        cases.append({
            "name": name,
            "signal": sig,
            "equity": 1000,
            "entry": 100000,
            "now": "2026-09-13T12:00:00Z",
            "expected": {"approved": d.approved, "reasons": reasons, "size": d.size},
        })
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="bandingkan saja, jangan menulis")
    args = ap.parse_args()

    produced = build()
    text = json.dumps(produced, indent=2, sort_keys=False) + "\n"

    if args.check:
        if not VECTORS.exists():
            print(f"{VECTORS} belum ada; jalankan tanpa --check")
            return 1
        existing = json.loads(VECTORS.read_text(encoding="utf-8"))
        if existing != produced:
            print("VEKTOR PARITAS BASI: jalankan `python tools/make_parity_vectors.py`")
            for key in produced:
                if key.startswith("_"):
                    continue
                if existing.get(key) != produced.get(key):
                    print(f"  bagian berbeda: {key}")
            return 1
        print("vektor paritas sinkron dengan risk_engine")
        return 0

    VECTORS.write_text(text, encoding="utf-8")
    print(f"ditulis: {VECTORS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
