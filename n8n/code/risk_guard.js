// n8n/code/risk_guard.js
// Port JavaScript dari risk_engine/risk_engine.py untuk dijalankan di n8n Code node.
//
// PENTING: risk_engine.py adalah SPESIFIKASI. Berkas ini adalah salinan untuk runtime
// n8n. Keduanya harus tetap sinkron -- bila salah satu diubah, ubah keduanya dan
// jalankan `python -m pytest -q` serta bandingkan keluaran pada sinyal uji yang sama.
//
// Mode n8n Code node: "Run Once for All Items"
// Input  : items = [{ json: { signal, equity, entry, rules, cfg, openPositions, breaker, now } }]
// Output : [{ json: { ...decision, clientOrderId?, orderBody? } }]

const DEFAULT_TAKER_FEE = 0.0005;
const DEFAULT_MMR = 0.005;

// ---------------------------------------------------------------- likuidasi

function liquidationRoom(leverage, mmr = DEFAULT_MMR, fee = DEFAULT_TAKER_FEE) {
  return 1 / leverage - mmr - fee;
}

// Jarak dari ENTRY ke harga likuidasi (fraksi). Identik dengan Python.
function liquidationDistance(leverage, mmr = DEFAULT_MMR, fee = DEFAULT_TAKER_FEE, side = 'LONG') {
  if (leverage <= 0) throw new Error('leverage harus > 0');
  const s = String(side).toUpperCase();
  if (s !== 'LONG' && s !== 'SHORT') throw new Error('side harus LONG atau SHORT');
  const room = liquidationRoom(leverage, mmr, fee);
  if (room <= 0) throw new Error(`leverage ${leverage}x tidak menyisakan ruang likuidasi`);
  return s === 'SHORT' ? (room + fee) / (1 - fee) : (room + fee) / (1 + fee);
}

function liquidationPrice(side, entry, leverage, mmr = DEFAULT_MMR, fee = DEFAULT_TAKER_FEE) {
  const s = String(side).toUpperCase();
  const imr = 1 / leverage;
  if (s === 'LONG') return (entry * (1 - imr + mmr + fee)) / (1 + fee);
  if (s === 'SHORT') return (entry * (1 + imr - mmr - fee)) / (1 - fee);
  throw new Error('side harus LONG atau SHORT');
}

// Guard: tolak entry bila estimator lokal tidak cocok dengan angka bursa.
function checkLiqAgainstExchange(side, entry, localLiq, exchangeLiq, tolerance = 0.002) {
  if (!exchangeLiq || exchangeLiq <= 0) return [false, 'EXCHANGE_LIQ_MISSING'];
  const drift = Math.abs(localLiq - exchangeLiq) / entry;
  if (drift > tolerance) return [false, `LIQ_MISMATCH drift=${drift.toFixed(5)} tol=${tolerance}`];
  const s = String(side).toUpperCase();
  if (s === 'LONG' && localLiq < exchangeLiq * (1 - tolerance)) return [false, 'LOCAL_LIQ_TOO_OPTIMISTIC_LONG'];
  if (s === 'SHORT' && localLiq > exchangeLiq * (1 + tolerance)) return [false, 'LOCAL_LIQ_TOO_OPTIMISTIC_SHORT'];
  return [true, 'OK'];
}

function maxUsableRr(leverage, stopDistance, mmr = DEFAULT_MMR, fee = DEFAULT_TAKER_FEE,
                     liqBufferPct = 33, targetFrac = 0.9) {
  const d = liquidationDistance(leverage, mmr, fee);
  const maxStop = d * (1 - liqBufferPct / 100);
  if (stopDistance <= 0 || stopDistance > maxStop) return 0;
  return (d * targetFrac) / stopDistance;
}

// ---------------------------------------------------------------- presisi

function roundStep(value, step, mode = 'down') {
  if (!(step > 0)) return value;
  const n = mode === 'down' ? Math.floor(value / step + 1e-12) : Math.ceil(value / step - 1e-12);
  return Math.round(n * step * 1e12) / 1e12;
}

function breakevenMove(fee = DEFAULT_TAKER_FEE) {
  return fee * 2;
}

// ---------------------------------------------------------------- EV

function expectedValue(winRate, avgWin, avgLoss, cost) {
  return winRate * avgWin - (1 - winRate) * avgLoss - cost;
}

function minWinRateForProfit(rr, fee = DEFAULT_TAKER_FEE) {
  const cost = breakevenMove(fee) * 100; // persen, agar satuannya sama dengan avg_win/avg_loss
  return (1 + cost) / (1 + rr);
}

// ---------------------------------------------------------------- sizing

function sizePosition(equity, entry, stopDistance, rules, cfg) {
  if (!(equity > 0) || !(entry > 0)) return { qty: 0, reason: 'EQUITY_OR_ENTRY_INVALID' };
  if (!(stopDistance > 0)) return { qty: 0, reason: 'STOP_DISTANCE_INVALID' };

  const effStop = stopDistance + cfg.slippage_buffer_pct / 100;
  const riskAmount = (equity * cfg.risk_per_trade_pct) / 100;

  const qtyRisk = riskAmount / (entry * effStop);
  const qtyLev = (equity * cfg.max_effective_leverage) / entry;
  const qtyMargin = ((equity * cfg.max_margin_per_trade_pct) / 100) * cfg.leverage / entry;

  const candidates = [
    ['RISK_BUDGET', qtyRisk],
    ['EFFECTIVE_LEVERAGE', qtyLev],
    ['MARGIN_CAP', qtyMargin],
  ];
  let qty = Math.min(qtyRisk, qtyLev, qtyMargin);
  let binding = candidates.reduce((a, b) => (b[1] < a[1] ? b : a))[0];

  const ceiling = (equity * cfg.max_effective_leverage) / entry;
  if (qty >= ceiling) {
    qty = ceiling;
    binding = 'EFFECTIVE_LEVERAGE_CEILING';
  }

  qty = roundStep(qty, rules.base_step, 'down');
  qty = Math.min(qty, rules.max_size);
  const notional = qty * entry;
  return {
    qty,
    notional,
    margin: notional / cfg.leverage,
    risk_amount: qty * entry * effStop,
    binding_constraint: binding,
  };
}

// ---------------------------------------------------------------- gerbang utama

function ageSec(iso, now) {
  if (!iso) return Infinity;
  const t = Date.parse(String(iso).replace('Z', '+00:00'));
  if (Number.isNaN(t)) return Infinity;
  return Math.max(0, (now - t) / 1000);
}

// DEFAULT DENY. Mengembalikan { approved, reasons, ... }.
function validateSignal(signal, equity, entry, rules, cfg, opts = {}) {
  const now = opts.now || Date.now();
  const reasons = [];
  const warnings = [];
  const stats = signal.stats || {};

  const direction = String(signal.direction || 'NONE').toUpperCase();
  if (direction !== 'LONG' && direction !== 'SHORT') reasons.push('DIRECTION_NONE');

  // --- Gerbang 1: MiroFish -------------------------------------------------
  const miro = signal.mirofish || {};
  if (!miro.schema_ok) reasons.push('MIROFISH_SCHEMA_INVALID');
  const verdictAge = ageSec(miro.verdict_ts, now);
  if (verdictAge > cfg.max_verdict_age_sec) reasons.push(`MIROFISH_STALE age=${verdictAge.toFixed(0)}s`);
  const conf = Number(miro.confidence || 0);
  if (conf < cfg.min_confidence) reasons.push(`MIROFISH_LOW_CONFIDENCE ${conf.toFixed(2)}<${cfg.min_confidence}`);
  const eventRisk = String(miro.event_risk || 'UNKNOWN').toUpperCase();
  if (cfg.veto_if_high_event_risk && eventRisk === 'HIGH') reasons.push('MIROFISH_HIGH_EVENT_RISK_VETO');
  const bias = String(miro.bias || 'NEUTRAL').toUpperCase();
  if (bias !== 'NEUTRAL' && bias !== direction && (direction === 'LONG' || direction === 'SHORT')) {
    reasons.push(`MIROFISH_BIAS_CONFLICT ${bias} vs ${direction}`);
  }

  // --- Gerbang 2: struktur trade ------------------------------------------
  const stop = Number(signal.stop);
  const tp = Number(signal.take_profit);
  let stopDistance = NaN;
  let rr = 0;
  let reward = NaN;
  if (!Number.isFinite(stop) || !Number.isFinite(tp) || stop <= 0 || tp <= 0) {
    reasons.push('MISSING_STOP_OR_TP');
  } else {
    if (direction === 'LONG') {
      stopDistance = (entry - stop) / entry;
      reward = (tp - entry) / entry;
    } else {
      stopDistance = (stop - entry) / entry;
      reward = (entry - tp) / entry;
    }
    if (stopDistance <= 0) reasons.push('STOP_ON_WRONG_SIDE');
    if (reward <= 0) reasons.push('TP_ON_WRONG_SIDE');
    rr = stopDistance > 0 ? reward / stopDistance : 0;
    if (rr < cfg.min_rr_ratio) reasons.push(`RR_TOO_LOW ${rr.toFixed(2)}<${cfg.min_rr_ratio}`);
  }

  const sigAge = ageSec(signal.ts, now);
  if (sigAge > cfg.max_signal_age_sec) reasons.push(`SIGNAL_STALE age=${sigAge.toFixed(0)}s`);

  // --- Gerbang 3: likuidasi ------------------------------------------------
  let liqDist = NaN;
  try {
    liqDist = liquidationDistance(cfg.leverage, rules.mmr, rules.taker_fee, direction);
  } catch (e) {
    reasons.push(`LEVERAGE_CONFIG_INVALID ${e.message}`);
  }
  if (Number.isFinite(liqDist) && Number.isFinite(stopDistance)) {
    const maxAllowed = liqDist * (1 - cfg.liq_buffer_pct / 100);
    if (stopDistance >= maxAllowed) {
      reasons.push(`STOP_TOO_FAR ${(stopDistance * 100).toFixed(4)}%>=${(maxAllowed * 100).toFixed(4)}%`);
    }
  }

  // --- Gerbang 4: statistik & EV -------------------------------------------
  const winRate = Number(stats.win_rate || 0);
  const avgWin = Number(stats.avg_win_pct || 0);
  const avgLoss = Number(stats.avg_loss_pct || 1);
  const cost = breakevenMove(rules.taker_fee);
  const ev = expectedValue(winRate, avgWin, avgLoss, cost);
  if (winRate <= 0) reasons.push('NO_TRACK_RECORD');
  else if (cfg.veto_on_negative_ev && ev <= 0) reasons.push(`NEGATIVE_EV ${ev.toFixed(5)}`);
  if (Number.isFinite(stopDistance) && stopDistance > 0 && avgLoss > stopDistance * 1.05) {
    reasons.push(`STATS_INCONSISTENT avg_loss=${avgLoss.toFixed(5)} > stop=${stopDistance.toFixed(5)}`);
  }
  if (avgWin > 0 && reward > 0 && avgWin > reward * 1.05) {
    reasons.push(`STATS_INCONSISTENT avg_win=${avgWin.toFixed(5)} > reward=${reward.toFixed(5)}`);
  }

  // --- Gerbang 5: kapasitas & breaker --------------------------------------
  const openPositions = Number(opts.openPositions || 0);
  if (openPositions >= cfg.max_open_positions) {
    reasons.push(`MAX_OPEN_POSITIONS ${openPositions}>=${cfg.max_open_positions}`);
  }
  if (opts.breaker && opts.breaker.tripped) {
    reasons.push(`CIRCUIT_BREAKER_TRIPPED ${opts.breaker.reason || ''}`);
  }

  // --- Sizing --------------------------------------------------------------
  let sizing = {};
  if (Number.isFinite(stopDistance) && stopDistance > 0) {
    sizing = sizePosition(equity, entry, stopDistance, rules, cfg);
  }
  const qty = Number(sizing.qty || 0);
  const notional = Number(sizing.notional || 0);
  if (qty <= 0) reasons.push('SIZE_ZERO');
  else {
    if (qty < rules.min_size) reasons.push(`BELOW_MIN_SIZE ${qty}<${rules.min_size}`);
    if (notional < rules.min_notional) reasons.push(`BELOW_MIN_NOTIONAL ${notional}<${rules.min_notional}`);
    if (notional > equity * cfg.max_effective_leverage) reasons.push('EFFECTIVE_LEVERAGE_BREACH');
  }

  // --- Peringatan ----------------------------------------------------------
  if (Number.isFinite(liqDist) && liqDist < 0.02) {
    warnings.push(`LIQ_DISTANCE_TINY ${(liqDist * 100).toFixed(3)}% -- ruang stop sempit`);
  }
  if (conf < 0.75) warnings.push('MIROFISH_CONFIDENCE_MARGINAL');
  if (rules.taker_fee > 0.0005) warnings.push('HIGH_TAKER_FEE');

  const approved = reasons.length === 0;
  return {
    approved,
    reasons,
    warnings,
    size: approved ? qty : 0,
    notional: approved ? notional : 0,
    margin: approved ? Number(sizing.margin || 0) : 0,
    stop_distance_pct: Number.isFinite(stopDistance) ? stopDistance : 0,
    liq_distance_pct: Number.isFinite(liqDist) ? liqDist : 0,
    risk_amount: approved ? Number(sizing.risk_amount || 0) : 0,
    rr_ratio: rr,
    ev_per_trade_pct: ev,
    effective_leverage: approved && equity > 0 ? notional / equity : 0,
    binding_constraint: sizing.binding_constraint || '',
  };
}

// ---------------------------------------------------------------- idempotensi & signing

function buildClientOrderId(signalId, symbol, action) {
  const crypto = require('crypto');
  const digest = crypto.createHash('sha256').update(`${signalId}|${symbol}|${action}`).digest('hex').slice(0, 24);
  const act = String(action).toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8) || 'ACT';
  return `pg-${act}-${digest}`.slice(0, 64);
}

function signPionex(method, path, query, body, secret) {
  const crypto = require('crypto');
  const joined = Object.keys(query).sort().map((k) => `${k}=${query[k]}`).join('&');
  const pathUrl = joined ? `${path}?${joined}` : path;
  let payload = `${method.toUpperCase()}${pathUrl}`;
  if (method.toUpperCase() === 'POST' || method.toUpperCase() === 'DELETE') payload += body || '';
  return crypto.createHmac('sha256', secret).update(payload, 'utf8').digest('hex');
}

// ---------------------------------------------------------------- entry point n8n

module.exports = {
  liquidationRoom, liquidationDistance, liquidationPrice, checkLiqAgainstExchange,
  maxUsableRr, roundStep, breakevenMove, expectedValue, minWinRateForProfit,
  sizePosition, validateSignal,
  buildClientOrderId,
  clientOrderId: buildClientOrderId,
  signPionex,
};

// Di n8n Code node, tempelkan fungsi-fungsi di atas lalu akhiri dengan blok berikut
// (module.exports diabaikan oleh n8n, tetapi membuat berkas ini bisa di-require saat uji):
/*
  const out = [];
  for (const item of items) {
    const { signal, equity, entry, rules, cfg, openPositions, breaker } = item.json;
    const decision = validateSignal(signal, equity, entry, rules, cfg,
                                    { openPositions, breaker, now: Date.now() });
    out.push({ json: { ...decision, signal_id: signal.id || null } });
  }
  return out;
*/
