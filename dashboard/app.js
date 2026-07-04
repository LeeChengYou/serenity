let state = { symbols: [], active: null, filter: 'all', chart: null };
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
    
    // Render radar chart now that the container is visible!
    if (state.active) {
      renderScorecard(state.active, state.scorecardData);
    }
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
      layout: {
        padding: 8
      },
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
      plugins: {
        legend: { display: false }
      }
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
    const res = await fetch(`/api/scorecard/generate/${encodeURIComponent(symbol)}`, {
      method: 'POST'
    });
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

async function init() {
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
    console.error("Failed to load backend config:", err);
  }

  try {
    const summary = await json('/api/summary');
    state.symbols = summary.symbols || [];
    renderKpis(summary.stats || {});
    renderSymbols();
    const first = state.symbols.find(s => s.has_prices) || state.symbols[0];
    if (first) selectSymbol(first.symbol);
  } catch (err) {
    console.error("Failed to load summary:", err);
    $('symbols').innerHTML = '<p style="padding: 16px; color: var(--muted); font-size: 13px;">⚠️ 載入股票清單失敗，請稍候重試。</p>';
  }

  try {
    const feed = await json('/api/feed?limit=36');
    renderFeed(feed.items || []);
  } catch (err) {
    console.error("Failed to load feed:", err);
  }
  
  updateMemoryStatus();
}

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

async function selectSymbol(symbol) {
  const prevActive = state.active;
  state.active = symbol;
  
  // Toggle active class without rebuilding the whole symbols panel
  document.querySelectorAll('.symbol-row').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.symbol === symbol);
  });
  
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
  
  // Fetch scorecard info but do not render immediately if hidden
  try {
    state.scorecardData = await json(`/api/scorecard/${encodeURIComponent(symbol)}`);
  } catch (err) {
    console.error("Failed to load scorecard:", err);
    state.scorecardData = null;
  }
  renderScorecard(symbol, state.scorecardData);
}
    state.scorecardData = null;
  }
  
  // If the scorecard tab is currently active, render it now!
  if ($('tabScorecardBtn').classList.contains('active')) {
    renderScorecard(symbol, state.scorecardData);
  }
}

function renderChart(data) {
  const allPrices = data.prices || [];
  const mentions = data.mentions || [];
  const firstMentionDate = mentions.reduce((min, m) => {
    const d = dateOnly(m.mentioned_at);
    return d && (!min || d < min) ? d : min;
  }, '');
  const prices = firstMentionDate ? allPrices.filter(p => p.date >= firstMentionDate) : allPrices;
  const priceByDate = new Map(prices.map(p => [p.date, p.close]));
  const mentionPoints = mentions.map(m => {
    const d = dateOnly(m.mentioned_at);
    const nearest = prices.find(p => p.date >= d) || prices[prices.length - 1];
    const chartDate = priceByDate.has(d) ? d : nearest?.date;
    return chartDate ? { x: chartDate, y: priceByDate.get(chartDate), mention: m } : null;
  }).filter(Boolean).filter(p => p.y != null);

  const ctx = $('priceChart');
  if (state.chart) state.chart.destroy();
  state.chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: prices.map(p => p.date),
      datasets: [
        { label: `${data.symbol} close`, data: prices.map(p => p.close), borderColor: '#1f7a4f', borderWidth: 2.5, pointRadius: 0, tension: .22, fill: true, backgroundColor: 'rgba(31,122,79,.10)' },
        { type: 'scatter', label: 'mentions', data: mentionPoints, parsing: false, pointRadius: 6, pointHoverRadius: 9, pointBackgroundColor: '#ff6b35', pointBorderColor: '#182019', pointBorderWidth: 1.5 }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      onHover: (event, elements) => {
        event.native.target.style.cursor = elements.some(el => el.datasetIndex === 1) ? 'pointer' : 'default';
      },
      onClick: (event, elements) => {
        const point = elements.find(el => el.datasetIndex === 1);
        if (!point) return;
        const mention = state.chart.data.datasets[1].data[point.index]?.mention;
        if (mention?.url) window.open(mention.url, '_blank', 'noopener,noreferrer');
      },
      scales: {
        x: { grid: { color: 'rgba(24,32,25,.08)' }, ticks: { maxTicksLimit: 8 } },
        y: { grid: { color: 'rgba(24,32,25,.08)' }, ticks: { callback: v => `$${v}` } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          padding: 14,
          bodySpacing: 5,
          callbacks: {
            title: items => items[0].raw?.mention ? fmtDate(items[0].raw.mention.mentioned_at) : items[0].label,
            label: item => {
              if (!item.raw?.mention) return `${money(item.parsed.y)}`;
              return [`${data.symbol} close ${money(item.parsed.y)}`, ...wrapTooltipText(item.raw.mention.text)];
            }
          }
        }
      }
    }
  });
}

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

document.querySelectorAll('.symbols-panel .tabs button').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('.symbols-panel .tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.filter = btn.dataset.filter;
  renderSymbols();
});
$('symbolSearch').addEventListener('input', renderSymbols);

// === AI Chat panel logic ===
state.chatHistory = [];

function appendChatMessage(role, text) {
  const container = $('chatMessages');
  const msgEl = document.createElement('div');
  msgEl.className = `msg ${role}`;
  
  if (role === 'model' || role === 'system') {
    // Process markdown linebreaks and basic bold styling safely
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
  const input = $('chatInput');
  const sendBtn = $('chatSend');
  const text = input.value.trim();
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
  const timeoutId = setTimeout(() => controller.abort(), 45000);
  
  try {
    const selectVal = $('chatModel').value;
    const modelName = selectVal === 'custom' ? $('customModelInput').value.trim() || 'gemini-2.5-flash' : selectVal;
    
    // Goal 4: Limit context to last 6 turns to save token usage
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
      appendChatMessage('system', `請求逾時（已過 45 秒未響應）。已自動釋放對話欄，請嘗試重新發送或切換為 Gemini 2.5 Flash。`);
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
    console.error("Failed to load memory status", e);
  }
}

$('chatSend').onclick = sendChatMessage;
$('chatInput').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
};

// Auto resize chat textarea height
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
  if (!confirm("確定要清空本機的所有長期對話記憶與對話記錄嗎？此動作無法復原。")) {
    return;
  }
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
        </div>
      `;
      updateMemoryStatus();
      showToast("本機長期記憶與歷史對話已完全清空！", "info");
    }
  } catch (err) {
    showToast("清空記憶失敗：" + err.message, "error");
  }
};

init().catch(err => document.body.insertAdjacentHTML('afterbegin', `<pre>${escapeHtml(err.message)}</pre>`));
