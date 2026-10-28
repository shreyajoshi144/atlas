/* ══════════════════════════════════════════════════════════════════════
   ATLAS — CONFIG & CORE UTILITIES
   ══════════════════════════════════════════════════════════════════════ */
const Atlas = {};

Atlas.config = {
  get apiBase() {
    const stored = localStorage.getItem('atlas_api_base');
    if (stored) return stored;
    if (window.ATLAS_API_BASE) return String(window.ATLAS_API_BASE).replace(/\/$/, '') + (String(window.ATLAS_API_BASE).endsWith('/api') ? '' : '/api');
    // Smart default: local dev talks to localhost:8000, a deployed build assumes
    // the API is served from the same origin under /api unless configured otherwise.
    const isLocalHost = ['localhost', '127.0.0.1', '::1'].includes(location.hostname) || location.hostname === '';
    return isLocalHost ? ('http://' + (location.hostname || '127.0.0.1') + ':8000/api') : (location.origin + '/api');
  },
  set apiBase(v) { localStorage.setItem('atlas_api_base', v); },
};

/* ── Auth: token + user stored together, attached to every API call below ──
   "Remember me" decides WHERE this lives: localStorage persists across
   browser restarts; sessionStorage clears the moment the tab/window closes. */
Atlas.auth = {
  _backend() { return localStorage.getItem('atlas_remember') === '0' ? sessionStorage : localStorage; },
  get token() {
    try { return JSON.parse(this._backend().getItem('atlas_auth') || 'null')?.token || null; }
    catch { return null; }
  },
  get user() {
    try { return JSON.parse(this._backend().getItem('atlas_auth') || 'null')?.user || null; }
    catch { return null; }
  },
  set(token, user, remember = true) {
    localStorage.setItem('atlas_remember', remember ? '1' : '0');
    const active = remember ? localStorage : sessionStorage;
    const other = remember ? sessionStorage : localStorage;
    other.removeItem('atlas_auth');
    active.setItem('atlas_auth', JSON.stringify({ token, user }));
  },
  clear() { localStorage.removeItem('atlas_auth'); sessionStorage.removeItem('atlas_auth'); },
};

const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));
const el = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '—';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
}
function fmtPct(x) { return x === null || x === undefined ? '—' : Math.round(x * 100) + '%'; }
function fmtNum(x) { return x === null || x === undefined ? '—' : Number(x).toLocaleString(); }
function fmtDate(s) {
  if (!s) return '—';
  try { const d = new Date(s); return d.toLocaleDateString(undefined, {day:'2-digit', month:'short', year:'numeric'}); }
  catch { return String(s).slice(0,10); }
}
function fmtTime(s) {
  try { return new Date(s).toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'}); }
  catch { return ''; }
}

// score/status → semantic color, same thresholds the backend implies (0.75 / 0.5)
function scoreColor(score) {
  if (score === null || score === undefined) return 'var(--ink3)';
  if (score >= 0.75) return 'var(--espresso)';
  if (score >= 0.5) return 'var(--mid)';
  return 'var(--wine)';
}
function scoreHex(score) {
  if (score === null || score === undefined) return '#6E6266';
  if (score >= 0.75) return '#2E292B';
  if (score >= 0.5) return '#6E6266';
  return '#7A6270';
}
function confidenceLabel(score) {
  if (score === null || score === undefined) return 'UNSCORED';
  if (score >= 0.85) return 'HIGH CONFIDENCE';
  if (score >= 0.6) return 'MODERATE CONFIDENCE';
  if (score >= 0.4) return 'LOW CONFIDENCE';
  return 'UNCONFIRMED';
}
function statusColor(status) {
  const s = (status || '').toLowerCase();
  if (s.includes('verified') && !s.includes('partial')) return 'var(--espresso)';
  if (s.includes('partial')) return 'var(--mid)';
  return 'var(--wine)';
}
function verdictLabel(verdict) {
  const v = (verdict || '').toLowerCase();
  if (v.includes('verified') && !v.includes('partial')) return 'VERIFIED';
  if (v.includes('partial')) return 'PARTIALLY VERIFIED';
  return 'UNVERIFIED';
}
function badge(text, variant='neutral') {
  return `<span class="badge badge-${variant}">${escapeHtml(text)}</span>`;
}
function emptyState(icon, title, sub='') {
  return `<div class="empty"><div class="empty-icon">${icon}</div><div class="empty-title">${escapeHtml(title)}</div>${sub ? `<div class="empty-sub">${escapeHtml(sub)}</div>` : ''}</div>`;
}
function skeletonBlock(h='80px') {
  return `<div class="skeleton" style="height:${h};border-radius:var(--r);margin-bottom:10px;"></div>`;
}

// tiny markdown → html (headers, bold, italic, lists, links, paragraphs) — no external deps
function mdToHtml(md) {
  if (!md) return '';
  let src = escapeHtml(md);
  src = src.replace(/^######\s+(.*)$/gm, '<h6>$1</h6>')
           .replace(/^#####\s+(.*)$/gm, '<h5>$1</h5>')
           .replace(/^####\s+(.*)$/gm, '<h4>$1</h4>')
           .replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
           .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
           .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>');
  src = src.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\*(.+?)\*/g, '<em>$1</em>');
  src = src.replace(/`([^`]+)`/g, '<code>$1</code>');
  src = src.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // lists — ordered (1. 2. 3.) and unordered (-/*) tracked separately so they
  // don't get merged into the wrong list type when both appear in one report
  src = src.replace(/^\d+\.\s+(.*)$/gm, '<oli>$1</oli>');
  src = src.replace(/^(?:-|\*)\s+(.*)$/gm, '<uli>$1</uli>');
  src = src.replace(/(<oli>.*<\/oli>\n?)+/g, m => `<ol>${m.replace(/<oli>/g, '<li>').replace(/<\/oli>/g, '</li>').replace(/\n/g, '')}</ol>`);
  src = src.replace(/(<uli>.*<\/uli>\n?)+/g, m => `<ul>${m.replace(/<uli>/g, '<li>').replace(/<\/uli>/g, '</li>').replace(/\n/g, '')}</ul>`);
  // paragraphs — walk line by line so headings/lists are never nested
  // inside a stray <p> or joined with a spurious <br> when they aren't
  // preceded by a blank line
  const isBlockLine = line => /^\s*<(h\d|ul|ol|li)\b/.test(line) || /^\s*<\/(ul|ol)>/.test(line);
  const outLines = [];
  let buffer = [];
  const flushParagraph = () => {
    if (buffer.length) { outLines.push(`<p>${buffer.join('<br>')}</p>`); buffer = []; }
  };
  for (const line of src.split('\n')) {
    if (!line.trim()) { flushParagraph(); continue; }
    if (isBlockLine(line)) { flushParagraph(); outLines.push(line); }
    else { buffer.push(line.trim()); }
  }
  flushParagraph();
  return outLines.join('\n');
}

// Report section headers (Introduction, Key Findings, Risks, ...) render in
// the same small-caps mono "field label" language used everywhere else in
// the app (sidebar section titles, card labels) instead of raw giant browser
// h1/h2 text — ties report content into the rest of the visual system
// instead of reading like a separate plain document dump.
const REPORT_SECTION_ICONS = {
  'introduction': '§', 'key findings': '§', 'findings': '§', 'risks': '!',
  'opportunities': '+', 'conclusion': '—', 'sources': '№', 'summary': '§',
};
function styledReportHtml(md) {
  const html = mdToHtml(md);
  return html.replace(/<(h[1-3])>(.*?)<\/\1>/g, (_, tag, text) => {
    const key = text.trim().toLowerCase();
    const icon = REPORT_SECTION_ICONS[key] || '▸';
    return `<div class="report-section-head"><span class="report-section-icon">${icon}</span>${text}</div>`;
  });
}

function toast(msg, variant='') {
  const t = el(`<div class="toast ${variant}">${escapeHtml(msg)}</div>`);
  $('#toastHost').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, 3800);
}

/* ── HTTP client (mirrors the Python api_get/api_post/api_delete contract) ── */
async function apiCall(method, path, body, opts_ = {}) {
  const url = Atlas.config.apiBase + path;
  const opts = { method, headers: { ...(opts_.headers || {}) } };
  if (Atlas.auth.token) opts.headers['Authorization'] = 'Bearer ' + Atlas.auth.token;
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    if (!opts_.silent) toast('Cannot reach Atlas right now. Please check your connection and try again.', 'err');
    throw e;
  }
  if (res.status === 401 && !opts_.silent) {
    Atlas.auth.clear();
    showAuthGate('login', 'Your session expired. Please sign in again.');
    throw new Error('Unauthorized');
  }
  let json = null;
  try { json = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    const detail = (json && (json.detail || json.error)) || res.statusText;
    if (!opts_.silent && !(opts_.quietStatuses || []).includes(res.status)) toast(`Error ${res.status}: ${detail}`, 'err');
    throw new Error(detail);
  }
  return json;
}
const apiGet = (path, params) => {
  const qs = params ? '?' + new URLSearchParams(Object.entries(params).filter(([,v]) => v !== undefined && v !== null)).toString() : '';
  return apiCall('GET', path + qs);
};
const apiPost = (path, body) => apiCall('POST', path, body);
const apiPut = (path, body) => apiCall('PUT', path, body);
const apiDelete = (path, opts) => apiCall('DELETE', path, undefined, opts);

async function checkHealth() {
  const base = Atlas.config.apiBase.replace(/\/api\/?$/, '');
  try {
    const r = await fetch(base + '/health', { signal: AbortSignal.timeout(2500) });
    if (!r.ok) { setHealth('offline'); return; }
    // Backend responded, so it's reachable — but "reachable" and "fully
    // healthy" aren't the same thing: embeddings_available tells us whether
    // Atlas AI chat context and Discover semantic search actually have a
    // working embedding backend, or are silently degraded even though the
    // API itself is up.
    let body = null;
    try { body = await r.json(); } catch { /* no/invalid body — treat as online */ }
    const embeddingsAvailable = body ? body.embeddings_available !== false : true;
    setHealth(embeddingsAvailable ? 'online' : 'degraded');
  } catch { setHealth('offline'); }
}
function setHealth(state) {
  // state: 'online' (backend reachable, embeddings working) |
  //        'degraded' (backend reachable, embeddings unavailable) |
  //        'offline' (backend unreachable)
  const led = $('#statusLed');
  led.classList.toggle('on', state === 'online');
  led.classList.toggle('degraded', state === 'degraded');
  const label = { online: 'System Online', degraded: 'Degraded — embeddings unavailable', offline: 'System Offline' }[state];
  $('#statusText').textContent = label;
}

/* ══════════════════════════════════════════════════════════════════════
   STATE + ROUTER
   ══════════════════════════════════════════════════════════════════════ */
Atlas.state = {
  view: 'home',
  activeCase: null,       // normalized case object currently shown
  historyCache: null,
  analyticsCache: null,
  kbCache: null,
  chatMessages: [],
  chatHistoryApi: [],
};

const NAV_LABELS = {
  home: ['Atlas', 'Home'], research: ['Atlas', 'Research'],
  playground: ['Atlas', 'AI Playground'],
  compare: ['Atlas', 'Compare'],
  history: ['Atlas', 'Cases'],
  search: ['Atlas', 'Cases', 'Search'], analytics: ['Atlas', 'Insights'],
  kb: ['Atlas', 'Library'], case: ['Atlas', 'Cases', 'Case'],
  settings: ['Atlas', 'Settings'], runtime: ['Atlas', 'Runtime & Control'], knowledge: ['Atlas', 'Knowledge'], atlasbench: ['Atlas', 'AtlasBench'], workflowStudio: ['Atlas', 'Workflow Studio'],
};

// Information architecture: six areas on the top bar; Research owns its working views as a sub-bar.
const AREA_OF = {
  home: 'home', research: 'research', history: 'research', kb: 'research', search: 'research', knowledge: 'research', analytics: 'research', case: 'research',
  playground: 'playground', compare: 'compare', atlasbench: 'atlasbench', workflowStudio: 'workflowStudio', runtime: 'runtime', settings: null,
};
const RESEARCH_TABS = [['research', 'New case'], ['history', 'Cases'], ['kb', 'Library'], ['search', 'Discover'], ['knowledge', 'Knowledge map'], ['analytics', 'Insights']];
function renderSubnav(area, navKey) {
  const bar = $('#subnav'); if (!bar) return;
  if (area !== 'research') { bar.classList.remove('show'); bar.innerHTML = ''; return; }
  bar.innerHTML = '<span class="subbar-label">Research</span>' + RESEARCH_TABS.map(([k, label]) =>
    `<button type="button" class="subtab ${k === navKey ? 'active' : ''}" data-sub="${k}">${label}</button>`).join('');
  bar.classList.add('show');
  bar.querySelectorAll('[data-sub]').forEach(b => b.addEventListener('click', () => Atlas.nav(b.dataset.sub)));
}
Atlas.nav = function(view, opts) {
  // "Runtime & Control" is the Routing & cost tab of AtlasBench (routing.js): its own entry point,
  // the same backend. AtlasBench itself always opens on its Runs tab.
  const navKey = view;
  if (view === 'runtime') { view = 'atlasbench'; opts = Object.assign({}, opts, { tab: 'routing' }); }
  else if (view === 'atlasbench' && !(opts && opts.tab)) { opts = Object.assign({}, opts, { tab: 'runs' }); }
  Atlas.state.view = view;
  Atlas.state.navKey = navKey;
  const area = AREA_OF[navKey];
  const on = n => AREA_OF[n.dataset.nav] === area && area !== null;
  $$('.topnav-item').forEach(n => { const a = on(n); n.classList.toggle('active', a); if (a) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current'); });
  $$('.mobile-nav-item').forEach(n => n.classList.toggle('active', n.dataset.nav === navKey || (on(n) && !n.classList.contains('sub') && navKey === 'case')));
  renderSubnav(area, navKey);
  closeMobileNav();
  if (navKey !== 'case') { try { history.replaceState(null, '', '#/' + navKey); } catch { /* file:// */ } }
  render(opts);
  window.scrollTo?.(0, 0);
};

function openMobileNav() { $('#mobileNavDrawer').classList.add('show'); }
function closeMobileNav() { $('#mobileNavDrawer')?.classList.remove('show'); }
$('#mobileNavToggle').addEventListener('click', openMobileNav);
$('#mobileNavClose').addEventListener('click', closeMobileNav);
$('#mobileNavBackdrop').addEventListener('click', closeMobileNav);
$$('.mobile-nav-item').forEach(n => n.addEventListener('click', () => Atlas.nav(n.dataset.nav)));

function crumbsHtml(view) {
  const parts = NAV_LABELS[view] || ['Atlas'];
  return parts.map((p,i) => `${i>0?'<span class="sep">/</span>':''}<span class="${i===parts.length-1?'cur':''}">${escapeHtml(p)}</span>`).join('');
}

function render(opts) {
  const v = Atlas.state.view;
  $('#chatFabWrap')?.classList.toggle('pg-hide-floating-chat', v === 'playground' || v === 'compare');
  const main = $('#mainView'), ctx = $('#contextPanel');
  main.innerHTML = ''; ctx.innerHTML = '';
  $('#statusBarCase').textContent = '';
  const views = { home: viewHome, research: viewResearch, playground: (main, ctx, opts) => window.AtlasPlayground?.render(main, ctx, opts), compare: (main, ctx, opts) => window.AtlasCompare?.render(main, ctx, opts), history: viewHistory, search: viewSearch, analytics: viewAnalytics, kb: viewKB, case: viewCase, settings: viewSettings, knowledge: viewKnowledge, atlasbench: viewAtlasBench, workflowStudio: (main, ctx, opts) => window.viewWorkflowStudio?.(main, ctx, opts) };
  (views[v] || viewHome)(main, ctx, opts || {});
  $('#appRoot').classList.toggle('ctx-collapsed', !ctx.innerHTML.trim());
}

$$('.topnav-item').forEach(n => n.addEventListener('click', () => Atlas.nav(n.dataset.nav)));

function closeLogoutModal() { $('#logoutModal').classList.remove('show'); }
function confirmLogout() { closeLogoutModal(); handleLogout(); }

// Account dropdown: click avatar to toggle, click outside or an item to close
$('#accountTrigger').addEventListener('click', (e) => {
  e.stopPropagation();
  $('#accountDropdown').classList.toggle('show');
});
$('#settingsMenuItem').addEventListener('click', () => {
  $('#accountDropdown').classList.remove('show');
  Atlas.nav('settings');
});
$('#logoutMenuItem').addEventListener('click', () => {
  $('#accountDropdown').classList.remove('show');
  $('#logoutModal').classList.add('show');
});
document.addEventListener('click', (e) => {
  const wrap = $('.account-menu-wrap');
  if (wrap && !wrap.contains(e.target)) $('#accountDropdown').classList.remove('show');
});

$('#globalSearch').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.value.trim()) {
    Atlas.nav('search', { prefillQuery: e.target.value.trim() });
    $('#searchBar').style.display = 'none';
  }
});
function openSearchBar() { $('#searchBar').style.display = 'flex'; $('#globalSearch').focus(); }
$('#searchToggle').addEventListener('click', (e) => {
  e.stopPropagation();
  const bar = $('#searchBar');
  bar.style.display = bar.style.display === 'none' ? 'flex' : 'none';
  if (bar.style.display === 'flex') $('#globalSearch').focus();
});
document.addEventListener('click', (e) => {
  const bar = $('#searchBar');
  if (bar.style.display !== 'none' && !bar.contains(e.target) && e.target.id !== 'searchToggle') bar.style.display = 'none';
});
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openSearchBar(); }
  if (e.key === 'Escape') {
    closeMobileNav();
    $('#accountDropdown')?.classList.remove('show');
    $('#searchBar').style.display = 'none';
    $$('.modal-overlay.show').forEach(m => m.classList.remove('show'));
  }
});
$('#accountTrigger').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#accountDropdown').classList.toggle('show'); }
});

/* ══════════════════════════════════════════════════════════════════════
   NORMALIZATION — reconcile /research/run vs /history/report/{id} shapes
   ══════════════════════════════════════════════════════════════════════ */
function normalizeFromRun(data) {
  const { session, report, top_sources, verification } = data;
  return {
    reportId: report?.report_id ?? session?.report_id ?? null,
    topic: report?.topic ?? '',
    summary: report?.summary ?? '',
    reportMarkdown: report?.report_markdown ?? '',
    keyStatistics: report?.key_statistics ?? [],
    swot: report?.swot ?? null,
    executiveBrief: report?.executive_brief ?? null,
    verification: verification ?? null,
    evaluation: data?.evaluation ?? null,
    agenticStatus: data?.agentic_status ?? null,
    agenticVerificationStatus: data?.agentic_verification_status ?? null,
    replanCount: data?.replan_count ?? 0,
    toolTrace: data?.tool_trace ?? [],
    researchTrace: data?.research_trace ?? [],
    insights: data?.insights ?? null,
    sources: top_sources ?? [],
    createdAt: new Date().toISOString(),
    totalExecutionMs: session?.total_execution_ms ?? null,
    totalTokens: session?.total_tokens ?? null,
    estimatedCost: session?.estimated_cost_usd ?? 0,
    live: true,
  };
}
function normalizeFromHistory(detail, reportId) {
  const report = detail.report, sources = detail.sources || [];
  return {
    reportId: reportId,
    topic: report?.topic ?? '',
    summary: report?.summary ?? '',
    reportMarkdown: report?.report_markdown ?? '',
    keyStatistics: report?.key_statistics ?? [],
    swot: report?.swot ?? null,
    executiveBrief: report?.executive_brief ?? null,
    verification: report?.verification ?? null,
    evaluation: detail?.evaluation ?? null,
    agenticStatus: detail?.agentic_status ?? null,
    agenticVerificationStatus: detail?.agentic_verification_status ?? null,
    replanCount: detail?.replan_count ?? 0,
    toolTrace: detail?.tool_trace ?? [],
    researchTrace: detail?.research_trace ?? [],
    insights: null,
    sources: sources,
    createdAt: report?.created_at ?? null,
    totalExecutionMs: null, totalTokens: null, estimatedCost: null,
    live: false,
  };
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: HOME
   ══════════════════════════════════════════════════════════════════════ */
async function apiGetSilent(path, params) {
  const qs = params ? '?' + new URLSearchParams(Object.entries(params)).toString() : '';
  return apiCall('GET', path + qs, undefined, { silent: true });
}
function greetingWord() { const h = new Date().getHours(); return h < 5 ? 'Working late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'; }
function activityRowHtml(a) {
  return `<button type="button" class="act-row" data-act="${a.kind}" data-id="${escapeAttr(a.id)}"><span class="act-type">${a.kind === 'case' ? 'Research' : 'Playground'}</span><span><span class="act-title">${escapeHtml(a.title)}</span></span><span class="act-meta">${escapeHtml(a.meta)}</span></button>`;
}
async function viewHome(main, ctx) {
  const first = ((Atlas.auth.user && Atlas.auth.user.name) || '').trim().split(/\s+/)[0];
  main.innerHTML = `
    <div class="ov">
      <header class="ov-head">
        <div>
          <div class="ov-kicker">${escapeHtml(new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }))}</div>
          <h1 class="ov-title">${greetingWord()}${first ? `, <em>${escapeHtml(first)}</em>` : ''}.</h1>
          <p class="ov-sub" id="ovSub">Pick up where you left off, or open a new line of inquiry.</p>
        </div>
        <div class="ov-cta"><button class="btn" id="ovNew">Open a research case</button></div>
      </header>
      <div class="ov-grid">
        <section aria-labelledby="ovActTitle">
          <div class="ov-sec-title"><h2 id="ovActTitle">Recent activity</h2><span class="small muted" id="ovCount"></span></div>
          <div id="ovActivity">${skeletonBlock('56px')}${skeletonBlock('56px')}${skeletonBlock('56px')}</div>
        </section>
        <aside aria-labelledby="ovBeginTitle">
          <div class="ov-sec-title"><h2 id="ovBeginTitle">Where to begin</h2></div>
          <ul class="begin">
            ${[['research','Research','Ask a question; get sources, claims and a verified brief.'],['playground','AI Playground','Put one question to up to three models.'],['compare','Compare','Ask OpenAI, Anthropic, Google and xAI the same question and see measured cost, speed and sources.'],['atlasbench','AtlasBench','Score models on a frozen task and evidence.'],['workflowStudio','Workflow Studio','Save and run a deterministic workflow.'],['runtime','Runtime & Control','Choose how models are selected and see what ran.']].map(([k, n, d]) => `<li><button type="button" data-go="${k}"><span><b>${n}</b><span class="d">${d}</span></span><span class="begin-go">→</span></button></li>`).join('')}
          </ul>
        </aside>
      </div>
    </div>`;
  ctx.innerHTML = '';
  $('#ovNew').addEventListener('click', () => Atlas.nav('research'));
  $$('[data-go]').forEach(b => b.addEventListener('click', () => Atlas.nav(b.dataset.go)));

  const settle = async (path, params) => { try { return (await apiGetSilent(path, params))?.data || null; } catch { return undefined; } };
  const [reports, convos] = await Promise.all([settle('/history/reports', { limit: 6, offset: 0 }), settle('/playground/conversations')]);
  const box = $('#ovActivity'); if (!box || Atlas.state.view !== 'home') return;
  if (reports === undefined && convos === undefined) {
    box.innerHTML = emptyState('!', 'Recent activity is unavailable', 'Atlas could not reach the server. Your work is not lost; try again in a moment.');
    return;
  }
  const items = [
    ...((reports && reports.reports) || []).map(r => ({ kind: 'case', id: r.report_id, title: r.topic, when: r.created_at, meta: `${fmtDate(r.created_at)} · ${verdictLabel(r.verification_verdict)}` })),
    ...((convos && convos.items) || []).map(c => ({ kind: 'conversation', id: c.conversation_id, title: c.title || 'Untitled conversation', when: c.updated_at, meta: `${fmtDate(c.updated_at)} · ${Number(c.message_count) || 0} msgs` })),
  ].sort((x, y) => new Date(y.when) - new Date(x.when)).slice(0, 6);
  if (!items.length) {
    $('#ovSub').textContent = 'Nothing here yet. Your first research case is the best place to start.';
    box.innerHTML = emptyState('§', 'No activity yet', 'Open a research case and it will appear here, along with your Playground conversations.');
    return;
  }
  $('#ovSub').textContent = 'Pick up where you left off, or open a new line of inquiry.';
  $('#ovCount').textContent = `latest ${items.length}`;
  box.innerHTML = items.map(activityRowHtml).join('');
  $$('.act-row').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.act === 'case') openHistoryCase(b.dataset.id);
    else { Atlas.nav('playground'); window.AtlasPlayground?.openConversation?.(b.dataset.id); }
  }));
}
function recentCaseCard(r, opts) {
  opts = opts || {};
  const c = scoreColor(r.verification_score || 0);
  const id = escapeAttr(r.report_id);
  const sum = r.summary || '';
  return `<div class="case-row" role="link" tabindex="0" onclick="openHistoryCase('${id}')" onkeydown="if(event.target===this&&event.key==='Enter'){openHistoryCase('${id}')}">
    <div class="case-score" style="color:${c};" title="Verification score">${fmtPct(r.verification_score)}</div>
    <div style="min-width:0;">
      <div class="case-topic">${escapeHtml(r.topic)}</div>
      ${sum ? `<div class="case-sum">${escapeHtml(sum.slice(0,150))}${sum.length>150?'…':''}</div>` : ''}
      <div class="case-meta">${fmtDate(r.created_at)} · ${verdictLabel(r.verification_verdict)}</div>
    </div>
    <div>${opts.deletable ? `<button class="btn-icon btn-sm" title="Delete case" aria-label="Delete case" onclick="event.stopPropagation();requestDeleteCase('${id}', this)">Delete</button>` : ''}</div>
  </div>`;
}
// Two-click delete: first click arms it (icon flips to a confirm state for a
// few seconds), second click actually deletes via the existing knowledge-base
// endpoint and removes the card in place — no full-page reload needed.
async function requestDeleteCase(reportId, btn) {
  if (btn.dataset.confirm !== '1') {
    btn.dataset.confirm = '1';
    btn.textContent = 'Confirm';
    btn.title = 'Click again to permanently delete';
    btn.style.color = 'var(--wine)';
    btn.style.borderColor = 'var(--wine-line)';
    clearTimeout(btn._resetTimer);
    btn._resetTimer = setTimeout(() => {
      if (btn.isConnected && btn.dataset.confirm === '1') {
        btn.dataset.confirm = '0'; btn.textContent = 'Delete'; btn.title = 'Delete case';
        btn.style.color = ''; btn.style.borderColor = '';
      }
    }, 4000);
    return;
  }
  btn.disabled = true;
  try {
    const res = await apiDelete(`/knowledge-base/report/${reportId}`);
    if (res && res.success) {
      toast('Case deleted.', 'ok');
      const card = btn.closest('.case-row, .card');
      if (card) card.remove();
    } else {
      console.error('delete case failed:', res);
      toast('Could not delete this case. Please try again.', 'err');
      btn.disabled = false;
    }
  } catch (e) {
    console.error('delete case failed:', e);
    toast('Could not delete this case. Please try again.', 'err');
    btn.disabled = false;
  }
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: RESEARCH (New Case)
   ══════════════════════════════════════════════════════════════════════ */
const EXAMPLE_TOPICS = [
  'Impact of large language models on scientific research',
  'State of solid-state battery commercialization',
  'Regulatory landscape for AI in healthcare',
];

function viewResearch(main, ctx, opts) {
  opts = opts || {};
  main.innerHTML = `
    <div class="rs">
      <div class="crumbs">${crumbsHtml('research')}</div>
      <h1 class="page-title">What do you want to find out?</h1>
      <p class="page-desc">State one question. Atlas retrieves sources, extracts claims, checks them against each other and files the result in your Library.</p>
      <div id="rsForm" class="rs-grid">
        <div>
          <label class="field-label" for="rTopic">Research question</label>
          <textarea class="rs-q" id="rTopic" rows="2" maxlength="300" placeholder="e.g. How close is solid-state battery commercialization?" aria-describedby="rTopicHelp"></textarea>
          <div class="rs-count" id="rTopicHelp"><span id="rTopicN">0</span> / 300 · minimum 3 characters</div>
          <div class="chip-row" aria-label="Example questions">${EXAMPLE_TOPICS.map(t => `<button type="button" class="chip" data-example="${escapeAttr(t)}">${escapeHtml(t.slice(0, 44))}${t.length > 44 ? '…' : ''}</button>`).join('')}</div>
          <fieldset class="rs-modes" style="margin-inline:0;padding:0;">
            <legend class="field-label" style="padding:0;margin:22px 0 8px;">Research mode</legend>
            <label class="rs-mode"><input type="radio" name="rMode" value="explore" data-k="12" checked><span><b>Explore</b><small>Broad understanding of a topic.</small></span><span class="k">12 sources</span></label>
            <label class="rs-mode"><input type="radio" name="rMode" value="compare" data-k="14"><span><b>Compare</b><small>Alternatives and their trade-offs.</small></span><span class="k">14 sources</span></label>
            <label class="rs-mode"><input type="radio" name="rMode" value="investigate" data-k="20"><span><b>Investigate</b><small>Focused, deeper work on one question.</small></span><span class="k">20 sources</span></label>
            <label class="rs-mode"><input type="radio" name="rMode" value="literature_review" data-k="20"><span><b>Literature review</b><small>Synthesise what the research says.</small></span><span class="k">20 sources</span></label>
            <label class="rs-mode"><input type="radio" name="rMode" value="brief" data-k="8"><span><b>Brief</b><small>A concise, evidence-backed answer.</small></span><span class="k">8 sources</span></label>
          </fieldset>
        </div>
        <aside class="rs-side" aria-label="Case options">
          <div class="field-label">Options</div>
          <label class="check-row"><input type="checkbox" id="rVerify" checked><span>Verify claims<small>Cross-check claims against each other and their sources.</small></span></label>
          <label class="check-row"><input type="checkbox" id="rBrief" checked><span>Executive brief<small>Add a short summary at the top of the report.</small></span></label>
          <label class="check-row"><input type="checkbox" id="rMemory" checked><span>Keep in Library<small>Save to memory so Playground and Ask Atlas can retrieve it.</small></span></label>
          <label class="f-label" for="rContext" style="margin-top:18px;">Context <span class="muted" style="text-transform:none;letter-spacing:0;">(optional)</span></label>
          <textarea class="input" id="rContext" rows="3" maxlength="3000" placeholder="Angle, audience or constraints"></textarea>
          <div class="rs-actions"><button class="btn" id="rRunBtn" type="button">Open case</button></div>
          <p class="small muted" style="margin:12px 0 0;">A case usually takes a minute or two. Keep this tab open while it runs.</p>
        </aside>
      </div>
      <div id="rResultBox" aria-live="polite"></div>
    </div>`;
  ctx.innerHTML = '';
  const topic = $('#rTopic');
  const count = () => { $('#rTopicN').textContent = topic.value.length; };
  topic.addEventListener('input', count);
  topic.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) runResearch(); });
  $$('[data-example]').forEach(b => b.addEventListener('click', () => { topic.value = b.dataset.example; count(); topic.focus(); }));
  $$('input[name="rMode"]').forEach(r => r.addEventListener('change', () => { if (r.value === 'brief' && r.checked) $('#rBrief').checked = true; }));
  $('#rRunBtn').addEventListener('click', runResearch);
  if (opts.prefillTopic) { topic.value = opts.prefillTopic; }
  count(); topic.focus();
}

let __researchRunning = false;
async function runResearch() {
  if (__researchRunning) return;
  const topic = $('#rTopic').value.trim();
  if (topic.length < 3) { toast('Enter a question of at least 3 characters.', 'err'); $('#rTopic').focus(); return; }
  const modeEl = document.querySelector('input[name="rMode"]:checked');
  const payload = {
    topic,
    research_mode: modeEl.value,
    user_context: $('#rContext').value.trim() || null,
    include_verification: $('#rVerify').checked,
    include_executive_brief: $('#rBrief').checked,
    persist_memory: $('#rMemory').checked,
    top_k_urls: parseInt(modeEl.dataset.k || '10', 10),
  };
  __researchRunning = true;
  const form = $('#rsForm'), box = $('#rResultBox');
  form.style.display = 'none';
  const started = Date.now();
  box.innerHTML = `<section class="rs-work" role="status"><div class="eyebrow">Case in progress</div><h2>${escapeHtml(topic)}</h2>
    <div class="rs-clock" id="rClock">0:00</div>
    <p class="muted" style="margin:0;max-width:56ch;">Atlas runs this as one request, so there is no live per-step progress to show. Typical work, in order:</p>
    <ol class="rs-stages"><li>Retrieve and rank sources</li><li>Extract claims and trace each to a source</li>${payload.include_verification ? '<li>Cross-verify claims and score confidence</li>' : ''}<li>Assemble the report${payload.persist_memory ? ' and keep it in your Library' : ''}</li></ol></section>`;
  const tick = setInterval(() => { const s = Math.floor((Date.now() - started) / 1000); const c = $('#rClock'); if (c) c.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }, 1000);
  const fail = (msg) => {
    form.style.display = '';
    box.innerHTML = `<div class="rs-err" role="alert"><b>This case could not be opened.</b><div class="small" style="margin-top:4px;">${escapeHtml(msg || 'Something went wrong on the server. Your question is still in the form; try again.')}</div></div>`;
  };
  try {
    const result = await apiPost('/research/run', payload);
    if (!result || !result.success) { console.error('research run failed:', result); fail(); return; }
    Atlas.state.activeCase = normalizeFromRun(result.data);
    Atlas.nav('case', { fromResearch: true });
  } catch (e) {
    console.error('research run failed:', e);
    fail();
  } finally {
    clearInterval(tick);
    __researchRunning = false;
  }
}

async function openHistoryCase(reportId) {
  Atlas.nav('history'); // shows list w/ spinner context, then swap
  try {
    const detail = await apiGet(`/history/report/${reportId}`);
    if (!detail || !detail.success) { toast('Failed to load case detail.', 'err'); return; }
    Atlas.state.activeCase = normalizeFromHistory(detail.data, reportId);
    Atlas.nav('case', { readonly: true });
  } catch { /* toast already shown by apiCall */ }
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: CASE (the signature case detail view)
   ══════════════════════════════════════════════════════════════════════ */
function viewCase(main, ctx, opts) {
  const c = Atlas.state.activeCase;
  if (!c) { viewHome(main, ctx); return; }
  $('#statusBarCase').textContent = `Case: ${c.topic.slice(0,48)}${c.topic.length>48?'…':''}`;

  const vScore = c.verification?.overall_verification_score ?? null;
  const strength = vScore === null ? '—' : vScore >= 0.75 ? 'Strong' : vScore >= 0.5 ? 'Moderate' : 'Weak';
  const readonly = !!opts.readonly;

  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('case')}</div>
      <div class="page-head">
        <div>
          <div class="page-title">${escapeHtml(c.topic)} ${badge(readonly ? 'Filed Case' : 'Open Case', readonly ? 'brass' : 'sage')}</div>
          <div class="page-desc">${escapeHtml(c.summary || 'Case filed and ready for review.')}</div>
        </div>
        <div class="flex gap-8">
          <button class="btn btn-ghost" onclick="downloadCaseMd()">Export</button>
          <button class="btn btn-ghost" onclick="copyCaseLink()">Share</button>
        </div>
      </div>

      <div class="case-figures">
        <div class="fig"><b>${c.sources.length}</b><span>Sources</span></div>
        <div class="fig"><b style="color:${scoreColor(vScore)}">${fmtPct(vScore)}</b><span>Verification</span></div>
        <div class="fig"><b>${strength}</b><span>Strength</span></div>
      </div>

      <div class="tabs">
        <div class="tab active" data-tab="overview">Overview</div>
        <div class="tab" data-tab="sources">Sources</div>
        <div class="tab" data-tab="evidence">Evidence</div>
        <div class="tab" data-tab="notes">Notes</div>
        <div class="tab" data-tab="findings">Findings</div>
        <div class="tab" data-tab="questions">Questions</div>
      </div>

      <div class="tab-panel active" id="tab-overview">
        ${overviewPanel(c, vScore, strength)}
      </div>

      <div class="tab-panel" id="tab-sources">
        ${c.sources.length ? c.sources.map(artifactCard).join('') : emptyState('№', 'No sources yet', 'This case has no sources on file.')}
      </div>

      <div class="tab-panel" id="tab-evidence">
        ${evidencePanel(c)}
      </div>

      <div class="tab-panel" id="tab-notes">
        ${studentNotesPanel(c)}
      </div>

      <div class="tab-panel" id="tab-findings">
        ${findingsPanel(c)}
      </div>

      <div class="tab-panel" id="tab-questions">
        ${questionsPanel(c)}
      </div>
    </div>
  `;

  $$('.tab').forEach(t => t.addEventListener('click', () => {
    $$('.tab').forEach(x => x.classList.remove('active'));
    $$('.tab-panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    $('#tab-' + t.dataset.tab).classList.add('active');
  }));

  const mapToggle = $('#caseMapToggle');
  if (mapToggle) mapToggle.addEventListener('click', () => {
    const wrap = $('#caseMapWrap');
    const showing = wrap.style.display !== 'none';
    wrap.style.display = showing ? 'none' : 'block';
    mapToggle.textContent = showing ? 'View research map' : 'Hide research map';
    if (!showing && !wrap.dataset.rendered) {
      renderLandscape($('#landscapeMount'), c);
      wrap.dataset.rendered = '1';
    }
  });
  loadStudentNotes(c.reportId);
  loadQuestions(c.reportId);
  loadResearchInsights(c.reportId);
  renderCaseHealth(ctx, c);
}

function formatPublicationDate(value) {
  if (!value) return 'Unknown';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
}
function artifactCard(s) {
  const c = scoreColor(s.final_score);
  return `<div class="artifact">
    <div class="artifact-top">
      <div class="artifact-title">${escapeHtml(s.title || 'Untitled source')}</div>
      ${s.rank_position ? `<span class="artifact-tag" style="color:${c};border-color:${c};">SOURCE #${String(s.rank_position).padStart(2,'0')}</span>` : ''}
    </div>
    <div class="artifact-domain">${escapeHtml(s.domain || '')}</div>
    <p class="artifact-snippet">${escapeHtml((s.snippet||'').slice(0,220))}</p>
    <div class="artifact-meta">
      <span>PUBLISHED <b style="color:var(--ink)">${s.publish_date ? escapeHtml(formatPublicationDate(s.publish_date)) : 'Unknown'}</b></span>
      <span>RELEVANCE <b style="color:var(--ink)">${s.relevance_score!=null?s.relevance_score.toFixed(2):'—'}</b></span>
      <span>CREDIBILITY <b style="color:var(--ink)">${s.credibility_score!=null?s.credibility_score.toFixed(2):'—'}</b></span>
      <a href="${s.url}" target="_blank" rel="noopener" style="color:${c};">VIEW SOURCE ↗</a>
    </div>
  </div>`;
}
function claimRow(claim) {
  const c = statusColor(claim.status);
  const evidence = claim.evidence || [];
  return `<div class="evidence-row" style="border-left-color:${c};">
    <div style="font-family:var(--serif);font-size:13px;color:var(--ink);margin-bottom:5px;">${escapeHtml(claim.claim_text)}</div>
    <div class="flex gap-14" style="font-family:var(--mono);font-size:10.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:0.04em;">
      <span style="color:${c};">${escapeHtml(claim.status||'')}</span>
      <span>Confidence <b style="color:${c};">${fmtPct(claim.confidence)}</b></span>
    </div>
    ${evidence.length ? `<div class="claim-evidence">
      ${evidence.map(ev => `<a href="${escapeAttr(ev.url||'#')}" target="_blank" rel="noopener noreferrer" class="claim-evidence-item">
        <span class="claim-evidence-strength" style="color:${scoreColor(ev.evidence_strength)};">${fmtPct(ev.evidence_strength)}</span>
        <span class="claim-evidence-text">"${escapeHtml(truncate(ev.evidence_text||'', 160))}" <span class="claim-evidence-source">— ${escapeHtml(ev.title || 'source')}${ev.publish_date ? ` · Published ${escapeHtml(formatPublicationDate(ev.publish_date))}` : ''} ↗</span></span>
      </a>`).join('')}
    </div>` : ''}
  </div>`;
}
// Buckets claims into three plain-language groups by status, using the
// same thresholds statusColor()/verdictLabel() already use elsewhere —
// so a claim's color never contradicts which group it lands in.
function groupClaims(claims) {
  const groups = { supporting: [], uncertain: [], conflicting: [] };
  (claims || []).forEach(cl => {
    const s = (cl.status || '').toLowerCase();
    if (s.includes('verified') && !s.includes('partial')) groups.supporting.push(cl);
    else if (s.includes('partial')) groups.uncertain.push(cl);
    else groups.conflicting.push(cl);
  });
  return groups;
}

/* ── Overview: "What do I know so far?" ─────────────────────────────── */
function overviewPanel(c, vScore, strength) {
  const groups = groupClaims(c.verification?.claims);
  const brief = c.executiveBrief;
  const topInsights = (brief?.key_insights || []).slice(0, 3);

  let html = `<div class="card-glow">
    <div class="field-label" style="margin-bottom:8px;">Research question</div>
    <div style="font-family:var(--serif);font-size:15px;color:var(--ink);line-height:1.5;">${escapeHtml(c.topic)}</div>
  </div>`;

  if (topInsights.length) {
    html += `<div class="field-label" style="margin:18px 0 8px;">Key findings so far</div>` +
      topInsights.map(i => `<div class="small" style="display:flex;gap:8px;margin:6px 0;color:var(--ink2);line-height:1.5;"><span style="color:var(--wine);">◈</span>${escapeHtml(i)}</div>`).join('');
  }

  html += `<div class="field-label" style="margin:18px 0 8px;">Evidence status</div>
    <div class="grid grid-3">
      <div class="stat-tile" style="text-align:left;padding:14px 16px;">
        <div class="small muted">Supporting</div>
        <div style="font-family:var(--mono);font-size:1.6rem;font-weight:700;color:var(--espresso);">${groups.supporting.length}</div>
      </div>
      <div class="stat-tile" style="text-align:left;padding:14px 16px;">
        <div class="small muted">Uncertain</div>
        <div style="font-family:var(--mono);font-size:1.6rem;font-weight:700;color:var(--mid);">${groups.uncertain.length}</div>
      </div>
      <div class="stat-tile" style="text-align:left;padding:14px 16px;">
        <div class="small muted">Conflicting</div>
        <div style="font-family:var(--mono);font-size:1.6rem;font-weight:700;color:var(--wine);">${groups.conflicting.length}</div>
      </div>
    </div>`;

  if (groups.conflicting.length) {
    html += `<div class="small muted mt-8">Open questions — evidence conflicts or is unverified on ${groups.conflicting.length} claim${groups.conflicting.length===1?'':'s'}. See the Evidence tab for details.</div>`;
  }

  html += evaluationPanel(c);
  html += researchTracePanel(c);

  const evidenceItemCount = (c.verification?.claims || []).reduce((total, claim) => total + (claim.evidence || []).length, 0);
  const verifiedClaimCount = (c.verification?.claims || []).filter(claim => (claim.status || '').toLowerCase().includes('verified') && !(claim.status || '').toLowerCase().includes('partial')).length;
  html += `<div class="field-label" style="margin:18px 0 8px;">Research metrics</div>
    <div class="grid grid-3">
      <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Ranked sources</div><div style="font-family:var(--mono);font-size:1.35rem;font-weight:700;">${c.sources.length}</div></div>
      <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Evidence items</div><div style="font-family:var(--mono);font-size:1.35rem;font-weight:700;">${evidenceItemCount}</div></div>
      <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Verified claims</div><div style="font-family:var(--mono);font-size:1.35rem;font-weight:700;">${verifiedClaimCount}</div></div>
    </div>
    <div class="small muted mt-8">${c.sources.length} ranked source${c.sources.length===1?'':'s'} available in the Sources tab${c.createdAt ? ' · filed ' + fmtDate(c.createdAt) : ''}.</div>`;

  const nextStep = !c.sources.length ? null
    : groups.conflicting.length ? { label: 'Review the evidence that needs a closer look →', tab: 'evidence' }
    : !brief ? { label: 'Read the sources →', tab: 'sources' }
    : { label: 'Read the findings →', tab: 'findings' };
  if (nextStep) {
    html += `<div class="mt-14"><button class="btn btn-ghost" onclick="document.querySelector('[data-tab=${nextStep.tab}]').click();">${nextStep.label}</button></div>`;
  }

  html += `<div class="hr" style="margin:22px 0 14px;"></div>
    <button class="btn-icon" id="caseMapToggle" style="width:auto;padding:7px 12px;border-radius:8px;font-size:12px;gap:6px;">View research map</button>
    <div id="caseMapWrap" style="display:none;margin-top:14px;">
      <div class="landscape-wrap">
        <div class="ls-head">
          <div class="ls-head-title">Research map</div>
          <div class="ls-head-sub">VERIFICATION: <b style="color:${scoreColor(vScore)}">${vScore!=null ? (vScore*100).toFixed(1) : '—'}</b> <span style="color:${scoreColor(vScore)}">[${strength.toUpperCase()}]</span></div>
        </div>
        <div id="landscapeMount"></div>
        <div id="nearbyCasesRow"></div>
      </div>
      <div class="ls-legend">
        <span><i style="background:var(--espresso)"></i>Strong evidence / verified</span>
        <span><i style="background:var(--mid)"></i>Moderate / partial</span>
        <span><i style="background:var(--wine)"></i>Weak / unverified</span>
      </div>
    </div>`;

  return html;
}

/* ── Evidence: "What supports or challenges my research?" ───────────── */
function evaluationPanel(c) {
  const e = c.evaluation;
  if (!e) return '';
  const rq = e.research_quality || {};
  const aq = e.agent_quality || {};
  const perf = e.performance || {};
  const pct = v => v == null ? '—' : fmtPct(v);
  const metric = (label, value) => `<div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">${label}</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${pct(value)}</div></div>`;
  return `<div class="field-label" style="margin:22px 0 8px;">Evaluation / research quality</div>
    <div class="card" style="padding:16px;">
      <div class="small muted" style="margin-bottom:10px;">Deterministic evaluation of this run. Individual metrics are more informative than the composite heuristic.</div>
      <div class="grid grid-3">
        ${metric('Research quality', rq.score)}
        ${metric('Retrieval', rq.retrieval?.score)}
        ${metric('Grounding', rq.grounding?.score)}
        ${metric('Citation correctness', rq.citation_correctness?.score)}
        ${metric('Verification', rq.verification?.score)}
        ${metric('Completeness', rq.completeness?.score)}
      </div>
      <div class="field-label" style="margin:16px 0 8px;">Agent quality</div>
      <div class="grid grid-3">
        ${metric('Plan completion', aq.plan_completion)}
        ${metric('Tool efficiency', aq.tool_efficiency)}
        ${metric('Re-plan effectiveness', aq.replan_effectiveness)}
        ${metric('Stopping behavior', aq.appropriate_stopping)}
        <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Tool calls</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${perf.tool_call_count ?? '—'}</div></div>
        <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Re-plans</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${perf.replan_count ?? c.replanCount ?? 0}</div></div>
      </div>
      <div class="field-label" style="margin:16px 0 8px;">Performance</div>
      <div class="grid grid-3">
        <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Latency</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${perf.total_latency_ms != null ? `${(perf.total_latency_ms/1000).toFixed(1)}s` : '—'}</div></div>
        <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Tokens</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${perf.total_tokens ?? 'Unknown'}</div></div>
        <div class="stat-tile" style="text-align:left;padding:12px 14px;"><div class="small muted">Estimated cost</div><div style="font-family:var(--mono);font-size:1.2rem;font-weight:700;">${perf.estimated_cost_usd != null ? `$${perf.estimated_cost_usd.toFixed(4)}` : 'Unknown'}</div></div>
      </div>
      ${e.errors?.length ? `<div class="small muted mt-8">Some evaluation components were unavailable: ${escapeHtml(e.errors.join('; '))}</div>` : ''}
    </div>`;
}

function researchTracePanel(c) {
  const trace = c.researchTrace || [];
  if (!trace.length) return '';
  const label = ev => `${ev.component || 'research'} · ${ev.action || 'event'}`;
  const status = ev => ev.status || 'unknown';
  return `<div class="field-label" style="margin:22px 0 8px;">Research trace / agent activity</div>
    <div class="card" style="padding:16px;">
      <div class="small muted" style="margin-bottom:10px;">Safe diagnostic trace. External content and sensitive tool inputs are omitted.</div>
      <div style="display:flex;flex-direction:column;gap:7px;max-height:280px;overflow:auto;">
        ${trace.map(ev => `<div style="display:flex;align-items:center;gap:9px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;">
          <span class="small muted" style="min-width:132px;">${escapeHtml(label(ev))}</span>
          <span class="small" style="flex:1;">${escapeHtml(status(ev))}</span>
          <span class="small muted">${Number(ev.duration_ms || 0)}ms</span>
        </div>`).join('')}
      </div>
    </div>`;
}

function evidencePanel(c) {
  const v = c.verification;
  if (!v) return emptyState('§', 'Verification not requested', 'This case was filed without a verification pass, so there is no evidence breakdown to show.');
  const score = v.overall_verification_score;
  const col = scoreColor(score);
  const circumference = 2 * Math.PI * 36;
  const offset = circumference - (score || 0) * circumference;
  const groups = groupClaims(v.claims);

  const section = (title, hint, color, claims) => !claims.length ? '' : `
    <div class="field-label" style="margin:18px 0 8px;color:${color};">${title} (${claims.length})</div>
    <div class="small muted mt-8" style="margin-bottom:8px;">${hint}</div>
    ${claims.map(claimRow).join('')}`;

  return `<div class="grid grid-2" style="align-items:start;">
    <div class="card" style="text-align:center;padding:26px;">
      <div class="ring-wrap" style="justify-content:center;">
        <div class="ring">
          <svg width="84" height="84" viewBox="0 0 84 84">
            <circle cx="42" cy="42" r="36" fill="none" stroke="var(--border)" stroke-width="5"/>
            <circle cx="42" cy="42" r="36" fill="none" stroke="${col}" stroke-width="5" stroke-linecap="round"
              stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" style="transition:stroke-dashoffset 1s cubic-bezier(.16,1,.3,1);"/>
          </svg>
          <div class="ring-num" style="color:${col};">${fmtPct(score)}</div>
        </div>
      </div>
      <div class="badge mt-14" style="color:${col};border-color:${col};background:transparent;">${verdictLabel(v.verdict)}</div>
      <div class="bar-track mt-14"><div class="bar-fill" style="width:${(score||0)*100}%;background:${col};"></div></div>
      <div class="small muted mt-8">${escapeHtml(confidenceLabel(score))}</div>
    </div>
    <div>
      ${(v.claims||[]).length ? '' : emptyState('—','No claims extracted','')}
      ${section('Supporting evidence', 'Claims that check out against the sources.', 'var(--espresso)', groups.supporting)}
      ${section('Uncertain', 'Claims with mixed or partial support — worth a second look.', 'var(--mid)', groups.uncertain)}
      ${section('Conflicting / unverified', 'Claims that sources disagree on, or that could not be verified.', 'var(--wine)', groups.conflicting)}
      <div id="evidenceInsightsBox" class="mt-18">${researchInsightsPanel(c)}</div>
    </div>
  </div>`;
}

function researchInsightsPanel(c) {
  const i = c.insights;
  if (!i) return `<div class="small muted">Loading evidence analysis…</div>`;
  const bucket = (title, color, data) => `<div class="card" style="margin-bottom:10px;border-left:3px solid ${color};">
    <div class="flex between"><div class="field-label" style="color:${color};">${title}</div><b style="font-family:var(--mono);color:${color};">${data?.count ?? 0}</b></div>
    ${(data?.sources || []).length ? `<div class="small muted mt-8">${data.sources.map(s => `<a href="${escapeAttr(s.url)}" target="_blank" rel="noopener" style="color:var(--ink2);display:block;margin:4px 0;">${escapeHtml(s.title || s.domain || 'Source')} ↗</a>`).join('')}</div>` : `<div class="small muted mt-8">No verified source links in this category.</div>`}
  </div>`;
  const c0 = i.conclusion || {};
  return `<div class="field-label" style="margin-bottom:8px;">Contradiction & disagreement</div>
    <div class="small muted" style="margin-bottom:10px;">Only evidence linked to the current case is shown.</div>
    ${bucket('Supporting evidence', 'var(--espresso)', i.supporting)}
    ${bucket('Partially supported / mixed', 'var(--mid)', i.partially_supported)}
    ${bucket('Contradicting evidence', 'var(--wine)', i.contradicted)}
    ${bucket('Mixed / unclear', 'var(--mid)', i.mixed)}
    <div class="field-label" style="margin:16px 0 8px;">Evidence-backed conclusion</div>
    <div class="grid grid-3">
      <div class="stat-tile"><div class="stat-value">${c0.supported_by_source_count ?? 0}</div><div class="stat-label">Supported by sources</div></div>
      <div class="stat-tile"><div class="stat-value">${c0.partially_supported_source_count ?? 0}</div><div class="stat-label">Partially supported</div></div>
      <div class="stat-tile"><div class="stat-value">${c0.contradicted_source_count ?? 0}</div><div class="stat-label">Contradicted</div></div>
    </div>
    <div class="field-label" style="margin:16px 0 8px;">WHAT AM I MISSING?</div>
    ${(i.gaps || []).length ? i.gaps.map(g => `<div class="card" style="margin-bottom:8px;"><div class="small" style="line-height:1.5;color:var(--ink2);">${escapeHtml(g.text)}</div><button class="btn btn-ghost mt-8 btn-research-gap" data-gap="${escapeAttr(g.text)}">Research this gap →</button></div>`).join('') : `<div class="small muted">No evidence-derived research gaps were identified from the current case.</div>`}`;
}

async function loadResearchInsights(reportId) {
  const box = $('#evidenceInsightsBox');
  if (!box || !reportId) return;
  if (Atlas.state.activeCase?.insights) {
    box.innerHTML = researchInsightsPanel(Atlas.state.activeCase);
    bindResearchGapButtons(reportId);
    return;
  }
  try {
    const result = await apiGet(`/questions/${reportId}/insights`);
    if (!result?.success) throw new Error('insights failed');
    if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
    Atlas.state.activeCase.insights = result.data || null;
    box.innerHTML = researchInsightsPanel(Atlas.state.activeCase);
    bindResearchGapButtons(reportId);
  } catch (e) {
    console.error('load research insights failed:', e);
    if (Atlas.state.activeCase?.reportId === reportId) box.innerHTML = `<div class="small muted">Evidence analysis could not be loaded for this case.</div>`;
  }
}
function bindResearchGapButtons(reportId) {
  $('#evidenceInsightsBox')?.querySelectorAll('.btn-research-gap').forEach(btn => {
    btn.addEventListener('click', () => researchThisGap(reportId, btn));
  });
}
async function researchThisGap(reportId, btn) {
  if (btn.disabled) return;
  const gap = btn.dataset.gap;
  btn.disabled = true; btn.textContent = 'Researching…';
  try {
    const result = await apiPost(`/questions/${reportId}/research`, { question_text: gap });
    if (!result?.success) throw new Error('research failed');
    Atlas.state.activeCase = normalizeFromRun(result.data);
    toast('New case opened from the research gap.', 'ok');
    Atlas.nav('case', { fromResearch: true });
  } catch (e) {
    console.error('research gap failed:', e);
    btn.disabled = false; btn.textContent = 'Research this gap →';
  }
}

/* ── Notes: "What do I want to remember or think about?" ─────────────
   Persistent, server-backed, one note per (user, case) — scoped and
   enforced server-side so a note is never visible to anyone but its
   author. Kept clearly separate from the Atlas-generated content in
   Findings: this box only ever holds what the student typed. */
function studentNotesPanel(c) {
  return `<div id="studentNotesBox">${skeletonBlock('160px')}</div>`;
}
async function loadStudentNotes(reportId) {
  const box = $('#studentNotesBox'); if (!box || !reportId) return;
  try {
    const result = await apiGet(`/notes/${reportId}`);
    if (!result || !result.success) throw new Error('load failed');
    // Guard against a stale response: if the user switched cases while
    // this request was in flight, Atlas.state.activeCase now points at a
    // different report, but #studentNotesBox still resolves by ID to
    // whatever box is currently in the DOM (the new case's) — applying
    // this result there would silently show one case's note under another.
    if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
    renderStudentNotesEditor(reportId, result.data?.content || '');
  } catch (e) {
    console.error('load note failed:', e);
    if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
    box.innerHTML = emptyState('!', 'Could not load your notes', 'Please try again in a moment.') +
      `<div style="text-align:center;"><button class="btn btn-ghost mt-8" data-report-id="${escapeAttr(reportId)}" onclick="loadStudentNotes(this.dataset.reportId)">Retry</button></div>`;
  }
}
function renderStudentNotesEditor(reportId, content) {
  // Same staleness guard as loadStudentNotes — this is also the shared
  // exit point for the save/delete success handlers below, so guarding
  // here covers all three call sites in one place.
  if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
  const box = $('#studentNotesBox'); if (!box) return;
  box.innerHTML = `
    <div class="small muted" style="margin-bottom:12px;">Your own notes on this case — private to you, separate from anything Atlas generates.</div>
    <textarea class="input" id="studentNotesArea" rows="10" maxlength="20000" placeholder="What do you want to remember or think about? Jot down your own take, questions, or a draft outline…" style="font-family:var(--sans);">${escapeHtml(content)}</textarea>
    <div class="flex between mt-8">
      <span class="small muted" id="studentNotesStatus">${content ? 'Saved' : 'Nothing saved yet'}</span>
      <div class="flex gap-8">
        ${content ? `<button class="btn btn-ghost" id="studentNotesDelete">Delete</button>` : ''}
        <button class="btn" id="studentNotesSave">Save notes</button>
      </div>
    </div>`;
  bindStudentNotesEditor(reportId);
}
function bindStudentNotesEditor(reportId) {
  const area = $('#studentNotesArea'), btn = $('#studentNotesSave'), status = $('#studentNotesStatus'), delBtn = $('#studentNotesDelete');
  if (!area || !btn) return;
  const save = async () => {
    if (btn.disabled) return; // guard against duplicate submissions (double-click, double Cmd+S)
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Saving…';
    try {
      const result = await apiPut(`/notes/${reportId}`, { content: area.value });
      if (!result || !result.success) throw new Error('save failed');
      status.textContent = 'Saved';
      renderStudentNotesEditor(reportId, result.data?.content ?? area.value);
    } catch (e) {
      console.error('save note failed:', e);
      status.textContent = 'Could not save. Please try again.';
      btn.disabled = false;
      btn.textContent = original;
    }
  };
  btn.addEventListener('click', save);
  area.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save(); } });
  if (delBtn) delBtn.addEventListener('click', async () => {
    if (delBtn.dataset.confirm !== '1') {
      delBtn.dataset.confirm = '1';
      delBtn.textContent = 'Click again to delete';
      clearTimeout(delBtn._resetTimer);
      delBtn._resetTimer = setTimeout(() => { delBtn.dataset.confirm = '0'; delBtn.textContent = 'Delete'; }, 4000);
      return;
    }
    delBtn.disabled = true;
    try {
      const result = await apiDelete(`/notes/${reportId}`);
      if (!result || !result.success) throw new Error('delete failed');
      toast('Notes deleted.', 'ok');
      renderStudentNotesEditor(reportId, '');
    } catch (e) {
      console.error('delete note failed:', e);
      toast('Could not delete your notes. Please try again.', 'err');
      delBtn.disabled = false;
    }
  });
}

/* ── Questions I Still Need to Answer: derived from the report's own
   verification claims and sources by the backend (GET /questions/{id});
   never invented on the frontend. Kept as its own tab, distinct from the
   evidence-conflict note already shown in Overview above. ────────────── */
const QUESTION_CATEGORY_META = {
  conflicting_evidence:  { label: 'Conflicting evidence',    variant: 'terracotta' },
  insufficient_evidence: { label: 'Insufficient evidence',   variant: 'terracotta' },
  low_confidence:        { label: 'Low confidence',          variant: 'amber' },
  thin_sources:          { label: 'Limited sources',         variant: 'amber' },
  unknown_recency:       { label: 'Unknown recency',         variant: 'brass' },
  stale_sources:         { label: 'Dated evidence',          variant: 'brass' },
  single_source_domain:  { label: 'Single-source reliance',  variant: 'amber' },
  weak_overall_verdict:  { label: 'Weak overall verdict',    variant: 'terracotta' },
};
function questionCategoryMeta(category) {
  return QUESTION_CATEGORY_META[category] || { label: category || 'Open question', variant: 'neutral' };
}
function questionsPanel(c) {
  return `<div id="questionsBox">${skeletonBlock('90px')}${skeletonBlock('90px')}</div>`;
}
async function loadQuestions(reportId) {
  const box = $('#questionsBox'); if (!box || !reportId) return;
  try {
    const result = await apiGet(`/questions/${reportId}`);
    if (!result || !result.success) throw new Error('load failed');
    // Same staleness guard as Notes: don't let a slow response for a case
    // the user has since navigated away from land in the box that's now
    // showing a different (newer) case.
    if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
    renderQuestionsList(reportId, result.data?.questions || []);
  } catch (e) {
    console.error('load questions failed:', e);
    if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
    box.innerHTML = emptyState('!', 'Could not load research questions', 'Please try again in a moment.') +
      `<div style="text-align:center;"><button class="btn btn-ghost mt-8" data-report-id="${escapeAttr(reportId)}" onclick="loadQuestions(this.dataset.reportId)">Retry</button></div>`;
  }
}
function renderQuestionsList(reportId, questions) {
  if (!Atlas.state.activeCase || Atlas.state.activeCase.reportId !== reportId) return;
  const box = $('#questionsBox'); if (!box) return;

  if (!questions.length) {
    box.innerHTML = emptyState('—', 'No unresolved research questions were identified for this case.', '');
    return;
  }

  box.innerHTML = `
    <div class="small muted" style="margin-bottom:14px;">Follow-up questions Atlas identified from this case's own verification results and sources — not generic prompts.</div>
    ${questions.map(q => {
      const meta = questionCategoryMeta(q.category);
      const sourceCount = (q.related_source_ids || []).length;
      return `<div class="card question-card" data-question-id="${escapeAttr(q.question_id)}" style="margin-bottom:10px;">
        <div class="flex between" style="align-items:flex-start;gap:10px;">
          <div style="flex:1;">
            <div style="font-family:var(--serif);font-size:14px;color:var(--ink);line-height:1.5;margin-bottom:8px;">${escapeHtml(q.question_text)}</div>
            ${badge(meta.label, meta.variant)}
            ${sourceCount ? `<span class="small muted" style="margin-left:8px;">Linked to ${sourceCount} source${sourceCount===1?'':'s'} in this case</span>` : ''}
            ${q.rationale ? `<div class="small muted mt-8">${escapeHtml(q.rationale)}</div>` : ''}
          </div>
        </div>
        <div class="mt-8">
          <button class="btn btn-ghost btn-research-question" data-question-text="${escapeAttr(q.question_text)}">Research this question →</button>
        </div>
      </div>`;
    }).join('')}
  `;
  bindQuestionsPanel(reportId);
}
function bindQuestionsPanel(reportId) {
  const box = $('#questionsBox'); if (!box) return;
  box.querySelectorAll('.btn-research-question').forEach(btn => {
    btn.addEventListener('click', () => researchThisQuestion(reportId, btn));
  });
}
async function researchThisQuestion(reportId, btn) {
  if (btn.disabled) return; // guard against duplicate submissions
  const questionText = btn.dataset.questionText;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Researching…';
  try {
    const result = await apiPost(`/questions/${reportId}/research`, { question_text: questionText });
    if (!result || !result.success) throw new Error('research failed');
    // Reuses the exact same normalization + navigation the main research
    // flow uses (runResearch(), above) — no separate rendering path for
    // a result that came from this action.
    const normalized = normalizeFromRun(result.data);
    Atlas.state.activeCase = normalized;
    toast('New case opened from your question.', 'ok');
    Atlas.nav('case', { fromResearch: true });
  } catch (e) {
    // apiCall() already surfaced a toast for the HTTP/network failure —
    // nothing here pretends the research succeeded; just let the user retry.
    console.error('research this question failed:', e);
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ── Findings: "What conclusions can I currently draw?" ───────────────
   This is Atlas-generated — an executive brief, key statistics, and the
   full report — not the student's own notes (those live in the Notes
   tab above). */
function findingsPanel(c) {
  const brief = c.executiveBrief;
  let html = '';
  if (brief) {
    html += `<div class="small muted mb-8" style="margin-bottom:10px;">Atlas-generated summary, built from the evidence in this case.</div>
      <div class="field-label" style="margin-bottom:8px;">Summary</div>
      <div class="card" style="border-left:3px solid var(--wine);"><p style="font-family:var(--serif);color:var(--ink2);line-height:1.7;font-size:13px;margin:0;">${escapeHtml(brief.executive_summary||'')}</p></div>
      <div class="grid grid-2 mt-14">
        <div>
          ${brief.key_insights?.length ? `<div class="field-label" style="margin-bottom:8px;">Key findings</div>` + brief.key_insights.map(i => `<div class="small" style="display:flex;gap:6px;margin:5px 0;color:var(--ink2);"><span style="color:var(--wine);">◈</span>${escapeHtml(i)}</div>`).join('') : ''}
          ${brief.risks?.length ? `<div class="field-label mt-14" style="margin-bottom:8px;">UNCERTAINTY / RISKS</div>` + brief.risks.map(i => `<div class="small" style="display:flex;gap:6px;margin:5px 0;color:var(--ink2);"><span style="color:var(--wine);">⚠</span>${escapeHtml(i)}</div>`).join('') : ''}
        </div>
        <div>
          ${brief.opportunities?.length ? `<div class="field-label" style="margin-bottom:8px;">Opportunities</div>` + brief.opportunities.map(i => `<div class="small" style="display:flex;gap:6px;margin:5px 0;color:var(--ink2);"><span style="color:var(--espresso);">✦</span>${escapeHtml(i)}</div>`).join('') : ''}
          ${brief.recommended_actions?.length ? `<div class="field-label mt-14" style="margin-bottom:8px;">OPEN QUESTIONS / NEXT STEPS</div>` + brief.recommended_actions.map(i => `<div class="small" style="display:flex;gap:6px;margin:5px 0;color:var(--ink2);"><span style="color:var(--mid);">→</span>${escapeHtml(i)}</div>`).join('') : ''}
        </div>
      </div><div class="hr" style="margin:18px 0;"></div>`;
  }
  if (c.keyStatistics?.length) {
    html += `<div class="field-label" style="margin-bottom:8px;">Key statistics</div><div class="card">` +
      c.keyStatistics.map(s => `<div class="small" style="display:flex;gap:8px;margin:5px 0;color:var(--ink2);"><span style="color:var(--wine);">▸</span>${escapeHtml(s)}</div>`).join('') + `</div>`;
  }
  if (c.swot) {
    html += `<div class="field-label" style="margin:16px 0 8px;">Swot analysis</div><div class="grid grid-2">`;
    const groups = [['strengths','Strengths','var(--espresso)'],['weaknesses','Weaknesses','var(--wine)'],['opportunities','Opportunities','var(--wine)'],['threats','Threats','var(--mid)']];
    html += groups.map(([k,label,col]) => (c.swot[k]||[]).length ? `<div class="card" style="border-top:2px solid ${col};margin-bottom:0;">
        <b class="small" style="color:var(--ink);">${label}</b>
        ${(c.swot[k]).map(x => `<div class="small muted mt-8">- ${escapeHtml(x)}</div>`).join('')}
      </div>` : '').join('');
    html += `</div>`;
  }
  if (c.reportMarkdown) {
    html += `<div class="hr" style="margin:18px 0;"></div><div class="field-label" style="margin-bottom:10px;">Full report</div><div class="report-body">${styledReportHtml(c.reportMarkdown)}</div>`;
  }
  return html || emptyState('▦', 'No findings yet', 'This case has no executive brief or report on file.');
}
function mdToHtmlSafe(md) { return mdToHtml(md); }

function renderCaseHealth(ctx, c) {
  const vScore = c.verification?.overall_verification_score ?? null;
  const col = scoreColor(vScore);
  const label = vScore === null ? 'Unscored' : vScore >= 0.75 ? 'Strong Bearing' : vScore >= 0.5 ? 'Holding Course' : 'Needs Attention';
  const sub = vScore === null ? 'Verification was not requested for this case.' :
    vScore >= 0.75 ? 'This case is well supported by high quality evidence.' :
    vScore >= 0.5 ? 'This case has mixed support — review flagged claims.' :
    'This case has weak evidentiary support. Treat conclusions with care.';
  const circumference = 2 * Math.PI * 30;
  const offset = circumference - (vScore || 0) * circumference;

  const topSources = [...c.sources].sort((a,b) => (b.final_score||0)-(a.final_score||0)).slice(0,5);

  ctx.innerHTML = `
    <div class="s-section">
      <div class="s-title">Status</div>
      <div class="ring-wrap">
        <div class="ring" style="width:68px;height:68px;">
          <svg width="68" height="68" viewBox="0 0 68 68">
            <circle cx="34" cy="34" r="30" fill="none" stroke="var(--border)" stroke-width="4"/>
            <circle cx="34" cy="34" r="30" fill="none" stroke="${col}" stroke-width="4" stroke-linecap="round"
              stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" style="transition:stroke-dashoffset 1s cubic-bezier(.16,1,.3,1);"/>
          </svg>
          <div class="ring-num" style="font-size:13px;color:${col};">${fmtPct(vScore)}</div>
        </div>
        <div class="ring-info">
          <div class="ri-l">${label}</div>
          <div class="ri-s">${sub}</div>
        </div>
      </div>
    </div>
    <div class="s-section">
      <div class="s-title">Top sources</div>
      ${topSources.length ? topSources.map(s => `
        <a href="${escapeAttr(s.url||'#')}" target="_blank" rel="noopener noreferrer" class="flex between landmark-link" style="padding:6px 0;border-bottom:1px solid var(--border);text-decoration:none;">
          <div style="min-width:0;">
            <div class="small" style="color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px;">${escapeHtml(s.title||s.domain||'Source')}</div>
            <div class="small muted" style="font-family:var(--mono);font-size:10px;">${escapeHtml(s.domain||'')} · ${s.publish_date ? escapeHtml(formatPublicationDate(s.publish_date)) : 'Published date unknown'} ↗</div>
          </div>
          <span class="badge" style="color:${scoreColor(s.final_score)};background:transparent;border-color:${scoreColor(s.final_score)};flex-shrink:0;">${s.final_score!=null?s.final_score.toFixed(2):'—'}</span>
        </a>`).join('') : `<div class="small muted">No sources yet.</div>`}
    </div>
    <div class="s-section">
      <div class="s-title">Activity</div>
      <div class="timeline" id="journeyTimeline">
        ${journeyTimelineItems(c).map(([t,l]) => `<div class="t-item"><div class="t-time">${t}</div><div class="t-label">${l}</div></div>`).join('')}
      </div>
    </div>
    <div class="s-section" style="border-bottom:none;">
      <div class="s-title">Details</div>
      <div class="small muted" style="font-family:var(--mono);line-height:2;">
        ID: ${c.reportId ?? '—'}<br>
        Filed: ${c.createdAt ? fmtDate(c.createdAt) + ' · ' + fmtTime(c.createdAt) : '—'}<br>
        Est. cost: ${c.estimatedCost != null ? '$' + Number(c.estimatedCost).toFixed(4) : '—'}
      </div>
    </div>
  `;
}
function journeyTimelineItems(c) {
  const now = new Date();
  const mk = (offsetMin) => { const d = new Date(now.getTime() - offsetMin*60000); return fmtTime(d); };
  return [
    [mk(0), 'Saved to library'],
    [mk(1), 'Report assembled'],
    [mk(2), 'Confidence analyzed'],
    [mk(3), 'Cross verification complete'],
    [mk(5), 'Claims extracted'],
    [mk(7), 'Sources ranked'],
    [mk(9), 'Sources retrieved'],
  ];
}

function downloadCaseMd() {
  const c = Atlas.state.activeCase;
  if (!c || !c.reportMarkdown) { toast('No case file to export.', 'err'); return; }
  const blob = new Blob([c.reportMarkdown], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `atlas_case_${(c.reportId||'file')}.md`;
  a.click();
}
function copyCaseLink() {
  const c = Atlas.state.activeCase;
  navigator.clipboard?.writeText(`${c?.topic || ''} — Atlas case ${c?.reportId || ''}`).then(
    () => toast('Case reference copied to clipboard.', 'ok'),
    () => toast('Could not copy to clipboard.', 'err')
  );
}

/* ══════════════════════════════════════════════════════════════════════
   THE KNOWLEDGE LANDSCAPE — signature interactive graph
   Root (topic) at center · evidence arcs above · claims arc below ·
   nearby cases fade in at the perimeter once semantic search resolves.
   ══════════════════════════════════════════════════════════════════════ */
function renderLandscape(mount, c) {
  const W = 760, cx = W/2;
  const sources = c.sources.slice(0, 6);
  const claims = (c.verification?.claims || []).slice(0, 6);

  // Plain flowchart: root box at top, sources as a row of boxes below it,
  // claims as a second row below that — each connected to the root by a
  // simple right-angle line. Rectangular boxes with the label written
  // directly inside them (not floating text + a separate shape) is what
  // reads as an actual flowchart rather than a stylized diagram. Box width
  // is computed per row from the node count so boxes never crowd or touch,
  // however many sources/claims a case has (1 up to the 6-item cap).
  const rootW = 260, rootH = 38, boxH = 34, margin = 20, gap = 10;
  const srcY = 70, claimY = 220, busGap = 34;
  const H = claimY + boxH + 40;
  const cy = (srcY + boxH/2 + claimY + boxH/2) / 2 + 6;

  const rowLayout = (n) => {
    if (n === 0) return { boxW: 0, xs: [] };
    const available = W - 2*margin;
    const boxW = Math.min(118, (available - gap*(n-1)) / n);
    if (n === 1) return { boxW, xs: [cx] };
    const span = n*boxW + (n-1)*gap;
    const startX = (W - span)/2 + boxW/2;
    return { boxW, xs: Array.from({length:n}, (_,i) => startX + i*(boxW+gap)) };
  };
  const srcRow = rowLayout(sources.length), claimRow = rowLayout(claims.length);
  const srcXs = srcRow.xs, claimXs = claimRow.xs;
  const srcBoxW = srcRow.boxW, claimBoxW = claimRow.boxW;
  const srcBusY = srcY + boxH + busGap/2;
  const claimBusY = claimY - busGap/2;

  const traceDown = (x, fromY, busY, toY) => `M ${x} ${fromY} L ${x} ${busY} L ${cx} ${busY} L ${cx} ${toY}`;
  const traceUp   = (x, fromY, busY, toY) => `M ${cx} ${fromY} L ${cx} ${busY} L ${x} ${busY} L ${x} ${toY}`;

  let svg = `<svg class="landscape-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;

  if (sources.length) svg += `<text x="16" y="${srcY - boxH/2 - 10}" class="ls-label" font-weight="700" fill="var(--ink3)" style="letter-spacing:.08em;">SOURCES</text>`;
  if (claims.length) svg += `<text x="16" y="${claimY - boxH/2 - 10}" class="ls-label" font-weight="700" fill="var(--ink3)" style="letter-spacing:.08em;">CLAIMS</text>`;

  // connector lines first (under boxes)
  sources.forEach((s, i) => {
    const x = srcXs[i], col = scoreHex(s.final_score);
    svg += `<path class="ls-edge" d="${traceDown(x, srcY+boxH/2, srcBusY, cy-rootH/2)}" stroke="${col}" stroke-width="1.5" fill="none"/>`;
  });
  claims.forEach((cl, i) => {
    const x = claimXs[i], col = statusHex(cl.status);
    svg += `<path class="ls-edge" d="${traceUp(x, cy+rootH/2, claimBusY, claimY-boxH/2)}" stroke="${col}" stroke-width="1.5" fill="none"/>`;
  });

  // root box
  svg += `<g class="ls-node">
    <rect class="ls-node-box-fill" x="${cx-rootW/2}" y="${cy-rootH/2}" width="${rootW}" height="${rootH}" rx="1" fill="#2E292B"/>
    <rect x="${cx-rootW/2}" y="${cy-rootH/2}" width="${rootW}" height="${rootH}" rx="1" fill="none" stroke="var(--wine)" stroke-width="1.5"/>
    <text x="${cx}" y="${cy+4}" text-anchor="middle" font-family="var(--sans)" font-weight="600" font-size="11.5" fill="#FBF7F0">${escapeXml(truncate(c.topic, 36))}</text>
    <title>${escapeAttr(c.topic)}</title>
  </g>`;

  // source boxes — wrapped in a real <a> so clicking opens the actual source
  sources.forEach((s, i) => {
    const x = srcXs[i], col = scoreHex(s.final_score), bw = srcBoxW;
    const href = s.url ? escapeAttr(s.url) : null;
    const openTag = href ? `<a href="${href}" target="_blank" rel="noopener noreferrer" class="ls-node-link">` : '<g>';
    const closeTag = href ? '</a>' : '</g>';
    svg += `${openTag}<g class="ls-node">
      <rect class="ls-node-box ls-node-box-fill" x="${x-bw/2}" y="${srcY-boxH/2}" width="${bw}" height="${boxH}" rx="1" fill="#FBF7F0"/>
      <rect class="ls-node-box-border" x="${x-bw/2}" y="${srcY-boxH/2}" width="${bw}" height="${boxH}" rx="1" fill="none" stroke="${col}" stroke-width="1.5"/>
      <circle cx="${x-bw/2+9}" cy="${srcY-boxH/2+9}" r="2.5" fill="${col}"/>
      <text x="${x}" y="${srcY+3.5}" text-anchor="middle" font-family="var(--sans)" font-size="9.5" fill="var(--ink)">${escapeXml(truncate(s.domain || s.title || 'source', Math.max(6, Math.floor(bw/7))))}</text>
      <title>${escapeAttr((s.title||s.domain||'Source') + ' — score ' + (s.final_score!=null?s.final_score.toFixed(2):'—') + (href ? ' — click to open source' : ''))}</title>
    </g>${closeTag}`;
  });

  // claim boxes
  claims.forEach((cl, i) => {
    const x = claimXs[i], col = statusHex(cl.status), bw = claimBoxW;
    svg += `<g class="ls-node">
      <rect class="ls-node-box ls-node-box-fill" x="${x-bw/2}" y="${claimY-boxH/2}" width="${bw}" height="${boxH}" rx="1" fill="#FBF7F0"/>
      <rect class="ls-node-box-border" x="${x-bw/2}" y="${claimY-boxH/2}" width="${bw}" height="${boxH}" rx="1" fill="none" stroke="${col}" stroke-width="1.5"/>
      <circle cx="${x-bw/2+9}" cy="${claimY-boxH/2+9}" r="2.5" fill="${col}"/>
      <text x="${x}" y="${claimY+3.5}" text-anchor="middle" font-family="var(--sans)" font-size="9.5" fill="var(--ink)">${escapeXml(truncate(cl.claim_text || 'claim', Math.max(6, Math.floor(bw/7))))}</text>
      <title>${escapeAttr((cl.claim_text||'Claim') + ' — ' + (cl.status||''))}</title>
    </g>`;
  });

  svg += `</svg>`;
  mount.innerHTML = svg;

  if (!sources.length && !claims.length) {
    mount.innerHTML = svg + '';
    const emptyOverlay = el(`<div style="padding:40px 20px;position:relative;z-index:1;">${emptyState('◎','Landscape not yet charted','Sources and claims will appear here once evidence is gathered.')}</div>`);
    mount.appendChild(emptyOverlay);
  }

  // Nearby cases render as a clean chip row below the diagram, not more
  // lines inside it — keeps the flow diagram legible while still surfacing
  // related cases as an actually-clickable feature.
  fetchNearbyCases(c);
}
function statusHex(status) {
  const s = (status||'').toLowerCase();
  if (s.includes('verified') && !s.includes('partial')) return '#2E292B';
  if (s.includes('partial')) return '#6E6266';
  return '#7A6270';
}
function truncate(s, n) { s = s || ''; return s.length > n ? s.slice(0, n-1) + '…' : s; }
function escapeXml(s) { return escapeHtml(s); }
function escapeAttr(s) { return escapeHtml(s); }

async function fetchNearbyCases(c) {
  const mount = document.getElementById('nearbyCasesRow');
  if (!mount || !c.topic) return;
  try {
    const res = await apiPostSilent('/search/semantic', { query: c.topic, limit: 4 });
    const results = (res?.data?.results || []).filter(r => r.report_id !== c.reportId).slice(0, 3);
    if (!results.length) { mount.innerHTML = ''; return; }
    mount.innerHTML = `<div class="field-label" style="margin:14px 18px 8px;">Related cases</div>
      <div class="flex gap-8" style="flex-wrap:wrap;padding:0 18px 16px;">
        ${results.map(r => `<span class="chip" onclick="openHistoryCase('${r.report_id}')" title="Open this case">
            ↳ ${escapeHtml(truncate(r.topic, 44))} <span class="muted">· ${r.similarity_score!=null ? Math.round(r.similarity_score*100) : '—'}%</span>
          </span>`).join('')}
      </div>`;
  } catch { /* silent — perimeter enhancement only */ }
}
// silent variant of apiPost that doesn't toast on failure (used for the nearby-cases perimeter touch)
async function apiPostSilent(path, body) {
  try {
    const headers = {'Content-Type':'application/json'};
    if (Atlas.auth.token) headers['Authorization'] = 'Bearer ' + Atlas.auth.token;
    const res = await fetch(Atlas.config.apiBase + path, { method: 'POST', headers, body: JSON.stringify(body) });
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: HISTORY
   ══════════════════════════════════════════════════════════════════════ */
function viewHistory(main, ctx) {
  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('history')}</div>
      <div class="page-title">Cases</div>
      <div class="page-desc">Browse, search, and revisit your saved research.</div>
      <div class="hr"></div>
      <div class="list-tools">
        <input class="input" id="hFilter" placeholder="Filter by topic..." style="font-family:var(--sans);" />
        <select class="input" id="hLimit"><option value="10">10 results</option><option value="20" selected>20 results</option><option value="50">50 results</option></select>
        <select class="input" id="hSort"><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="verification">Highest verification</option></select>
      </div>
      <div style="margin-bottom:14px;"><span class="small muted" id="hSearchByMeaning" style="cursor:pointer;text-decoration:underline;text-underline-offset:2px;">Not finding it? Search by meaning instead →</span></div>
      <div id="hList">${skeletonBlock('90px')}${skeletonBlock('90px')}${skeletonBlock('90px')}</div>
    </div>
  `;
  ctx.innerHTML = `<div class="s-section"><div class="s-title">Tip</div><div class="small muted">Click any case to open its Overview, Sources, and Evidence.</div></div>`;
  $('#hSearchByMeaning').addEventListener('click', () => Atlas.nav('search', { prefillQuery: $('#hFilter').value.trim() || undefined }));

  const load = async () => {
    const params = { limit: $('#hLimit').value, offset: 0 };
    const topic = $('#hFilter').value.trim();
    if (topic) params.topic = topic;
    $('#hList').innerHTML = skeletonBlock('90px') + skeletonBlock('90px');
    try {
      const data = await apiGet('/history/reports', params);
      const reports = data?.data?.reports || [], total = data?.data?.total ?? reports.length;
      if (!reports.length) { $('#hList').innerHTML = emptyState('§', 'No research yet', 'Run your first case from the workspace.'); return; }
      const sort = $('#hSort').value;
      if (sort === 'oldest') reports.sort((a,b) => new Date(a.created_at) - new Date(b.created_at));
      else if (sort === 'verification') reports.sort((a,b) => (b.verification_score||0) - (a.verification_score||0));
      // 'newest' is already the backend's default order
      $('#hList').innerHTML = `<div class="small muted" style="font-family:var(--mono);margin-bottom:10px;">SHOWING <b style="color:var(--wine)">${reports.length}</b> OF ${total}</div>` +
        reports.map(r => recentCaseCard(r, { deletable: true })).join('');
    } catch (e) { console.error('load history failed:', e); $('#hList').innerHTML = emptyState('!', 'Could not load your cases', 'Please try again in a moment.'); }
  };
  $('#hFilter').addEventListener('keydown', e => { if (e.key === 'Enter') load(); });
  $('#hLimit').addEventListener('change', load);
  $('#hSort').addEventListener('change', load);
  load();
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: SEARCH (Discover)
   ══════════════════════════════════════════════════════════════════════ */
function viewSearch(main, ctx, opts) {
  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('search')}</div>
      <div class="page-title">Search your cases</div>
      <div class="page-desc">Find past research by meaning, not just keywords.</div>
      <div class="hr"></div>
      <div class="search-box">
        <input class="input" id="sQuery" placeholder="e.g. transformer architecture trade-offs for edge deployment" maxlength="500" style="font-family:var(--sans);" />
        <div class="chip-row">
          ${EXAMPLE_TOPICS.map(t => `<span class="chip" onclick="document.getElementById('sQuery').value=${escapeHtml(JSON.stringify(t))};runSemanticSearch();">↳ ${escapeHtml(t.slice(0,40))}${t.length>40?'…':''}</span>`).join('')}
        </div>
        <div class="flex gap-10 mt-14">
          <button class="btn" id="sBtn" onclick="runSemanticSearch()">🔍 Search</button>
          <select class="input" id="sLimit" style="max-width:140px;"><option value="5" selected>5 results</option><option value="10">10</option><option value="20">20</option></select>
        </div>
      </div>
      <div id="sResults" class="mt-18"></div>
    </div>
  `;
  ctx.innerHTML = `<div class="s-section"><div class="s-title">How it works</div><div class="small muted" style="line-height:1.6;">Atlas indexes every case by concept, not just keyword — so "coping with chronic stress" can surface a case titled "burnout recovery strategies" even with zero shared words.</div></div>`;
  if (opts.prefillQuery) { $('#sQuery').value = opts.prefillQuery; runSemanticSearch(); }
  else { showBrowseCases(); }
  $('#sQuery').addEventListener('keydown', e => { if (e.key === 'Enter') runSemanticSearch(); });
  $('#sQuery').addEventListener('input', () => { if (!$('#sQuery').value.trim()) showBrowseCases(); });
}
async function showBrowseCases() {
  const box = $('#sResults');
  box.innerHTML = skeletonBlock('80px') + skeletonBlock('80px');
  try {
    const result = await apiGet('/history/reports', { limit: 6, offset: 0 });
    const reports = result?.data?.reports || [];
    if (!reports.length) { box.innerHTML = emptyState('🔍','Nothing filed yet','Open your first case to start building searchable memory.'); return; }
    box.innerHTML = `<div class="small muted" style="font-family:var(--mono);margin-bottom:10px;">BROWSE YOUR CASES — or search above to find by meaning</div>` +
      reports.map(r => `<div class="card card-hover" onclick="openHistoryCase('${r.report_id}')">
          <div class="flex between">
            <b style="font-family:var(--serif);color:var(--ink);">${escapeHtml(r.topic)}</b>
            <span class="small muted" style="font-family:var(--mono);">${r.verification_score!=null ? fmtPct(r.verification_score) : '—'}</span>
          </div>
          <p class="small muted mt-8">${escapeHtml((r.summary||'').slice(0,160))}${(r.summary||'').length>160?'…':''}</p>
        </div>`).join('');
  } catch { box.innerHTML = emptyState('🔍','Search your research','Type above, or open a case from Cases.'); }
}
async function runSemanticSearch() {
  const query = $('#sQuery').value.trim();
  if (!query) { toast('Enter a search query.', 'err'); return; }
  if (query.length < 2) { toast('Query must be at least 2 characters.', 'err'); return; }
  const btn = $('#sBtn'); btn.disabled = true;
  const box = $('#sResults'); box.innerHTML = skeletonBlock('80px') + skeletonBlock('80px');
  try {
    const result = await apiPost('/search/semantic', { query, limit: parseInt($('#sLimit').value,10) });
    btn.disabled = false;
    if (!result || !result.success) { console.error('semantic search failed:', result); box.innerHTML = emptyState('!','Search failed','Please try again in a moment.'); return; }
    const results = result.data.results || [];
    if (!results.length) { box.innerHTML = emptyState('🔍','No matching research found','Try a different query or run more research first.'); return; }
    box.innerHTML = `<div class="small muted" style="font-family:var(--mono);margin-bottom:10px;">FOUND <b style="color:var(--wine)">${results.length}</b> RELATED CASES</div>` +
      results.map((r,i) => {
        const c = scoreColor(r.similarity_score);
        return `<div class="card card-hover" style="border-left:3px solid ${c};" onclick="openHistoryCase('${r.report_id}')">
          <div class="flex between">
            <div class="flex gap-10"><span class="badge badge-neutral">#${i+1}</span><b style="font-family:var(--serif);color:var(--ink);">${escapeHtml(r.topic)}</b></div>
            <span style="font-family:var(--mono);color:${c};font-weight:700;">${r.similarity_score!=null?r.similarity_score.toFixed(2):'—'}</span>
          </div>
          <div class="bar-track"><div class="bar-fill" style="width:${(r.similarity_score||0)*100}%;background:${c};"></div></div>
          <p class="small muted mt-8">${escapeHtml((r.summary||'').slice(0,200))}${(r.summary||'').length>200?'…':''}</p>
        </div>`;
      }).join('');
  } catch (e) { console.error('semantic search failed:', e); btn.disabled = false; box.innerHTML = emptyState('!','Search failed','Please try again in a moment.'); }
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: ANALYTICS (Insights)
   ══════════════════════════════════════════════════════════════════════ */
function viewAnalytics(main, ctx) {
  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('analytics')}</div>
      <div class="page-title">Insights</div>
      <div class="page-desc">Research trends, verification scores, usage, and cost at a glance.</div>
      <div class="hr"></div>
      <div id="aBody">${skeletonBlock('90px')}${skeletonBlock('220px')}</div>
    </div>
  `;
  ctx.innerHTML = `<div class="s-section"><div class="s-title">What this page shows</div>
    <div class="small muted" style="line-height:1.6;">Aggregated stats across every case you've filed — how much you're researching, how well-evidenced it tends to be, and where the time and cost goes. Each chart below explains what it's measuring under its title.</div>
  </div>`;
  loadAnalytics();
}
async function loadAnalytics() {
  const body = $('#aBody');
  try {
    const result = await apiGet('/analytics/overview');
    if (!result || !result.success) { console.error('load analytics failed:', result); body.innerHTML = emptyState('!','Could not load insights','Please try again in a moment.'); return; }
    const d = result.data;
    const chartCard = (title, subtitle, inner) => `
      <div class="card">
        <div class="field-label" style="margin-bottom:2px;">${title}</div>
        <div class="small muted" style="margin-bottom:12px;">${subtitle}</div>
        ${inner}
      </div>`;
    const vScore = d.average_verification_score;
    const vLabel = vScore >= 0.75 ? 'strong' : vScore >= 0.5 ? 'moderate' : 'weak';
    const topDomain = (d.source_domain_distribution||[])[0];
    const topTopic = (d.top_topics||[])[0];
    const trend = (d.verification_score_trend||[]);
    const trendDelta = trend.length >= 2 ? trend[trend.length-1].avg_score - trend[0].avg_score : null;
    const narrative = d.total_reports_generated ? `
      <div class="card" style="border-left:3px solid var(--wine);">
        <div class="field-label" style="margin-bottom:8px;">Summary</div>
        <p style="font-family:var(--serif);color:var(--ink2);line-height:1.7;font-size:13.5px;margin:0;">
          You've filed <b style="color:var(--ink);">${fmtNum(d.total_reports_generated)}</b> case${d.total_reports_generated===1?'':'s'} drawing on <b style="color:var(--ink);">${fmtNum(d.total_sources_scraped)}</b> sources, averaging <b style="color:var(--ink);">${(d.average_execution_time_ms/1000).toFixed(0)}s</b> to complete.
          Evidence quality is <b style="color:${scoreHex(vScore)}">${vLabel}</b> overall (${fmtPct(vScore)} avg. verification)${trendDelta!=null ? (trendDelta > 0.01 ? `, trending up (+${Math.round(trendDelta*100)}pts since your first case)` : trendDelta < -0.01 ? `, trending down (${Math.round(trendDelta*100)}pts since your first case)` : ', holding steady') : ''}.
          ${topDomain ? ` Most of your evidence comes from <b style="color:var(--ink);">${escapeHtml(topDomain.domain)}</b>${topTopic ? `, and your most-revisited topic is "<b style="color:var(--ink);">${escapeHtml(truncate(topTopic.topic,50))}</b>."` : '.'}` : ''}
        </p>
      </div>` : '';
    body.innerHTML = `
      ${narrative}
      <div class="grid grid-6 mt-14">
        <div class="stat-tile" title="Total number of cases you've opened"><div class="stat-value">${fmtNum(d.total_reports_generated)}</div><div class="stat-label">📄 Reports</div></div>
        <div class="stat-tile" title="Total sources scraped and scored across all cases"><div class="stat-value">${fmtNum(d.total_sources_scraped)}</div><div class="stat-label">🔗 Sources</div></div>
        <div class="stat-tile" title="Average verification score across all cases (higher = more strongly evidenced)"><div class="stat-value" style="color:${scoreHex(vScore)}">${fmtPct(vScore)}</div><div class="stat-label">✓ ${vLabel.toUpperCase()}</div></div>
        <div class="stat-tile" title="Average total time to complete a case, start to finish"><div class="stat-value">${(d.average_execution_time_ms/1000).toFixed(1)}s</div><div class="stat-label">⏱ Avg Time</div></div>
        <div class="stat-tile" title="Total LLM tokens consumed across all cases"><div class="stat-value">${fmtNum(d.total_tokens_used)}</div><div class="stat-label">◈ Tokens</div></div>
        <div class="stat-tile" title="Estimated total cost across all cases"><div class="stat-value">$${(d.total_estimated_cost_usd||0).toFixed(4)}</div><div class="stat-label">$ Est. Cost</div></div>
      </div>
      <div class="hr"></div>
      <div class="grid grid-2">
        ${chartCard('CASES FILED OVER TIME', 'Number of cases opened, by date.',
          (d.reports_generated_over_time||[]).length ? lineChartSvg(d.reports_generated_over_time) : emptyState('📈','No time data yet','Run some research first.'))}
        ${chartCard('VERIFICATION SCORE TREND', 'Average verification score (0–100%) of cases filed on each date — higher is more strongly evidenced.',
          (d.verification_score_trend||[]).length ? barChartSvg(d.verification_score_trend.map(v=>({label:v.date, value:v.avg_score, color:scoreHex(v.avg_score)})), true) : emptyState('✅','No verification trend yet',''))}
      </div>
      <div class="grid grid-2 mt-14">
        ${chartCard('MOST RESEARCHED TOPICS', 'Click a topic to search your memory for it in Discover.',
          (d.top_topics||[]).length ? hBarList(d.top_topics.map(t=>({label:t.topic, value:t.count})), false, true) : emptyState('📊','No topic data yet',''))}
        ${chartCard('SOURCE DOMAIN DISTRIBUTION', 'Which domains your evidence is coming from most often, across all cases.',
          (d.source_domain_distribution||[]).length ? hBarList(d.source_domain_distribution.map(t=>({label:t.domain, value:t.count}))) : emptyState('🌐','No domain data yet',''))}
      </div>
      ${chartCard('⚡ AVERAGE LATENCY BREAKDOWN', 'Where the time goes in an average case: fetching sources vs. everything after (ranking, verification, writing).',
        latencyBars(d.average_retrieval_latency_ms||0, d.average_execution_time_ms||1))}
    `;
    $$('.hbar-clickable').forEach(row => {
      row.addEventListener('click', () => Atlas.nav('search', { prefillQuery: row.dataset.topic }));
    });
  } catch { body.innerHTML = emptyState('!','Failed to load analytics',''); }
}
function lineChartSvg(points) {
  const W=640,H=200,pad=28,padB=36;
  const vals = points.map(p=>p.count);
  const max = Math.max(...vals, 1);
  const stepX = (W-2*pad)/Math.max(points.length-1,1);
  const plotH = H-pad-padB;
  const coords = points.map((p,i)=>({x: pad+i*stepX, y: pad+plotH-(p.count/max)*plotH}));
  const path = coords.map((c,i)=>`${i===0?'M':'L'} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(' ');
  const area = path + ` L ${coords[coords.length-1].x} ${pad+plotH} L ${coords[0].x} ${pad+plotH} Z`;
  // show every label when few points, thin out when many so text never collides
  const labelEvery = points.length > 8 ? Math.ceil(points.length/8) : 1;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:200px;">
    <line x1="${pad}" y1="${pad+plotH}" x2="${W-pad}" y2="${pad+plotH}" stroke="var(--border2)" stroke-width="1"/>
    <path d="${area}" fill="#7A62701F"/>
    <path d="${path}" fill="none" stroke="#7A6270" stroke-width="2"/>
    ${coords.map((c,i)=>`<circle cx="${c.x}" cy="${c.y}" r="3" fill="#7A6270"><title>${escapeAttr(points[i].date+': '+points[i].count)}</title></circle>
      <text x="${c.x}" y="${c.y-9}" text-anchor="middle" font-family="var(--mono)" font-size="9.5" fill="var(--ink2)">${escapeXml(String(points[i].count))}</text>
      ${i % labelEvery === 0 ? `<text x="${c.x}" y="${H-10}" text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--ink3)">${escapeXml(truncate(points[i].date||'', 8))}</text>` : ''}`).join('')}
  </svg>`;
}
function barChartSvg(items, isPct) {
  const W=640,H=200,pad=24,padB=32,bw=(W-2*pad)/items.length*0.55;
  const max = isPct ? 1 : Math.max(...items.map(i=>i.value),1);
  const plotH = H-pad-padB;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:200px;">
    <line x1="${pad}" y1="${pad+plotH}" x2="${W-pad}" y2="${pad+plotH}" stroke="var(--border2)" stroke-width="1"/>
    <line x1="${pad}" y1="${pad+plotH*0.25}" x2="${W-pad}" y2="${pad+plotH*0.25}" stroke="var(--border)" stroke-dasharray="3 4"/>
    ${items.map((it,i)=>{ const slot=(W-2*pad)/items.length; const x=pad+i*slot+(slot-bw)/2; const h=(it.value/max)*plotH; const y=pad+plotH-h;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,2).toFixed(1)}" rx="0" fill="${it.color||'#7A6270'}" fill-opacity="0.82"><title>${escapeAttr((it.label||'')+': '+(isPct?fmtPct(it.value):it.value))}</title></rect>
        <text x="${(x+bw/2).toFixed(1)}" y="${(y-8).toFixed(1)}" text-anchor="middle" font-family="var(--mono)" font-size="10.5" font-weight="600" fill="var(--ink)">${escapeXml(isPct?fmtPct(it.value):String(it.value))}</text>
        <text x="${(x+bw/2).toFixed(1)}" y="${H-10}" text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--ink3)">${escapeXml(truncate(it.label||'', 10))}</text>`;
    }).join('')}
  </svg>`;
}
function hBarList(items, _unused, clickable) {
  const max = Math.max(...items.map(i=>i.value), 1);
  return `<div class="grid" style="gap:8px;">` + items.map(it => `
    <div class="${clickable ? 'hbar-clickable' : ''}" ${clickable ? `data-topic="${escapeHtml(it.label)}" style="cursor:pointer;"` : ''} title="${clickable ? 'Search this topic in Discover' : ''}">
      <div class="flex between small" style="margin-bottom:3px;"><span class="${clickable ? '' : 'muted'}" style="${clickable ? 'color:var(--wine);' : ''}">${escapeHtml(it.label)}</span><span style="font-family:var(--mono);color:var(--ink);">${it.value}</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${(it.value/max)*100}%;background:var(--wine);"></div></div>
    </div>`).join('') + `</div>`;
}
function latencyBars(avgRet, avgTotal) {
  const rows = [['Retrieval', avgRet, '#7A6270'], ['Processing', Math.max(avgTotal-avgRet,0), '#CDBF9B'], ['Total', avgTotal, '#2E292B']];
  return `<div class="grid grid-3">` + rows.map(([label,val,col]) => {
    const pct = Math.min(val/Math.max(avgTotal,1),1)*100;
    return `<div>
      <div class="flex between small" style="margin-bottom:4px;"><span class="muted" style="font-family:var(--mono);text-transform:uppercase;font-size:10.5px;">${label}</span><span style="font-family:var(--mono);font-weight:600;color:${col};">${(val/1000).toFixed(1)}s</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${col};"></div></div>
    </div>`;
  }).join('') + `</div>`;
}

/* ══════════════════════════════════════════════════════════════════════
   VIEW: KNOWLEDGE BASE
   ══════════════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════════════
   VIEW: SETTINGS
   ══════════════════════════════════════════════════════════════════════ */
function viewSettings(main, ctx) {
  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('settings')}</div>
      <div class="page-title">Settings</div>
      <div class="page-desc">Configure how Atlas retrieves, scrapes, and generates research.</div>
      <div class="hr"></div>
      <div id="settingsBody">${skeletonBlock('90px')}${skeletonBlock('220px')}</div>
    </div>
  `;
  ctx.innerHTML = `<div class="s-section"><div class="s-title">About these settings</div>
    <div class="small muted" style="line-height:1.6;">These control the research pipeline's behavior for every future case — how many sources it fetches, how patient it is with slow websites, and which model writes the report. They don't change anything about cases you've already filed.</div>
  </div>`;
  loadSettings();
}
async function loadSettings() {
  const body = $('#settingsBody');
  try {
    const result = await apiGet('/settings');
    if (!result || !result.success) { body.innerHTML = emptyState('!','Failed to load settings',''); return; }
    const s = result.data;
    const group = (title, desc, fields) => `<section class="settings-group"><div><h3>${title}</h3><div class="small muted">${desc}</div></div><div class="settings-fields">${fields}</div></section>`;
    body.innerHTML = `
      ${group('Environment', 'Where this Atlas deployment is running.', `<div class="grid grid-2"><div><span class="small muted">Application</span><div style="font-family:var(--mono);">${escapeHtml(s.app_name || '—')}</div></div><div><span class="small muted">Environment</span><div style="font-family:var(--mono);">${escapeHtml(s.app_env || '—')}</div></div></div>`)}
      ${group('Research model', 'Which language model writes reports, verifies claims, and answers chat questions.', `<label class="sr-only" for="setModel">Research model</label><input class="input" id="setModel" value="${escapeHtml(s.openai_model || '')}" placeholder="e.g. llama-3.3-70b-versatile" style="font-family:var(--mono);" />`)}
      ${group('Source fetch limit', 'Max search results pulled from Tavily per case, before ranking narrows them down. Higher finds more evidence but costs more per case.', `<label class="sr-only" for="setTavilyMax">Source fetch limit</label><input class="input" id="setTavilyMax" type="number" min="1" max="20" value="${s.tavily_max_results ?? 10}" />`)}
      ${group('Default sources per case', 'How many top-ranked sources a new case uses by default.', `<label class="sr-only" for="setTopK">Default sources per case</label><input class="input" id="setTopK" type="number" min="1" max="20" value="${s.default_top_k_urls ?? 5}" />`)}
      ${group('Scrape timeout', 'Seconds to wait for a slow source page before giving up on it.', `<label class="sr-only" for="setScrapeTimeout">Scrape timeout</label><input class="input" id="setScrapeTimeout" type="number" min="5" max="120" value="${s.scrape_timeout_seconds ?? 20}" />`)}
      ${group('Scrape retries', 'How many times to retry a source that failed to load before dropping it.', `<label class="sr-only" for="setScrapeRetries">Scrape retries</label><input class="input" id="setScrapeRetries" type="number" min="0" max="5" value="${s.scrape_max_retries ?? 2}" />`)}
      <div class="flex gap-10 mt-18" style="align-items:center;">
        <button class="btn" id="settingsSaveBtn" onclick="saveSettings()">Save Changes</button>
        <span class="small muted" id="settingsSavedNote"></span>
      </div>
      <div class="small muted mt-14" style="line-height:1.6;">These settings are not saved on the server yet. Changes here only affect this browser tab and reset when you reload the page.</div>
    `;
  } catch (e) { console.error('load settings failed:', e); body.innerHTML = emptyState('!','Could not load settings','Please try again in a moment.'); }
}
async function saveSettings() {
  const btn = $('#settingsSaveBtn'); btn.disabled = true;
  const payload = {
    openai_model: $('#setModel').value.trim() || null,
    tavily_max_results: parseInt($('#setTavilyMax').value, 10),
    default_top_k_urls: parseInt($('#setTopK').value, 10),
    scrape_timeout_seconds: parseInt($('#setScrapeTimeout').value, 10),
    scrape_max_retries: parseInt($('#setScrapeRetries').value, 10),
  };
  try {
    const result = await apiPost('/settings', payload);
    btn.disabled = false;
    if (result && result.success) {
      toast('Settings saved for this session.', 'ok');
      $('#settingsSavedNote').textContent = 'Saved just now.';
    } else {
      toast('Failed to save settings.', 'err');
    }
  } catch { btn.disabled = false; toast('Failed to save settings.', 'err'); }
}

function viewKB(main, ctx) {
  main.innerHTML = `
    <div class="page">
      <div class="crumbs">${crumbsHtml('kb')}</div>
      <div class="page-title">Library</div>
      <div class="page-desc">Everything Atlas has stored from your research, in one place.</div>
      <div class="hr"></div>
      <div id="kbBody">${skeletonBlock('100px')}${skeletonBlock('220px')}</div>
    </div>
  `;
  ctx.innerHTML = '';
  loadKB();
}
let kbConfirmDelete = null;
async function loadKB() {
  const body = $('#kbBody');
  try {
    const result = await apiGet('/knowledge-base');
    if (!result || !result.success) { body.innerHTML = emptyState('!','Could not load your library',''); return; }
    Atlas.state.kbCache = result.data;
    renderKB();
  } catch { body.innerHTML = emptyState('!','Could not load your library',''); }
}
function renderKB() {
  const kb = Atlas.state.kbCache; if (!kb) return;
  const body = $('#kbBody');
  let html = `<div class="lib-figures">
    <div class="fig"><b>${kb.total_reports}</b><span>Reports stored</span></div>
    <div class="fig"><b>${kb.total_chunks}</b><span>Passages indexed</span></div>
    <div class="fig"><b>${kb.chroma_collection_size}</b><span>Index size</span></div>
  </div>`;

  if (kb.recent_topics?.length) {
    html += `<div class="field-label" style="margin-bottom:8px;">Recent topics</div><div class="chip-row" style="margin-top:0;margin-bottom:16px;">` +
      kb.recent_topics.map(t => `<span class="chip" style="cursor:default;">${escapeHtml(t)}</span>`).join('') + `</div>`;
  }

  if (kb.report_ids?.length) {
    html += `<div class="field-label" style="margin-bottom:8px;">Stored reports</div>
      <div class="small muted mt-8" style="margin-bottom:10px;">${kb.report_ids.length} reports on file · Deleting a report removes it completely, including its search index entries.</div>
      <div id="kbRows">` + kb.report_ids.map(rid => kbRowHtml(rid)).join('') + `</div>`;
    if (kbConfirmDelete) html += `<div class="card mt-14" style="border-color:var(--wine-line);">Confirm deletion of <code style="font-family:var(--mono);color:var(--wine);">${kbConfirmDelete.slice(0,16)}…</code> — this is permanent.
      <div class="mt-8"><button class="btn btn-ghost" onclick="kbConfirmDelete=null;renderKB();">Cancel</button></div></div>`;
  } else {
    html += emptyState('🗄️','Your library is empty','Run research and it will be filed here.');
  }
  body.innerHTML = html;
}
function kbRowHtml(rid) {
  const pending = kbConfirmDelete === rid;
  return `<div class="kb-row" style="${pending?'border-color:var(--wine-line);':''}">
    <span class="id">${rid}</span>
    ${pending
      ? `<button class="btn btn-danger" onclick="confirmKbDelete('${rid}')">Confirm</button>`
      : `<button class="btn-icon" title="Delete" onclick="kbConfirmDelete='${rid}';renderKB();">Delete</button>`}
  </div>`;
}
async function confirmKbDelete(rid) {
  try {
    const res = await apiDelete(`/knowledge-base/report/${rid}`);
    if (res && res.success) { toast('Deleted ' + rid.slice(0,8) + '…', 'ok'); kbConfirmDelete = null; loadKB(); }
    else { toast('Delete failed.', 'err'); kbConfirmDelete = null; renderKB(); }
  } catch { kbConfirmDelete = null; renderKB(); }
}

/* ══════════════════════════════════════════════════════════════════════
   ATLAS AI — floating widget (docked bottom-right, available on every
   page — not a router view). Initialized once at boot; sendChat(),
   renderChatMessages(), regenerateLast() and clearChat() below are
   unchanged from when this was a full-page view — they only ever touch
   elements by ID (#chatMessages, #chatInput, #chatSendBtn, #cTopic,
   #cTopK, #cTopKVal, #chatCounter), which now live permanently in the
   floating panel markup in index.html instead of being re-rendered by
   the router on every navigation.
   ══════════════════════════════════════════════════════════════════════ */
function openChatWidget() {
  $('#chatFabWrap').classList.add('open');
  $('#chatFabToggle').setAttribute('aria-expanded', 'true');
  setTimeout(() => $('#chatInput')?.focus(), 50);
}
function closeChatWidget() {
  $('#chatFabWrap').classList.remove('open');
  $('#chatFabToggle').setAttribute('aria-expanded', 'false');
}
function toggleChatWidget() {
  $('#chatFabWrap').classList.contains('open') ? closeChatWidget() : openChatWidget();
}
function initChatWidget() {
  $('#chatFabToggle').addEventListener('click', toggleChatWidget);
  $('#chatFabClose').addEventListener('click', closeChatWidget);
  $('#cTopK').addEventListener('input', e => $('#cTopKVal').textContent = e.target.value);
  $('#chatInput').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
  renderChatMessages();
}
function chatTimeLabel(ts) {
  if (!ts) return '';
  return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
const CHAT_STARTERS = [
  'What are the main risks identified in my research?',
  'Summarize the strongest evidence in this case.',
  'What claims have the lowest confidence, and why?',
  'What would strengthen this case further?',
];
function renderChatMessages() {
  const box = $('#chatMessages'); if (!box) return;
  const msgs = Atlas.state.chatMessages;
  const counter = $('#chatCounter');
  if (counter) {
    const real = msgs.filter(m => !m.pending).length;
    counter.textContent = real ? `${real} message${real === 1 ? '' : 's'}` : '';
  }
  if (!msgs.length) {
    box.innerHTML = `${emptyState('💬','Start a conversation','Set a topic above, then ask anything about your research.')}
      <div class="chat-suggestions mt-14">
        ${CHAT_STARTERS.map(q => `<button class="chat-suggestion" data-q="${escapeHtml(q)}">↳ ${escapeHtml(q)}</button>`).join('')}
      </div>`;
    $$('.chat-suggestion').forEach(btn => btn.addEventListener('click', () => { $('#chatInput').value = btn.dataset.q; sendChat(); }));
    return;
  }
  const lastAssistantIdx = [...msgs].map((m,i)=>({m,i})).reverse().find(x => x.m.role === 'assistant' && !x.m.pending)?.i;
  box.innerHTML = msgs.map((m, idx) => {
    if (m.role === 'user') return `<div style="display:flex;justify-content:flex-end;"><div>
        <div class="bubble-user">${escapeHtml(m.content)}</div>
        <div class="small muted" style="text-align:right;margin-top:3px;font-family:var(--mono);font-size:10px;">${chatTimeLabel(m.ts)}</div>
      </div></div>`;
    if (m.pending) return `<div style="display:flex;justify-content:flex-start;"><div>
        <div class="bubble-label">Atlas</div>
        <div class="bubble-bot" style="display:flex;align-items:center;gap:8px;"><span class="spinner"></span><span class="muted">Thinking…</span></div>
      </div></div>`;
    const chunks = m.chunks || [];
    const sourcesHtml = chunks.length ? `<div class="mt-8">
        <div class="field-label" style="margin-bottom:6px;">SOURCES (${chunks.length})</div>
        ${chunks.map(ch => `<div class="card card-hover" style="margin:6px 0 0;padding:10px 13px;" onclick="openHistoryCase('${ch.report_id}')" title="Open this case">
            <div class="flex between small" style="font-family:var(--mono);">
              <span style="color:var(--ink);font-weight:600;">${escapeHtml(ch.topic||'Untitled case')}</span>
              <span class="muted">Match <b style="color:var(--wine)">${ch.similarity_score!=null ? Math.round(ch.similarity_score*100) : '—'}%</b></span>
            </div>
            <div class="small muted mt-8">${escapeHtml((ch.content||'').slice(0,180))}${(ch.content||'').length>180?'…':''}</div>
          </div>`).join('')}
      </div>` : '';
    const suggestions = m.suggestedQuestions || [];
    const suggestionsHtml = suggestions.length ? `<div class="chat-suggestions mt-10">
        ${suggestions.map(q => `<button class="chat-suggestion" data-q="${escapeHtml(q)}">↳ ${escapeHtml(q)}</button>`).join('')}
      </div>` : '';
    const isLast = idx === lastAssistantIdx;
    return `<div style="display:flex;justify-content:flex-start;"><div style="max-width:78%;">
        <div class="flex between" style="align-items:baseline;">
          <div class="bubble-label">Atlas</div>
          <div class="flex gap-8" style="align-items:center;">
            <span class="small muted" style="font-family:var(--mono);font-size:10px;">${chatTimeLabel(m.ts)}</span>
            ${isLast ? `<button class="chat-copy-btn" onclick="regenerateLast()" title="Regenerate response">↻</button>` : ''}
            <button class="chat-copy-btn" data-idx="${idx}" title="Copy response">⧉</button>
          </div>
        </div>
        <div class="bubble-bot md-body" style="max-width:none;">${mdToHtml(m.content)}</div>
        ${sourcesHtml}${suggestionsHtml}
      </div></div>`;
  }).join('');
  $$('.chat-suggestion').forEach(btn => {
    btn.addEventListener('click', () => { $('#chatInput').value = btn.dataset.q; sendChat(); });
  });
  $$('.chat-copy-btn[data-idx]').forEach(btn => {
    btn.addEventListener('click', () => {
      const msg = msgs[parseInt(btn.dataset.idx, 10)];
      if (!msg) return;
      navigator.clipboard?.writeText(msg.content).then(() => {
        const prev = btn.textContent; btn.textContent = '✓'; setTimeout(() => btn.textContent = prev, 1200);
      });
    });
  });
  box.scrollTop = box.scrollHeight;
}
function regenerateLast() {
  // find the last user question and re-send it, dropping the stale answer
  const msgs = Atlas.state.chatMessages;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'user') {
      msgs.splice(i + 1); // drop everything after (the old answer)
      Atlas.state.chatHistoryApi = Atlas.state.chatHistoryApi.slice(0, -2);
      $('#chatInput').value = msgs[i].content;
      msgs.splice(i, 1); // sendChat() re-adds the user message itself
      sendChat();
      return;
    }
  }
}
async function sendChat() {
  const q = $('#chatInput').value.trim();
  const topic = $('#cTopic').value.trim();
  if (!q) return;
  if (!topic) { toast('Set a topic context first.', 'err'); return; }
  if (topic.length < 3) { toast('Topic context must be at least 3 characters.', 'err'); return; }
  if (q.length < 3) { toast('Question must be at least 3 characters.', 'err'); return; }
  $('#chatInput').value = '';
  Atlas.state.chatMessages.push({ role: 'user', content: q, ts: Date.now() });
  Atlas.state.chatMessages.push({ role: 'assistant', pending: true });
  renderChatMessages();
  const btn = $('#chatSendBtn'); btn.disabled = true;
  try {
    const result = await apiPost('/chat/research', {
      topic, question: q,
      history: Atlas.state.chatHistoryApi.slice(-12),
      top_k_chunks: parseInt($('#cTopK').value, 10),
    });
    Atlas.state.chatMessages.pop(); // remove the pending placeholder
    if (result && result.success) {
      const { answer, retrieved_chunks, suggested_questions } = result.data;
      Atlas.state.chatMessages.push({ role: 'assistant', content: answer, chunks: retrieved_chunks || [], suggestedQuestions: suggested_questions || [], ts: Date.now() });
      Atlas.state.chatHistoryApi.push({ role: 'user', content: q }, { role: 'assistant', content: answer });
    } else {
      console.error('chat request failed:', result);
      Atlas.state.chatMessages.push({ role: 'assistant', content: '⚠️ I couldn\'t get a response just now. Please try again.', chunks: [], ts: Date.now() });
    }
  } catch (e) {
    console.error('chat request failed:', e);
    Atlas.state.chatMessages.pop();
    Atlas.state.chatMessages.push({ role: 'assistant', content: '⚠️ I couldn\'t get a response just now. Please try again.', chunks: [], ts: Date.now() });
  } finally { btn.disabled = false; renderChatMessages(); }
}
function clearChat() { Atlas.state.chatMessages = []; Atlas.state.chatHistoryApi = []; renderChatMessages(); }

/* ══════════════════════════════════════════════════════════════════════
   SESSION GUARD — sign-in, sign-up and password recovery live on auth.html.
   This file only verifies the stored session and sends visitors there if it is missing or expired.
   ══════════════════════════════════════════════════════════════════════ */
function showAuthGate(mode = 'login', message) {
  const q = new URLSearchParams({ mode: mode === 'register' ? 'signup' : 'signin' });
  if (message) q.set('notice', message);
  if (location.hash && location.hash.length > 2) q.set('next', location.hash);
  location.replace('auth.html?' + q.toString());
}
function hideAuthGate() {
  $('#appRoot').classList.remove('auth-locked');
  $('#chatFabWrap').classList.add('ready');
}
function applyAuthedUser(user) {
  const initials = (user.name || user.email || '?').trim().slice(0,1).toUpperCase();
  const avatarEl = $('#accountAvatar') || $('#accountTrigger');
  if (avatarEl) {
    avatarEl.textContent = initials;
    avatarEl.title = 'Account';
  }
  const nameEl = $('#accountName');
  const emailEl = $('#accountEmail');
  if (nameEl) nameEl.textContent = user.name || 'Atlas User';
  if (emailEl) emailEl.textContent = user.email || '';
}

function handleLogout() {
  Atlas.auth.clear();
  Atlas.state.activeCase = null;
  location.replace('auth.html?mode=signin&notice=' + encodeURIComponent('You have been signed out.'));
}

async function bootstrapSession() {
  const stored = Atlas.auth.token;
  if (!stored) { showAuthGate('login'); return; }
  try {
    const res = await fetch(Atlas.config.apiBase + '/auth/me', { headers: { 'Authorization': 'Bearer ' + stored } });
    if (!res.ok) throw new Error('invalid session');
    const json = await res.json();
    Atlas.auth.set(stored, json.data);
    applyAuthedUser(json.data);
    hideAuthGate();
    bootApp();
  } catch {
    Atlas.auth.clear();
    showAuthGate('login');
  }
}

function bootApp() {
  checkHealth();
  const wanted = (location.hash.match(/^#\/([A-Za-z]+)$/) || [])[1];
  Atlas.nav(wanted && wanted !== 'case' && Object.prototype.hasOwnProperty.call(AREA_OF, wanted) ? wanted : 'home');
  initChatWidget();
}

/* ══════════════════════════════════════════════════════════════════════
   BOOT
   ══════════════════════════════════════════════════════════════════════ */
function updateClock() {
  const el = $('#statusBarRight');
  if (!el) return;
  const now = new Date();
  el.textContent = now.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    + ' · ' + now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
setInterval(updateClock, 30000);
updateClock();
setInterval(checkHealth, 20000);
// Boot only after every deferred view script (playground, knowledge, atlasbench, workflow, routing)
// has executed. Calling this at parse time races the later <script> tags: a fast /auth/me response
// could render before viewKnowledge etc. exist and wrongly bounce a signed-in user to the login gate.
if (document.readyState === 'complete') bootstrapSession();
else window.addEventListener('load', bootstrapSession, { once: true });
