"""Unit test risk_engine.

Dijalankan dengan:  python -m pytest -q   (atau  python -m unittest discover -s tests)
Tidak ada dependensi eksternal: semua angka ekspektasi dihitung tertutup (closed form).
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "risk_engine"))

from risk_engine import (  # noqa: E402
    Decision,
    ExchangeRules,
    RiskConfig,
    Position,
    assert_local_liq_matches_exchange,
    atr_stop,
    breakeven_move_pct,
    client_order_id,
    cost_of_trade,
    describe_leverage_plan,
    evaluate_breaker,
    breakeven_win_rate,
    max_usable_rr,
    min_win_rate_for_profit,
    expected_value_pct,
    funding_cost_estimate,
    liquidation_distance_pct,
    liquidation_price,
    round_step,
    should_exit,
    sign_payload,
    size_position,
    trailing_stop,
    validate_signal,
)

CONFIG_PATH = ROOT / "config" / "risk_config.json"
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def cfg(**over) -> RiskConfig:
    return RiskConfig(**over)


def rules(**over) -> ExchangeRules:
    return ExchangeRules(symbol="BTC_USDT_PERP", **over)


def fresh_signal(**over):
    sig = {
        "direction": "LONG",
        "entry": 100_000.0,
        "stop": 99_200.0,          # 0.80% di bawah entry (plafon 50x = 0.9715%)
        "take_profit": 100_970.0,  # 0.97% -> RR 1.2125, target paling jauh yang masuk akal di 50x
        "ts": (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        "mirofish": {
            "schema_ok": True,
            "verdict_ts": (NOW - timedelta(minutes=40)).isoformat().replace("+00:00", "Z"),
            "confidence": 0.78,
            "event_risk": "LOW",
            "bias": "NEUTRAL",
            "run_id": "run-demo-1",
        },
        # avg_win/avg_loss adalah PERGERAKAN HARGA (fraksi), konsisten dengan jarak
        # TP (0.97%) dan stop (0.80%). Biaya round-trip 0.10% harga dipotong dari EV.
        "stats": {"win_rate": 0.62, "avg_win_pct": 0.0097, "avg_loss_pct": 0.0080},
    }
    sig.update(over)
    return sig


class TestLiquidationMath(unittest.TestCase):
    def test_liq_distance_50x(self):
        d = liquidation_distance_pct(50, mmr=0.005, taker_fee=0.0005)
        self.assertAlmostEqual(d, 0.014992503748125937, places=14)  # 1.49925% dari entry

    def test_liq_distance_shrinks_with_leverage(self):
        d10 = liquidation_distance_pct(10, 0.005, 0.0005)
        d50 = liquidation_distance_pct(50, 0.005, 0.0005)
        d100 = liquidation_distance_pct(100, 0.005, 0.0005)
        self.assertGreater(d10, d50)
        self.assertGreater(d50, d100)
        self.assertAlmostEqual(d100, 0.0049975012493753135, places=14)  # 0.45% -> praktis bunuh diri

    def test_liq_distance_rejects_absurd_leverage(self):
        with self.assertRaises(ValueError):
            liquidation_distance_pct(300, 0.005, 0.0005)  # 1/300 < mmr+fee

    def test_liq_price_long_uses_documented_formula(self):
        liq = liquidation_price("LONG", 100_000, 50, 0.005, 0.0005)
        expected = 100_000 * (1 - 0.02 + 0.005 + 0.0005) / (1 + 0.0005)
        self.assertAlmostEqual(liq, expected, places=6)
        self.assertAlmostEqual(liq, 98_500.74962518741, places=9)

    def test_liq_price_short_uses_documented_formula(self):
        liq = liquidation_price("SHORT", 100_000, 50, 0.005, 0.0005)
        expected = 100_000 * (1 + 0.02 - 0.005 - 0.0005) / (1 - 0.0005)
        self.assertAlmostEqual(liq, expected, places=6)
        self.assertAlmostEqual(liq, 101_500.7503751876, places=9)

    def test_liq_price_side_dependent_distance(self):
        d_long = liquidation_distance_pct(50, 0.005, 0.0005, side="LONG")
        d_short = liquidation_distance_pct(50, 0.005, 0.0005, side="SHORT")
        self.assertGreater(d_short, d_long)  # fee likuidasi membuat sisi SHORT sedikit lebih jauh
        # jarak ini harus konsisten dengan liquidation_price() -- bukan dua rumus beda
        self.assertAlmostEqual(liquidation_price("LONG", 100_000, 50, 0.005, 0.0005),
                               100_000 * (1 - d_long), places=6)
        self.assertAlmostEqual(liquidation_price("SHORT", 100_000, 50, 0.005, 0.0005),
                               100_000 * (1 + d_short), places=6)
        with self.assertRaises(ValueError):
            liquidation_distance_pct(50, 0.005, 0.0005, side="FLAT")

    def test_exchange_liquidation_guard(self):
        local = liquidation_price("LONG", 100_000, 50, 0.005, 0.0005)
        ok, msg = assert_local_liq_matches_exchange("LONG", 100_000, local, local * 1.0005)
        self.assertTrue(ok, msg)
        ok, msg = assert_local_liq_matches_exchange("LONG", 100_000, local, local * 0.9)
        self.assertFalse(ok)
        self.assertEqual(msg.split()[0], "LIQ_MISMATCH")
        ok, msg = assert_local_liq_matches_exchange("LONG", 100_000, local, 0)
        self.assertFalse(ok)
        self.assertEqual(msg, "EXCHANGE_LIQ_MISSING")
        # estimator lokal yang terlalu optimis untuk LONG harus ditolak
        ok, msg = assert_local_liq_matches_exchange("LONG", 100_000, local * 0.985, local * 0.99)
        self.assertFalse(ok)


class TestPrecisionAndCost(unittest.TestCase):
    def test_round_step_never_rounds_up(self):
        self.assertEqual(round_step(0.0019, 0.001), 0.001)
        self.assertEqual(round_step(0.002, 0.001), 0.002)
        self.assertEqual(round_step(0.0021, 0.001, "up"), 0.003)

    def test_breakeven_move(self):
        self.assertAlmostEqual(breakeven_move_pct(0.0005), 0.001, places=12)  # 0.10%

    def test_cost_of_round_trip_scales_with_notional(self):
        self.assertAlmostEqual(cost_of_trade(10_000, 0.0005), 10.0, places=8)

    def test_fee_as_percent_of_margin_grows_with_leverage(self):
        """Di 50x, biaya round-trip 0.10% notional = 5% margin. Ini angka yang paling
        sering dilupakan orang saat memilih leverage tinggi."""
        notional = 50_000.0
        fee = cost_of_trade(notional, 0.0005)
        self.assertAlmostEqual(fee / (notional / 50) * 100, 5.0, places=8)
        self.assertAlmostEqual(fee / (notional / 10) * 100, 1.0, places=8)


class TestSizing(unittest.TestCase):
    def test_risk_budget_binds(self):
        s = size_position(1_000, 100_000, 0.008, rules(base_step=0.0001), cfg())
        # risk 5 USDT / (100000 * 0.0085) = 0.005882 -> dibulatkan ke step 0.0001
        self.assertEqual(s["qty"], 0.0058)
        self.assertEqual(s["binding_constraint"], "RISK_BUDGET")
        self.assertAlmostEqual(s["notional"], 580.0, places=6)
        self.assertAlmostEqual(s["margin"], 580.0 / 50, places=6)

    def test_risk_per_trade_is_respected(self):
        s = size_position(1_000, 100_000, 0.008, rules(base_step=0.0001), cfg())
        # kerugian bila stop kena (dengan buffer slippage) tidak boleh lewat 0.5% ekuitas
        self.assertLessEqual(s["risk_amount"], 1_000 * 0.005 + 1e-9)

    def test_effective_leverage_ceiling_is_hard(self):
        """Jaring pengaman terakhir: kalau ada konfigurasi aneh yang membuat anggaran
        risiko atau plafon margin melonggar, plafon leverage efektif tetap mengikat."""
        absurd = cfg(
            risk_per_trade_pct=1000.0,
            max_effective_leverage=2.0,
            max_margin_per_trade_pct=1000.0,
        )
        s = size_position(1_000, 100_000, 0.008, rules(base_step=0.0001), absurd)
        self.assertEqual(s["binding_constraint"], "EFFECTIVE_LEVERAGE_CEILING")
        self.assertLessEqual(s["notional"], 2_000.0 + 1e-9)

    def test_margin_cap_binds_when_risk_budget_is_loose(self):
        loose = cfg(risk_per_trade_pct=50.0, max_effective_leverage=100.0)
        s = size_position(1_000, 100_000, 0.008, rules(base_step=0.0001), loose)
        self.assertEqual(s["binding_constraint"], "MARGIN_CAP")
        self.assertLessEqual(s["margin"], 1_000 * 0.05 + 1e-9)

    def test_margin_cap_is_tighter_than_leverage_cap_by_construction(self):
        """max_margin_per_trade_pct * leverage harus < max_effective_leverage, kalau
        tidak plafon leverage efektif tidak akan pernah tercapai."""
        c = cfg()
        self.assertLess(c.max_margin_per_trade_pct / 100 * c.leverage, c.max_effective_leverage)

    def test_zero_equity_rejected(self):
        s = size_position(0, 100_000, 0.008, rules(), cfg())
        self.assertEqual(s["qty"], 0.0)
        self.assertEqual(s["reason"], "EQUITY_OR_ENTRY_INVALID")


class TestValidateSignal(unittest.TestCase):
    def test_happy_path_approved(self):
        d = validate_signal(fresh_signal(), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertTrue(d.approved, d.reasons)
        self.assertGreater(d.size, 0)
        self.assertLessEqual(d.notional, 1_000 * 3.0 + 1e-9)
        self.assertLess(d.stop_distance_pct, d.liq_distance_pct)
        self.assertGreater(d.ev_per_trade_pct, 0)
        self.assertLessEqual(d.effective_leverage, 3.0 + 1e-9)

    def test_default_deny_when_nothing_supplied(self):
        d = validate_signal({}, 1_000, 100_000, rules(), cfg(), now=NOW)
        self.assertFalse(d.approved)
        self.assertIn("DIRECTION_NONE", d.reasons)

    def test_stop_must_stay_inside_liquidation_buffer(self):
        # stop 1.45% == jarak likuidasi pada 50x -> wajib ditolak
        sig = fresh_signal(stop=98_550.0, take_profit=103_000.0)
        d = validate_signal(sig, 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertFalse(d.approved)
        self.assertTrue(any(r.startswith("STOP_TOO_FAR") for r in d.reasons), d.reasons)

    def test_stale_mirofish_verdict_vetoes(self):
        miro = fresh_signal()["mirofish"]
        miro["verdict_ts"] = (NOW - timedelta(hours=9)).isoformat().replace("+00:00", "Z")
        d = validate_signal(
            fresh_signal(mirofish=miro), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertFalse(d.approved)
        self.assertTrue(any(r.startswith("MIROFISH_STALE") for r in d.reasons))

    def test_high_event_risk_is_a_hard_veto(self):
        miro = dict(fresh_signal()["mirofish"], event_risk="HIGH")
        d = validate_signal(
            fresh_signal(mirofish=miro), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertIn("MIROFISH_HIGH_EVENT_RISK_VETO", d.reasons)

    def test_low_confidence_rejected(self):
        miro = dict(fresh_signal()["mirofish"], confidence=0.31)
        d = validate_signal(
            fresh_signal(mirofish=miro), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertTrue(any(r.startswith("MIROFISH_LOW_CONFIDENCE") for r in d.reasons))

    def test_broken_verdict_schema_rejected(self):
        miro = dict(fresh_signal()["mirofish"], schema_ok=False)
        d = validate_signal(
            fresh_signal(mirofish=miro), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertIn("MIROFISH_SCHEMA_INVALID", d.reasons)

    def test_bias_conflict_rejected(self):
        miro = dict(fresh_signal()["mirofish"], bias="SHORT")
        d = validate_signal(
            fresh_signal(mirofish=miro), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertTrue(any(r.startswith("MIROFISH_BIAS_CONFLICT") for r in d.reasons))

    def test_low_rr_rejected(self):
        d = validate_signal(
            fresh_signal(take_profit=100_400.0),
            1_000,
            100_000,
            rules(base_step=0.0001),
            cfg(),
            now=NOW,
        )
        self.assertTrue(any(r.startswith("RR_TOO_LOW") for r in d.reasons))

    def test_stop_on_wrong_side_rejected(self):
        d = validate_signal(
            fresh_signal(stop=100_500.0), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertIn("STOP_ON_WRONG_SIDE", d.reasons)

    def test_negative_ev_rejected(self):
        d = validate_signal(
            fresh_signal(stats={"win_rate": 0.40, "avg_win_pct": 1.0, "avg_loss_pct": 1.0}),
            1_000,
            100_000,
            rules(base_step=0.0001),
            cfg(),
            now=NOW,
        )
        self.assertTrue(any(r.startswith("NEGATIVE_EV") for r in d.reasons))

    def test_no_track_record_rejected(self):
        d = validate_signal(
            fresh_signal(stats={}), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW
        )
        self.assertIn("NO_TRACK_RECORD", d.reasons)

    def test_effective_leverage_breach_detected(self):
        d = validate_signal(fresh_signal(), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertTrue(d.approved, d.reasons)
        tampered = Decision(
            approved=True, reasons=[], size=d.size, notional=1_000 * 4.0, margin=d.margin
        )
        self.assertGreater(tampered.notional, 1_000 * cfg().max_effective_leverage)
        self.assertLessEqual(d.notional, 1_000 * cfg().max_effective_leverage)

    def test_max_open_positions(self):
        d = validate_signal(
            fresh_signal(), 1_000, 100_000, rules(base_step=0.0001), cfg(),
            open_positions=1, now=NOW,
        )
        self.assertTrue(any(r.startswith("MAX_OPEN_POSITIONS") for r in d.reasons))

    def test_breaker_blocks(self):
        d = validate_signal(
            fresh_signal(), 1_000, 100_000, rules(base_step=0.0001), cfg(),
            breaker={"tripped": True, "reason": "DAILY_LOSS -3.4%"}, now=NOW,
        )
        self.assertTrue(any(r.startswith("CIRCUIT_BREAKER_TRIPPED") for r in d.reasons))

    def test_min_notional_enforced(self):
        # ekuitas kecil -> qty jadi 0 setelah pembulatan step; jangan pernah dipaksa
        d = validate_signal(
            fresh_signal(), 5, 100_000, rules(base_step=0.0001, min_notional=50.0), cfg(), now=NOW
        )
        self.assertFalse(d.approved)
        self.assertTrue(
            any(r.startswith("BELOW_MIN_") for r in d.reasons) or "SIZE_ZERO" in d.reasons,
            d.reasons,
        )

    def test_short_direction_validated_symmetrically(self):
        sig = fresh_signal(direction="SHORT", stop=100_800.0, take_profit=98_360.0)
        d = validate_signal(sig, 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertTrue(d.approved, d.reasons)
        self.assertAlmostEqual(d.stop_distance_pct, 0.008, places=10)

    def test_stale_execution_signal_rejected(self):
        sig = fresh_signal(ts=(NOW - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"))
        d = validate_signal(sig, 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertTrue(any(r.startswith("SIGNAL_STALE") for r in d.reasons))


class TestCircuitBreaker(unittest.TestCase):
    def test_daily_loss_trips(self):
        b = evaluate_breaker(1_000, 965, 1_000, 1, cfg(), now=NOW)
        self.assertTrue(b["tripped"])
        self.assertTrue(b["reason"].startswith("DAILY_LOSS"))

    def test_drawdown_trips_when_daily_loss_not_hit(self):
        # rugi hari ini kecil (-1%) tetapi turun 11% dari puncak -> tetap trip
        b = evaluate_breaker(1_000, 990, 1_110, 0, cfg(), now=NOW)
        self.assertTrue(b["tripped"])
        self.assertTrue(b["reason"].startswith("DRAWDOWN"), b["reason"])

    def test_consecutive_losses_trip(self):
        b = evaluate_breaker(1_000, 990, 1_000, 3, cfg(), now=NOW)
        self.assertTrue(b["tripped"])
        self.assertTrue(b["reason"].startswith("CONSEC_LOSSES"))

    def test_healthy_account_not_tripped(self):
        b = evaluate_breaker(1_000, 1_010, 1_010, 0, cfg(), now=NOW)
        self.assertFalse(b["tripped"])
        self.assertAlmostEqual(b["day_pnl_pct"], 1.0, places=6)

    def test_invalid_day_start_equity_trips(self):
        b = evaluate_breaker(0, 1_000, 1_000, 0, cfg(), now=NOW)
        self.assertTrue(b["tripped"])

    def test_cooldown_window_is_set(self):
        b = evaluate_breaker(1_000, 960, 1_000, 0, cfg(), now=NOW)
        self.assertIsNotNone(b["cooldown_until"])
        self.assertAlmostEqual(b["cooldown_until"] - NOW.timestamp(), 240 * 60, places=3)


class TestPositionManagement(unittest.TestCase):
    def pos(self, side="LONG", entry=100_000.0, stop=99_200.0, tp=101_640.0):
        return Position(
            symbol="BTC_USDT_PERP", side=side, entry=entry, size=0.0058,
            stop=stop, take_profit=tp, highest=entry, lowest=entry, opened_at=NOW,
        )

    def test_stop_loss_long(self):
        ex, why = should_exit(self.pos(), 99_100, NOW + timedelta(minutes=5), cfg())
        self.assertTrue(ex)
        self.assertEqual(why, "STOP_LOSS")

    def test_take_profit_long(self):
        ex, why = should_exit(self.pos(), 101_700, NOW + timedelta(minutes=5), cfg())
        self.assertEqual(why, "TAKE_PROFIT")

    def test_time_stop(self):
        ex, why = should_exit(self.pos(), 100_000, NOW + timedelta(hours=25), cfg())
        self.assertTrue(ex)
        self.assertEqual(why, "TIME_STOP")

    def test_short_stop_and_tp(self):
        p = self.pos(side="SHORT", stop=100_800.0, tp=98_360.0)
        self.assertEqual(should_exit(p, 100_900, NOW, cfg())[1], "STOP_LOSS")
        self.assertEqual(should_exit(p, 98_000, NOW, cfg())[1], "TAKE_PROFIT")

    def test_trailing_only_ratchets_up_for_long(self):
        p = self.pos()
        trailing_stop(p, 101_000, atr=300, atr_multiplier=2.0)
        first = p.stop
        self.assertGreater(first, 99_200)
        trailing_stop(p, 100_200, atr=300, atr_multiplier=2.0)  # harga turun
        self.assertEqual(p.stop, first)  # tidak boleh turun
        trailing_stop(p, 102_000, atr=300, atr_multiplier=2.0)
        self.assertGreater(p.stop, first)

    def test_trailing_ratchets_down_for_short(self):
        p = self.pos(side="SHORT", stop=100_800.0, tp=98_360.0)
        trailing_stop(p, 99_000, atr=300, atr_multiplier=2.0)
        first = p.stop
        self.assertLess(first, 100_800)
        trailing_stop(p, 99_800, atr=300, atr_multiplier=2.0)
        self.assertEqual(p.stop, first)

    def test_atr_stop_side(self):
        self.assertAlmostEqual(atr_stop(100_000, "LONG", 400, 1.5), 99_400.0, places=6)
        self.assertAlmostEqual(atr_stop(100_000, "SHORT", 400, 1.5), 100_600.0, places=6)

    def test_funding_cost_sign(self):
        self.assertGreater(funding_cost_estimate(10_000, "LONG", 0.0001, 24), 0)
        self.assertLess(funding_cost_estimate(10_000, "SHORT", 0.0001, 24), 0)
        self.assertAlmostEqual(
            abs(funding_cost_estimate(10_000, "LONG", 0.0001, 24)), 3.0, places=8
        )


class TestOrderPlumbing(unittest.TestCase):
    def test_client_order_id_deterministic_and_legal(self):
        a = client_order_id("sig-1", "BTC_USDT_PERP", "ENTRY")
        b = client_order_id("sig-1", "BTC_USDT_PERP", "ENTRY")
        c = client_order_id("sig-2", "BTC_USDT_PERP", "ENTRY")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLessEqual(len(a), 64)
        self.assertTrue(all(ch.isalnum() or ch == "-" for ch in a))
        self.assertNotEqual(
            client_order_id("sig-1", "BTC_USDT_PERP", "ENTRY"),
            client_order_id("sig-1", "BTC_USDT_PERP", "EXIT"),
        )

    def test_signature_is_stable_hex(self):
        sig = sign_payload(
            "POST",
            "/uapi/v1/trade/order",
            {"timestamp": 1_700_000_000_000},
            '{"symbol":"BTC_USDT_PERP"}',
            "secret",
        )
        self.assertEqual(len(sig), 64)
        self.assertEqual(sig, sig.lower())
        # GET tidak menyertakan body
        get_sig = sign_payload("GET", "/uapi/v1/account/positions", {"timestamp": 1}, "", "s")
        get_sig_with_body = sign_payload(
            "GET", "/uapi/v1/account/positions", {"timestamp": 1}, "X", "s"
        )
        self.assertEqual(get_sig, get_sig_with_body)

    def test_query_sorted_alphabetically(self):
        import hashlib
        import hmac

        q = {"timestamp": 2, "symbol": "BTC_USDT_PERP"}
        expect = hmac.new(
            b"secret",
            b"GET/uapi/v1/account/positions?symbol=BTC_USDT_PERP&timestamp=2",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sign_payload("GET", "/uapi/v1/account/positions", q, "", "secret"), expect)

    def test_post_signature_includes_body(self):
        import hashlib
        import hmac

        body = '{"symbol":"BTC_USDT_PERP","side":"BUY"}'
        expect = hmac.new(
            b"secret",
            f"POST/uapi/v1/trade/order?timestamp=9{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            sign_payload("POST", "/uapi/v1/trade/order", {"timestamp": 9}, body, "secret"),
            expect,
        )


class TestConfigFile(unittest.TestCase):
    """config/risk_config.json adalah artefak yang benar-benar dibaca n8n: harus valid
    dan setiap kuncinya harus dikenal RiskConfig (tidak ada typo yang diam-diam lolos)."""

    def test_config_file_matches_riskconfig_fields(self):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        known = set(RiskConfig().__dict__.keys())
        self.assertTrue(known, "RiskConfig harus punya field")
        for key in raw:
            self.assertIn(key, known, f"kunci tak dikenal di risk_config.json: {key}")
        rc = RiskConfig(**raw)
        self.assertEqual(rc.leverage, 50)
        self.assertLessEqual(rc.max_daily_loss_pct, 5.0)

    def test_configured_stop_ceiling_is_inside_liquidation(self):
        rc = RiskConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        liq = liquidation_distance_pct(rc.leverage, 0.005, 0.0005)
        ceiling = liq * (1 - rc.liq_buffer_pct / 100.0)
        self.assertLess(ceiling, liq)
        self.assertGreater(ceiling, 0)
        # jarak stop tipikal strategi (0.8%) harus muat di bawah plafon
        self.assertGreater(ceiling, 0.008)

    def test_leverage_plan_numbers(self):
        rc = RiskConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        plan = describe_leverage_plan(1_000, rc)
        # plafon margin (5% ekuitas x 50x = 2500) lebih ketat dari plafon leverage (3x = 3000)
        # margin per posisi adalah min(plafon leverage, plafon margin) / leverage bursa
        self.assertAlmostEqual(plan["notional_max"], 50 * rc.leverage, places=6)
        self.assertAlmostEqual(plan["margin_per_posisi"], 50.0, places=6)
        self.assertEqual(plan["binding"], "MARGIN_CAP")
        self.assertLessEqual(plan["margin_pct_equity"], rc.max_margin_per_trade_pct + 1e-9)


class TestEvMath(unittest.TestCase):
    """Semua angka di bawah dihitung ulang dengan interpreter sebelum ditulis."""

    def test_expected_value_definition(self):
        # EV = p*win - (1-p)*loss - biaya, semuanya satuan pergerakan harga
        self.assertAlmostEqual(expected_value_pct(0.5, 0.02, 0.01, 0.001), 0.004, places=12)
        self.assertAlmostEqual(expected_value_pct(0.5, 2.0, 1.0, 1.0), -0.5, places=12)
        self.assertAlmostEqual(expected_value_pct(2 / 3, 2.0, 1.0, 1.0), 0.0, places=9)

    def test_ev_per_price_move_is_leverage_independent(self):
        """EV dalam satuan harga tidak berubah saat leverage berubah. Yang berubah
        adalah berapa % margin yang dipertaruhkan -- itu urusan sizing, bukan EV."""
        a = expected_value_pct(0.62, 0.0097, 0.0080, breakeven_move_pct(0.0005))
        b = expected_value_pct(0.62, 0.0097, 0.0080, breakeven_move_pct(0.0005))
        self.assertAlmostEqual(a, 0.001974, places=12)
        self.assertEqual(a, b)

    def test_fee_as_pct_of_margin_grows_with_leverage(self):
        """Biaya yang sama terasa jauh lebih besar relatif terhadap margin di 50x.
        Inilah beban nyata leverage tinggi, dan alasan membatasi margin per posisi."""
        notional = 3_000.0
        fee = cost_of_trade(notional, 0.0005)
        self.assertAlmostEqual(fee, 3.0, places=9)
        self.assertAlmostEqual(fee / (notional / 50) * 100, 5.0, places=9)   # 5% margin
        self.assertAlmostEqual(fee / (notional / 10) * 100, 1.0, places=9)   # 1% margin

    def test_min_win_rate_for_profit(self):
        self.assertAlmostEqual(min_win_rate_for_profit(2.0, 50), 0.36666666666666664, places=12)
        self.assertAlmostEqual(min_win_rate_for_profit(4.0, 50), 0.22, places=12)
        self.assertAlmostEqual(min_win_rate_for_profit(1.1, 50), 0.5238095238095238, places=12)
        # verifikasi silang: pada wr_min, EV harus tepat nol
        wr = min_win_rate_for_profit(2.0, 50)
        self.assertAlmostEqual(expected_value_pct(wr, 2.0, 1.0, 0.1), 0.0, places=12)
        # tidak bergantung leverage
        self.assertEqual(min_win_rate_for_profit(2.0, 50), min_win_rate_for_profit(2.0, 10))

    def test_breakeven_win_rate_helper(self):
        self.assertAlmostEqual(breakeven_win_rate(0.02, 0.01, 50), 0.36666666666666664, places=12)
        self.assertAlmostEqual(breakeven_win_rate(4.0, 2.0, 50), 0.33349999999999996, places=12)
        self.assertEqual(breakeven_win_rate(4.0, 2.0, 50), breakeven_win_rate(4.0, 2.0, 10))
        with self.assertRaises(ValueError):
            breakeven_win_rate(0.0, 0.0, 50)

    def test_stats_must_be_consistent_with_trade_shape(self):
        """avg_loss yang lebih besar dari jarak stop = backtest tidak merepresentasikan
        trade ini. Guard menolak, karena EV-nya dihitung dari angka yang salah."""
        sig = fresh_signal(stats={"win_rate": 0.99, "avg_win_pct": 0.0097, "avg_loss_pct": 0.05})
        d = validate_signal(sig, 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        self.assertTrue(any(r.startswith("STATS_INCONSISTENT") for r in d.reasons), d.reasons)


class TestUsableRoomAt50x(unittest.TestCase):
    """Batasan sesungguhnya dari leverage 50x: ruang geraknya, bukan biayanya."""

    def test_50x_leaves_less_than_one_percent_for_the_stop(self):
        room = liquidation_distance_pct(50, 0.005, 0.0005)
        ceiling = room * (1 - cfg().liq_buffer_pct / 100.0)
        self.assertAlmostEqual(room, 0.014992503748125937, places=14)
        self.assertAlmostEqual(ceiling, 0.010044977511244378, places=14)

    def test_max_usable_rr_at_50x_is_about_1_6(self):
        rr = max_usable_rr(50, 0.008, 0.005, 0.0005, 33.0)
        self.assertAlmostEqual(rr, 1.686656671664168, places=12)

    def test_10x_gives_room_to_breathe(self):
        rr = max_usable_rr(10, 0.008, 0.005, 0.0005, 33.0)
        self.assertAlmostEqual(rr, 10.682158920539731, places=12)
        self.assertGreater(rr, max_usable_rr(50, 0.008, 0.005, 0.0005, 33.0) * 6)

    def test_no_room_if_stop_already_beyond_ceiling(self):
        self.assertEqual(max_usable_rr(50, 0.02, 0.005, 0.0005, 33.0), 0.0)

    def test_config_min_rr_is_achievable_at_50x(self):
        rc = RiskConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        achievable = max_usable_rr(rc.leverage, 0.008, 0.005, 0.0005, rc.liq_buffer_pct)
        self.assertLessEqual(rc.min_rr_ratio, achievable,
                             "min_rr_ratio mustahil dicapai pada leverage ini")
        # dan tetap profit setelah biaya
        self.assertLess(min_win_rate_for_profit(rc.min_rr_ratio, rc.leverage), 1.0)


class TestDecisionSerialization(unittest.TestCase):
    def test_to_dict_is_json_serializable(self):
        d = validate_signal(fresh_signal(), 1_000, 100_000, rules(base_step=0.0001), cfg(), now=NOW)
        payload = json.dumps(d.to_dict())
        self.assertIn("approved", payload)

    def test_rejection_returns_zero_size(self):
        d = validate_signal({}, 1_000, 100_000, rules(), cfg(), now=NOW)
        self.assertIsInstance(d, Decision)
        self.assertEqual(d.size, 0.0)
        self.assertEqual(d.notional, 0.0)
        self.assertEqual(d.effective_leverage, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
