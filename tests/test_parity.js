// tests/test_parity.js — memastikan port JavaScript (n8n/code/risk_guard.js)
// menghasilkan angka yang IDENTIK dengan risk_engine.py pada vektor bersama.
//
// Dijalankan:  node --test tests/test_parity.js
// Vektor dibuat oleh:  python tools/make_parity_vectors.py

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const guard = require('../n8n/code/risk_guard.js');
const vectors = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'parity_vectors.json'), 'utf8'),
);

const CLOSE = 1e-9;

test('liquidation_distance identik dengan Python', () => {
  for (const c of vectors.liquidation_distance) {
    const got = guard.liquidationDistance(c.args.leverage, c.args.mmr, c.args.fee, c.args.side);
    assert.ok(Math.abs(got - c.expected) < CLOSE,
      `${JSON.stringify(c.args)} -> ${got} != ${c.expected}`);
  }
});

test('liquidation_price identik dengan Python', () => {
  for (const c of vectors.liquidation_price) {
    const got = guard.liquidationPrice(c.args.side, c.args.entry, c.args.leverage, c.args.mmr, c.args.fee);
    assert.ok(Math.abs(got - c.expected) < 1e-7,
      `${JSON.stringify(c.args)} -> ${got} != ${c.expected}`);
  }
});

test('identitas: liquidationPrice == entry * (1 -/+ distance)', () => {
  for (const L of [5, 10, 20, 50, 100]) {
    const dL = guard.liquidationDistance(L, 0.005, 0.0005, 'LONG');
    const dS = guard.liquidationDistance(L, 0.005, 0.0005, 'SHORT');
    const pL = guard.liquidationPrice('LONG', 100000, L, 0.005, 0.0005);
    const pS = guard.liquidationPrice('SHORT', 100000, L, 0.005, 0.0005);
    assert.ok(Math.abs(pL - 100000 * (1 - dL)) < 1e-7, `L=${L} LONG`);
    assert.ok(Math.abs(pS - 100000 * (1 + dS)) < 1e-7, `L=${L} SHORT`);
  }
});

test('max_usable_rr identik dengan Python', () => {
  for (const c of vectors.max_usable_rr) {
    const got = guard.maxUsableRr(c.args.leverage, c.args.stop, c.args.mmr, c.args.fee, c.args.buffer);
    assert.ok(Math.abs(got - c.expected) < CLOSE,
      `${JSON.stringify(c.args)} -> ${got} != ${c.expected}`);
  }
});

test('size_position identik dengan Python', () => {
  for (const c of vectors.size_position) {
    const a = c.args;
    const rules = { base_step: a.base_step, max_size: 1e12, min_size: 0, min_notional: 5 };
    const cfg = {
      risk_per_trade_pct: a.risk_per_trade_pct,
      max_effective_leverage: a.max_effective_leverage,
      max_margin_per_trade_pct: a.max_margin_per_trade_pct,
      leverage: a.leverage,
      slippage_buffer_pct: a.slippage_buffer_pct,
    };
    const got = guard.sizePosition(a.equity, a.entry, a.stop_distance, rules, cfg);
    assert.strictEqual(got.qty, c.expected.qty, 'qty');
    assert.ok(Math.abs(got.notional - c.expected.notional) < CLOSE, 'notional');
    assert.ok(Math.abs(got.margin - c.expected.margin) < CLOSE, 'margin');
    assert.strictEqual(got.binding_constraint, c.expected.binding, 'binding');
  }
});

test('min_win_rate_for_profit identik dengan Python', () => {
  for (const c of vectors.min_win_rate_for_profit) {
    const got = guard.minWinRateForProfit(c.args.rr, c.args.fee);
    assert.ok(Math.abs(got - c.expected) < CLOSE,
      `${JSON.stringify(c.args)} -> ${got} != ${c.expected}`);
  }
});

test('validate_signal identik dengan Python (approved + alasan utama + ukuran)', () => {
  const cfg = {
    risk_per_trade_pct: 0.5, max_effective_leverage: 3.0, max_margin_per_trade_pct: 5.0,
    max_open_positions: 1, slippage_buffer_pct: 0.05, leverage: 50, liq_buffer_pct: 33.0,
    max_signal_age_sec: 900, max_verdict_age_sec: 21600, min_confidence: 0.6,
    veto_if_high_event_risk: true, veto_on_negative_ev: true, min_rr_ratio: 1.1,
  };
  const rules = {
    base_step: 0.0001, quote_step: 0.01, min_size: 0, max_size: 1e12,
    min_notional: 5, mmr: 0.005, taker_fee: 0.0005,
  };

  for (const c of vectors.validate_signal) {
    const now = Date.parse(c.now.replace('Z', '+00:00'));
    const got = guard.validateSignal(c.signal, c.equity, c.entry, rules, cfg, { now });
    assert.strictEqual(got.approved, c.expected.approved, `${c.name}: approved`);
    assert.strictEqual(got.size, c.expected.size, `${c.name}: size`);
    // alasan pertama harus cocok (urutan gerbang sama di kedua implementasi)
    const wantPrefix = String(c.expected.reasons[0] || '').split(' ')[0];
    const gotFirst = String(got.reasons[0] || '').split(' ')[0];
    assert.strictEqual(gotFirst, wantPrefix,
      `${c.name}: alasan pertama ${gotFirst} != ${wantPrefix} (semua: ${got.reasons})`);
  }
});

test('client_order_id deterministik dan sesuai aturan Pionex', () => {
  const seen = new Set();
  for (const c of vectors.client_order_id) {
    const a = c.args;
    const once = guard.buildClientOrderId(a.signal_id, a.symbol, a.action);
    const twice = guard.buildClientOrderId(a.signal_id, a.symbol, a.action);
    assert.strictEqual(once, twice, 'harus deterministik');
    assert.ok(once.startsWith(c.expected_prefix), `prefix ${once}`);
    assert.ok(once.length <= 64, 'maks 64 karakter');
    assert.match(once, /^[A-Za-z0-9-]+$/, 'hanya alfanumerik dan tanda hubung');
    assert.ok(!seen.has(once), 'ENTRY dan EXIT tidak boleh bertabrakan');
    seen.add(once);
  }
});

test('signPionex menandatangani query terurut dan body untuk POST', () => {
  const crypto = require('node:crypto');
  const expect = (payload, secret = 'secret') =>
    crypto.createHmac('sha256', secret).update(payload, 'utf8').digest('hex');

  assert.strictEqual(
    guard.signPionex('GET', '/uapi/v1/account/positions',
      { timestamp: 2, symbol: 'BTC_USDT_PERP' }, '', 'secret'),
    expect('GET/uapi/v1/account/positions?symbol=BTC_USDT_PERP&timestamp=2'),
  );

  const body = '{"symbol":"BTC_USDT_PERP","side":"BUY"}';
  assert.strictEqual(
    guard.signPionex('POST', '/uapi/v1/trade/order', { timestamp: 9 }, body, 'secret'),
    expect(`POST/uapi/v1/trade/order?timestamp=9${body}`),
  );

  // GET tidak menyertakan body
  assert.strictEqual(
    guard.signPionex('GET', '/uapi/v1/account/positions', { timestamp: 1 }, 'X', 's'),
    guard.signPionex('GET', '/uapi/v1/account/positions', { timestamp: 1 }, '', 's'),
  );
});

test('checkLiqAgainstExchange menolak estimator yang terlalu optimis', () => {
  const local = guard.liquidationPrice('LONG', 100000, 50, 0.005, 0.0005);
  assert.strictEqual(guard.checkLiqAgainstExchange('LONG', 100000, local, local * 1.0005)[0], true);
  assert.strictEqual(guard.checkLiqAgainstExchange('LONG', 100000, local, 0)[0], false);
  assert.strictEqual(guard.checkLiqAgainstExchange('LONG', 100000, local * 0.98, local)[0], false);
});
