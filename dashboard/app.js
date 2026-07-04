let state = { symbols: [], active: null, filter: 'all', chart: null, rsiChart: null, scorecardData: null };
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
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// Tab switching inside detail panel
window.switchDetailTab = function(tab) {
  const chartBtn = $('tabChartBtn');
  const scoreBtn = $('tabScorecardBtn');
  const chartView = $('chartView');
  const scoreView = $('scorecardView');

  if (tab === 'chart') {
    chartBtn.classList.add('active');
    scoreBtn.classList.remove('active');
    chartView.style.display = 'flex';
    scoreView.style.display = 'none';
  } else {
    chartBtn.classList.remove('active');
    scoreBtn.classList.add('active');
    chartView.style.display = 'none';
    scoreView.style.display = 'block';

    // Render radar chart now that the container is visible
    if (state.active) {
      renderScorecard(state.active, state.scorecardData);
    }
  }

  // Update URL tab param (Task C — no extra history entry)
  if (state.active) {
    const url = new URL(location.href);
    url.searchParams.set('tab', tab);
    history.replaceState({ symbol: state.active, tab }, '', url.toString());
  }
};

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
  if (card.final_score >= 85) {
    badge.className = 'badge success';
  } else if (card.final_score >= 70) {
    badge.className = 'badge info';
  } else {
    badge.className = 'badge';
  }

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

  const weaknesses = card.kill_switches || [];
  $('scorecardWeakness').innerHTML = weaknesses.map(w => `<li>${escapeHtml(w)}</li>`).join('') || '<li>無特殊削弱因素紀錄</li>';

  const evidence = card.evidence || [];
  $('scorecardEvidence').innerHTML = evidence.map(ev => {
    const claim = ev.claim || '';
    const source = ev.source || '';
    const strength = ev.strength || 'weak';
    return `<li><b>[${strength}]</b> ${escapeHtml(claim)} <i>(${escapeHtml(source)})</i></li>`;
  }).join('') || '<li>無證據 notes 紀錄</li>';

  const factors = card.factor_details || {};
  const labels = ['需求拐點', '架構耦合', '瓶頸嚴重性', '供應商集中', '擴產難度', '證據品質', '估值落差', '催化劑時機'];
  const keys = ['demand_inflection', 'architecture_coupling', 'chokepoint_severity', 'supplier_concentration', 'expansion_difficulty', 'evidence_quality', 'valuation_disconnect', 'catalyst_timing'];

  const datasetData = keys.map(k => (factors[k] ? factors[k].rating : 0));

  const ctx = $('scorecardRadar');
  if (scorecardChart) scorecardChart.destroy();

  scorecardChart = new Chart(ctx, {
    type: 'radar',
    data: {
      labels: labels,
      datasets: [{
        label: '因素評分 (0-5)',
        data: datasetData,
        backgroundColor: 'rgba(31, 122, 79, 0.15)',
        borderColor: '#1f7a4f',
        borderWidth: 2,
        pointBackgroundColor: '#1f7a4f',
        pointBorderColor: '#fff',
        pointRadius: 3
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: 8 },
      scales: {
        r: {
          angleLines: { color: 'rgba(24, 32, 25, 0.08)' },
          grid: { color: 'rgba(24, 32, 25, 0.08)' },
          suggestedMin: 0,
          suggestedMax: 5,
          ticks: { stepSize: 1, display: false },
          pointLabels: {
            font: { size: 9, weight: 'bold' },
            color: '#182019',
            padding: 3
          }
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
      } else {
        showToast(`產生分析失敗：${result.error || '未知錯誤'}`, 'error');
      }
    } else {
      showToast(`伺服器錯誤 ${res.status}`, 'error');
    }
  } catch (err) {
    showToast(`連線錯誤：${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText;
      btn.style.opacity = 1.0;
    }
  }
};

function wrapTooltipText(text, maxChars = 54, maxLines = 9) {
  const lines = [];
  const paragraphs = String(text || '').replace(/\s+/g, ' ').trim().split(/\n+/);
  for (const paragraph of paragraphs) {
    const words = paragraph.split(' ');
    let line = '';
    for (const word of words) {
      if (word.length > maxChars) {
        if (line) lines.push(line);
        for (let i = 0; i < word.length; i += maxChars) lines.push(word.slice(i, i + maxChars));
        line = '';
      } else if (!line || `${line} ${word}`.length <= maxChars) {
        line = line ? `${line} ${word}` : word;
      } else {
        lines.push(line);
        line = word;
      }
      if (lines.length >= maxLines) break;
    }
    if (line && lines.length < maxLines) lines.push(line);
    if (lines.length >= maxLines) break;
  }
  if (lines.length === maxLines && text.length > lines.join(' ').length) {
    lines[maxLines - 1] = `${lines[maxLines - 1].replace(/\.{3}$/, '')}...`;
  }
  return lines.length ? lines : [''];
}

async function json(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

// ── Task C: URL routing ─────────────────────────────────────────────────────

window.copyLink = function() {
  const url = location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => showToast('Link copied!', 'info', 2000))
      .catch(() => _fallbackCopy(url));
  } else {
    _fallbackCopy(url);
  }
};

function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); showToast('Link copied!', 'info', 2000); }
  catch (_) { showToast('Could not copy — copy the URL manually.', 'error'); }
  document.body.removeChild(ta);
}

// ── Task D: Mobile bottom navigation ────────────────────────────────────────

window.setMobilePanel = function(panel) {
  const panelMap = { list: '.symbols-panel', chart: '.chart-panel', score: '.chart-panel', chat: '.chat-panel' };
  document.querySelectorAll('.symbols-panel, .chart-panel, .chat-panel').forEach(el => el.classList.remove('mobile-visible'));
  const target = document.querySelector(panelMap[panel]);
  if (target) target.classList.add('mobile-visible');
  if (panel === 'score') switchDetailTab('scorecard');
  else if (panel === 'chart') switchDetailTab('chart');
  document.querySelectorAll('.mobile-bottom-nav button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.panel === panel));
};

// ── Core data loading ────────────────────────────────────────────────────────

async function init() {
  // Task C: read URL params on load
  const params = new URLSearchParams(location.search);
  const initSymbol = (params.get('s') || '').toUpperCase() || null;
  const initTab    = params.get('tab') || 'chart';

  try {
    const config = await json('/api/config');
    if (config.default_model) {
      const select = $('chatModel');
      const hasOption = Array.from(select.options).some(opt => opt.value === config.default_model);
      if (hasOption) {
        select.value = config.default_model;
      } else {
        select.value = 'custom';
        const customInput = $('customModelInput');
        customInput.style.display = 'inline-block';
        customInput.value = config.default_model;
      }
    }
  } catch (err) {
    console.error('Failed to load backend config:', err);
  }

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
      // Replace current URL with canonical state (no extra history entry)
      const tab = $('tabScorecardBtn').classList.contains('active') ? 'scorecard' : 'chart';
      history.replaceState({ symbol: target, tab }, '', `/?s=${encodeURIComponent(target)}&tab=${tab}`);
    }
  } catch (err) {
    console.error('Failed to load summary:', err);
    $('symbols').innerHTML = '<p style="padding: 16px; color: var(--muted); font-size: 13px;">⚠️ 載入股票清單失敗，請稍候重試。</p>';
  }

  try {
    const feed = await json('/api/feed?limit=36');
    renderFeed(feed.items || []);
  } catch (err) {
    console.error('Failed to load feed:', err);
  }

  updateMemoryStatus();

  // Mobile: show list panel by default
  if (window.innerWidth <= 768) setMobilePanel('list');
}

// Task C: browser back/forward
window.addEventListener('popstate', (e) => {
  if (!e.state || !e.state.symbol) return;
  const { symbol, tab = 'chart' } = e.state;
  const curTab = $('tabScorecardBtn').classList.contains('active') ? 'scorecard' : 'chart';
  if (tab !== curTab) switchDetailTab(tab);
  if (symbol !== state.active) selectSymbol(symbol, { pushState: false });
});

function renderKpis(s) {
  const items = [
    ['tweets', '貼文入庫'], ['mentions', 'Symbol 提及'], ['symbols', '唯一 Symbol'], ['priced_symbols', '已下載價格'], ['latest_mention', '最新提及']
  ];
  $('kpis').innerHTML = items.map(([k, label]) => `
    <div class="kpi"><b>${k === 'latest_mention' ? fmtDate(s[k]) : (s[k] ?? 0)}</b><span>${label}</span></div>
  `).join('');
}

function visibleSymbols() {
  const q = $('symbolSearch').value.trim().toUpperCase();
  return state.symbols.filter(s => {
    if (q && !s.symbol.includes(q)) return false;
    if (state.filter === 'priced' && !s.has_prices) return false;
    if (state.filter === 'hot' && s.mention_count < 5) return false;
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

async function selectSymbol(symbol, { pushState = true } = {}) {
  state.active = symbol;

  // Task C: push URL state
  if (pushState) {
    const activeTab = $('tabScorecardBtn').classList.contains('active') ? 'scorecard' : 'chart';
    history.pushState({ symbol, tab: activeTab }, '', `/?s=${encodeURIComponent(symbol)}&tab=${activeTab}`);
  }

  // Toggle active class without rebuilding the whole symbols panel
  document.querySelectorAll('.symbol-row').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.symbol === symbol));

  // Task D: switch to chart panel on mobile when a symbol is selected
  if (window.innerWidth <= 768) setMobilePanel('chart');

  const data = await json(`/api/symbol/${encodeURIComponent(symbol)}`);
  const info = state.symbols.find(s => s.symbol === symbol) || {};
  $('activeTitle').textContent = `$${symbol}`;
  $('activeMeta').innerHTML = [
    `${info.mention_count || 0} mentions`,
    `${data.prices.length} bars`,
    `first ${fmtDate(info.first_mention)}`,
    `latest ${fmtDate(info.latest_mention)}`
  ].map(x => `<span>${x}</span>`).join('');
  $('neighbors').innerHTML = (data.neighbors || []).slice(0, 12).map(n => `<span>${n.symbol} x${n.count}</span>`).join('');

  renderChart(data);

  // Task B: fetch signal, degrade gracefully if /api/signal not yet deployed
  try {
    const signalData = await json(`/api/signal/${encodeURIComponent(symbol)}`);
    // Guard: some endpoints return {error:...} with HTTP 200 when not implemented
    renderSignalPanel(signalData && !signalData.error ? signalData : null);
  } catch (_err) {
    renderSignalPanel(null); // non-200 or network error — silent
  }

  // Fetch scorecard
  try {
    state.scorecardData = await json(`/api/scorecard/${encodeURIComponent(symbol)}`);
  } catch (err) {
    console.error('Failed to load scorecard:', err);
    state.scorecardData = null;
  }

  // Only render scorecard if that tab is visible (radar needs visible container)
  if ($('tabScorecardBtn').classList.contains('active')) {
    renderScorecard(symbol, state.scorecardData);
  }
}

// ── Task A: Chart with indicator overlays ────────────────────────────────────

function renderChart(data) {
  const allPrices  = data.prices   || [];
  const mentions   = data.mentions  || [];
  const indicators = data.indicators || {};

  const firstMentionDate = mentions.reduce((min, m) => {
    const d = dateOnly(m.mentioned_at);
    return d && (!min || d < min) ? d : min;
  }, '');

  // Find slice start so per-bar indicator arrays stay aligned with visible prices
  let startIdx = 0;
  if (firstMentionDate) {
    const idx = allPrices.findIndex(p => p.date >= firstMentionDate);
    startIdx = idx < 0 ? 0 : idx;
  }

  const prices = allPrices.slice(startIdx);
  const priceByDate = new Map(prices.map(p => [p.date, p.close]));

  // Slice indicator arrays to the same visible range
  const ema20 = (indicators.ema20 || []).slice(startIdx);
  const ema50 = (indicators.ema50 || []).slice(startIdx);
  const bb    = (indicators.bb    || []).slice(startIdx);
  const rsi14 = (indicators.rsi14 || []).slice(startIdx);

  // Extract BB upper/lower; null where band not yet computed
  const bbUpper = bb.map(b => (b && b.upper != null) ? b.upper : null);
  const bbLower = bb.map(b => (b && b.lower != null) ? b.lower : null);

  const mentionPoints = mentions.map(m => {
    const d = dateOnly(m.mentioned_at);
    const nearest = prices.find(p => p.date >= d) || prices[prices.length - 1];
    const chartDate = priceByDate.has(d) ? d : nearest?.date;
    return chartDate ? { x: chartDate, y: priceByDate.get(chartDate), mention: m } : null;
  }).filter(Boolean).filter(p => p.y != null);

  const ctx = $('priceChart');
  if (state.chart) state.chart.destroy();

  // Dataset index constants (keep in sync with onClick/onHover)
  // 0: BB Lower, 1: BB Upper, 2: EMA20, 3: EMA50, 4: Price close, 5: Mentions
  state.chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: prices.map(p => p.date),
      datasets: [
        // ── Bollinger Band lower boundary (no fill; upper fills back to this)
        {
          label: 'BB Lower',
          data: bbLower,
          borderColor: 'rgba(31,77,122,0.25)',
          borderWidth: 1,
          borderDash: [3, 3],
          pointRadius: 0,
          tension: 0.2,
          fill: false,
          order: 6
        },
        // ── Bollinger Band upper boundary (fills toward BB Lower)
        {
          label: 'BB Upper',
          data: bbUpper,
          borderColor: 'rgba(31,77,122,0.25)',
          borderWidth: 1,
          borderDash: [3, 3],
          pointRadius: 0,
          tension: 0.2,
          fill: '-1',
          backgroundColor: 'rgba(31,77,122,0.055)',
          order: 6
        },
        // ── EMA 20 (acid-green)
        {
          label: 'EMA 20',
          data: ema20,
          borderColor: '#8db800',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.2,
          fill: false,
          order: 3
        },
        // ── EMA 50 (ember-orange)
        {
          label: 'EMA 50',
          data: ema50,
          borderColor: '#ff6b35',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.2,
          fill: false,
          order: 3
        },
        // ── Price close line
        {
          label: `${data.symbol} close`,
          data: prices.map(p => p.close),
          borderColor: '#1f7a4f',
          borderWidth: 2.5,
          pointRadius: 0,
          tension: 0.22,
          fill: true,
          backgroundColor: 'rgba(31,122,79,.10)',
          order: 2
        },
        // ── Mention scatter points
        {
          type: 'scatter',
          label: 'mentions',
          data: mentionPoints,
          parsing: false,
          pointRadius: 6,
          pointHoverRadius: 9,
          pointBackgroundColor: '#ff6b35',
          pointBorderColor: '#182019',
          pointBorderWidth: 1.5,
          order: 1
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      onHover: (event, elements) => {
        event.native.target.style.cursor =
          elements.some(el => el.datasetIndex === 5) ? 'pointer' : 'default';
      },
      onClick: (event, elements) => {
        const point = elements.find(el => el.datasetIndex === 5);
        if (!point) return;
        const mention = state.chart.data.datasets[5].data[point.index]?.mention;
        if (mention?.url) window.open(mention.url, '_blank', 'noopener,noreferrer');
      },
      scales: {
        x: { grid: { color: 'rgba(24,32,25,.08)' }, ticks: { maxTicksLimit: 8 } },
        y: { grid: { color: 'rgba(24,32,25,.08)' }, ticks: { callback: v => `$${v}` } }
      },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: {
            filter: item => !['BB Lower', 'BB Upper', 'mentions'].includes(item.text),
            font: { size: 10, family: 'ui-monospace, SFMono-Regular, monospace' },
            boxWidth: 20,
            padding: 6
          }
        },
        tooltip: {
          displayColors: false,
          padding: 14,
          bodySpacing: 5,
          callbacks: {
            title: items => items[0].raw?.mention ? fmtDate(items[0].raw.mention.mentioned_at) : items[0].label,
            label: item => {
              if (item.dataset.label === 'BB Lower' || item.dataset.label === 'BB Upper') return null;
              if (!item.raw?.mention) return `${item.dataset.label}: ${money(item.parsed.y)}`;
              return [`${data.symbol} close ${money(item.parsed.y)}`, ...wrapTooltipText(item.raw.mention.text)];
            }
          }
        }
      }
    }
  });

  // Render RSI sub-chart below
  renderRsi(prices, rsi14);
}

// ── Task A: RSI sub-chart ────────────────────────────────────────────────────

function renderRsi(prices, rsi14) {
  const ctx  = $('rsiChart');
  const wrap = $('rsiWrap');
  if (!ctx || !wrap) return;

  const hasData = Array.isArray(rsi14) && rsi14.some(v => v != null);
  wrap.style.display = hasData ? 'block' : 'none';
  if (!hasData) return;

  if (state.rsiChart) state.rsiChart.destroy();

  const flatLine = (val, color) => ({
    label: `${val}`,
    data: prices.map(() => val),
    borderColor: color,
    borderWidth: 1,
    borderDash: [4, 3],
    pointRadius: 0,
    fill: false,
    tension: 0
  });

  state.rsiChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: prices.map(p => p.date),
      datasets: [
        {
          label: 'RSI(14)',
          data: rsi14,
          borderColor: '#1f4d7a',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.2,
          fill: false
        },
        flatLine(70, 'rgba(255,107,53,0.55)'),  // overbought
        flatLine(30, 'rgba(31,122,79,0.55)')    // oversold
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { display: false },
        y: {
          min: 0,
          max: 100,
          grid: { color: 'rgba(24,32,25,.06)' },
          ticks: {
            maxTicksLimit: 3,
            font: { size: 10 },
            callback: v => `${v}`
          }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          padding: 8,
          callbacks: {
            title: items => items[0].label,
            label: item => item.dataset.label === 'RSI(14)'
              ? `RSI: ${Number(item.parsed.y).toFixed(1)}`
              : null
          }
        }
      }
    }
  });
}

// ── Task B: Signal panel ─────────────────────────────────────────────────────

function renderSignalPanel(signal) {
  const el = $('signalPanel');
  if (!el) return;

  if (!signal) {
    el.style.display = 'none';
    return;
  }

  if (signal.insufficient_data) {
    el.style.display = 'block';
    el.innerHTML = '<p class="signal-insufficient">📊 Not enough price history to compute signal.</p>';
    return;
  }

  const sig = signal.signal || 'NEUTRAL';
  const badgeClass = {
    BUY_WATCH:   'sig-buy-watch',
    BUY_TRIGGER: 'sig-buy-trigger',
    HOLD:        'sig-hold',
    EXIT_ALERT:  'sig-exit',
    OVERBOUGHT:  'sig-overbought',
    NEUTRAL:     'sig-neutral'
  }[sig] || 'sig-neutral';

  const fmt  = v => (v == null ? '—' : `$${Number(v).toFixed(2)}`);
  const fmtRR = v => (v == null ? '—' : `1:${Number(v).toFixed(1)}`);

  const conditions = signal.conditions || [];
  const entry = signal.entry_zone || {};
  const entryStr = (entry.low != null && entry.high != null)
    ? `${fmt(entry.low)} – ${fmt(entry.high)}`
    : '—';

  el.style.display = 'block';
  el.innerHTML = `
    <div class="signal-head">
      <span class="sig-badge ${badgeClass}">${sig.replace(/_/g, ' ')}</span>
      ${signal.score != null ? `<span class="signal-pill">Score ${signal.score}</span>` : ''}
      ${signal.atr14 != null ? `<span class="signal-pill">ATR ${Number(signal.atr14).toFixed(2)}</span>` : ''}
    </div>
    <div class="signal-conditions">
      ${conditions.map(c => `
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

// ── Feed & helpers ───────────────────────────────────────────────────────────

function renderFeed(items) {
  $('feed').innerHTML = items.map(i => `
    <article class="feed-item">
      <div><span class="ticker">$${i.symbol}</span> <span class="tiny">${fmtDate(i.mentioned_at)} / ${i.source}</span></div>
      <p>${escapeHtml(clip(i.text, 340))}</p>
      <a href="${i.url}" target="_blank" rel="noreferrer">open on X</a>
    </article>
  `).join('');
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}

// ── Symbol panel filter tabs & search ────────────────────────────────────────

document.querySelectorAll('.symbols-panel .tabs button').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('.symbols-panel .tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.filter = btn.dataset.filter;
  renderSymbols();
});
$('symbolSearch').addEventListener('input', renderSymbols);

// ── AI Chat panel logic ───────────────────────────────────────────────────────
state.chatHistory = [];

function appendChatMessage(role, text) {
  const container = $('chatMessages');
  const msgEl = document.createElement('div');
  msgEl.className = `msg ${role}`;

  if (role === 'model' || role === 'system') {
    let htmlContent = escapeHtml(text)
      .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
      .replace(/`(.*?)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
    msgEl.innerHTML = htmlContent;
  } else {
    msgEl.textContent = text;
  }

  container.appendChild(msgEl);
  container.scrollTop = container.scrollHeight;
}

window.clickSampleQuestion = function(text) {
  $('chatInput').value = text;
  sendChatMessage();
};

async function sendChatMessage() {
  const input   = $('chatInput');
  const sendBtn = $('chatSend');
  const text    = input.value.trim();
  if (!text) return;

  input.value = '';
  input.disabled = true;
  sendBtn.disabled = true;

  appendChatMessage('user', text);
  state.chatHistory.push({ role: 'user', content: text });

  const loadingEl = document.createElement('div');
  loadingEl.className = 'msg system loading';
  loadingEl.textContent = 'Serenity 正在分析中...';
  $('chatMessages').appendChild(loadingEl);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;

  const controller = new AbortController();
  const timeoutId  = setTimeout(() => controller.abort(), 45000);

  try {
    const selectVal = $('chatModel').value;
    const modelName = selectVal === 'custom' ? $('customModelInput').value.trim() || 'gemini-2.5-flash' : selectVal;

    const trimmedHistory = state.chatHistory.length > 6 ? state.chatHistory.slice(-6) : state.chatHistory;

    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: trimmedHistory, model: modelName }),
      signal: controller.signal
    });

    clearTimeout(timeoutId);
    loadingEl.remove();

    if (!res.ok) throw new Error(`HTTP Error ${res.status}`);
    const data = await res.json();

    if (data.error) {
      appendChatMessage('system', `錯誤：${data.error}`);
    } else {
      const reply = data.response || 'AI 未能給出有效回覆。';
      appendChatMessage('model', reply);
      state.chatHistory.push({ role: 'model', content: reply });
      setTimeout(updateMemoryStatus, 2000);
    }
  } catch (err) {
    clearTimeout(timeoutId);
    loadingEl.remove();
    if (err.name === 'AbortError') {
      appendChatMessage('system', '請求逾時（已過 45 秒未響應）。已自動釋放對話欄，請嘗試重新發送或切換為 Gemini 2.5 Flash。');
    } else {
      appendChatMessage('system', `連線錯誤：${err.message}`);
    }
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

async function updateMemoryStatus() {
  try {
    const res = await fetch('/api/memory');
    if (res.ok) {
      const data = await res.json();
      const count = (data.memories || []).length;
      $('memoryStatus').textContent = `🧠 記憶：${count} 條`;
    }
  } catch (e) {
    console.error('Failed to load memory status', e);
  }
}

$('chatSend').onclick = sendChatMessage;
$('chatInput').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
};

$('chatInput').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

$('chatModel').addEventListener('change', (e) => {
  const customInput = $('customModelInput');
  if (e.target.value === 'custom') {
    customInput.style.display = 'inline-block';
    customInput.focus();
  } else {
    customInput.style.display = 'none';
  }
});

$('clearMemoryBtn').onclick = async () => {
  if (!confirm('確定要清空本機的所有長期對話記憶與對話記錄嗎？此動作無法復原。')) return;
  try {
    const res = await fetch('/api/memory/clear', { method: 'POST' });
    if (res.ok) {
      state.chatHistory = [];
      $('chatMessages').innerHTML = `
        <div class="msg system">
          <b>Serenity 投研夥伴：</b>本機對話記憶已成功清除！對話已重置。<br>
          歡迎來到 Serenity 投研對話空間。我是您的 AI 助理，已載入 <code>serenity-skill</code> 瓶頸獵人架構，能幫您分析個別股票的物理供應鏈瓶頸或進行產業掃描。請輸入您的問題，例如：
          <br>💡 <a href="#" onclick="clickSampleQuestion('分析 NVDA 的瓶頸與風險')">「分析 NVDA 的瓶頸與風險」</a>
          或 <a href="#" onclick="clickSampleQuestion('用 Serenity 的方式看 TSM')">「用 Serenity 的方式看 TSM」</a>。
        </div>`;
      updateMemoryStatus();
      showToast('本機長期記憶與歷史對話已完全清空！', 'info');
    }
  } catch (err) {
    showToast('清空記憶失敗：' + err.message, 'error');
  }
};

init().catch(err => document.body.insertAdjacentHTML('afterbegin', `<pre>${escapeHtml(err.message)}</pre>`));
