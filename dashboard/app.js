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

// ── R4-2 Translation state ────────────────────────────────────────────────────
// cache persists across symbol changes; mode resets on new symbol selection
const _xlate = {
  news: { mode: 'en', cache: new Map(), data: null },
  feed: { mode: 'en', cache: new Map(), items: [] },
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
  // Phase 2 guard: require API key
  if (_settingsState && !_settingsState.has_key) {
    _requireApiKey('供應鏈瓶頸分析生成');
    return;
  }
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

// ── R3-4: Earnings countdown badge + signal warning ───────────────────────────

function renderEarningsBadge(nextDate) {
  const badge = $('earningsBadge');
  const warn  = $('earningsWarningBar');

  if (!nextDate) {
    if (badge) badge.style.display = 'none';
    if (warn)  warn.style.display  = 'none';
    return;
  }

  const today  = new Date(); today.setHours(0, 0, 0, 0);
  const target = new Date(nextDate); target.setHours(0, 0, 0, 0);
  const days   = Math.round((target - today) / 86400000);

  // Title badge: show when 0–7 days
  if (badge) {
    if (days >= 0 && days <= 7) {
      badge.textContent = `📅 ${days} 天後財報`;
      badge.className   = days <= 2 ? 'earnings-badge earnings-badge--urgent' : 'earnings-badge earnings-badge--warn';
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  }

  // Signal panel warning bar: show when 0–5 days
  if (warn) {
    if (days >= 0 && days <= 5) {
      warn.textContent  = `⚠️ 財報臨近（${days} 天後），波動風險升高`;
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
  }
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
    // R3-4: update earnings countdown badge from fundamentals data
    renderEarningsBadge(data.next_earnings_date || null);
  } catch (_) {
    el.innerHTML = '<p class="placeholder-text">資料尚未抓取</p>';
    renderEarningsBadge(null);
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

// ── R3-3: Analyst Estimates card ──────────────────────────────────────────────

async function loadEstimates(symbol) {
  const el = $('estimatesContent');
  if (!el) return;
  el.innerHTML = '<p class="placeholder-text">資料尚未抓取</p>';
  try {
    const data = await fetch(`/api/estimates/${encodeURIComponent(symbol)}`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    if (!data || data.error) throw new Error('empty');
    renderEstimates(data);
  } catch (_) {
    el.innerHTML = '<p class="placeholder-text">資料尚未抓取</p>';
  }
}

function renderEstimates(d) {
  const el = $('estimatesContent');
  if (!el) return;
  const dash   = v => (v == null ? '—' : v);
  const fmtUSD = v => (v == null ? '—' : `$${Number(v).toFixed(2)}`);
  const fmtPct = v => (v == null ? '' : `(${Number(v) > 0 ? '+' : ''}${(Number(v) * 100).toFixed(1)}%)`);
  const tgtColor = d.target_vs_price != null ? (d.target_vs_price > 0 ? '#1a6640' : '#b84000') : 'inherit';
  const revMap = { up: { icon: '↑', color: '#1a6640' }, down: { icon: '↓', color: '#b84000' }, neutral: { icon: '→', color: 'var(--muted)' } };
  const rev = revMap[d.revision_direction] || { icon: '—', color: 'var(--muted)' };
  el.innerHTML = `
    <div class="estimates-grid">
      <div class="est-item est-item--wide">
        <span class="fund-label">評級</span>
        <span class="fund-val">${escapeHtml(d.recommendation_key || '—')}&nbsp;<span style="font-size:10px;color:var(--muted);">(mean ${d.recommendation_mean != null ? Number(d.recommendation_mean).toFixed(2) : '—'})</span></span>
      </div>
      <div class="est-item">
        <span class="fund-label">分析師數</span>
        <span class="fund-val">${dash(d.n_analysts)}</span>
      </div>
      <div class="est-item">
        <span class="fund-label">目標價均值</span>
        <span class="fund-val">${fmtUSD(d.target_mean)}&nbsp;<span style="font-size:10px;color:${tgtColor};">${fmtPct(d.target_vs_price)}</span></span>
      </div>
      <div class="est-item">
        <span class="fund-label">EPS 預估（本季）</span>
        <span class="fund-val">${d.eps_estimate_current_q != null ? Number(d.eps_estimate_current_q).toFixed(2) : '—'}</span>
      </div>
      <div class="est-item">
        <span class="fund-label">修正方向</span>
        <span class="fund-val" style="font-size:20px;color:${rev.color};line-height:1;">${rev.icon}</span>
      </div>
    </div>
  `;
}

// ── R5-3: Expert views card ───────────────────────────────────────────────────

async function loadExpertViews(symbol) {
  const card = $('expertViewsCard');
  if (!card) return;
  // Hide card initially; show only when items exist
  card.style.display = 'none';
  try {
    const data = await fetch(`/api/expert-views/${encodeURIComponent(symbol)}`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    if (!data || data.error || !data.items || !data.items.length) return;
    renderExpertViews(data);
    card.style.display = '';
  } catch (_) {
    // Graceful degradation — card stays hidden
  }
}

function renderExpertViews(data) {
  const el = $('expertViewsContent');
  if (!el) return;
  const credBadge = cred => {
    const map = { official: '官方', aggregator: '聚合', individual: '個人' };
    const cls = { official: 'badge success', aggregator: 'badge info', individual: 'badge' };
    const label = map[cred] || cred;
    const klass = cls[cred] || 'badge';
    return `<span class="${klass}" style="font-size:10px;padding:2px 6px;border-radius:4px;">${escapeHtml(label)}</span>`;
  };
  const items = (data.items || []).map(it => {
    const is13f = (it.source || '').includes('edgar') || (it.source || '').includes('13f');
    const delayWarning = is13f
      ? `<div style="font-size:10px;color:#b84000;margin-top:2px;">⚠ 13F 申報有 45 天延遲，資料非即時</div>`
      : '';
    return `
      <div style="padding:8px 0;border-bottom:1px solid var(--line);">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">
          ${credBadge(it.credibility)}
          <span style="font-size:11px;color:var(--muted);">${escapeHtml(it.author || it.source || '')}</span>
          <span style="font-size:10px;color:var(--muted);margin-left:auto;">${dateOnly(it.published_at)}</span>
        </div>
        <div style="font-size:12px;line-height:1.45;">${escapeHtml(it.text || '')}</div>
        ${delayWarning}
        ${it.url ? `<a href="${escapeHtml(it.url)}" target="_blank" rel="noreferrer" style="font-size:10px;color:var(--blue);">查看申報原文 →</a>` : ''}
      </div>
    `;
  }).join('');
  el.innerHTML = items
    + (data.as_of ? `<div style="font-size:10px;color:var(--muted);margin-top:6px;text-align:right;">截至 ${data.as_of}</div>` : '');
}

// ── News panel ────────────────────────────────────────────────────────────────

async function loadNews(symbol) {
  const el = $('newsContent');
  if (!el) return;
  // Reset translation mode on new symbol (keep cache)
  _xlate.news.mode = 'en';
  _xlate.news.data = null;
  _updateTranslateBtn('newsTranslateBtn', 'en');
  try {
    const data = await fetch(`/api/news/${encodeURIComponent(symbol)}`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    if (!data || data.error || (!data.items?.length && !data.macro?.length)) throw new Error('empty');
    _xlate.news.data = data;
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

function renderNewsItems(items, xlateCache) {
  if (!items || !items.length) return '<p class="placeholder-text">暫無新聞</p>';
  return items.map(it => {
    const titleZh  = xlateCache && it.title   ? xlateCache.get(it.title)   : null;
    const summaryZh = xlateCache && it.summary ? xlateCache.get(it.summary) : null;
    const titleHtml = titleZh
      ? `<a href="${escapeHtml(it.url || '#')}" target="_blank" rel="noreferrer" class="news-title">${escapeHtml(titleZh)}</a>
         <div class="news-title-en">${escapeHtml(it.title || '')}</div>`
      : `<a href="${escapeHtml(it.url || '#')}" target="_blank" rel="noreferrer" class="news-title">${escapeHtml(it.title || '—')}</a>`;
    let summaryHtml = '';
    if (it.summary) {
      if (summaryZh) {
        summaryHtml = `<p class="news-summary-zh">${escapeHtml(summaryZh)}</p>
                       <p class="news-summary-en">${escapeHtml(it.summary)}</p>`;
      } else {
        summaryHtml = `<p class="news-summary">${escapeHtml(it.summary)}</p>`;
      }
    }
    return `
      <div class="news-item">
        ${titleHtml}
        <div class="news-meta">
          <span class="news-source">${escapeHtml(it.source || '')}</span>
          <span class="news-time">${relTime(it.published_at)}</span>
        </div>
        ${summaryHtml}
      </div>
    `;
  }).join('');
}

function renderNews(data, xlateCache) {
  const el = $('newsContent');
  if (!el) return;
  const items = data.items || [];
  const macro = data.macro || [];
  const cache = xlateCache || null;
  el.innerHTML = `
    <div class="news-section-label">個股新聞</div>
    ${renderNewsItems(items, cache)}
    ${macro.length ? `<div class="news-section-label" style="margin-top:12px;">國際 / 總經</div>${renderNewsItems(macro, cache)}` : ''}
    ${data.as_of ? `<div style="font-size:10px;color:var(--muted);margin-top:8px;text-align:right;">截至 ${data.as_of}</div>` : ''}
  `;
}

// ── Feed (X posts) rendered inside accordion ──────────────────────────────────

function renderFeed(items, xlateCache) {
  const el = $('feed');
  if (!el) return;
  // Update evidence count badge
  const badge = $('evidenceCount');
  if (badge) badge.textContent = items.length ? `(${items.length})` : '';
  el.innerHTML = items.map(i => {
    const textZh = xlateCache ? xlateCache.get(clip(i.text, 340)) : null;
    const bodyHtml = textZh
      ? `<p class="feed-text-zh">${escapeHtml(textZh)}</p>
         <p class="feed-text-en">${escapeHtml(clip(i.text, 340))}</p>`
      : `<p>${escapeHtml(clip(i.text, 340))}</p>`;
    return `
      <article class="feed-item">
        <div><span class="ticker">$${i.symbol}</span> <span class="tiny">${fmtDate(i.mentioned_at)} / ${i.source}</span></div>
        ${bodyHtml}
        <a href="${i.url}" target="_blank" rel="noreferrer">open on X</a>
      </article>
    `;
  }).join('');
}

// ── R4-2: Translation toggle helpers ─────────────────────────────────────────

function _updateTranslateBtn(id, mode) {
  const btn = $(id);
  if (!btn) return;
  btn.textContent = mode === 'zh' ? 'EN' : '譯 中';
  btn.disabled = false;
}

/** Collect unique non-empty strings from items' title/summary (deduplicated, cache-miss only). */
function _uncachedTexts(items, fields, cache) {
  const texts = [];
  const seen = new Set();
  for (const it of items) {
    for (const f of fields) {
      const t = (it[f] || '').trim();
      if (t && !seen.has(t) && !cache.has(t)) {
        texts.push(t);
        seen.add(t);
      }
    }
  }
  return texts;
}

/** POST /api/translate with up to 20 texts per call; updates cache in-place. */
async function _fetchTranslations(texts, cache) {
  // Batch in groups of 20
  for (let i = 0; i < texts.length; i += 20) {
    const chunk = texts.slice(i, i + 20);
    const resp = await fetch('/api/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: chunk }),
    }).then(r => r.json());
    if (resp.error && !resp.translations?.length) throw new Error(resp.error);
    (resp.translations || []).forEach((t, idx) => {
      if (t) cache.set(chunk[idx], t);
    });
  }
}

/** Toggle news panel between English and 繁中. */
window.toggleNewsTranslation = async function() {
  const btn = $('newsTranslateBtn');
  if (!btn || !_xlate.news.data) return;
  // Phase 2 guard
  if (_settingsState && !_settingsState.has_key) {
    _requireApiKey('新聞翻譯');
    return;
  }

  if (_xlate.news.mode === 'zh') {
    // Revert to English
    _xlate.news.mode = 'en';
    _updateTranslateBtn('newsTranslateBtn', 'en');
    renderNews(_xlate.news.data, null);
    return;
  }

  // Collect uncached texts
  const allItems = [...(_xlate.news.data.items || []), ...(_xlate.news.data.macro || [])];
  const toFetch = _uncachedTexts(allItems, ['title', 'summary'], _xlate.news.cache);

  if (toFetch.length > 0) {
    btn.textContent = '翻譯中…';
    btn.disabled = true;
    try {
      await _fetchTranslations(toFetch, _xlate.news.cache);
    } catch (_) {
      btn.textContent = '翻譯暫時不可用';
      btn.disabled = false;
      setTimeout(() => _updateTranslateBtn('newsTranslateBtn', 'en'), 3000);
      return;
    }
  }

  _xlate.news.mode = 'zh';
  _updateTranslateBtn('newsTranslateBtn', 'zh');
  renderNews(_xlate.news.data, _xlate.news.cache);
};

/** Toggle X posts feed between English and 繁中. */
window.toggleFeedTranslation = async function() {
  const btn = $('feedTranslateBtn');
  if (!btn) return;
  // Phase 2 guard
  if (_settingsState && !_settingsState.has_key) {
    _requireApiKey('X 貼文翻譯');
    return;
  }

  if (_xlate.feed.mode === 'zh') {
    // Revert to English
    _xlate.feed.mode = 'en';
    _updateTranslateBtn('feedTranslateBtn', 'en');
    renderFeed(_xlate.feed.items, null);
    return;
  }

  // Feed stores clipped text, so translate the clipped version
  const clippedItems = (_xlate.feed.items || []).map(i => ({ text: clip(i.text, 340) }));
  const toFetch = _uncachedTexts(clippedItems, ['text'], _xlate.feed.cache);

  if (toFetch.length > 0) {
    btn.textContent = '翻譯中…';
    btn.disabled = true;
    try {
      await _fetchTranslations(toFetch, _xlate.feed.cache);
    } catch (_) {
      btn.textContent = '翻譯暫時不可用';
      btn.disabled = false;
      setTimeout(() => _updateTranslateBtn('feedTranslateBtn', 'en'), 3000);
      return;
    }
  }

  _xlate.feed.mode = 'zh';
  _updateTranslateBtn('feedTranslateBtn', 'zh');
  renderFeed(_xlate.feed.items, _xlate.feed.cache);
};

// ── R3-2: Regime badge ────────────────────────────────────────────────────────

async function loadRegime() {
  try {
    const data = await fetch('/api/regime').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    renderRegimeBadge(data);
  } catch (_) {
    // Keep default placeholder — endpoint will 404 until backend lands
  }
}

function renderRegimeBadge(data) {
  const el = $('regimeBadge');
  if (!el) return;
  const map = {
    bull:    { icon: '🟢', label: '多頭', cls: 'regime-bull' },
    neutral: { icon: '🟡', label: '中性', cls: 'regime-neutral' },
    bear:    { icon: '🔴', label: '空頭', cls: 'regime-bear' },
    unknown: { icon: '⚪', label: '未知', cls: 'regime-unknown' },
  };
  const key = (data.regime || 'unknown').toLowerCase();
  const r   = map[key] || map.unknown;
  el.textContent = `${r.icon} ${r.label}`;
  el.className   = `regime-badge ${r.cls}`;

  const lines = [];
  [['SPY', data.spy], ['SOXX', data.soxx], ['QQQ', data.qqq]].forEach(([sym, d]) => {
    if (!d) return;
    const pos   = d.above ? '上方 ✓' : '下方 ✗';
    const price = d.close  != null ? `$${Number(d.close).toFixed(2)}`  : '—';
    const ema   = d.ema200 != null ? `$${Number(d.ema200).toFixed(2)}` : '—';
    lines.push(`${sym}: ${price}  EMA200 ${ema}  ${pos}`);
  });
  if (data.universe_above_ema50_pct != null)
    lines.push(`個股廣度：${(Number(data.universe_above_ema50_pct) * 100).toFixed(0)}% 站上 EMA50`);
  if (data.note) lines.push(`\n注：${data.note}`);
  el.title = lines.length ? lines.join('\n') : '市場狀態';
}

// ── Global page navigation ────────────────────────────────────────────────────

window.switchGlobalPage = function(page) {
  const kpis      = $('kpis');
  const workbench = document.querySelector('.workbench');
  const hitrate   = $('hitrateView');
  const arena     = $('arenaView');

  document.querySelectorAll('.global-page-nav button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.page === page));

  if (page === 'dashboard') {
    if (kpis)      kpis.style.display      = '';
    if (workbench) workbench.style.display  = '';
    if (hitrate)   hitrate.style.display    = 'none';
    if (arena)     arena.style.display      = 'none';
  } else if (page === 'hitrate') {
    if (kpis)      kpis.style.display      = 'none';
    if (workbench) workbench.style.display  = 'none';
    if (hitrate)   hitrate.style.display    = 'block';
    if (arena)     arena.style.display      = 'none';
    loadHitRate();
  } else if (page === 'arena') {
    if (kpis)      kpis.style.display      = 'none';
    if (workbench) workbench.style.display  = 'none';
    if (hitrate)   hitrate.style.display    = 'none';
    if (arena)     arena.style.display      = 'block';
    loadArena();
  }
};

// ── R3-1: Hit-rate page ───────────────────────────────────────────────────────

let _hitrateLoaded = false;

async function loadHitRate() {
  const container = $('hitrateView');
  if (!container || _hitrateLoaded) return;

  container.innerHTML = '<p class="placeholder-text" style="padding:16px 0;">載入命中率資料中...</p>';
  try {
    const data = await fetch('/api/hitrate').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    renderHitRate(data);
    _hitrateLoaded = true;
  } catch (_) {
    // Graceful degradation when endpoint 404s
    container.innerHTML = `
      <div class="hitrate-banner">
        <span class="hitrate-banner-icon">ℹ️</span>
        <div>
          <b>資料可靠性說明</b>：
          回溯重建數據為樣本外方法，非實時發布；實時紀錄需每日訊號快照排程累積後啟動。
        </div>
      </div>
      <div class="hitrate-empty">
        <p>📊 命中率資料尚未建立</p>
        <p class="hitrate-empty-sub">需累積至少 30 天訊號快照後才能計算統計命中率（見 ROADMAP B-1）</p>
      </div>
      <div id="hitrateChanges" class="hitrate-section"></div>
    `;
    _hitrateLoaded = true;
  }
  loadChangesForHitrate();
}

function renderHitRate(data) {
  const container = $('hitrateView');
  if (!container) return;

  const summary   = data.summary       || [];
  const calls     = data.recent_calls  || [];
  const note      = data.reliability_note || '';
  const liveSince = data.live_since || null;

  container.innerHTML = `
    <div class="hitrate-banner">
      <span class="hitrate-banner-icon">ℹ️</span>
      <div>
        <b>資料可靠性說明</b>：
        回溯重建數據為樣本外方法，非實時發布；
        ${liveSince
          ? `實時紀錄自 <b>${escapeHtml(liveSince)}</b> 起累積。`
          : '實時紀錄尚未啟動（每日快照排程開始後自動累積）。'}
        ${note ? `<br><span style="color:var(--muted);font-size:11px;">${escapeHtml(note)}</span>` : ''}
      </div>
    </div>

    <div class="hitrate-section">
      <h3 class="hitrate-section-title">訊號統計總覽</h3>
      ${summary.length
        ? renderHitRateSummaryTable(summary)
        : '<p class="placeholder-text">暫無足夠樣本（每個訊號需 n≥30）</p>'}
    </div>

    <div class="hitrate-section">
      <h3 class="hitrate-section-title">最近訊號記錄</h3>
      ${calls.length
        ? renderHitRateCalls(calls)
        : '<p class="placeholder-text">暫無訊號記錄</p>'}
    </div>

    <div id="hitrateChanges" class="hitrate-section"></div>
  `;
}

function sigBadgeClass(sig) {
  return {
    BUY_WATCH:   'sig-buy-watch',
    BUY_TRIGGER: 'sig-buy-trigger',
    HOLD:        'sig-hold',
    EXIT_ALERT:  'sig-exit',
    OVERBOUGHT:  'sig-overbought',
    NEUTRAL:     'sig-neutral',
  }[sig] || 'sig-neutral';
}

function renderHitRateSummaryTable(summary) {
  const rows = summary.map(s => {
    const wr  = s.win_rate != null ? `${(Number(s.win_rate) * 100).toFixed(0)}%` : '樣本不足';
    const ret = s.median_fwd_return_30d != null ? `${(Number(s.median_fwd_return_30d) * 100).toFixed(1)}%` : '—';
    const vu  = s.vs_universe != null ? `${(Number(s.vs_universe) * 100).toFixed(1)}%` : '—';
    const src = s.source === 'live'
      ? '<span class="hr-badge hr-live">實時</span>'
      : '<span class="hr-badge hr-backtest">回溯</span>';
    const wrStyle = s.win_rate == null ? '' :
      s.win_rate >= 0.55 ? 'color:#1a6640;font-weight:bold;' :
      s.win_rate <  0.45 ? 'color:#b84000;' : '';
    return `<tr>
      <td><span class="sig-badge ${sigBadgeClass(s.signal)}">${escapeHtml((s.signal || '—').replace(/_/g, ' '))}</span></td>
      <td style="text-align:center;">${s.n != null ? s.n : '—'}</td>
      <td style="text-align:center;">${ret}</td>
      <td style="text-align:center;${wrStyle}">${wr}</td>
      <td style="text-align:center;">${vu}</td>
      <td>${src}</td>
    </tr>`;
  }).join('');

  return `<table class="hitrate-table">
    <thead><tr>
      <th>訊號</th><th>樣本數</th><th>30日中位報酬</th><th>勝率</th><th>vs 宇宙</th><th>來源</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderHitRateCalls(calls) {
  const rows = calls.map(c => {
    const hitClass = c.hit === true ? 'hit-yes' : c.hit === false ? 'hit-no' : 'hit-pending';
    const hitIcon  = c.hit === true ? '✓' : c.hit === false ? '✗' : '⏳';
    const ret  = c.fwd_return      != null ? `${(Number(c.fwd_return)      * 100).toFixed(1)}%` : '—';
    const univ = c.universe_return != null ? `vs ${(Number(c.universe_return) * 100).toFixed(1)}%` : '';
    const src  = c.source === 'live'
      ? '<span class="hr-badge hr-live">實時</span>'
      : '<span class="hr-badge hr-backtest">回溯</span>';
    const sym = escapeHtml(c.symbol || '');
    return `<div class="hitrate-call-row">
      <span class="hitrate-call-hit ${hitClass}">${hitIcon}</span>
      <span class="hitrate-call-sym"><a href="#" onclick="event.preventDefault();switchGlobalPage('dashboard');selectSymbol('${sym}')">${sym}</a></span>
      <span class="hitrate-call-date">${escapeHtml((c.date || '').slice(0, 10))}</span>
      <span class="sig-badge ${sigBadgeClass(c.signal)}">${escapeHtml((c.signal || '—').replace(/_/g, ' '))}</span>
      <span class="hitrate-call-ret">${ret}</span>
      <span class="hitrate-call-univ">${univ}</span>
      ${src}
    </div>`;
  }).join('');
  return `<div class="hitrate-calls">${rows}</div>`;
}

// ── R3-5: Signal changes + Δ badges ──────────────────────────────────────────

let _changesData = [];

async function loadChanges() {
  try {
    const data = await fetch('/api/changes?days=7').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    _changesData = data.items || data.changes || (Array.isArray(data) ? data : []);
    applyChangeBadgesToSymbols();
  } catch (_) {
    _changesData = [];
  }
}

function applyChangeBadgesToSymbols() {
  if (!_changesData || !_changesData.length) return;
  // Build set of symbols with changes within the last 24 hours
  const now = Date.now();
  const recentMap = {};
  for (const c of _changesData) {
    const ts = c.date ? new Date(c.date).getTime() : 0;
    if (now - ts <= 24 * 3600 * 1000) {
      recentMap[c.symbol] = c;
    }
  }
  document.querySelectorAll('.symbol-row').forEach(btn => {
    const sym = btn.dataset.symbol;
    // Remove any existing delta badge first
    const existing = btn.querySelector('.delta-badge');
    if (existing) existing.remove();
    if (!recentMap[sym]) return;
    const c = recentMap[sym];
    const badge = document.createElement('span');
    badge.className = 'delta-badge';
    badge.textContent = 'Δ';
    badge.title = `訊號變化：${c.prev_signal || '—'} → ${c.new_signal || '—'}`;
    // Append inside .ticker cell to avoid breaking the 3-col grid layout
    const ticker = btn.querySelector('.ticker');
    if (ticker) ticker.appendChild(badge);
    else btn.appendChild(badge);
  });
}

function loadChangesForHitrate() {
  const el = $('hitrateChanges');
  if (!el) return;
  if (!_changesData || !_changesData.length) {
    el.innerHTML = '';
    return;
  }
  const rows = _changesData.slice(0, 20).map(c => {
    const sym = escapeHtml(c.symbol || '');
    return `<div class="hitrate-call-row">
      <span class="hitrate-call-sym"><a href="#" onclick="event.preventDefault();switchGlobalPage('dashboard');selectSymbol('${sym}')">${sym}</a></span>
      <span class="hitrate-call-date">${escapeHtml((c.date || '').slice(0, 10))}</span>
      <span class="sig-badge ${sigBadgeClass(c.prev_signal)}">${escapeHtml((c.prev_signal || '—').replace(/_/g, ' '))}</span>
      <span style="color:var(--muted);font-size:13px;font-weight:bold;">→</span>
      <span class="sig-badge ${sigBadgeClass(c.new_signal)}">${escapeHtml((c.new_signal || '—').replace(/_/g, ' '))}</span>
    </div>`;
  }).join('');
  el.innerHTML = `
    <h3 class="hitrate-section-title">最近訊號變化（7 天）</h3>
    <div class="hitrate-calls">${rows}</div>
  `;
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

// ── Settings Modal (Phase 2: V7 §2) ──────────────────────────────────────────

// Cached settings state (loaded once from /api/settings)
let _settingsState = null;
// Original values on load, to detect changes
let _settingsOriginal = {};

window.openSettingsModal = function() {
  const overlay = $('settings-modal');
  if (overlay) { overlay.classList.remove('hidden'); }
  // Reload latest settings when opening
  _loadSettingsIntoModal();
};

window.closeSettingsModal = function() {
  const overlay = $('settings-modal');
  if (overlay) overlay.classList.add('hidden');
  // Clear test result on close
  const tr = $('settingsTestResult');
  if (tr) { tr.className = 'settings-test-result'; tr.textContent = ''; }
};

window.onSettingsOverlayClick = function(e) {
  if (e.target === $('settings-modal')) closeSettingsModal();
};

async function _loadSettingsIntoModal() {
  try {
    const data = await fetch('/api/settings').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
    _settingsState = data;
    _renderSettingsModal(data);
  } catch (err) {
    // If we can't load settings, just show empty modal
    _renderSettingsModal(null);
  }
}

function _renderSettingsModal(data) {
  const keys = (data && data.keys) || [
    { slot: 1, set: false, masked: null },
    { slot: 2, set: false, masked: null },
    { slot: 3, set: false, masked: null },
    { slot: 4, set: false, masked: null },
  ];
  const models = (data && data.models) || {};
  const hasKey = data ? data.has_key : false;

  // Guide block: show when no key set
  const guide = $('settingsGuide');
  if (guide) {
    if (hasKey) guide.classList.remove('visible');
    else guide.classList.add('visible');
  }

  // Render key slots
  const container = $('settingsKeysContainer');
  if (container) {
    container.innerHTML = keys.map(k => {
      const fieldName = k.slot === 1 ? 'gemini_api_key' : `gemini_api_key_${k.slot}`;
      const dot = `<span class="key-status-dot ${k.set ? 'set' : 'unset'}"></span>`;
      const ph = k.set ? k.masked : '輸入新金鑰…';
      return `
        <div style="margin-bottom:8px;">
          <div class="settings-key-label">${dot} Slot ${k.slot}${k.set ? '（已設定）' : '（未設定）'}</div>
          <div class="settings-key-row">
            <input type="password" id="settingsKey${k.slot}"
              data-field="${fieldName}"
              placeholder="${escapeHtml(ph || '')}"
              autocomplete="new-password"
              spellcheck="false" />
            <button class="settings-key-clear-btn"
              onclick="clearSettingsKeySlot(${k.slot})"
              title="清除此 Slot（送空字串以刪除）">清除</button>
          </div>
        </div>`;
    }).join('');

    // Save original (empty = no change intent)
    _settingsOriginal = {};
  }

  // Fill model fields
  if ($('settingsGeminiModel'))    $('settingsGeminiModel').value    = models.gemini_model           || '';
  if ($('settingsTranslateModel')) $('settingsTranslateModel').value = models.gemini_translate_model || '';
  if ($('settingsMemoryModel'))    $('settingsMemoryModel').value    = models.gemini_memory_model    || '';

  // Clear status messages
  const ss = $('settingsSaveStatus');
  if (ss) ss.textContent = '';
  const tr = $('settingsTestResult');
  if (tr) { tr.className = 'settings-test-result'; tr.textContent = ''; }
}

// "清除" button on a key slot: marks it to be sent as empty string (= delete)
window.clearSettingsKeySlot = function(slot) {
  const inp = $(`settingsKey${slot}`);
  if (!inp) return;
  inp.value = '';
  inp.placeholder = '（清除——儲存後生效）';
  inp.dataset.clearIntent = 'true';
};

window.testSettingsKey = async function() {
  const btn = $('settingsTestBtn');
  const result = $('settingsTestResult');
  if (!btn || !result) return;

  // Use Slot 1 input value; if empty, nothing to test
  const inp = $('settingsKey1');
  const key = inp ? inp.value.trim() : '';

  if (!key) {
    result.className = 'settings-test-result fail';
    result.textContent = '請先在 Slot 1 填入金鑰再測試';
    return;
  }

  btn.disabled = true;
  result.className = 'settings-test-result testing';
  result.textContent = '測試中…';

  try {
    const res = await fetch('/api/settings/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }).then(r => r.json());

    if (res.ok) {
      result.className = 'settings-test-result ok';
      result.textContent = '✓ 連線成功';
    } else {
      result.className = 'settings-test-result fail';
      result.textContent = `✗ 失敗：${res.error || '未知錯誤'}`;
    }
  } catch (err) {
    result.className = 'settings-test-result fail';
    result.textContent = `✗ 連線錯誤：${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

window.saveSettings = async function() {
  const btn = $('settingsSaveBtn');
  const status = $('settingsSaveStatus');
  if (!btn || !status) return;

  btn.disabled = true;
  status.textContent = '儲存中…';

  const payload = {};

  // Collect key slots: only include if user typed something OR if clearIntent is set
  for (let slot = 1; slot <= 4; slot++) {
    const inp = $(`settingsKey${slot}`);
    if (!inp) continue;
    const fieldName = inp.dataset.field;
    if (!fieldName) continue;
    const val = inp.value.trim();
    const clearIntent = inp.dataset.clearIntent === 'true';
    if (val !== '') {
      // New value entered: include it
      payload[fieldName] = val;
    } else if (clearIntent) {
      // Explicit clear: send empty string to remove key
      payload[fieldName] = '';
    }
    // If empty and no clearIntent: skip (no change)
  }

  // Collect model fields (only if changed from original)
  const origModels = (_settingsState && _settingsState.models) || {};
  const modelFields = [
    ['settingsGeminiModel',    'gemini_model'],
    ['settingsTranslateModel', 'gemini_translate_model'],
    ['settingsMemoryModel',    'gemini_memory_model'],
  ];
  for (const [elId, field] of modelFields) {
    const el = $(elId);
    if (!el) continue;
    const val = el.value.trim();
    if (val !== (origModels[field] || '')) {
      payload[field] = val;
    }
  }

  if (Object.keys(payload).length === 0) {
    btn.disabled = false;
    status.textContent = '（無變動）';
    setTimeout(() => { if (status) status.textContent = ''; }, 2000);
    return;
  }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const newData = await res.json();
    _settingsState = newData;
    _renderSettingsModal(newData);
    status.textContent = '✓ 已儲存';
    // If keys are now set, hide the guide
    if (newData.has_key) {
      const guide = $('settingsGuide');
      if (guide) guide.classList.remove('visible');
    }
    setTimeout(() => { if (status) status.textContent = ''; }, 3000);
  } catch (err) {
    status.textContent = `✗ 儲存失敗：${err.message}`;
    setTimeout(() => { if (status) status.textContent = ''; }, 5000);
  } finally {
    btn.disabled = false;
  }
};

/** Called by guarded actions when has_key=false */
function _requireApiKey(actionName) {
  showToast(`請先在 ⚙ 設定填入 Gemini API key，才能使用「${actionName}」功能。`, 'info', 5000);
  openSettingsModal();
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const params    = new URLSearchParams(location.search);
  const initSymbol = (params.get('s') || '').toUpperCase() || null;
  const initTab   = params.get('tab') || 'chart';

  // R3-2: load regime badge (once per page load, graceful degradation)
  loadRegime();
  // R3-5: load signal changes for Δ badges (once per page load)
  loadChanges();
  // 資料時效徽章（once per page load）
  loadHealthBadge();

  // Phase 2: load settings; auto-open modal if no API key configured
  try {
    const settings = await fetch('/api/settings').then(r => r.ok ? r.json() : null);
    if (settings) {
      _settingsState = settings;
      if (!settings.has_key) {
        // Show modal with guide
        _renderSettingsModal(settings);
        openSettingsModal();
      }
    }
  } catch (_) { /* graceful degradation */ }

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

    // 階段三：Onboarding 引導（has_key=true 且資料庫空白時顯示）
    if (_settingsState && _settingsState.has_key && state.symbols.length === 0) {
      _showOnboardingBlock();
    } else {
      _hideOnboardingBlock();
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
    }
  } catch (err) {
    console.error('Failed to load summary:', err);
    $('symbols').innerHTML = '<p style="padding: 16px; color: var(--muted); font-size: 13px;">⚠️ 載入股票清單失敗，請稍候重試。</p>';
  }

  try {
    const feed = await json('/api/feed?limit=36');
    _xlate.feed.items = feed.items || [];
    _xlate.feed.mode = 'en';
    renderFeed(_xlate.feed.items);
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
  // R3-5: apply Δ badges for symbols with recent signal changes
  applyChangeBadgesToSymbols();
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

  // Reset earnings badge on symbol switch
  renderEarningsBadge(null);

  // Load new panels (graceful degradation)
  loadFundamentals(symbol);
  loadEstimates(symbol);   // R3-3
  loadExpertViews(symbol); // R5-3
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
  // Phase 2 guard: require API key
  if (_settingsState && !_settingsState.has_key) {
    _requireApiKey('AI 對話');
    return;
  }
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

// ── V6 Arena page ─────────────────────────────────────────────────────────────

let _arenaLoaded = false;

async function loadArena() {
  const leaderEl  = $('arenaLeaderboard');
  const navEl     = $('arenaNavChart');
  const refEl     = $('arenaReflections');
  const selectEl  = $('arenaAgentSelect');

  const picker = $('arenaMonthPicker');
  const now = new Date();
  const month = picker && picker.value
    ? picker.value
    : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

  // Set picker default
  if (picker && !picker.value) picker.value = month;

  // Fetch all four endpoints in parallel
  const base = `/api/arena/`;
  const [lb, nv, rf] = await Promise.all([
    fetch(`${base}leaderboard?month=${month}`).then(r => r.ok ? r.json() : {rows:[]}).catch(() => ({rows:[]})),
    fetch(`${base}nav?month=${month}`).then(r => r.ok ? r.json() : {series:{},benchmark:{}}).catch(() => ({series:{},benchmark:{}})),
    fetch(`${base}reflections?month=${month}`).then(r => r.ok ? r.json() : {rows:[]}).catch(() => ({rows:[]})),
  ]);

  // Render leaderboard
  if (leaderEl) {
    const rows = lb.rows || [];
    if (!rows.length) {
      leaderEl.innerHTML = '<p class="placeholder-text">本月尚無排行榜資料</p>';
    } else {
      leaderEl.innerHTML = `
        <table class="arena-table">
          <thead><tr>
            <th>排名</th><th>Agent</th><th>領域</th>
            <th>月報酬率</th><th>MDD</th><th>交易筆數</th><th>領域排名</th>
          </tr></thead>
          <tbody>
          ${rows.map(r => `<tr>
            <td class="arena-mono">${r.rank_overall ?? '-'}</td>
            <td><a href="#" onclick="event.preventDefault();$('arenaAgentSelect').value='${escapeHtml(r.agent_id)}';loadArenaTrades()">${escapeHtml(r.agent_id)}</a></td>
            <td>${escapeHtml(r.domain ?? '')}</td>
            <td class="arena-mono ${(r.ret_pct ?? 0) >= 0 ? 'arena-pos' : 'arena-neg'}">${r.ret_pct != null ? (r.ret_pct >= 0 ? '+' : '') + r.ret_pct.toFixed(2) + '%' : '-'}</td>
            <td class="arena-mono">${r.mdd_pct != null ? r.mdd_pct.toFixed(2) + '%' : '-'}</td>
            <td class="arena-mono">${r.n_trades ?? '-'}</td>
            <td class="arena-mono">${r.rank_domain ?? '-'}</td>
          </tr>`).join('')}
          </tbody>
        </table>`;
      // Populate agent selector; auto-select the top-ranked agent so the
      // trades log shows data immediately instead of "請選擇 Agent".
      if (selectEl) {
        const existing = new Set(Array.from(selectEl.options).map(o => o.value));
        rows.forEach(r => {
          if (!existing.has(r.agent_id)) {
            const opt = document.createElement('option');
            opt.value = r.agent_id;
            opt.textContent = `${r.agent_id}（${r.ret_pct != null ? (r.ret_pct >= 0 ? '+' : '') + r.ret_pct.toFixed(1) + '%' : '-'}）`;
            selectEl.appendChild(opt);
          }
        });
        if (!selectEl.value && rows.length) {
          selectEl.value = rows[0].agent_id;
          loadArenaTrades();
        }
      }
    }
  }

  // Render NAV chart as a real SVG line chart (排行榜走勢折線圖)
  if (navEl) {
    renderArenaNavChart(navEl, nv.series || {}, nv.benchmark || {});
  }

  // Render reflections
  if (refEl) {
    const rows = rf.rows || [];
    if (!rows.length) {
      refEl.innerHTML = '<p class="placeholder-text">本月尚無反思資料</p>';
    } else {
      refEl.innerHTML = rows.map(r => `
        <div class="arena-reflection-card">
          <div class="arena-reflection-agent">${escapeHtml(r.agent_id)}</div>
          ${r.public_letter ? `<div class="arena-letter"><strong>公開信：</strong><p>${escapeHtml(r.public_letter)}</p></div>` : ''}
          ${r.reflection_md ? `<div class="arena-reflection"><strong>月度反思：</strong><p>${escapeHtml(r.reflection_md)}</p></div>` : ''}
        </div>`).join('');
    }
  }

  _arenaLoaded = true;
}

// Shared, stable color per series so chart + leaderboard agree
const ARENA_COLORS = ['#4a90d9','#e07b39','#5cb85c','#9b59b6','#f39c12','#1abc9c','#e74c3c','#16a085','#c0392b'];
function arenaColorFor(key, index) { return ARENA_COLORS[index % ARENA_COLORS.length]; }

// Draw an SVG line chart of every agent's NAV path plus the SPY benchmark.
// Hidden series are tracked in a module-level set toggled by clicking the legend.
const _arenaHidden = new Set();
function renderArenaNavChart(navEl, series, benchmark) {
  const agentKeys = Object.keys(series);
  const benchKeys = Object.keys(benchmark);
  if (!agentKeys.length && !benchKeys.length) {
    navEl.innerHTML = '<p class="placeholder-text">本月尚無 NAV 資料</p>';
    return;
  }

  // Union of all dates across every series, sorted ascending
  const datesSet = new Set();
  agentKeys.forEach(k => (series[k] || []).forEach(p => datesSet.add(p.date)));
  benchKeys.forEach(k => (benchmark[k] || []).forEach(p => datesSet.add(p.date)));
  const dates = Array.from(datesSet).sort();
  const xOf = {};
  dates.forEach((d, i) => { xOf[d] = i; });

  // Build a unified list: agents first (solid), benchmark last (dashed)
  const all = [];
  agentKeys.forEach((k, i) => all.push({ key: k, pts: series[k] || [], color: arenaColorFor(k, i), dashed: false }));
  benchKeys.forEach((k) => all.push({ key: k, pts: benchmark[k] || [], color: '#888', dashed: true }));

  // y range across visible series
  let lo = Infinity, hi = -Infinity;
  all.forEach(s => { if (_arenaHidden.has(s.key)) return; s.pts.forEach(p => { if (p.nav < lo) lo = p.nav; if (p.nav > hi) hi = p.nav; }); });
  if (!isFinite(lo) || !isFinite(hi)) { lo = 2900; hi = 3100; }
  if (lo === hi) { lo -= 50; hi += 50; }
  const pad = (hi - lo) * 0.08 || 50;
  lo -= pad; hi += pad;

  // SVG geometry
  const W = 720, H = 320, mL = 56, mR = 16, mT = 16, mB = 34;
  const plotW = W - mL - mR, plotH = H - mT - mB;
  const xPix = i => mL + (dates.length <= 1 ? plotW / 2 : (i / (dates.length - 1)) * plotW);
  const yPix = v => mT + (1 - (v - lo) / (hi - lo)) * plotH;

  // Y gridlines (5 ticks)
  let grid = '';
  const TICKS = 5;
  for (let t = 0; t <= TICKS; t++) {
    const v = lo + (hi - lo) * (t / TICKS);
    const y = yPix(v);
    grid += `<line x1="${mL}" y1="${y.toFixed(1)}" x2="${W - mR}" y2="${y.toFixed(1)}" stroke="#888" stroke-width="0.5" opacity="0.35"/>`;
    grid += `<text x="${mL - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="#999">${v.toFixed(0)}</text>`;
  }
  // X labels
  let xlabels = '';
  dates.forEach((d, i) => {
    xlabels += `<text x="${xPix(i).toFixed(1)}" y="${H - 12}" text-anchor="middle" font-size="10" fill="#999">${escapeHtml(d.slice(5))}</text>`;
  });

  // Series polylines + dots
  let paths = '';
  all.forEach(s => {
    if (_arenaHidden.has(s.key)) return;
    const pts = (s.pts || []).filter(p => p.date in xOf).sort((a, b) => a.date < b.date ? -1 : 1);
    if (!pts.length) return;
    const coords = pts.map(p => `${xPix(xOf[p.date]).toFixed(1)},${yPix(p.nav).toFixed(1)}`);
    if (coords.length > 1) {
      paths += `<polyline points="${coords.join(' ')}" fill="none" stroke="${s.color}" stroke-width="1.8"${s.dashed ? ' stroke-dasharray="5 4"' : ''}/>`;
    }
    pts.forEach(p => {
      paths += `<circle cx="${xPix(xOf[p.date]).toFixed(1)}" cy="${yPix(p.nav).toFixed(1)}" r="2.6" fill="${s.color}"><title>${escapeHtml(s.key)} ${escapeHtml(p.date)}: ${p.nav.toFixed(2)}</title></circle>`;
    });
  });

  const svg = `<svg viewBox="0 0 ${W} ${H}" class="arena-nav-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="NAV 走勢折線圖">
    ${grid}
    <line x1="${mL}" y1="${mT}" x2="${mL}" y2="${H - mB}" stroke="#999" stroke-width="1"/>
    <line x1="${mL}" y1="${H - mB}" x2="${W - mR}" y2="${H - mB}" stroke="#999" stroke-width="1"/>
    ${xlabels}
    ${paths}
  </svg>`;

  // Clickable legend (toggle visibility)
  const legend = `<div class="arena-nav-legend">` + all.map(s => {
    const off = _arenaHidden.has(s.key);
    return `<span class="arena-legend-item${off ? ' arena-legend-off' : ''}" data-key="${escapeHtml(s.key)}" style="color:${s.color};cursor:pointer;user-select:none;">${s.dashed ? '┄' : '■'} ${escapeHtml(s.key)}</span>`;
  }).join(' ') + `</div>`;

  navEl.innerHTML = legend + svg;
  navEl.querySelectorAll('.arena-legend-item').forEach(el => {
    el.addEventListener('click', () => {
      const k = el.getAttribute('data-key');
      if (_arenaHidden.has(k)) _arenaHidden.delete(k); else _arenaHidden.add(k);
      renderArenaNavChart(navEl, series, benchmark);
    });
  });
}

window.loadArenaTrades = async function() {
  const selectEl = $('arenaAgentSelect');
  const tradesEl = $('arenaTradesLog');
  const picker   = $('arenaMonthPicker');
  if (!selectEl || !tradesEl) return;

  const agentId = selectEl.value;
  if (!agentId) { tradesEl.innerHTML = '<p class="placeholder-text">請選擇 Agent</p>'; return; }

  const now = new Date();
  const month = picker && picker.value
    ? picker.value
    : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

  tradesEl.innerHTML = '<p class="placeholder-text">載入中...</p>';
  try {
    const data = await fetch(`/api/arena/trades?agent=${encodeURIComponent(agentId)}&month=${month}`).then(r => r.json());
    const trades = data.trades || [];
    if (!trades.length) {
      tradesEl.innerHTML = '<p class="placeholder-text">本月無交易記錄</p>';
      return;
    }
    const BADGE = {
      filled:   '<span class="arena-badge arena-badge-filled">成交</span>',
      pending:  '<span class="arena-badge arena-badge-pending">待成交</span>',
      rejected: '<span class="arena-badge arena-badge-rejected">拒單</span>',
    };
    const nFilled = trades.filter(t => t.status === 'filled').length;
    const nPending = trades.filter(t => t.status === 'pending').length;
    const nRejected = trades.filter(t => t.status === 'rejected').length;
    tradesEl.innerHTML = `<div class="arena-trades-summary">共 ${trades.length} 筆 · 成交 ${nFilled} · 待成交 ${nPending} · 拒單 ${nRejected}</div>
    <table class="arena-table arena-trades-table">
      <thead><tr><th>決策日</th><th>成交日</th><th>標的</th><th>方向</th><th>金額/成交</th><th>狀態</th><th>購買依據</th></tr></thead>
      <tbody>
      ${trades.map(t => {
        const statusBadge = BADGE[t.status] || `<span class="arena-badge">${escapeHtml(t.status || '-')}</span>`;
        // Filled → show exec price × qty; otherwise the ordered USD notional
        let amount = '-';
        if (t.status === 'filled' && t.price != null) {
          amount = `$${Number(t.price).toFixed(2)}${t.qty != null ? ' × ' + Number(t.qty).toFixed(2) : ''}`;
        } else if (t.usd != null) {
          amount = `$${Number(t.usd).toFixed(0)}`;
        } else if (t.qty != null) {
          amount = `${Number(t.qty).toFixed(2)}sh`;
        }
        const reason = t.rejected_reason
          ? `${escapeHtml(t.reason || '')}<div class="arena-rej-note">拒單原因：${escapeHtml(t.rejected_reason)}</div>`
          : escapeHtml(t.reason || '');
        return `<tr>
          <td class="arena-mono">${escapeHtml(t.decided_date || '-')}</td>
          <td class="arena-mono">${escapeHtml(t.exec_date || '-')}</td>
          <td class="arena-mono">${escapeHtml(t.symbol || '-')}</td>
          <td class="arena-mono ${t.side === 'SELL' ? 'arena-neg' : 'arena-pos'}">${escapeHtml(t.side || '-')}</td>
          <td class="arena-mono">${amount}</td>
          <td>${statusBadge}</td>
          <td class="arena-reason">${reason || '<span class="arena-muted">—</span>'}</td>
        </tr>`;
      }).join('')}
      </tbody>
    </table>`;
  } catch (e) {
    tradesEl.innerHTML = `<p class="placeholder-text">載入失敗：${escapeHtml(e.message)}</p>`;
  }
};

// ── Onboarding（階段三：空 DB + has_key=true 時顯示初始資料引導）────────────────

let _bootstrapPollTimer = null;

function _showOnboardingBlock() {
  let el = $('onboardingBlock');
  if (!el) {
    el = document.createElement('div');
    el.id = 'onboardingBlock';
    el.style.cssText = 'padding:24px;text-align:center;max-width:520px;margin:40px auto;border:1px solid var(--border,#ddd);border-radius:8px;background:var(--surface,#fff)';
    document.body.insertBefore(el, document.body.firstChild);
  }
  el.innerHTML = `
    <h2 style="margin:0 0 12px">歡迎使用 Serenity Signal</h2>
    <p style="color:var(--muted,#666);margin:0 0 20px">資料庫是空的，點擊下方按鈕抓取初始資料（價格、指數、新聞）。</p>
    <button id="bootstrapBtn" onclick="startBootstrap()"
      style="padding:10px 28px;background:#0070f3;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer">
      抓取初始資料
    </button>
    <div id="bootstrapProgress" style="margin-top:16px;min-height:60px"></div>
  `;
  el.style.display = 'block';
}

function _hideOnboardingBlock() {
  const el = $('onboardingBlock');
  if (el) el.style.display = 'none';
}

window.startBootstrap = async function() {
  const btn = $('bootstrapBtn');
  if (btn) { btn.disabled = true; btn.textContent = '啟動中…'; }
  try {
    const resp = await fetch('/api/admin/bootstrap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      if ($('bootstrapProgress')) $('bootstrapProgress').innerHTML =
        `<p style="color:red">錯誤：${escapeHtml(d.error || resp.statusText)}</p>`;
      return;
    }
    _pollBootstrap();
  } catch (e) {
    if ($('bootstrapProgress')) $('bootstrapProgress').innerHTML =
      `<p style="color:red">請求失敗：${escapeHtml(e.message)}</p>`;
  }
};

function _pollBootstrap() {
  if (_bootstrapPollTimer) clearTimeout(_bootstrapPollTimer);
  _bootstrapPollTimer = setTimeout(async () => {
    try {
      const resp = await fetch('/api/admin/bootstrap/status');
      const d = await resp.json();
      const el = $('bootstrapProgress');
      if (el) {
        const rows = (d.steps || []).map(s => {
          const icon = s.status === 'done' ? '✅' : s.status === 'error' ? '❌'
            : s.status === 'running' ? '🔄' : '⬜';
          return `<div>${icon} ${escapeHtml(s.name)}${s.detail ? ' — ' + escapeHtml(s.detail) : ''}</div>`;
        }).join('');
        el.innerHTML = rows || '<p>等待中…</p>';
      }
      if (d.running) {
        _pollBootstrap();
      } else {
        // 完成後重新載入頁面
        const allDone = (d.steps || []).every(s => s.status === 'done');
        if (allDone) {
          setTimeout(() => location.reload(), 1000);
        } else {
          const btn = $('bootstrapBtn');
          if (btn) { btn.disabled = false; btn.textContent = '重試'; }
        }
      }
    } catch (e) {
      _pollBootstrap(); // 暫時錯誤，繼續輪詢
    }
  }, 3000);
}

// ── Health badge & panel ──────────────────────────────────────────────────────

const HEALTH_DOMAIN_NAMES = {
  prices:         '股票價格',
  benchmarks:     '基準指數',
  signal_history: '訊號快照',
  news:           '新聞',
  stocktwits:     'StockTwits',
  tweets:         'X 貼文',
  fundamentals:   '基本面',
  estimates:      '分析師預估',
  expert_views:   '專家觀點',
  arena_nav:      '競技場 NAV',
};
const HEALTH_SAFE_DOMAINS = new Set([
  'prices','benchmarks','signal_history','news','stocktwits','fundamentals','estimates'
]);

let _healthData = null;
let _healthRefreshPollTimer = null;

function _updateHealthBadge(data) {
  const badge = $('health-badge');
  if (!badge || !data) return;
  const stale = (data.checks || []).filter(c => c.status !== 'ok');
  if (stale.length === 0) {
    badge.className = 'health-badge health-badge-ok';
    badge.textContent = '🟢 資料時效';
    badge.title = '所有資料域均已最新';
  } else {
    badge.className = 'health-badge health-badge-stale';
    badge.textContent = `🟡 ${stale.length} 項過期`;
    badge.title = `過期：${stale.map(c => c.name).join(', ')}`;
  }
}

function _renderHealthPanel(data) {
  const body = $('health-panel-body');
  if (!body || !data) return;
  const checks = data.checks || [];
  body.innerHTML = checks.map(c => {
    const name = HEALTH_DOMAIN_NAMES[c.name] || c.name;
    const dotCls = c.status === 'ok' ? 'health-dot-ok' : c.status === 'missing' ? 'health-dot-missing' : 'health-dot-stale';
    const timeStr = c.latest ? c.latest.slice(0, 19).replace('T', ' ') : '無資料';
    const isManual = !HEALTH_SAFE_DOMAINS.has(c.name);
    const manualNote = isManual ? '<span class="health-manual-note">需排程/開發者功能</span>' : '';
    return `<div class="health-row">
      <span class="health-row-name">${escapeHtml(name)}</span>
      <span class="health-row-time">${escapeHtml(timeStr)} ${manualNote}</span>
      <span class="health-dot ${dotCls}"></span>
    </div>`;
  }).join('');
  const asOf = data.checked_at ? new Date(data.checked_at).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei' }) : '';
  if (asOf) body.innerHTML += `<p style="font-size:10.5px; color:var(--muted); margin:6px 4px 0;">檢查時間：${escapeHtml(asOf)}</p>`;
}

async function loadHealthBadge() {
  const badge = $('health-badge');
  if (!badge) return;
  try {
    const resp = await fetch('/api/health');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _healthData = data;
    _updateHealthBadge(data);
    if ($('health-panel').style.display !== 'none') _renderHealthPanel(data);
  } catch (e) {
    if (badge) {
      badge.className = 'health-badge health-badge-loading';
      badge.textContent = '⬜ 資料時效';
    }
  }
}

window.toggleHealthPanel = function() {
  const panel = $('health-panel');
  if (!panel) return;
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    if (_healthData) _renderHealthPanel(_healthData);
    else loadHealthBadge();
  } else {
    panel.style.display = 'none';
  }
};

window.triggerHealthRefresh = async function() {
  const btn = $('health-refresh-btn');
  const status = $('health-refresh-status');
  if (btn) { btn.disabled = true; btn.textContent = '更新中...'; }
  if (status) status.textContent = '';
  try {
    const resp = await fetch('/api/admin/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await resp.json();
    if (d.error) {
      if (status) status.textContent = `⚠️ ${d.error}`;
      if (btn) { btn.disabled = false; btn.textContent = '立即更新過期安全域'; }
      return;
    }
    if (!d.started) {
      if (status) status.textContent = '所有安全域均已最新';
      if (btn) { btn.disabled = false; btn.textContent = '立即更新過期安全域'; }
      return;
    }
    if (status) status.textContent = '背景更新中...';
    _pollHealthRefreshStatus();
  } catch (e) {
    if (status) status.textContent = `錯誤：${e.message}`;
    if (btn) { btn.disabled = false; btn.textContent = '立即更新過期安全域'; }
  }
};

function _pollHealthRefreshStatus() {
  if (_healthRefreshPollTimer) clearTimeout(_healthRefreshPollTimer);
  _healthRefreshPollTimer = setTimeout(async () => {
    try {
      const resp = await fetch('/api/admin/refresh/status');
      const d = await resp.json();
      const statusEl = $('health-refresh-status');
      const steps = d.steps || [];
      const running = steps.filter(s => s.status === 'running').map(s => s.name).join(', ');
      if (statusEl) {
        if (d.running) {
          statusEl.textContent = running ? `更新中：${running}` : '更新中...';
        } else {
          const errors = steps.filter(s => s.status === 'error');
          statusEl.textContent = errors.length ? `完成（${errors.length} 項失敗）` : '更新完成';
        }
      }
      if (d.running) {
        _pollHealthRefreshStatus();
      } else {
        const btn = $('health-refresh-btn');
        if (btn) { btn.disabled = false; btn.textContent = '立即更新過期安全域'; }
        // 重新載入健康狀態
        await loadHealthBadge();
      }
    } catch (e) {
      _pollHealthRefreshStatus(); // 暫時錯誤，繼續輪詢
    }
  }, 3000);
}

// 每 10 分鐘自動重查健康狀態
setInterval(loadHealthBadge, 10 * 60 * 1000);

// ── Boot ──────────────────────────────────────────────────────────────────────

init().catch(err => document.body.insertAdjacentHTML('afterbegin', `<pre>${escapeHtml(err.message)}</pre>`));
