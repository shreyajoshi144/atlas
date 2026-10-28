/* ══════════════════════════════════════════════════════════════════════
   ROUTING & COST (M9) — a tab inside AtlasBench.
   Shows which model answered each call, why it was chosen, what it cost, how long
   it took and whether a fallback happened. Lets the user pick explicit or autopilot
   routing, preview a decision (no model is called) and turn fallback on.

   Same conventions as atlasbench.js: pure HTML builders first (unit-tested under Node),
   all dynamic text through esc(), interaction through data-* attributes and one
   delegated listener — no inline JS built from data.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const esc = (typeof escapeHtml === 'function') ? escapeHtml : function (s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  };

  /* ── vocabulary ───────────────────────────────────────────────────── */
  const REASON = {
    explicit: ['Explicit', 'badge-neutral'], autopilot_selected: ['Autopilot', 'badge-jade'],
    autopilot_no_evidence: ['Autopilot · no benchmark yet', 'badge-amber'], autopilot_no_qualifying_model: ['Autopilot · nothing qualified', 'badge-amber'],
    model_not_allowed: ['Default · model not allowed', 'badge-amber'], routing_unavailable: ['Default · routing unavailable', 'badge-terracotta'],
  };
  const PURPOSE = { research: 'Research run', chat: 'Chat', verification: 'Verification', other: 'Other' };
  const PROMPT_SIZES = [[1000, 'Short call (~1k tokens)'], [4000, 'Typical call (~4k tokens)'], [20000, 'Large call (~20k tokens)']];

  /* ── pure helpers ─────────────────────────────────────────────────── */
  const isNum = (x) => typeof x === 'number' && isFinite(x);
  const ms = (x) => !isNum(x) ? '—' : (x < 1000 ? Math.round(x) + ' ms' : (x / 1000).toFixed(x < 10000 ? 1 : 0) + ' s');
  const num = (x) => isNum(x) ? Math.round(x).toLocaleString('en-US') : '—';
  const usd = (x) => !isNum(x) ? '—' : '$' + (x < 0.01 ? x.toFixed(4) : x.toFixed(3));
  const q = (x) => isNum(x) ? x.toFixed(2) : '—';
  const when = (iso) => { const d = new Date(iso); return isNaN(d) ? '—' : d.toISOString().slice(0, 16).replace('T', ' '); };
  const badge = (text, cls, title) => `<span class="badge ${cls}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
  const banner = (kind, html) => `<div class="km-banner ${kind === 'warn' ? 'km-banner-warn' : 'km-banner-neutral'}"${kind === 'warn' ? ' role="alert"' : ''}>${html}</div>`;
  const tile = (label, value, sub) => `<div class="card km-tile"><div class="km-tile-num">${value}</div><div class="small muted">${esc(label)}</div>${sub ? `<div class="small muted">${sub}</div>` : ''}</div>`;
  const modelLabel = (ov, m) => { const f = ((ov && ov.models) || []).find(x => x.model === m); return f ? f.label : m; };

  function reasonBadge(code, reason) {
    const r = REASON[code] || [String(code || 'Unknown'), 'badge-neutral'];
    return badge(r[0], r[1], reason);
  }
  /** "requested → actual" with an unmissable fallback marker; a failed call shows no answering model. */
  function modelFlow(r) {
    if (r.status === 'failed') return `<span>${esc(r.requested_model)}</span> ${badge('Failed', 'badge-terracotta')}`;
    if (r.fallback_used) return `<span class="rt-strike">${esc(r.requested_model)}</span> → <strong>${esc(r.actual_model)}</strong> ${badge('Fallback', 'badge-amber', r.fallback_reason || '')}`;
    return `<strong>${esc(r.actual_model)}</strong>`;
  }
  function costCell(r) {
    const usageEstimate = Object.prototype.hasOwnProperty.call(r, 'usage_cost_estimate_usd') ? r.usage_cost_estimate_usd : r.actual_cost_usd;
    const usage = isNum(usageEstimate) && usageEstimate >= 0 ? usd(usageEstimate) : (r.status === 'failed' ? '—' : '<span class="ab-na" title="Token counts or the model price are unavailable; no usage-based estimate can be calculated">Not reported</span>');
    return `<span title="Usage-based cost estimate from provider-reported token counts and catalog prices">${usage}</span><div class="small muted">Usage-based estimate</div><div class="small muted">Pre-call estimate: ${usd(r.est_cost_usd)}</div>`;
  }
  function usageTable(items) {
    if (!items || !items.length) return '<div class="km-empty">No model calls recorded yet. Run a research or ask a question and each call will appear here.</div>';
    const rows = items.map(r => `<tr>
      <td class="small">${esc(when(r.created_at))}<div class="muted">${esc(PURPOSE[r.purpose] || r.purpose)}</div></td>
      <td>${modelFlow(r)}${r.fallback_used || (r.status === 'failed' && r.fallback_reason) ? `<div class="small rt-fb">${esc(r.fallback_reason)}</div>` : ''}</td>
      <td class="small">${reasonBadge(r.reason_code, r.reason)}<div class="rt-reason">${esc(r.reason)}</div></td>
      <td class="small">${r.tokens_reported ? num(r.total_tokens) : '<span class="muted" title="The provider did not report token counts for this call">not reported</span>'}</td>
      <td class="small">${costCell(r)}</td><td class="small">${ms(r.latency_ms)}${r.attempts && r.attempts.length > 1 ? `<div class="muted">${esc(r.attempts.length)} attempts</div>` : ''}</td>
      <td class="small">${r.status === 'ok' ? badge('OK', 'badge-jade') : badge('Failed', 'badge-terracotta')}</td></tr>`).join('');
    return `<div class="ab-scroll"><table class="km-table ab-table rt-usage"><thead><tr><th>When</th><th>Model</th><th>Why this model</th><th>Tokens</th><th>Cost</th><th>Time</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  // Share of calls by answering model, drawn from the same usage summary the table below uses.
  // Fill pattern + a text legend carry identity, so nothing depends on colour alone.
  function mixBar(list, total) {
    const rows = (list || []).filter(m => Number(m.calls) > 0);
    if (!rows.length || !(total > 0)) return '';
    const pct = m => Math.round(Number(m.calls) / total * 100);
    const label = rows.map(m => `${m.model}: ${pct(m)}% of calls`).join('; ');
    return `<div class="rt-mix-wrap"><div class="field-label">Who answered</div>
      <div class="rt-mix" role="img" aria-label="${esc(label)}">${rows.map((m, i) => `<span class="rt-seg rt-seg-${i % 4}" style="flex:${Number(m.calls)}" title="${esc(m.model)}: ${num(m.calls)} calls"></span>`).join('')}</div>
      <ul class="rt-legend">${rows.map((m, i) => `<li><i class="rt-sw rt-seg-${i % 4}"></i><b>${esc(m.model)}</b> ${num(m.calls)} calls · ${pct(m)}%${Number(m.failed) ? ` · <span class="rt-bad">${num(m.failed)} failed</span>` : ''}${Number(m.fallbacks) ? ` · ${num(m.fallbacks)} via fallback` : ''}</li>`).join('')}</ul></div>`;
  }
  function summaryHtml(s) {
    if (!s || !s.total || !s.total.calls) return '<div class="km-empty">No usage in this period.</div>';
    const t = s.total;
    const costSub = t.cost_usd === null ? 'not reported' : (t.cost_complete ? 'usage-based estimate' : 'some calls could not be priced');
    const rows = (s.by_model || []).map(m => `<tr><th scope="row">${esc(m.model)}</th><td>${num(m.calls)}</td><td>${num(m.failed)}</td><td>${num(m.fallbacks)}</td>
      <td>${m.total_tokens === null || m.total_tokens === undefined ? '—' : num(m.total_tokens)}${m.total_tokens !== null && !m.tokens_complete ? ' <span class="muted" title="Some calls reported no tokens">+</span>' : ''}</td>
      <td>${usd(m.cost_usd)}${m.cost_usd !== null && !m.cost_complete ? ' <span class="muted" title="Some calls could not be priced">+</span>' : ''}</td><td>${ms(m.avg_latency_ms)}</td></tr>`).join('');
    return `<div class="grid grid-4 km-tiles">${tile('Model calls', num(t.calls), `${num(t.failed)} failed`)}${tile('Usage-based cost estimate', usd(t.cost_usd), esc(costSub))}
      ${tile('Tokens', t.total_tokens === null ? 'not reported' : num(t.total_tokens))}${tile('Fallbacks', num(t.fallbacks), isNum(t.fallback_rate) ? Math.round(t.fallback_rate * 100) + '% of calls' : '')}</div>
      ${mixBar(s.by_model, Number(t.calls))}
      <table class="km-table ab-table"><thead><tr><th>Answering model</th><th>Calls</th><th>Failed</th><th>Fallbacks</th><th>Tokens</th><th>Cost</th><th>Avg time</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="small muted">Usage-based costs are estimates from catalog prices × provider-reported token counts, not provider-billed charges. A “+” means some calls could not be priced.</div>`;
  }
  function candidateTable(cands) {
    const rows = (cands || []).map(c => `<tr><th scope="row">${esc(c.model)}</th><td>${c.eligible ? badge('Qualifies', 'badge-jade') : badge('Rejected', 'badge-neutral')}</td>
      <td>${c.evidence ? q(c.evidence.quality) : '—'}</td><td>${c.evidence ? ms(c.evidence.mean_call_latency_ms) : '—'}</td><td>${usd(c.est_cost_usd)}</td>
      <td class="small">${(c.rejected || []).length ? (c.rejected || []).map(esc).join('; ') : (c.evidence ? `AtlasBench run ${esc(String(c.evidence.run_id).slice(0, 12))}…` : '')}</td></tr>`).join('');
    return `<div class="ab-scroll"><table class="km-table ab-table"><thead><tr><th>Model</th><th></th><th>Quality</th><th>Time / call</th><th>Est. cost</th><th>Why / evidence</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function decisionHtml(p) {
    if (!p || !p.decision) return '';
    const d = p.decision;
    const fb = p.fallback && p.fallback.enabled ? `Fallback on: ${(p.fallback.models || []).map(esc).join(' → ') || '—'}` : 'Fallback off';
    return `<div class="card km-detail"><div class="km-claim-text">${esc(d.requested_model)}</div>
      <div class="km-row-sub">${reasonBadge(d.reason_code, d.reason)}<span>policy “${esc(p.policy_name)}” (${esc(p.source)})</span><span>est. ${usd(d.est_cost_usd)} for ~${num(p.prompt_tokens_assumed)} tokens</span><span>${fb}</span></div>
      <p class="small">${esc(d.reason)}</p>${d.candidates && d.candidates.length ? `<div class="km-group-label">Candidates considered</div>${candidateTable(d.candidates)}` : ''}
      <div class="small muted">This was a preview: no model was called.</div></div>`;
  }
  function requirementText(p) {
    return `quality ≥ ${q(p.min_quality)} · ≤ ${num(p.max_latency_ms)} ms/call · ≤ ${usd(p.max_cost_per_call_usd)}/call`;
  }
  function policyRow(p) {
    const fb = p.fallback_enabled ? `fallback → ${(p.fallback_models || []).map(esc).join(' → ')}` : 'no fallback';
    return `<div class="km-row" tabindex="0"><div class="km-row-title">${esc(p.name)} ${p.active ? badge('Active', 'badge-jade') : ''}</div>
      <div class="km-row-sub"><span>${p.mode === 'autopilot' ? 'Autopilot' : 'Explicit'}</span><span>${p.mode === 'autopilot' ? 'default ' : ''}${esc(p.model)}</span>
        ${p.mode === 'autopilot' ? `<span>${esc(requirementText(p))}</span>` : ''}<span>${fb}</span></div>
      <div class="km-toolbar" style="margin-top:6px;"><button class="btn btn-sm" data-act="rt-preview-saved" data-id="${esc(p.policy_id)}">Preview</button>
        ${p.active ? '' : `<button class="btn btn-sm btn-primary" data-act="rt-activate" data-id="${esc(p.policy_id)}">Activate</button>`}
        <button class="btn btn-sm btn-danger" data-act="rt-delete" data-id="${esc(p.policy_id)}"${p.active ? ' disabled title="Deactivate it first"' : ''}>Delete</button></div></div>`;
  }
  function activeBanner(ov) {
    const a = ov && ov.active_policy;
    if (!a) return banner('neutral', `No routing policy is active, so every call uses <strong>${esc(ov ? ov.default_model : '—')}</strong> with no fallback — exactly how Atlas behaved before routing.
      <button class="btn btn-sm" data-act="rt-refresh" style="margin-left:8px;">Refresh</button>`);
    return banner('neutral', `Active policy: <strong>${esc(a.name)}</strong> (${a.mode === 'autopilot' ? 'Autopilot' : 'Explicit'} · ${esc(a.model)}${a.fallback_enabled ? ' · fallback on' : ''}).
      <button class="btn btn-sm" data-act="rt-deactivate" style="margin-left:8px;">Use application default</button>`);
  }
  function policyFormHtml(ov) {
    const models = ((ov && ov.models) || []);
    const opts = models.map(m => `<option value="${esc(m.model)}">${esc(m.label)}${m.price_in === null ? ' (no price)' : ''}</option>`).join('');
    const checks = models.map(m => `<label class="km-check"><input type="checkbox" class="rtFb" value="${esc(m.model)}"> ${esc(m.label)}</label>`).join('');
    const sizes = PROMPT_SIZES.map(([n, l]) => `<option value="${n}"${n === 4000 ? ' selected' : ''}>${esc(l)}</option>`).join('');
    return `<div class="card km-detail"><div class="km-claim-text">New routing policy</div>
      <div class="km-toolbar"><div class="km-sel"><label class="field-label" for="rtName">Name</label><input class="input" id="rtName" maxlength="80" placeholder="e.g. Cheapest that is good enough"></div>
        <div class="km-sel"><label class="field-label" for="rtMode">Mode</label><select class="input" id="rtMode"><option value="explicit">Explicit — always this model</option><option value="autopilot">Autopilot — cheapest that meets my requirements</option></select></div>
        <div class="km-sel"><label class="field-label" for="rtModel" id="rtModelLabel">Model</label><select class="input" id="rtModel">${opts}</select></div></div>
      <div class="rt-auto" id="rtAuto" hidden><div class="small muted">Autopilot only uses models you have benchmarked in AtlasBench. If none qualifies, it uses the model chosen above, and says so.</div>
        <div class="km-toolbar"><div class="km-sel"><label class="field-label" for="rtQuality">Minimum quality (0–1)</label><input class="input" id="rtQuality" type="number" min="0" max="1" step="0.05" value="0.8"></div>
          <div class="km-sel"><label class="field-label" for="rtLatency">Max time per call (ms)</label><input class="input" id="rtLatency" type="number" min="200" max="120000" step="100" value="8000"></div>
          <div class="km-sel"><label class="field-label" for="rtCost">Max cost per call (USD)</label><input class="input" id="rtCost" type="number" min="0.0001" max="5" step="0.001" value="0.02"></div></div></div>
      <label class="km-check"><input type="checkbox" id="rtFallback"> Fall back to another model on a timeout, rate limit (429) or provider error (5xx)</label>
      <div class="rt-fbpick" id="rtFbPick" hidden><div class="small muted">Tried in the order of the list below; never used during AtlasBench runs. Every fallback is recorded.</div><div class="ab-checks">${checks}</div></div>
      <div class="km-toolbar" style="margin-top:10px;"><button class="btn btn-primary" data-act="rt-save">Save policy</button>
        <div class="km-sel"><label class="field-label" for="rtSize">Preview for</label><select class="input" id="rtSize">${sizes}</select></div>
        <button class="btn" data-act="rt-preview-form">Preview without saving</button></div></div>`;
  }
  /** Form values → API payload. Numbers are coerced; the server validates everything else. */
  function parsePolicyForm(v) {
    const n = (x, d) => { const k = Number(x); return isFinite(k) ? k : d; };
    const fb = Array.isArray(v.fallback_models) ? v.fallback_models.filter(Boolean) : [];
    const p = { name: String(v.name || '').trim() || 'Untitled policy', mode: v.mode === 'autopilot' ? 'autopilot' : 'explicit', model: v.model,
      min_quality: n(v.min_quality, 0.8), max_latency_ms: Math.round(n(v.max_latency_ms, 8000)), max_cost_per_call_usd: n(v.max_cost_per_call_usd, 0.02),
      fallback_enabled: !!v.fallback_enabled, fallback_models: v.fallback_enabled ? fb : [] };
    return p;
  }

  /* ── browser part ─────────────────────────────────────────────────── */
  const R = { overview: null, policies: null, usage: null, summary: null, preview: null, fallbackOnly: false, error: null };
  const API = '/routing';
  const get = (path, params) => apiGet(API + path, params).then(r => (r && r.success ? r.data : null));
  const val = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };

  function html() {
    const items = (R.policies && R.policies.items) || [];
    return `${R.error ? banner('warn', esc(R.error)) : ''}
      <div class="small muted">Atlas picks a model for every call it makes for you. With no policy it uses the application model. Autopilot chooses the cheapest model whose AtlasBench results meet your quality, cost and latency limits; a fallback only ever happens if you turn it on, and is always shown below.</div>
      ${activeBanner(R.overview)}
      <div class="km-group-label">Model calls and cost (last 30 days)</div>${summaryHtml(R.summary)}
      <div class="km-group-label">Recent calls</div>
      <div class="km-toolbar"><label class="km-check"><input type="checkbox" data-act="rt-fb-only"${R.fallbackOnly ? ' checked' : ''}> Fallbacks only</label><button class="btn btn-sm" data-act="rt-refresh">Refresh</button></div>
      ${usageTable(R.usage && R.usage.items)}
      <div class="km-group-label">Policies (${items.length})</div>${items.map(policyRow).join('') || '<div class="km-empty">No saved policies.</div>'}
      <div id="rtPreview">${R.preview ? decisionHtml(R.preview) : ''}</div>${policyFormHtml(R.overview)}`;
  }
  async function load(el) {
    try {
      const [ov, pol, us, sm] = await Promise.all([get('/overview'), get('/policies'), get('/usage', { limit: 25, fallback_only: R.fallbackOnly ? 'true' : undefined }), get('/usage/summary', { days: 30 })]);
      R.overview = ov; R.policies = pol; R.usage = us; R.summary = sm; R.error = null;
    } catch (e) { R.error = 'Routing information could not be loaded. Try again in a moment.'; }
    if (el.isConnected) el.innerHTML = html();
  }
  function formValues() {
    return { name: val('rtName'), mode: val('rtMode'), model: val('rtModel'), min_quality: val('rtQuality'), max_latency_ms: val('rtLatency'), max_cost_per_call_usd: val('rtCost'),
      fallback_enabled: (document.getElementById('rtFallback') || {}).checked, fallback_models: Array.from(document.querySelectorAll('.rtFb:checked')).map(x => x.value) };
  }
  function showPreview(p) { R.preview = p; const b = document.getElementById('rtPreview'); if (b) { b.innerHTML = decisionHtml(p); b.scrollIntoView && b.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } }
  async function onClick(ev, el) {
    const t = ev.target.closest('[data-act]'); if (!t) return;
    const act = t.dataset.act, id = t.dataset.id;
    try {
      if (act === 'rt-refresh') { await load(el); }
      else if (act === 'rt-fb-only') { R.fallbackOnly = t.checked; await load(el); }
      else if (act === 'rt-activate') { await apiPut(API + '/active', { policy_id: id }); toast('Policy activated', 'ok'); await load(el); }
      else if (act === 'rt-deactivate') { await apiPut(API + '/active', { policy_id: null }); toast('Back to the application default', 'ok'); await load(el); }
      else if (act === 'rt-delete') { if (!window.confirm('Delete this policy?')) return; await apiDelete(API + '/policies/' + encodeURIComponent(id)); toast('Policy deleted', 'ok'); await load(el); }
      else if (act === 'rt-save') { await apiPost(API + '/policies', parsePolicyForm(formValues())); toast('Policy saved', 'ok'); R.preview = null; await load(el); }
      else if (act === 'rt-preview-saved') { const r = await apiPost(API + '/preview', { policy_id: id, prompt_tokens: 4000 }); showPreview(r.data); }
      else if (act === 'rt-preview-form') { const r = await apiPost(API + '/preview', { policy: parsePolicyForm(formValues()), prompt_tokens: Number(val('rtSize')) || 4000 }); showPreview(r.data); }
    } catch (e) { /* apiCall already toasted the reason */ }
  }
  function onChange(ev) {
    if (ev.target.id === 'rtMode') {
      const auto = ev.target.value === 'autopilot';
      const box = document.getElementById('rtAuto'); if (box) box.hidden = !auto;
      const lab = document.getElementById('rtModelLabel'); if (lab) lab.textContent = auto ? 'Default model (used if none qualifies)' : 'Model';
    } else if (ev.target.id === 'rtFallback') {
      const box = document.getElementById('rtFbPick'); if (box) box.hidden = !ev.target.checked;
    }
  }
  async function mount(el) {
    el.innerHTML = (typeof skeletonBlock === 'function' ? skeletonBlock('200px') : '');
    el.addEventListener('click', ev => onClick(ev, el));
    el.addEventListener('change', onChange);
    await load(el);
  }

  const helpers = { mount, usageTable, summaryHtml, decisionHtml, candidateTable, policyRow, policyFormHtml, parsePolicyForm, activeBanner, modelFlow, costCell, reasonBadge,
    requirementText, ms, usd, num, esc };
  if (typeof window !== 'undefined') window.AtlasRouting = helpers;
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
})();
