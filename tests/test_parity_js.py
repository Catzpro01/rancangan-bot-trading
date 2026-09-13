"""Paritas dua bahasa: risk_engine.py (spesifikasi) vs n8n/code/risk_guard.js (runtime).

Test ini melakukan dua hal:
  1. memastikan `risk_engine.py` masih cocok dengan `tests/parity_vectors.json`
     (vektor dibuat oleh `tools/make_parity_vectors.py`, bukan ditulis tangan);
  2. menjalankan `node --test tests/test_parity.js` sebagai subprocess sehingga
     port JavaScript ikut teruji dalam satu perintah `python -m pytest -q`.

Bila Node tidak terpasang, test kedua di-skip dengan pesan jelas -- bukan diam-diam lolos.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "risk_engine"))

from risk_engine import (  # noqa: E402
    ExchangeRules,
    RiskConfig,
    client_order_id,
    liquidation_distance_pct,
    liquidation_price,
    max_usable_rr,
    min_win_rate_for_profit,
    size_position,
    validate_signal,
)

VECTORS = json.loads((ROOT / "tests" / "parity_vectors.json").read_text(encoding="utf-8"))


class TestPythonMatchesVectors(unittest.TestCase):
    def test_vectors_are_fresh(self):
        """Vektor tidak boleh basi terhadap risk_engine."""
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "make_parity_vectors.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_liquidation_distance(self):
        for c in VECTORS["liquidation_distance"]:
            a = c["args"]
            got = liquidation_distance_pct(a["leverage"], a["mmr"], a["fee"], a["side"])
            self.assertAlmostEqual(got, c["expected"], places=14, msg=str(a))

    def test_liquidation_price(self):
        for c in VECTORS["liquidation_price"]:
            a = c["args"]
            got = liquidation_price(a["side"], a["entry"], a["leverage"], a["mmr"], a["fee"])
            self.assertAlmostEqual(got, c["expected"], places=7, msg=str(a))

    def test_max_usable_rr(self):
        for c in VECTORS["max_usable_rr"]:
            a = c["args"]
            got = max_usable_rr(a["leverage"], a["stop"], a["mmr"], a["fee"], a["buffer"])
            self.assertAlmostEqual(got, c["expected"], places=12, msg=str(a))

    def test_size_position(self):
        for c in VECTORS["size_position"]:
            a = c["args"]
            rules = ExchangeRules(symbol="BTC_USDT_PERP", base_step=a["base_step"])
            cfg = RiskConfig(
                risk_per_trade_pct=a["risk_per_trade_pct"],
                max_effective_leverage=a["max_effective_leverage"],
                max_margin_per_trade_pct=a["max_margin_per_trade_pct"],
                leverage=a["leverage"],
                slippage_buffer_pct=a["slippage_buffer_pct"],
            )
            got = size_position(a["equity"], a["entry"], a["stop_distance"], rules, cfg)
            self.assertEqual(got["qty"], c["expected"]["qty"])
            self.assertAlmostEqual(got["notional"], c["expected"]["notional"], places=9)
            self.assertEqual(got["binding_constraint"], c["expected"]["binding"])

    def test_min_win_rate(self):
        for c in VECTORS["min_win_rate_for_profit"]:
            got = min_win_rate_for_profit(c["args"]["rr"], 50, c["args"]["fee"])
            self.assertAlmostEqual(got, c["expected"], places=12, msg=str(c["args"]))

    def test_validate_signal_cases(self):
        rules = ExchangeRules(symbol="BTC_USDT_PERP", base_step=0.0001)
        cfg = RiskConfig()
        for c in VECTORS["validate_signal"]:
            now = datetime.fromisoformat(c["now"].replace("Z", "+00:00"))
            got = validate_signal(c["signal"], c["equity"], c["entry"], rules, cfg, now=now)
            self.assertEqual(got.approved, c["expected"]["approved"], c["name"])
            self.assertEqual(got.size, c["expected"]["size"], c["name"])
            want = str(c["expected"]["reasons"][0] if c["expected"]["reasons"] else "").split(" ")[0]
            have = str(got.reasons[0] if got.reasons else "").split(" ")[0]
            self.assertEqual(have, want, f'{c["name"]}: {got.reasons}')

    def test_client_order_id_prefix(self):
        for c in VECTORS["client_order_id"]:
            a = c["args"]
            got = client_order_id(a["signal_id"], a["symbol"], a["action"])
            self.assertTrue(got.startswith(c["expected_prefix"]), got)
            self.assertLessEqual(len(got), 64)


@unittest.skipIf(shutil.which("node") is None, "node tidak terpasang; port JS tidak diuji")
class TestJavaScriptPort(unittest.TestCase):
    def test_node_parity_suite_passes(self):
        """Jalankan suite Node; kegagalannya harus menggagalkan pytest juga."""
        res = subprocess.run(
            ["node", "--test", str(ROOT / "tests" / "test_parity.js")],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(res.returncode, 0, res.stdout[-4000:] + res.stderr[-2000:])
        self.assertIn("pass", res.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
