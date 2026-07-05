// ── Global state ─────────────────────────────────────────────────────────────
let state = {
  symbols: [], active: null, filter: 'all',
  // legacy Chart.js chart instances (radar only now)
  chart: null, rsiChart: null,
  scorecardData: null, dossierData: null,
  // Lightweight Charts instances
  lwChart: null, lwSubChart: null,
  lwSeries: {},          // { candle, ema20, ema50, bbUpper, bbMid, bbLower, vol }
  lwSubSeries: null,
  chartData: null,       // cached raw API payload for re-render
  // UI toggles
  indToggles: { ema20: true, ema50: true, bb: true, vol: true },
  subchart: 'rsi',
  timeRange: '1Y',
  chatHistory: []
};

const $ = (id) => document.getElementById(id);
const fmtDate = (v) => v ? new Date(v).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-';
const money = (v) => v == null ? '-' : `$${Number(v).toFixed(2)}`;
const clip = (s, n = 220) => (s || '').length > n ? `${s.slice(0, n)}...` : (s || '');
const dateOnly = (v) => (v || '').slice(0, 10);

function showToast(msg, type = 'info', duration = 4000) {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = msg;
  document.body.appendChild(toast);
  toast.offsetHeight;
  toast.classList.add('show');
  setTimeout(() => { toast.classList.remove('show'); setTimeout(() => toast.remove(), 300); }, duration);
}

// ── Tab switching ─────────────────────────────────────────────────────────────

window.switchDetailTab = function(tab) {
  const chartBtn   = $('tabChartBtn');
  const scoreBtn   = $('tabScorecardBtn');
  const dossierBtn = $('tabDossierBtn');
  const chartView   = $('chartView');
  const scoreView   = $('scorecardView');
  const dossierView = $('dossierView');

  [chartBtn, scoreBtn, dossierBtn].forEach(b => b && b.classList.remove('active'));
  chartView.style.display   = 'none';
  scoreView.style.display   = 'none';
  if (dossierView) dossierView.style.display = 'none';

  if (tab === 'chart') {
    chartBtn.classList.add('active');
    chartView.style.display = 'flex';
    // Resize lw chart after display
    setTimeout(() => { if (state.lwChart) state.lwChart.applyOptions({ width: $('priceChartWrap').clientWidth }); }, 10);
  } else if (tab === 'scorecard') {
    scoreBtn.classList.add('active');
    scoreView.style.display = 'block';
    if (state.active) renderScorecard(state.active, state.scorecardData);
  } else if (tab === 'dossier') {
    if (dossierBtn) dossierBtn.classList.add('active');
    if (dossierView) dossierView.style.display = 'block';
    if (state.active) loadAndRenderDossier(state.active);
  }

  if (state.active) {
    const url = new URL(location.href);
    url.searchParams.set('tab', tab);
    history.replaceState({ symbol: state.active, tab }, '', url.toString());
  }
};

// ── Scorecard (radar — still uses Chart.js) ───────────────────────────────────

let scorecardChart = null;

function renderScorecard(symbol, card) {
  const placeholder = $('scorecardPlaceholder');
  const content = $('scorecardContent');
  $('scorecardCmdHelp').textContent = `python scripts/integrated_scorer.py ${symbol} --scorecard data/${symbol.toLowerCase()}_scorecard.json`;

  if (!card || !card.symbol) {
    placeholder.style.display = 'flex';
    content.style.display = 'none';
    return;
  }
  placeholder.style.display = 'none';
  content.style.display = 'grid';

  const badge = $('scorecardBadge');
  badge.textContent = `Score: ${card.final_score} / 100`;
  badge.className = card.final_score >= 85 ? 'badge success' : card.final_score >= 70 ? 'badge info' : 'badge';

  $('scorecardVerdict').textContent = card.verdict || '';
  $('scorecardMeta').innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
      <div>
        🏢 公司名稱：<b>${card.company || symbol}</b> |
        🌍 市場分類：<b>${card.market || '-'}</b><br>
        📅 評分更新：<b>${card.updated_at ? new Date(card.updated_at).toLocaleDateString() : '-'}</b>
      </div>
      <button id="regenerateScorecardBtn" onclick="triggerScorecardGeneration(true)" style="font-size: 11px; font-weight: bold; background: transparent; color: var(--green); border: 1px solid var(--green); padding: 4px 10px; border-radius: 6px; cursor: pointer; display: inline-flex; align-items: center; gap: 4px;">🔄 重新產生 AI 分析</button>
    </div>
  `;

  $('scorecardWeakness').innerHTML = (card.kill_switches || []).map(w => `<li>${escapeHtml(w)}</li>`).join('') || '<li>無特殊削弱因素紀錄</li>';
  $('scorecardEvidence').innerHTML = (card.evidence || []).map(ev =>
    `<li><b>[${ev.strength || 'weak'}]</b> ${escapeHtml(ev.claim || '')} <i>(${escapeHtml(ev.source || '')})</i></li>`
  ).join('') || '<li>無證據 notes 紀錄</li>';

  const factors = card.factor_details || {};
  const labels = ['需求拐點','架構耦合','瓶頸嚴重性','供應商集中','擴產難度','證據品質','估值落差','催化劑時機'];
  const keys   = ['demand_inflection','architecture_coupling','chokepoint_severity','supplier_concentration','expansion_difficulty','evidence_quality','valuation_disconnect','catalyst_timing'];

  const ctx = $('scorecardRadar');
  if (scorecardChart) scorecardChart.destroy();
  scorecardChart = new Chart(ctx, {
    type: 'radar',
    data: {
      labels,
      datasets: [{
        label: '因素評分 (0-5)',
        data: keys.map(k => (factors[k] ? factors[k].rating : 0)),
        backgroundColor: 'rgba(31,122,79,0.15)',
        borderColor: '#1f7a4f',
        borderWidth: 2,
        pointBackgroundColor: '#1f7a4f',
        pointBorderColor: '#fff',
        pointRadius: 3
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, layout: { padding: 8 },
      scales: {
        r: {
          angleLines: { color: 'rgba(24,32,25,.08)' },
          grid: { color: 'rgba(24,32,25,.08)' },
          suggestedMin: 0, suggestedMax: 5,
          ticks: { stepSize: 1, display: false },
          pointLabels: { font: { size: 9, weight: 'bold' }, color: '#182019', padding: 3 }
        }
      },
      plugins: { legend: { display: false } }
    }
  });
}

window.triggerScorecardGeneration = async function(isRegen = false) {
  const symbol = state.active;
  if (!symbol) return;
  const btn = isRegen ? $('regenerateScorecardBtn') : $('generateScorecardBtn');
  if (!btn) return;
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = isRegen ? '🔄 正在重新分析中...' : '⚡ 正在產生 AI 瓶頸分析，請稍候 (約 10-20 秒)...';
  btn.style.opacity = 0.7;
  try {
    const res = await fetch(`/api/scorecard/generate/${encodeURIComponent(symbol)}`, { method: 'POST' });
    if (res.ok) {
      const result = await res.json();
      if (result.success) {
        state.scorecardData = await json(`/api/scorecard/${encodeURIComponent(symbol)}`);
        renderScorecard(symbol, state.scorecardData);
        showToast(`$${symbol} AI 供應鏈瓶頸分析已更新完成！`, 'info');
      } else { showToast(`產生分析失敗：${result.error || '未知錯誤'}`, 'error'); }
    } else { showToast(`伺服器錯誤 ${res.status}`, 'error'); }
  } catch (err) { showToast(`連線錯誤：${err.message}`, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = originalText; btn.style.opacity = 1.0; }
  }
};

// ── Lightweight Charts helpers ─────────────────────────────────────────────────

const LW_THEME = {
  layout:      { background: { type: 'solid', color: 'transparent' }, textColor: '#182019' },
  grid:        { vertLines: { color: 'rgba(24,32,25,0.07)' }, horzLines: { color: 'rgba(24,32,25,0.07)' } },
  crosshair:   { mode: 1 },  // Normal
  rightPriceScale: { borderColor: 'rgba(24,32,25,0.15)' },
  timeScale:   { borderColor: 'rgba(24,32,25,0.15)', timeVisible: false }
};

function destroyLwCharts() {
  if (state.lwChart)    { state.lwChart.remove();    state.lwChart    = null; }
  if (state.lwSubChart) { state.lwSubChart.remove();  state.lwSubChart = null; }
  state.lwSeries    = {};
  state.lwSubSeries = null;
}

// Compute cutoff date from range string
function rangeCutoff(range, lastDate) {
  if (range === 'ALL' || !lastDate) return null;
  const months = { '1M': 1, '3M': 3, '6M': 6, '1Y': 12 }[range] || 12;
  const d = new Date(lastDate);
  d.setMonth(d.getMonth() - months);
  return d.toISOString().slice(0, 10);
}

function applyTimeRange(range) {
  if (!state.lwChart || !state.chartData) return;
  const bars = state.chartData.bars || [];
  if (!bars.length) return;
  const last = bars[bars.length - 1].date;
  if (range === 'ALL') {
    state.lwChart.timeScale().fitContent();
    if (state.lwSubChart) state.lwSubChart.timeScale().fitContent();
  } else {
    const from = rangeCutoff(range, last);
    const to   = last;
    try { state.lwChart.timeScale().setVisibleRange({ from, to }); } catch(_) {}
    try { if (state.lwSubChart) state.lwSubChart.timeScale().setVisibleRange({ from, to }); } catch(_) {}
  }
}

window.setTimeRange = function(range) {
  state.timeRange = range;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.toggle('active', b.dataset.range === range));
  applyTimeRange(range);
};

window.toggleIndicator = function(key) {
  state.indToggles[key] = !state.indToggles[key];
  const idMap = { ema20: 'indEMA20', ema50: 'indEMA50', bb: 'indBB', vol: 'indVol' };
  const btn = $(idMap[key]);
  if (btn) btn.classList.toggle('active', state.indToggles[key]);

  const s = state.lwSeries;
  const vis = state.indToggles[key];
  if (key === 'ema20' && s.ema20) s.ema20.applyOptions({ visible: vis });
  if (key === 'ema50' && s.ema50) s.ema50.applyOptions({ visible: vis });
  if (key === 'bb') {
    if (s.bbUpper) s.bbUpper.applyOptions({ visible: vis });
    if (s.bbMid)   s.bbMid.applyOptions({ visible: vis });
    if (s.bbLower) s.bbLower.applyOptions({ visible: vis });
  }
  if (key === 'vol' && s.vol) s.vol.applyOptions({ visible: vis });
};

window.setSubchart = function(type) {
  state.subchart = type;
  document.querySelectorAll('.sub-btn').forEach(b => b.classList.toggle('active', b.dataset.sub === type));
  if (state.chartData) renderSubchart(state.chartData);
};

// ── Main chart rendering (LightweightCharts) ──────────────────────────────────

function renderChart(data) {
  state.chartData = data;
  const allBars    = data.bars       || [];
  const mentions   = data.mentions   || [];
  const indicators = data.indicators || {};

  destroyLwCharts();

  const container = $('priceChartWrap');
  if (!container) return;

  // ── Build main chart ───────────────────────────────────────────────────────
  const chart = LightweightCharts.createChart(container, {
    ...LW_THEME,
    width:  container.clientWidth,
    height: Math.max(360, Math.floor(container.clientHeight || 380)),
    handleScroll:  { mouseWheel: true, pressedMouseMove: true },
    handleScale:   { mouseWheel: true, pinch: true },
  });
  state.lwChart = chart;

  // Candlestick series
  const candle = chart.addCandlestickSeries({
    upColor:        '#1f7a4f',
    downColor:      '#ff6b35',
    borderUpColor:  '#1f7a4f',
    borderDownColor:'#ff6b35',
    wickUpColor:    '#1f7a4f',
    wickDownColor:  '#ff6b35',
    priceLineVisible: false,
  });

  const candleData = allBars
    .filter(b => b.close != null)
    .map(b => ({
      time:  b.date,
      open:  b.open  ?? b.close,
      high:  b.high  ?? b.close,
      low:   b.low   ?? b.close,
      close: b.close,
    }));
  candle.setData(candleData);

  // Mention markers on candle series
  const dateSet = new Set(allBars.map(b => b.date));
  const markerDates = new Set();
  const markers = [];
  for (const m of mentions) {
    const d = dateOnly(m.mentioned_at);
    if (!d || markerDates.has(d)) continue;
    // Find closest bar date >= d
    const bar = allBars.find(b => b.date >= d) || allBars[allBars.length - 1];
    if (!bar) continue;
    markerDates.add(d);
    markers.push({ time: bar.date, position: 'aboveBar', color: '#ff6b35', shape: 'circle', size: 0.8, text: '' });
  }
  if (markers.length) candle.setMarkers(markers.sort((a,b) => a.time < b.time ? -1 : 1));

  // Volume histogram (secondary price scale)
  const vol = chart.addHistogramSeries({
    color: 'rgba(31,122,79,0.3)',
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
    lastValueVisible: false,
    priceLineVisible: false,
  });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
  const volData = allBars
    .filter(b => b.volume != null)
    .map(b => ({
      time:  b.date,
      value: b.volume,
      color: (b.close >= (b.open ?? b.close)) ? 'rgba(31,122,79,0.35)' : 'rgba(255,107,53,0.35)',
    }));
  vol.setData(volData);
  vol.applyOptions({ visible: state.indToggles.vol });

  // EMA 20
  const ema20Raw = indicators.ema20 || [];
  const ema20 = chart.addLineSeries({
    color: '#8db800', lineWidth: 1.5,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
  });
  ema20.setData(allBars.map((b, i) => ema20Raw[i] != null ? { time: b.date, value: ema20Raw[i] } : null).filter(Boolean));
  ema20.applyOptions({ visible: state.indToggles.ema20 });

  // EMA 50
  const ema50Raw = indicators.ema50 || [];
  const ema50 = chart.addLineSeries({
    color: '#ff6b35', lineWidth: 1.5,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
  });
  ema50.setData(allBars.map((b, i) => ema50Raw[i] != null ? { time: b.date, value: ema50Raw[i] } : null).filter(Boolean));
  ema50.applyOptions({ visible: state.indToggles.ema50 });

  // Bollinger Bands
  const bbRaw = indicators.bb || [];
  const bbLineOpts = { color: 'rgba(31,77,122,0.35)', lineWidth: 1, lineStyle: 1 /* dashed */, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  const bbUpper = chart.addLineSeries({ ...bbLineOpts });
  const bbMid   = chart.addLineSeries({ ...bbLineOpts, color: 'rgba(31,77,122,0.5)', lineStyle: 0 });
  const bbLower = chart.addLineSeries({ ...bbLineOpts });
  bbUpper.setData(allBars.map((b, i) => bbRaw[i]?.upper != null ? { time: b.date, value: bbRaw[i].upper } : null).filter(Boolean));
  bbMid.setData(allBars.map((b, i)   => bbRaw[i]?.mid   != null ? { time: b.date, value: bbRaw[i].mid   } : null).filter(Boolean));
  bbLower.setData(allBars.map((b, i) => bbRaw[i]?.lower != null ? { time: b.date, value: bbRaw[i].lower } : null).filter(Boolean));
  [bbUpper, bbMid, bbLower].forEach(s => s.applyOptions({ visible: state.indToggles.bb }));

  state.lwSeries = { candle, vol, ema20, ema50, bbUpper, bbMid, bbLower };

  // ── Crosshair tooltip ──────────────────────────────────────────────────────
  chart.subscribeCrosshairMove(param => {
    const tooltip = $('chartTooltip');
    if (!tooltip) return;
    if (!param || !param.time || !param.seriesData || !param.seriesData.has(candle)) {
      tooltip.style.display = 'none';
      return;
    }
    const ohlc = param.seriesData.get(candle);
    const e20  = param.seriesData.get(ema20);
    const e50  = param.seriesData.get(ema50);
    const bbu  = param.seriesData.get(bbUpper);
    const bbl  = param.seriesData.get(bbLower);
    const v    = param.seriesData.get(vol);
    const fmt  = x => x != null ? `$${Number(x).toFixed(2)}` : '—';
    const fmtV = x => x != null ? (x >= 1e6 ? `${(x/1e6).toFixed(1)}M` : x >= 1e3 ? `${(x/1e3).toFixed(0)}K` : String(x)) : '—';

    const changeAmt  = (ohlc?.close != null && ohlc?.open != null) ? (ohlc.close - ohlc.open) : null;
    const changePct  = (changeAmt != null && ohlc.open) ? (changeAmt / ohlc.open * 100) : null;
    const upDay = changeAmt != null ? changeAmt >= 0 : null;

    tooltip.innerHTML = `
      <div class="tt-date">${param.time}</div>
      <div class="tt-ohlc">
        <span>O: ${fmt(ohlc?.open)}</span>
        <span>H: ${fmt(ohlc?.high)}</span>
        <span>L: ${fmt(ohlc?.low)}</span>
        <span style="font-weight:700;color:${upDay === null ? 'inherit' : upDay ? '#1f7a4f' : '#ff6b35'}">C: ${fmt(ohlc?.close)}</span>
        ${changePct != null ? `<span style="color:${upDay ? '#1f7a4f' : '#ff6b35'}">${upDay ? '+' : ''}${changePct.toFixed(2)}%</span>` : ''}
      </div>
      ${e20  ? `<div class="tt-ind">EMA20: ${fmt(e20.value)}</div>` : ''}
      ${e50  ? `<div class="tt-ind">EMA50: ${fmt(e50.value)}</div>` : ''}
      ${bbu  ? `<div class="tt-ind">BB上軌: ${fmt(bbu.value)}</div>` : ''}
      ${bbl  ? `<div class="tt-ind">BB下軌: ${fmt(bbl.value)}</div>` : ''}
      ${v    ? `<div class="tt-ind">成交量: ${fmtV(v.value)}</div>` : ''}
    `;

    const px = param.point?.x ?? 0;
    const py = param.point?.y ?? 0;
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    tooltip.style.display = 'block';
    tooltip.style.left = (px + 12 + 160 > cw) ? `${px - 170}px` : `${px + 12}px`;
    tooltip.style.top  = `${Math.min(py, ch - 110)}px`;
  });

  // ── Resize observer ────────────────────────────────────────────────────────
  if (state._chartRO) state._chartRO.disconnect();
  state._chartRO = new ResizeObserver(() => {
    if (state.lwChart) state.lwChart.applyOptions({ width: container.clientWidth });
  });
  state._chartRO.observe(container);

  // ── Sync subchart time range when main chart scrolls ──────────────────────
  chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (range && state.lwSubChart) {
      try { state.lwSubChart.timeScale().setVisibleLogicalRange(range); } catch(_) {}
    }
  });

  // Apply initial time range
  applyTimeRange(state.timeRange);

  // Render sub-chart
  renderSubchart(data);
}

// ── Sub-chart (RSI or MACD) ───────────────────────────────────────────────────

function renderSubchart(data) {
  if (state.lwSubChart) {
    state.lwSubChart.remove();
    state.lwSubChart    = null;
    state.lwSubSeries   = null;
  }

  const wrap = $('subChartWrap');
  const cont = $('subChartContainer');
  const label = $('subChartLabel');
  if (!wrap || !cont) return;

  if (state.subchart === 'none') {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = 'block';

  const allBars    = (data.bars       || []);
  const indicators = data.indicators || {};

  const subChart = LightweightCharts.createChart(cont, {
    ...LW_THEME,
    width:  cont.clientWidth,
    height: cont.clientHeight || 100,
    handleScroll: { mouseWheel: true, pressedMouseMove: true },
    handleScale:  { mouseWheel: true },
    timeScale: { ...LW_THEME.timeScale, visible: false },
  });
  state.lwSubChart = subChart;

  if (state.subchart === 'rsi') {
    label.textContent = 'RSI (14)';
    const rsiRaw = indicators.rsi14 || [];
    const rsiSeries = subChart.addLineSeries({
      color: '#1f4d7a', lineWidth: 1.5,
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: true,
    });
    rsiSeries.setData(allBars.map((b, i) => rsiRaw[i] != null ? { time: b.date, value: rsiRaw[i] } : null).filter(Boolean));
    // Overbought / Oversold reference lines
    const ob = subChart.addLineSeries({ color: 'rgba(255,107,53,0.55)', lineWidth: 1, lineStyle: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const os = subChart.addLineSeries({ color: 'rgba(31,122,79,0.55)',  lineWidth: 1, lineStyle: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const boundData = (val) => allBars.map(b => ({ time: b.date, value: val }));
    ob.setData(boundData(70));
    os.setData(boundData(30));
    subChart.priceScale('right').applyOptions({ autoScale: false, minValue: 0, maxValue: 100 });
    state.lwSubSeries = rsiSeries;
  } else if (state.subchart === 'macd') {
    label.textContent = 'MACD (12/26/9)';
    const macdRaw = indicators.macd || [];
    const macdLine = subChart.addLineSeries({ color: '#1f7a4f', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
    const sigLine  = subChart.addLineSeries({ color: '#ff6b35', lineWidth: 1,   priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
    const hist     = subChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });

    macdLine.setData(allBars.map((b, i) => macdRaw[i]?.macd     != null ? { time: b.date, value: macdRaw[i].macd }      : null).filter(Boolean));
    sigLine.setData (allBars.map((b, i) => macdRaw[i]?.signal   != null ? { time: b.date, value: macdRaw[i].signal }    : null).filter(Boolean));
    hist.setData    (allBars.map((b, i) => macdRaw[i]?.histogram != null ? { time: b.date, value: macdRaw[i].histogram, color: macdRaw[i].histogram >= 0 ? 'rgba(31,122,79,0.5)' : 'rgba(255,107,53,0.5)' } : null).filter(Boolean));
    state.lwSubSeries = macdLine;
  }

  // Resize observer for subchart
  if (state._subRO) state._subRO.disconnect();
  state._subRO = new ResizeObserver(() => {
    if (state.lwSubChart) state.lwSubChart.applyOptions({ width: cont.clientWidth });
  });
  state._subRO.observe(cont);

  // Sync subchart ↔ main chart logical range
  subChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (range && state.lwChart) {
      try { state.lwChart.timeScale().setVisibleLogicalRange(range); } catch(_) {}
    }
  });

  // Apply current time range
  applyTimeRange(state.timeRange);
}

// ── Signal panel ──────────────────────────────────────────────────────────────

function renderSignalPanel(signal) {
  const el = $('signalPanel');
  if (!el) return;
  if (!signal) { el.style.display = 'none'; return; }
  if (signal.insufficient_data) {
    el.style.display = 'block';
    el.innerHTML = '<p class="signal-insufficient">📊 Not enough price history to compute signal.</p>';
    return;
  }
  const sig = signal.signal || 'NEUTRAL';
  const badgeClass = { BUY_WATCH:'sig-buy-watch', BUY_TRIGGER:'sig-buy-trigger', HOLD:'sig-hold', EXIT_ALERT:'sig-exit', OVERBOUGHT:'sig-overbought', NEUTRAL:'sig-neutral' }[sig] || 'sig-neutral';
  const fmt   = v => (v == null ? '—' : `$${Number(v).toFixed(2)}`);
  const fmtRR = v => (v == null ? '—' : `1:${Number(v).toFixed(1)}`);
  const entry = signal.entry_zone || {};
  const entryStr = (entry.low != null && entry.high != null) ? `${fmt(entry.low)} – ${fmt(entry.high)}` : '—';

  el.style.display = 'block';
  el.innerHTML = `
    <div class="signal-head">
      <span class="sig-badge ${badgeClass}">${sig.replace(/_/g, ' ')}</span>
      ${signal.score != null ? `<span class="signal-pill">Score ${signal.score}</span>` : ''}
      ${signal.rsi   != null ? `<span class="signal-pill">RSI ${Number(signal.rsi).toFixed(1)}</span>` : ''}
      ${signal.atr14 != null ? `<span class="signal-pill">ATR ${Number(signal.atr14).toFixed(2)}</span>` : ''}
    </div>
    <div class="signal-conditions">
      ${(signal.conditions || []).map(c => `
        <span class="signal-cond ${c.met ? 'cond-met' : 'cond-unmet'}">
          ${c.met ? '✓' : '✗'} ${escapeHtml(c.label)}${c.detail ? ` <em>${escapeHtml(c.detail)}</em>` : ''}
        </span>`).join('')}
    </div>
    <div class="signal-levels">
      <div class="sig-level"><span>Entry</span><b>${entryStr}</b></div>
      <div class="sig-level"><span>Stop</span><b>${fmt(signal.stop_loss)}</b></div>
      <div class="sig-level"><span>Risk/sh</span><b>${fmt(signal.risk_per_share)}</b></div>
      <div class="sig-level"><span>Target</span><b>${fmt(signal.target)}</b></div>
      <div class="sig-level"><span>R:R</span><b>${fmtRR(signal.rr_ratio)}</b></div>
    </div>
  `;
}

// ── Fundamentals card ─────────────────────────────────────────────────────────

async function loadFundamentals(symbol) {
  const el = $('fundamentalsContent');
  if (!el) return;
  try {
    const data = await fetch(`/api/fundamentals/${encodeURIComponent(symbol)}`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    if (!data || data.error || !data.symbol) throw new Error('empty');
    renderFundamentals(data);
  } catch (_) {
    el.innerHTML = '<p class="placeholder-text">資料尚未抓取</p>';
  }
}

function renderFundamentals(d) {
  const el = $('fundamentalsContent');
  if (!el) return;
  const fmt  = v => v != null ? v : '—';
  const fmtPct = v => v != null ? `${(Number(v)*100).toFixed(1)}%` : '—';
  const fmtCap = v => {
    if (v == null) return '—';
    const n = Number(v);
    if (n >= 1e12) return `$${(n/1e12).toFixed(2)}T`;
    if (n >= 1e9)  return `$${(n/1e9).toFixed(1)}B`;
    if (n >= 1e6)  return `$${(n/1e6).toFixed(0)}M`;
    return `$${n}`;
  };
  el.innerHTML = `
    <div class="fund-item"><span class="fund-label">P/E</span><span class="fund-val">${fmt(d.pe)}</span></div>
    <div class="fund-item"><span class="fund-label">預期 P/E</span><span class="fund-val">${fmt(d.forward_pe)}</span></div>
    <div class="fund-item"><span class="fund-label">EPS (TTM)</span><span class="fund-val">${fmt(d.eps_ttm)}</span></div>
    <div class="fund-item"><span class="fund-label">營收年增</span><span class="fund-val">${fmtPct(d.revenue_growth_yoy)}</span></div>
    <div class="fund-item"><span class="fund-label">毛利率</span><span class="fund-val">${fmtPct(d.gross_margin)}</span></div>
    <div class="fund-item"><span class="fund-label">市值</span><span class="fund-val">${fmtCap(d.market_cap)}</span></div>
    <div class="fund-item fund-item--wide"><span class="fund-label">下次財報日</span><span class="fund-val">${fmt(d.next_earnings_date)}</span></div>
    ${d.updated_at ? `<div class="fund-item fund-item--wide" style="opacity:.5;font-size:10px;"><span class="fund-label">更新時間</span><span class="fund-val">${d.updated_at}</span></div>` : ''}
  `;
}

// ── News panel ────────────────────────────────────────────────────────────────

async function loadNews(symbol) {
  const el = $('newsContent');
  if (!el) return;
  try {
    const data = await fetch(`/api/news/${encodeURIComponent(symbol)}`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    if (!data || data.error || (!data.items?.length && !data.macro?.length)) throw new Error('empty');
    renderNews(data);
  } catch (_) {
    el.innerHTML = '<p class="placeholder-text">資料尚未抓取</p>';
  }
}

function relTime(ts) {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 60)   return `${m}m 前`;
  const h = Math.floor(m / 60);
  if (h < 24)   return `${h}h 前`;
  const d = Math.floor(h / 24);
  return `${d}d 前`;
}

function renderNewsItems(items) {
  if (!items || !items.length) return '<p class="placeholder-text">暫無新聞</p>';
  return items.map(it => `
    <div class="news-item">
      <a href="${escapeHtml(it.url || '#')}" target="_blank" rel="noreferrer" class="news-title">${escapeHtml(it.title || '—')}</a>
      <div class="news-meta">
        <span class="news-source">${escapeHtml(it.source || '')}</span>
        <span class="news-time">${relTime(it.published_at)}</span>
      </div>
      ${it.summary ? `<p class="news-summary">${escapeHtml(it.summary)}</p>` : ''}
    </div>
  `).join('');
}

function renderNews(data) {
  const el = $('newsContent');
  if (!el) return;
  const items  = data.items  || [];
  const macro  = data.macro  || [];
  el.innerHTML = `
    <div class="news-section-label">個股新聞</div>
    ${renderNewsItems(items)}
    ${macro.length ? `<div class="news-section-label" style="margin-top:12px;">國際 / 總經</div>${renderNewsItems(macro)}` : ''}
    ${data.as_of ? `<div style="font-size:10px;color:var(--muted);margin-top:8px;text-align:right;">截至 ${data.as_of}</div>` : ''}
  `;
}

// ── Feed (X posts) rendered inside accordion ──────────────────────────────────

function renderFeed(items) {
  const el = $('feed');
  if (!el) return;
  // Update evidence count badge
  const badge = $('evidenceCount');
  if (badge) badge.textContent = items.length ? `(${items.length})` : '';
  el.innerHTML = items.map(i => `
    <article class="feed-item">
      <div><span class="ticker">$${i.symbol}</span> <span class="tiny">${fmtDate(i.mentioned_at)} / ${i.source}</span></div>
      <p>${escapeHtml(clip(i.text, 340))}</p>
      <a href="${i.url}" target="_blank" rel="noreferrer">open on X</a>
    </article>
  `).join('');
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return (s || '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}

async function json(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

// ── URL routing ───────────────────────────────────────────────────────────────

window.copyLink = function() {
  const url = location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => showToast('Link copied!', 'info', 2000)).catch(() => _fallbackCopy(url));
  } else { _fallbackCopy(url); }
};

function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); showToast('Link copied!', 'info', 2000); }
  catch (_) { showToast('Could not copy — copy the URL manually.', 'error'); }
  document.body.removeChild(ta);
}

// ── Mobile bottom navigation ──────────────────────────────────────────────────

window.setMobilePanel = function(panel) {
  const panelMap = { list: '.symbols-panel', chart: '.main-panel', score: '.main-panel', chat: '.chat-panel' };
  document.querySelectorAll('.symbols-panel, .main-panel, .chat-panel').forEach(el => el.classList.remove('mobile-visible'));
  const target = document.querySelector(panelMap[panel]);
  if (target) target.classList.add('mobile-visible');
  if (panel === 'score') switchDetailTab('scorecard');
  else if (panel === 'chart') switchDetailTab('chart');
  document.querySelectorAll('.mobile-bottom-nav button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.panel === panel));
};

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const params    = new URLSearchParams(location.search);
  const initSymbol = (params.get('s') || '').toUpperCase() || null;
  const initTab   = params.get('tab') || 'chart';

  try {
    const config = await json('/api/config');
    if (config.default_model) {
      const select = $('chatModel');
      const hasOption = Array.from(select.options).some(opt => opt.value === config.default_model);
      if (hasOption) { select.value = config.default_model; }
      else { select.value = 'custom'; $('customModelInput').style.display = 'inline-block'; $('customModelInput').value = config.default_model; }
    }
  } catch (_) {}

  try {
    const summary = await json('/api/summary');
    state.symbols = summary.symbols || [];
    renderKpis(summary.stats || {});
    renderSymbols();
    const urlSym = initSymbol && state.symbols.find(s => s.symbol === initSymbol) ? initSymbol : null;
    const first  = state.symbols.find(s => s.has_prices) || state.symbols[0];
    const target = urlSym || first?.symbol;
    if (target) {
      if (initTab !== 'chart') switchDetailTab(initTab);
      await selectSymbol(target, { pushState: false });
      const tab = _activeTab();
      history.replaceState({ symbol: target, tab }, '', `/?s=${encodeURIComponent(target)}&tab=${tab}`);
    }
  } catch (err) {
    console.error('Failed to load summary:', err);
    $('symbols').innerHTML = '<p style="padding: 16px; color: var(--muted); font-size: 13px;">⚠️ 載入股票清單失敗，請稍候重試。</p>';
  }

  try {
    const feed = await json('/api/feed?limit=36');
    renderFeed(feed.items || []);
  } catch (err) { console.error('Failed to load feed:', err); }

  updateMemoryStatus();
  if (window.innerWidth <= 768) setMobilePanel('list');
}

window.addEventListener('popstate', (e) => {
  if (!e.state || !e.state.symbol) return;
  const { symbol, tab = 'chart' } = e.state;
  if (tab !== _activeTab()) switchDetailTab(tab);
  if (symbol !== state.active) selectSymbol(symbol, { pushState: false });
});

function renderKpis(s) {
  const items = [
    ['tweets','貼文入庫'],['mentions','Symbol 提及'],['symbols','唯一 Symbol'],
    ['priced_symbols','已下載價格'],['latest_mention','最新提及']
  ];
  $('kpis').innerHTML = items.map(([k, label]) =>
    `<div class="kpi"><b>${k === 'latest_mention' ? fmtDate(s[k]) : (s[k] ?? 0)}</b><span>${label}</span></div>`
  ).join('');
}

function visibleSymbols() {
  const q = $('symbolSearch').value.trim().toUpperCase();
  return state.symbols.filter(s => {
    if (q && !s.symbol.includes(q)) return false;
    if (state.filter === 'priced' && !s.has_prices) return false;
    if (state.filter === 'hot'    && s.mention_count < 5) return false;
    return true;
  });
}

function renderSymbols() {
  $('symbols').innerHTML = visibleSymbols().map(s => `
    <button class="symbol-row ${state.active === s.symbol ? 'active' : ''}" data-symbol="${s.symbol}">
      <span class="ticker">${s.symbol}</span>
      <span><b>${s.mention_count}</b> mentions<br><span class="tiny">latest ${fmtDate(s.latest_mention)}</span></span>
      <span class="badge">${s.has_prices ? money(s.last_close) : '無價格'}</span>
    </button>
  `).join('');
  document.querySelectorAll('.symbol-row').forEach(btn => btn.onclick = () => selectSymbol(btn.dataset.symbol));
}

function _activeTab() {
  if ($('tabDossierBtn') && $('tabDossierBtn').classList.contains('active')) return 'dossier';
  if ($('tabScorecardBtn').classList.contains('active')) return 'scorecard';
  return 'chart';
}

async function selectSymbol(symbol, { pushState = true } = {}) {
  state.active = symbol;
  state.dossierData = null;

  if (pushState) {
    history.pushState({ symbol, tab: _activeTab() }, '', `/?s=${encodeURIComponent(symbol)}&tab=${_activeTab()}`);
  }

  document.querySelectorAll('.symbol-row').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.symbol === symbol));

  if (window.innerWidth <= 768) setMobilePanel('chart');

  const data = await json(`/api/symbol/${encodeURIComponent(symbol)}`);
  const info = state.symbols.find(s => s.symbol === symbol) || {};
  $('activeTitle').textContent = `$${symbol}`;
  $('activeMeta').innerHTML = [
    `${info.mention_count || 0} mentions`,
    `${(data.bars || data.prices || []).length} bars`,
    `first ${fmtDate(info.first_mention)}`,
    `latest ${fmtDate(info.latest_mention)}`
  ].map(x => `<span>${x}</span>`).join('');
  $('neighbors').innerHTML = (data.neighbors || []).slice(0, 12).map(n => `<span>${n.symbol} x${n.count}</span>`).join('');

  renderChart(data);

  try {
    const signalData = await json(`/api/signal/${encodeURIComponent(symbol)}`);
    renderSignalPanel(signalData && !signalData.error ? signalData : null);
  } catch (_) { renderSignalPanel(null); }

  try { state.scorecardData = await json(`/api/scorecard/${encodeURIComponent(symbol)}`); }
  catch (_) { state.scorecardData = null; }

  if ($('tabScorecardBtn').classList.contains('active')) renderScorecard(symbol, state.scorecardData);
  if ($('tabDossierBtn') && $('tabDossierBtn').classList.contains('active')) loadAndRenderDossier(symbol);

  // Load new panels (graceful degradation)
  loadFundamentals(symbol);
  loadNews(symbol);
}

// ── Symbol filter tabs & search ───────────────────────────────────────────────

document.querySelectorAll('.symbols-panel .tabs button').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('.symbols-panel .tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.filter = btn.dataset.filter;
  renderSymbols();
});
$('symbolSearch').addEventListener('input', renderSymbols);

// ── AI Chat panel ─────────────────────────────────────────────────────────────

function appendChatMessage(role, text) {
  const container = $('chatMessages');
  const msgEl = document.createElement('div');
  msgEl.className = `msg ${role}`;
  if (role === 'model' || role === 'system') {
    msgEl.innerHTML = escapeHtml(text)
      .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
      .replace(/`(.*?)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
  } else { msgEl.textContent = text; }
  container.appendChild(msgEl);
  container.scrollTop = container.scrollHeight;
}

window.clickSampleQuestion = function(text) { $('chatInput').value = text; sendChatMessage(); };

async function sendChatMessage() {
  const input = $('chatInput'), sendBtn = $('chatSend');
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; input.disabled = true; sendBtn.disabled = true;
  appendChatMessage('user', text);
  state.chatHistory.push({ role: 'user', content: text });
  const loadingEl = document.createElement('div');
  loadingEl.className = 'msg system loading';
  loadingEl.textContent = 'Serenity 正在分析中...';
  $('chatMessages').appendChild(loadingEl);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 45000);
  try {
    const selectVal = $('chatModel').value;
    const modelName = selectVal === 'custom' ? $('customModelInput').value.trim() || 'gemini-2.5-flash' : selectVal;
    const trimmedHistory = state.chatHistory.length > 6 ? state.chatHistory.slice(-6) : state.chatHistory;
    const res = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: trimmedHistory, model: modelName }), signal: controller.signal
    });
    clearTimeout(timeoutId); loadingEl.remove();
    if (!res.ok) throw new Error(`HTTP Error ${res.status}`);
    const data = await res.json();
    if (data.error) { appendChatMessage('system', `錯誤：${data.error}`); }
    else {
      const reply = data.response || 'AI 未能給出有效回覆。';
      appendChatMessage('model', reply);
      state.chatHistory.push({ role: 'model', content: reply });
      setTimeout(updateMemoryStatus, 2000);
    }
  } catch (err) {
    clearTimeout(timeoutId); loadingEl.remove();
    if (err.name === 'AbortError') appendChatMessage('system', '請求逾時（已過 45 秒未響應）。已自動釋放對話欄，請嘗試重新發送或切換為 Gemini 2.5 Flash。');
    else appendChatMessage('system', `連線錯誤：${err.message}`);
  } finally { input.disabled = false; sendBtn.disabled = false; input.focus(); }
}

async function updateMemoryStatus() {
  try {
    const res = await fetch('/api/memory');
    if (res.ok) {
      const data = await res.json();
      $('memoryStatus').textContent = `🧠 記憶：${(data.memories || []).length} 條`;
    }
  } catch (_) {}
}

$('chatSend').onclick = sendChatMessage;
$('chatInput').onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); } };
$('chatInput').addEventListener('input', function() { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 120) + 'px'; });
$('chatModel').addEventListener('change', (e) => {
  const ci = $('customModelInput');
  ci.style.display = e.target.value === 'custom' ? 'inline-block' : 'none';
  if (e.target.value === 'custom') ci.focus();
});
$('clearMemoryBtn').onclick = async () => {
  if (!confirm('確定要清空本機的所有長期對話記憶與對話記錄嗎？此動作無法復原。')) return;
  try {
    const res = await fetch('/api/memory/clear', { method: 'POST' });
    if (res.ok) {
      state.chatHistory = [];
      $('chatMessages').innerHTML = `<div class="msg system"><b>Serenity 投研夥伴：</b>本機對話記憶已成功清除！對話已重置。<br>歡迎來到 Serenity 投研對話空間。我是您的 AI 助理，已載入 <code>serenity-skill</code> 瓶頸獵人架構，能幫您分析個別股票的物理供應鏈瓶頸或進行產業掃描。請輸入您的問題，例如：<br>💡 <a href="#" onclick="clickSampleQuestion('分析 NVDA 的瓶頸與風險')">「分析 NVDA 的瓶頸與風險」</a>或 <a href="#" onclick="clickSampleQuestion('用 Serenity 的方式看 TSM')">「用 Serenity 的方式看 TSM」</a>。</div>`;
      updateMemoryStatus();
      showToast('本機長期記憶與歷史對話已完全清空！', 'info');
    }
  } catch (err) { showToast('清空記憶失敗：' + err.message, 'error'); }
};

// ── Dossier (Manager View) ─────────────────────────────────────────────────────

async function loadAndRenderDossier(symbol, refresh = false) {
  const el = $('dossierContent');
  if (!el) return;
  el.innerHTML = '<p style="color:var(--muted);padding:20px;font-size:13px;">載入經理人分析中...</p>';
  try {
    const data = await json(`/api/dossier/${encodeURIComponent(symbol)}${refresh ? '?refresh=1' : ''}`);
    state.dossierData = data;
    renderDossier(data);
  } catch (err) {
    el.innerHTML = `<p style="color:var(--muted);padding:20px;font-size:13px;">載入失敗：${escapeHtml(err.message)}</p>`;
  }
}

window.refreshDossier = function() { if (state.active) loadAndRenderDossier(state.active, true); };

function renderDossier(d) {
  const el = $('dossierContent');
  if (!el || !d) return;
  const mv = d.manager_view || null;
  const rec = mv ? mv.recommendation : null;
  const conv = mv ? mv.conviction : null;
  const recColor = { AVOID:'#ff6b35', REDUCE:'#ff6b35', WATCH:'#888', ACCUMULATE:'#c3f73a', HOLD:'#c3f73a' }[rec] || '#888';
  const recTextColor = (rec === 'ACCUMULATE' || rec === 'HOLD') ? '#182019' : '#fff';
  const fmt = v => v == null ? '—' : `$${Number(v).toFixed(2)}`;
  const ent = d.signal && d.signal.entry_zone;
  const entStr = (ent && ent.low != null && ent.high != null) ? `${fmt(ent.low)} – ${fmt(ent.high)}` : '—';
  const tech = d.technicals || {}, quant = d.quant || {}, sent = d.sentiment || null, scorecard = d.scorecard || null;

  el.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        ${rec ? `<span style="font-size:15px;font-weight:bold;padding:5px 14px;border-radius:6px;background:${recColor};color:${recTextColor};">${rec}</span>` : '<span style="color:var(--muted);font-size:13px;">推薦評等：AI 未生成</span>'}
        ${conv ? `<span style="font-size:12px;color:var(--muted);background:rgba(0,0,0,0.06);padding:3px 10px;border-radius:4px;">信心度：${conv}</span>` : ''}
        <span style="font-size:11px;color:var(--muted);">${escapeHtml(d.symbol || '')} · ${escapeHtml(d.as_of || '')}</span>
      </div>
      <button onclick="refreshDossier()" style="font-size:11px;font-weight:bold;background:transparent;color:var(--green);border:1px solid var(--green);padding:4px 10px;border-radius:6px;cursor:pointer;">重新生成</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:14px;">
      <div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">量化分數</div><div style="font-size:18px;font-weight:bold;">${quant.score != null ? quant.score : '—'}</div><div style="font-size:10px;color:var(--muted);">${escapeHtml(quant.source || '')}</div></div>
      <div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">訊號狀態</div><div style="font-size:13px;font-weight:bold;">${escapeHtml((d.signal && d.signal.state) || '—')}</div></div>
      <div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">RSI</div><div style="font-size:18px;font-weight:bold;">${tech.rsi != null ? Number(tech.rsi).toFixed(1) : '—'}</div></div>
      <div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">ATR%</div><div style="font-size:18px;font-weight:bold;">${tech.atr_pct != null ? tech.atr_pct + '%' : '—'}</div></div>
      <div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">趨勢</div><div style="font-size:12px;font-weight:bold;">${escapeHtml(tech.trend || '—')}</div></div>
      ${sent ? `<div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">StockTwits 多空比</div><div style="font-size:13px;font-weight:bold;">${sent.stocktwits_bull_ratio != null ? (sent.stocktwits_bull_ratio*100).toFixed(0)+'% 多' : '—'}</div><div style="font-size:10px;color:var(--muted);">n=${sent.sample}</div></div>` : ''}
      ${scorecard ? `<div style="background:rgba(0,0,0,0.04);border-radius:6px;padding:8px 10px;"><div style="font-size:10px;color:var(--muted);margin-bottom:2px;">供應鏈記分卡</div><div style="font-size:15px;font-weight:bold;">${scorecard.final_score}</div><div style="font-size:10px;color:var(--muted);">${escapeHtml(scorecard.verdict || '')}</div></div>` : ''}
    </div>
    ${mv ? `
    <div style="margin-bottom:14px;"><h4 style="font-size:12px;font-weight:bold;color:var(--ink);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em;">投資論述</h4><p style="font-size:13px;line-height:1.6;margin:0;">${escapeHtml(mv.thesis)}</p></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;">
      <div style="background:rgba(195,247,58,0.08);border:1px solid rgba(195,247,58,0.3);border-radius:6px;padding:10px;"><h4 style="font-size:11px;font-weight:bold;color:#4a7c00;margin:0 0 5px;">多頭案例</h4><p style="font-size:12px;line-height:1.5;margin:0;">${escapeHtml(mv.bull_case)}</p></div>
      <div style="background:rgba(255,107,53,0.06);border:1px solid rgba(255,107,53,0.2);border-radius:6px;padding:10px;"><h4 style="font-size:11px;font-weight:bold;color:#c0390a;margin:0 0 5px;">空頭案例</h4><p style="font-size:12px;line-height:1.5;margin:0;">${escapeHtml(mv.bear_case)}</p></div>
    </div>
    <div style="margin-bottom:14px;background:rgba(0,0,0,0.04);border-radius:6px;padding:10px;"><h4 style="font-size:11px;font-weight:bold;margin:0 0 4px;">倉位建議</h4><p style="font-size:12px;line-height:1.5;margin:0;">${escapeHtml(mv.position_guidance)}</p><div style="margin-top:6px;font-size:11px;color:var(--muted);">進場區：${entStr} &nbsp;|&nbsp; 止損：${fmt(d.signal && d.signal.stop_loss)} &nbsp;|&nbsp; 目標：${fmt(d.signal && d.signal.target)}</div></div>
    ` : `<div style="margin-bottom:14px;padding:12px;background:rgba(0,0,0,0.04);border-radius:6px;color:var(--muted);font-size:12px;">AI 敘事未生成（API 金鑰未設定或呼叫失敗）。以上量化數據已完整呈現。</div>`}
    ${(d.evidence && d.evidence.length) ? `
    <div style="margin-bottom:14px;"><h4 style="font-size:12px;font-weight:bold;color:var(--ink);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em;">高參與度推文證據</h4>
    ${d.evidence.map(ev => `<div style="margin-bottom:8px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;font-size:11.5px;line-height:1.4;"><div style="color:var(--muted);font-size:10px;margin-bottom:3px;">${escapeHtml(ev.date||'')} · 參與度 ${ev.engagement||0}</div><p style="margin:0 0 4px;">${escapeHtml((ev.text||'').slice(0,300))}${(ev.text||'').length>300?'...':''}</p>${ev.url?`<a href="${escapeHtml(ev.url)}" target="_blank" rel="noreferrer" style="font-size:10px;color:var(--green);">在 X 上查看</a>`:''}</div>`).join('')}</div>` : ''}
    <div style="margin-top:16px;padding:8px 10px;border:1px dashed rgba(0,0,0,0.18);border-radius:4px;font-size:10px;color:var(--muted);line-height:1.4;">${escapeHtml(d.reliability_note || '')}</div>
  `;
}

// ── Boot ──────────────────────────────────────────────────────────────────────

init().catch(err => document.body.insertAdjacentHTML('afterbegin', `<pre>${escapeHtml(err.message)}</pre>`));
