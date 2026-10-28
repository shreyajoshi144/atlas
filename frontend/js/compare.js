/* Compare — same question to several providers, with measured (never invented) differences.
   Talks to /api/compare. Globals from script.js: apiGet, apiPost, apiDelete, escapeHtml, escapeAttr, toast. */
(function () {
  'use strict';
  const esc = (v) => escapeHtml(v == null ? '' : String(v));
  const attr = (v) => (typeof escapeAttr === 'function' ? escapeAttr(v == null ? '' : String(v)) : esc(v));

  /* ── value formatting: missing is "Not reported", never 0 ─────────── */
  const num = (v) => {
    if (v === null || v === undefined || (typeof v === 'string' && v.trim() === '') || typeof v === 'boolean') return null;
    const n = typeof v === 'number' ? v : Number(String(v).trim());
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const NR = 'Not reported';
  const fmtUsd = (v) => { const n = num(v); return n === null ? NR : (n > 0 && n < 0.0001 ? '<$0.0001' : '$' + n.toFixed(n >= 1 ? 2 : 4)); };
  const fmtInt = (v) => { const n = num(v); return n === null || !Number.isInteger(n) ? NR : n.toLocaleString('en-US'); };
  const fmtMsv = (v) => { const n = num(v); return n === null ? NR : (n >= 1000 ? (n / 1000).toFixed(1) + ' s' : Math.round(n) + ' ms'); };
  const fmtRate = (v) => { const n = num(v); return n === null || n > 1 ? NR : Math.round(n * 100) + '%'; };
  const fmtPrice = (v) => { const n = num(v); return n === null ? NR : '$' + n; };

  /* ── safe URL + strict markdown (escape first; nothing raw survives) ── */
  function safeUrl(u) {
    if (typeof u !== 'string') return null;
    const s = u.trim();
    if (!/^https?:\/\//i.test(s) || /[\s<>"'`\\]/.test(s)) return null;
    try { const p = new URL(s); return (p.protocol === 'http:' || p.protocol === 'https:') ? p.href : null; } catch (_) { return null; }
  }
  function inlineMd(text) {
    // text is already HTML-escaped
    let out = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, href) => {
      const raw = href.replace(/&amp;/g, '&');
      const ok = safeUrl(raw);
      return ok ? `<a href="${attr(ok)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
    return out;
  }
  function safeMarkdown(md) {
    if (md == null || md === '') return '';
    const lines = esc(md).split('\n');
    const out = []; let para = []; let list = null;
    const flushP = () => { if (para.length) { out.push('<p>' + para.map(inlineMd).join('<br>') + '</p>'); para = []; } };
    const flushL = () => { if (list) { out.push(`<${list.t}>${list.items.map(i => '<li>' + inlineMd(i) + '</li>').join('')}</${list.t}>`); list = null; } };
    for (const line of lines) {
      let m;
      if (!line.trim()) { flushP(); flushL(); continue; }
      if ((m = line.match(/^\s*#{1,6}\s+(.*)$/))) { flushP(); flushL(); out.push('<p><strong>' + inlineMd(m[1]) + '</strong></p>'); continue; }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ul') { flushL(); list = { t: 'ul', items: [] }; } list.items.push(m[1]); continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ol') { flushL(); list = { t: 'ol', items: [] }; } list.items.push(m[1]); continue; }
      flushL(); para.push(line.trim());
    }
    flushP(); flushL();
    return out.join('');
  }

  /* ── ids, estimates, gating ───────────────────────────────────────── */
  function newIdempotencyKey() {
    try {
      if (typeof crypto !== 'undefined' && crypto.randomUUID) return 'cmp-' + crypto.randomUUID();
      if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
        const b = new Uint8Array(16); crypto.getRandomValues(b);
        return 'cmp-' + Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
      }
    } catch (_) { /* fall through */ }
    return 'cmp-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }
  // Worst-case estimate scales the server's per-call figure (computed at the default output cap) by the chosen cap.
  function estimateRunCost(models, selectedIds, maxOut, defaultOut) {
    const chosen = (models || []).filter(m => selectedIds.includes(m.id));
    let total = 0; let unknown = 0;
    const scale = (num(maxOut) && num(defaultOut)) ? Math.max(1, num(maxOut) / num(defaultOut)) : 1;
    for (const m of chosen) { const n = num(m.est_max_cost_usd); if (n === null) unknown++; else total += n * scale; }
    return { usd: chosen.length && unknown === 0 ? total : null, unknown, count: chosen.length };
  }
  function gateRun(s) {
    const L = s.limits || {};
    const reasons = [];
    const n = s.selected.length;
    const min = s.mode === 'ask' ? 1 : (L.min_models || 1);
    const max = s.mode === 'ask' ? 3 : (L.max_models || 4);
    if (n < min) reasons.push(s.mode === 'ask' ? 'Choose at least one model.' : `Choose at least ${min} model${min === 1 ? '' : 's'}.`);
    if (n > max) reasons.push(`Choose at most ${max} models.`);
    const p = (s.prompt || '').trim();
    if (p.length < 3) reasons.push('Enter a question of at least 3 characters.');
    if (L.max_prompt_chars && p.length > L.max_prompt_chars) reasons.push(`Question is longer than ${L.max_prompt_chars} characters.`);
    if (!s.consent) reasons.push('Confirm that your question and any evidence may be sent to the selected providers.');
    const mo = num(s.maxOut);
    if (L.max_output_tokens && mo !== null && mo > L.max_output_tokens) reasons.push(`Output limit cannot exceed ${fmtInt(L.max_output_tokens)} tokens.`);
    const est = estimateRunCost(s.models, s.selected, s.maxOut, L.default_max_output_tokens);
    if (est.usd !== null && num(L.max_cost_per_request_usd) !== null && est.usd > L.max_cost_per_request_usd) {
      reasons.push(`Worst-case cost ${fmtUsd(est.usd)} is above the ${fmtUsd(L.max_cost_per_request_usd)} per-request ceiling. Remove a model or lower the output limit.`);
    }
    if (s.selected.some(id => { const m = (s.models || []).find(x => x.id === id); return !m || !m.configured; })) reasons.push('A selected provider has no server-side key configured.');
    return { ok: reasons.length === 0, reasons, estimate: est };
  }

  /* ── error + status explanations ──────────────────────────────────── */
  const ERROR_HELP = {
    auth: 'The provider rejected the server\'s API key. An administrator needs to check the key; retrying will not help.',
    rate_limit: 'The provider asked Atlas to slow down (rate limit). Wait a moment and try again.',
    timeout: 'The provider did not answer within the time limit.',
    server: 'The provider had an internal problem. This is usually temporary.',
    overloaded: 'The provider is overloaded right now. This is usually temporary.',
    network: 'Atlas could not reach the provider.',
    bad_request: 'The provider refused the request as invalid (for example, an unsupported option for this model).',
    model_not_found: 'The provider does not offer this model ID for this key.',
    content_filter: 'The provider\'s safety system declined to answer.',
    parse: 'The provider replied in a format Atlas could not read.',
    budget: 'Your account budget or call limit blocked this call before it was sent.',
    circuit_open: 'Atlas paused calls to this provider after repeated failures and will retry shortly.',
    cost_ceiling: 'The estimated worst-case cost exceeded the per-request ceiling.',
  };
  const STATUS_LABEL = { ok: 'Complete', failed: 'Failed', timeout: 'Timed out', blocked: 'Blocked', skipped: 'Skipped' };
  const explainError = (e) => {
    if (!e) return 'This model could not complete the request.';
    const help = ERROR_HELP[e.category];
    return (e.message ? String(e.message) : '') + (help ? (e.message ? ' — ' : '') + help : '') + (e.retryable ? ' (Retrying may work.)' : '');
  };

  /* ── pieces of the result card ────────────────────────────────────── */
  function citationsHtml(list) {
    if (!Array.isArray(list) || !list.length) return '<p class="cmp-muted">No citations returned.</p>';
    return '<ul class="cmp-cites">' + list.map(c => {
      const title = esc(c.title || c.url || c.source_id || 'Source');
      const url = safeUrl(c.url);
      const label = c.kind === 'atlas_source' ? `[${esc(c.source_id || '?')}]` : 'Web';
      const head = url ? `<a href="${attr(url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : `<span>${title}</span>`;
      const valid = c.valid === true ? '<span class="cmp-tag">label exists</span>' : '<span class="cmp-tag cmp-tag-warn">not found in evidence</span>';
      return `<li><b>${label}</b> ${head} ${c.kind === 'atlas_source' ? valid : ''}${c.snippet ? `<small>${esc(String(c.snippet).slice(0, 220))}</small>` : ''}</li>`;
    }).join('') + '</ul>';
  }
  function groundingHtml(g) {
    if (!g) return '<p class="cmp-muted">No grounding check ran for this answer (no evidence was supplied or the model failed).</p>';
    const rows = [
      ['Citations that point to real evidence', fmtRate(g.valid_citation_rate)],
      ['Sentences carrying a citation', fmtRate(g.sentence_coverage)],
      ['Cited text that overlaps its source', g.checked ? `${fmtRate(g.support_rate)} (${fmtInt(g.supported)} of ${fmtInt(g.checked)} checked)` : NR],
    ];
    const bad = (g.invalid_labels || []).length ? `<p class="cmp-warn">Cited labels not in the evidence: ${(g.invalid_labels).map(esc).join(', ')}</p>` : '';
    return `<dl class="cmp-kv">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>${bad}
      <p class="cmp-muted">Method: ${esc(g.method || 'not stated')}. This is a rough text-overlap check, not proof the answer is correct.</p>
      ${(g.notes || []).map(n => `<p class="cmp-muted">${esc(n)}</p>`).join('')}`;
  }
  function attemptsHtml(attempts) {
    if (!Array.isArray(attempts) || !attempts.length) return '';
    return '<ol class="cmp-attempts">' + attempts.map(a =>
      `<li><span class="cmp-tag ${a.outcome === 'ok' ? '' : 'cmp-tag-warn'}">${esc(a.outcome || '?')}</span> attempt ${esc(a.n)} · ${esc(fmtMsv(a.ms))}${a.category ? ' · ' + esc(a.category) : ''}</li>`).join('') + '</ol>';
  }
  function usageHtml(r) {
    const u = r.usage || {}; const c = r.cost || {}; const l = r.latency || {};
    const basis = { provider_reported: 'reported by the provider', catalog_price_estimate: 'estimated from token counts × catalog price', unknown: 'unknown' }[c.basis] || 'unknown';
    const rows = [
      ['Input tokens', fmtInt(u.input_tokens)], ['Output tokens', fmtInt(u.output_tokens)],
      ['Reasoning tokens', fmtInt(u.reasoning_tokens)], ['Total tokens', fmtInt(u.total_tokens)],
      ['Estimated cost', fmtUsd(c.usd) + (c.usd != null && c.complete === false ? ' (incomplete)' : '')],
      ['Cost basis', basis],
      ['Response time', fmtMsv(l.total_ms)], ['Retries', fmtInt(l.retries)],
    ];
    return `<dl class="cmp-kv">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
      ${c.price_verified_on ? `<p class="cmp-muted">Catalog price last verified ${esc(c.price_verified_on)}. This is an estimate, not an invoice.</p>` : ''}
      ${attemptsHtml(l.attempts)}`;
  }
  function resultCardHtml(r) {
    r = r || {};
    const ok = r.status === 'ok';
    const label = STATUS_LABEL[r.status] || 'Unavailable';
    const flags = (r.safety && r.safety.flags) || [];
    const trunc = r.truncated ? '<div class="pg-warning" role="status">This answer was cut off at the output limit, so it may be incomplete.</div>' : '';
    let body;
    if (!ok) body = `<div class="pg-error-copy" role="status"><b>${esc(r.error && r.error.category ? r.error.category : r.status || 'error')}</b> ${esc(explainError(r.error))}</div>`;
    else body = `<div class="pg-answer md-body">${safeMarkdown(r.answer || '')}</div>`;
    let structured = '';
    if (r.structured) {
      const s = r.structured;
      structured = `<details class="cmp-det"><summary>Structured claims ${s.valid ? '(valid)' : '(did not validate)'}${s.repaired ? ' · repaired once' : ''}</summary>
        ${(s.errors || []).map(e => `<p class="cmp-warn">${esc(e)}</p>`).join('')}
        ${s.data && Array.isArray(s.data.claims) ? '<ul class="cmp-cites">' + s.data.claims.map(c => `<li>${esc(c && c.text)} <small>${((c && c.sources) || []).map(x => '[' + esc(x) + ']').join(' ') || 'no sources cited'}</small></li>`).join('') + '</ul>' : '<p class="cmp-muted">No claims returned.</p>'}
        ${s.data && s.data.abstained ? '<p class="cmp-muted">The model said it could not answer from the evidence.</p>' : ''}</details>`;
    }
    return `<article class="pg-output cmp-card ${ok ? '' : 'pg-output-error'}" data-cmp-result="${attr(r.id)}">
      <header><div><span class="pg-provider-dot"></span><strong>${esc(r.label || r.model || r.id)}</strong><span class="pg-model-id">${esc(r.provider || '')} · ${esc(r.model || '')}</span></div>
        <span class="pg-state ${ok ? '' : 'is-error'}">${esc(label)}</span></header>
      ${trunc}${body}${structured}
      ${flags.length ? `<div class="pg-warning" role="status">Safety notes: ${flags.map(esc).join(', ')}</div>` : ''}
      <details class="cmp-det" ${ok ? 'open' : ''}><summary>Citations (${Array.isArray(r.citations) ? r.citations.length : 0})</summary>${citationsHtml(r.citations)}</details>
      <details class="cmp-det"><summary>Grounding</summary>${groundingHtml(r.grounding)}</details>
      <details class="cmp-det" open><summary>Tokens, cost and speed</summary>${usageHtml(r)}</details>
    </article>`;
  }

  /* ── summary / evaluation / sources ───────────────────────────────── */
  function summaryHtml(summary) {
    const ms = (summary && summary.measurements) || [];
    const notes = (summary && summary.notes) || [];
    if (!ms.length && !notes.length) return '';
    const fmt = (m) => {
      const n = num(m.value); if (n === null) return NR;
      if (m.unit === 'usd') return fmtUsd(n); if (m.unit === 'ms') return fmtMsv(n);
      if (m.unit === 'ratio' || m.unit === 'rate') return fmtRate(n);
      return fmtInt(n) + (m.unit ? ' ' + m.unit : '');
    };
    return `<section class="cmp-summary" aria-label="Measured differences in this run"><h3>Measured differences in this run</h3>
      <p class="cmp-muted">These are measurements from one run, not a verdict on answer quality.</p>
      ${ms.length ? `<ul>${ms.map(m => `<li><span>${esc(m.label || m.key)}</span><b>${esc(m.winner_id || '—')}</b><em>${esc(fmt(m))}</em></li>`).join('')}</ul>` : ''}
      ${notes.map(n => `<p class="cmp-muted">${esc(n)}</p>`).join('')}</section>`;
  }
  function evaluationHtml(runEval, evals) {
    const parts = [];
    const runs = (evals && evals.runs) || [];
    if (!runs.length) {
      parts.push(`<p><b>No measured evaluation evidence yet.</b> ${esc((runEval && runEval.message) || '')} Atlas cannot say which model gives better answers, so no ranking is shown.</p>`);
    } else {
      const r = runs[0];
      parts.push(`<p>Latest evaluation: <b>${esc((r.dataset && r.dataset.name) || 'dataset')}</b> v${esc((r.dataset && r.dataset.version) || '?')}, ${fmtInt(r.dataset && r.dataset.n_cases)} cases, run ${esc(r.ran_at || 'date not reported')}.</p>`);
      parts.push('<table class="cmp-table"><thead><tr><th>Model</th><th>Cases</th><th>Pass rate</th><th>95% interval</th></tr></thead><tbody>' +
        (r.models || []).map(m => `<tr><td>${esc(m.id)}</td><td>${fmtInt(m.n)}</td><td>${fmtRate(m.pass_rate)}</td><td>${Array.isArray(m.ci95) ? esc(fmtRate(m.ci95[0]) + ' to ' + fmtRate(m.ci95[1])) : NR}</td></tr>`).join('') + '</tbody></table>');
      parts.push(r.ranking_supported
        ? '<p>On this dataset the intervals do not overlap, so the measured difference is large enough to report. It applies only to these test cases.</p>'
        : '<p><b>The intervals overlap, so this evaluation cannot rank these models.</b> Differences may be chance.</p>');
      (r.caveats || []).forEach(c => parts.push(`<p class="cmp-muted">${esc(c)}</p>`));
    }
    if (evals && evals.explanation) parts.push(`<p class="cmp-muted">${esc(evals.explanation)}</p>`);
    return `<section class="cmp-eval" aria-label="Evaluation evidence"><h3>Evaluation evidence</h3>${parts.join('')}</section>`;
  }
  function sourcesHtml(sources) {
    if (!Array.isArray(sources) || !sources.length) return '';
    return `<div class="pg-sources"><h3>Evidence given to every model <span>${sources.length}</span></h3><div class="pg-source-grid">${sources.map(s => {
      const url = safeUrl(s.url); const title = esc(s.title || s.domain || 'Source');
      const head = url ? `<a href="${attr(url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : `<span>${title}</span>`;
      const flags = (s.risk_flags || []).length ? `<p class="cmp-warn">Contains instruction-like text (${s.risk_flags.map(esc).join(', ')}). It was wrapped as untrusted data, but treat answers with care.</p>` : '';
      return `<article class="pg-source"><div class="pg-source-head"><b>[${esc(s.id)}]</b>${head}<span>${s.kind === 'web' ? 'Web' : 'Your library'}</span></div><p>${esc(String(s.excerpt || '').slice(0, 320))}</p>${flags}</article>`;
    }).join('')}</div></div>`;
  }

  /* ── glossary ─────────────────────────────────────────────────────── */
  const GLOSSARY = [
    ['Token', 'A small chunk of text (roughly ¾ of a word). Providers count and bill by tokens, and different providers count differently, so token totals are not directly comparable.'],
    ['Latency', 'How long Atlas waited for the full answer, including any retries. One run is a single sample; speed varies from minute to minute.'],
    ['Cost estimate vs. invoice', 'Atlas multiplies reported token counts by a catalog price. Your provider\'s invoice is the real number, and prices change, which is why each price shows the date it was verified.'],
    ['Grounding vs. accuracy', 'Grounding asks "does the answer stay tied to the evidence it was given?" It does not tell you the answer is true, or that the evidence was right.'],
    ['Citation validity vs. support', 'A citation is valid if the label exists in the evidence. It is supported only if the cited text actually backs the sentence. Atlas checks the second with a rough word-overlap test, so treat it as a hint.'],
    ['Reliability', 'In this view it means only what happened in this run: failures, time-outs and retries. A single run says little about long-term uptime.'],
    ['Structured output', 'Asking the model for a fixed JSON shape (answer plus claims and their sources) so claims can be checked one by one. Some models enforce the shape natively; others are asked politely and then validated.'],
    ['One run is an anecdote', 'Comparing answers to one question shows how models differ on that question. Only an evaluation over many test cases, with confidence intervals, can support a statement like "model A is better".'],
    ['Evidence modes', '"Atlas evidence" gives every model the same passages, so differences are fair to compare. "Provider search" lets each provider search the web itself, so each model sees different material and answers are not directly comparable.'],
  ];
  function glossaryHtml() {
    return `<details class="cmp-det cmp-how"><summary>How to read this page</summary><dl class="cmp-gloss">${GLOSSARY.map(([t, d]) => `<div><dt>${esc(t)}</dt><dd>${esc(d)}</dd></div>`).join('')}</dl></details>`;
  }

  /* ── state + view ─────────────────────────────────────────────────── */
  const S = { loaded: false, models: [], providers: [], limits: {}, disclosures: [], stale: false, verifiedOn: null,
    selected: [], mode: 'compare', evidence: 'atlas', docs: true, web: false, structured: false, consent: false,
    maxOut: '', prompt: '', busy: false, error: null, run: null, ask: null, runs: [], evals: null, key: null };

  function modelPickerHtml() {
    const groups = {};
    S.models.forEach(m => { (groups[m.provider] = groups[m.provider] || []).push(m); });
    return Object.keys(groups).map(pid => {
      const prov = S.providers.find(p => p.id === pid) || { id: pid, label: pid, configured: false };
      const head = `<div class="cmp-prov-head"><strong>${esc(prov.label || pid)}</strong><span class="cmp-tag ${prov.configured ? '' : 'cmp-tag-warn'}">${prov.configured ? 'Key configured' : 'Not configured'}</span>${prov.circuit && prov.circuit !== 'closed' ? `<span class="cmp-tag cmp-tag-warn">circuit ${esc(prov.circuit)}</span>` : ''}</div>`;
      const hint = !prov.configured && prov.key_hint ? `<p class="cmp-muted">Set <code>${esc(prov.key_hint)}</code> on the server to enable.</p>` : '';
      const items = groups[pid].map(m => {
        const dis = !m.configured || m.available === false;
        const avail = m.available === true ? 'available' : m.available === false ? 'not offered to this key' : 'availability not checked';
        const price = (m.price_in_per_mtok == null || m.price_out_per_mtok == null) ? 'price unknown' : `${fmtPrice(m.price_in_per_mtok)} in / ${fmtPrice(m.price_out_per_mtok)} out per 1M tokens`;
        return `<label class="pg-model-choice cmp-choice ${dis ? 'is-disabled' : ''}"><input type="checkbox" data-cmp-model value="${attr(m.id)}" ${S.selected.includes(m.id) ? 'checked' : ''} ${dis ? 'disabled' : ''}>
          <span><b>${esc(m.label || m.model)}</b><small>${esc(m.model)} · ${esc(m.tier || '')}</small>
          <small>${esc(price)}${m.price_note ? ' — ' + esc(m.price_note) : ''}</small>
          <small>Price verified ${esc(m.price_verified_on || 'date not reported')} · ${m.context_window ? fmtInt(m.context_window) + ' token context · ' : ''}${esc(avail)}</small></span></label>`;
      }).join('');
      return `<fieldset class="cmp-prov"><legend class="sr-only">${esc(prov.label || pid)} models</legend>${head}${hint}<div class="pg-model-list">${items}</div></fieldset>`;
    }).join('');
  }
  function gateHtml(g) {
    const e = g.estimate;
    const est = e.count === 0 ? 'Choose models to see a worst-case cost.' :
      e.usd === null ? `Worst-case cost cannot be estimated (${e.unknown} selected model${e.unknown === 1 ? ' has' : 's have'} no known price). The server will enforce your budget.` :
      `Worst-case cost before running: <b>${esc(fmtUsd(e.usd))}</b> (every model uses its full output limit). Most runs cost less.`;
    return `<div class="cmp-gate" id="cmpGate"><p>${est}</p>${g.reasons.length ? `<ul>${g.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}</div>`;
  }
  function resultsHtml() {
    if (S.mode === 'ask' && S.ask) {
      const a = S.ask;
      return `<section class="pg-turn-output"><div class="pg-turn-label">Answer with fallback</div>
        <div class="cmp-timeline"><p>${a.fallback_used ? `<b>Fallback was used:</b> ${esc(a.fallback_reason || 'reason not reported')}` : 'The first model answered; no fallback was needed.'}</p>
        <ol class="cmp-attempts">${(a.attempts || []).map(x => `<li><span class="cmp-tag ${x.status === 'ok' ? '' : 'cmp-tag-warn'}">${esc(x.status)}</span> ${esc(x.model_id)} · ${esc(fmtMsv(x.ms))}${x.category ? ' · ' + esc(x.category) : ''}</li>`).join('')}</ol></div>
        <div class="pg-output-grid cmp-grid">${resultCardHtml(a.result)}</div>${sourcesHtml(a.sources)}${(a.warnings || []).map(w => `<div class="pg-warning" role="status">${esc(w)}</div>`).join('')}</section>`;
    }
    if (!S.run) return '';
    const r = S.run;
    return `<section class="pg-turn-output"><div class="pg-turn-label">Results · ${esc(r.created_at || '')}</div>
      <div class="pg-output-grid cmp-grid">${(r.results || []).map(resultCardHtml).join('')}</div>
      ${summaryHtml(r.summary)}${evaluationHtml(r.evaluation, S.evals)}${sourcesHtml(r.sources)}
      ${(r.warnings || []).map(w => `<div class="pg-warning" role="status">${esc(w)}</div>`).join('')}
      <p class="cmp-muted">Actual estimated cost of this run: ${esc(fmtUsd(r.budget && r.budget.actual_cost_usd))}${r.budget && r.budget.cost_complete === false ? ' (some costs unknown)' : ''}.</p></section>`;
  }
  function html() {
    const g = gateRun(S);
    const hist = S.runs.map(r => `<div class="pg-history-row"><button class="pg-history-item" data-cmp-open="${attr(r.run_id)}"><b>${esc(r.prompt)}</b><small>${fmtInt(r.model_count)} models · ${esc(r.created_at ? new Date(r.created_at).toLocaleDateString() : '')}</small></button><button class="pg-history-delete" aria-label="Delete this run" data-cmp-del="${attr(r.run_id)}">×</button></div>`).join('');
    return `<div class="pg-page cmp-page">
      <div class="pg-page-heading"><div><div class="eyebrow">Compare providers</div><h1>Compare</h1><p>Ask OpenAI, Anthropic, Google and xAI the same question and see what was measured: answer, sources, grounding, cost and speed.</p></div></div>
      ${S.stale ? '<div class="pg-warning" role="status">Catalog prices were last verified ' + esc(S.verifiedOn || 'a while ago') + ' and may be out of date.</div>' : ''}
      ${S.error ? `<div class="pg-warning" role="alert">${esc(S.error)}</div>` : ''}
      ${glossaryHtml()}
      <div class="pg-layout">
        <aside class="pg-history card"><div class="pg-side-title"><span>Past runs</span><span>${S.runs.length}</span></div><div class="pg-history-list">${hist || '<div class="pg-history-empty">Saved comparisons appear here.</div>'}</div></aside>
        <section class="pg-workspace card cmp-work">
          <form id="cmpForm" class="cmp-form" novalidate>
            <div class="cmp-mode" role="radiogroup" aria-label="Mode">
              <label><input type="radio" name="cmpMode" value="compare" ${S.mode === 'compare' ? 'checked' : ''}> Compare side by side</label>
              <label><input type="radio" name="cmpMode" value="ask" ${S.mode === 'ask' ? 'checked' : ''}> Ask with fallback</label>
            </div>
            <p class="cmp-muted">${S.mode === 'ask' ? 'Tries your chosen models in the order selected (up to 3). If one fails for a provider reason, the next is used and the switch is shown.' : 'Sends the same question to each chosen model (1–' + esc((S.limits.max_models || 4)) + '). Nothing is substituted if a model fails.'}</p>
            <div class="pg-kicker">Models <span>${S.selected.length} selected</span></div>
            ${S.loaded ? modelPickerHtml() : '<p class="cmp-muted">Loading models…</p>'}
            <fieldset class="cmp-evid"><legend class="pg-kicker">Evidence</legend>
              <label><input type="radio" name="cmpEvid" value="none" ${S.evidence === 'none' ? 'checked' : ''}> No evidence (the model answers from its own knowledge)</label>
              <label><input type="radio" name="cmpEvid" value="atlas" ${S.evidence === 'atlas' ? 'checked' : ''}> Atlas evidence (same passages for every model)</label>
              <div class="cmp-sub" ${S.evidence === 'atlas' ? '' : 'hidden'}><label><input type="checkbox" id="cmpDocs" ${S.docs ? 'checked' : ''}> My research library</label><label><input type="checkbox" id="cmpWeb" ${S.web ? 'checked' : ''}> Web search (Tavily)</label></div>
              <label><input type="radio" name="cmpEvid" value="provider_search" ${S.evidence === 'provider_search' ? 'checked' : ''}> Each provider's own web search</label>
              <p class="cmp-muted">${S.evidence === 'provider_search' ? 'Each provider searches on its own, so each model sees different material. Differences may come from the search, not the model, so answers are not directly comparable.' : S.evidence === 'atlas' ? 'Every model receives identical passages, so differences reflect the models. Passages are wrapped as untrusted text.' : 'Without evidence, citations and grounding cannot be checked.'}</p>
            </fieldset>
            <label class="cmp-line"><input type="checkbox" id="cmpStruct" ${S.structured ? 'checked' : ''}> Ask for structured claims (answer plus each claim's sources)</label>
            <label class="cmp-line" for="cmpMaxOut">Output limit (tokens) <input class="input cmp-num" id="cmpMaxOut" type="number" min="16" step="1" inputmode="numeric" placeholder="${attr(S.limits.default_max_output_tokens || '')}" value="${attr(S.maxOut)}"></label>
            <label for="cmpPrompt" class="pg-kicker">Your question</label>
            <textarea id="cmpPrompt" maxlength="${attr(S.limits.max_prompt_chars || 6000)}" rows="3" placeholder="Ask a question…" required>${esc(S.prompt)}</textarea>
            <div class="cmp-consent"><label><input type="checkbox" id="cmpConsent" ${S.consent ? 'checked' : ''}> I understand my question${S.evidence === 'atlas' && S.docs ? ', excerpts of my research library' : ''} and any evidence will be sent to the selected third-party providers.</label>
              ${(S.disclosures || []).map(d => `<p class="cmp-muted">${esc(d)}</p>`).join('')}</div>
            ${gateHtml(g)}
            <div class="cmp-actions"><button class="btn btn-primary" type="submit" id="cmpRun" ${g.ok && !S.busy ? '' : 'disabled'}>${S.busy ? 'Running…' : (S.mode === 'ask' ? 'Ask' : 'Run comparison')}</button></div>
          </form>
          <div class="cmp-status" id="cmpStatus" role="status" aria-live="polite">${S.busy ? 'Waiting for providers. Slow models can take up to a minute.' : ''}</div>
          <div class="cmp-results" id="cmpResults">${S.busy ? '<div class="pg-output pg-pending"><header><strong>Running…</strong><span class="pg-spinner"></span></header></div>' : resultsHtml()}</div>
        </section>
      </div></div>`;
  }

  let mainEl = null;
  function refresh() {
    if (!mainEl || (window.Atlas && Atlas.state && Atlas.state.view !== 'compare')) return;
    const keep = { p: document.activeElement && document.activeElement.id };
    mainEl.innerHTML = html(); bind();
    if (keep.p) { const n = document.getElementById(keep.p); if (n && typeof n.focus === 'function') { n.focus(); } }
  }
  function updateGate() { // light refresh while typing: keep focus and caret
    const g = gateRun(S); const gate = document.getElementById('cmpGate'); if (gate) gate.outerHTML = gateHtml(g);
    const btn = document.getElementById('cmpRun'); if (btn) btn.disabled = !(g.ok && !S.busy);
  }
  function bind() {
    document.querySelectorAll('[data-cmp-model]').forEach(i => i.addEventListener('change', () => {
      const max = S.mode === 'ask' ? 3 : (S.limits.max_models || 4);
      if (i.checked) { if (S.selected.length >= max) { i.checked = false; toast(`Choose up to ${max} models.`, 'err'); return; } S.selected.push(i.value); }
      else S.selected = S.selected.filter(x => x !== i.value);
      refresh();
    }));
    document.querySelectorAll('input[name=cmpMode]').forEach(i => i.addEventListener('change', () => { S.mode = i.value; if (S.mode === 'ask') S.selected = S.selected.slice(0, 3); refresh(); }));
    document.querySelectorAll('input[name=cmpEvid]').forEach(i => i.addEventListener('change', () => { S.evidence = i.value; refresh(); }));
    const bindCheck = (id, key) => { const n = document.getElementById(id); if (n) n.addEventListener('change', () => { S[key] = n.checked; refresh(); }); };
    bindCheck('cmpDocs', 'docs'); bindCheck('cmpWeb', 'web'); bindCheck('cmpStruct', 'structured'); bindCheck('cmpConsent', 'consent');
    const p = document.getElementById('cmpPrompt'); if (p) { p.addEventListener('input', () => { S.prompt = p.value; updateGate(); }); p.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); document.getElementById('cmpForm').requestSubmit(); } }); }
    const m = document.getElementById('cmpMaxOut'); if (m) m.addEventListener('input', () => { S.maxOut = m.value; updateGate(); });
    const f = document.getElementById('cmpForm'); if (f) f.addEventListener('submit', submit);
    document.querySelectorAll('[data-cmp-open]').forEach(b => b.addEventListener('click', () => openRun(b.dataset.cmpOpen)));
    document.querySelectorAll('[data-cmp-del]').forEach(b => b.addEventListener('click', () => delRun(b.dataset.cmpDel)));
  }
  function buildBody() {
    const body = { prompt: S.prompt.trim(), models: S.selected.slice(), evidence_mode: S.evidence,
      include_documents: S.evidence === 'atlas' && S.docs, include_web: S.evidence === 'atlas' && S.web,
      structured: S.structured, consent_external: S.consent === true, idempotency_key: S.key || newIdempotencyKey() };
    const mo = num(S.maxOut); if (mo !== null && mo > 0) body.max_output_tokens = Math.floor(mo);
    return body;
  }
  async function submit(ev) {
    ev.preventDefault();
    if (S.busy) return;
    const g = gateRun(S);
    if (!g.ok) { S.error = g.reasons[0]; refresh(); return; }
    S.key = newIdempotencyKey(); // one key per submit; a repeat of the same request body is the server's to dedupe
    const body = buildBody(); S.busy = true; S.error = null; S.run = null; S.ask = null; refresh();
    try {
      const res = await apiPost(S.mode === 'ask' ? '/compare/ask' : '/compare/run', body);
      const d = res && res.data; if (!d) throw new Error('Compare returned an empty response.');
      if (S.mode === 'ask') S.ask = d; else S.run = d;
      loadRuns();
    } catch (e) { S.error = (e && e.message) || 'The request failed.'; }
    finally { S.busy = false; S.key = null; refresh(); }
  }
  async function loadRuns() {
    try { const r = await apiGet('/compare/runs'); S.runs = (r && r.data && r.data.items) || []; refresh(); } catch (_) { /* history is optional */ }
  }
  async function openRun(id) {
    try { const r = await apiGet('/compare/runs/' + encodeURIComponent(id)); S.run = r && r.data; S.ask = null; S.mode = 'compare'; S.error = null; } catch (e) { S.error = e.message || 'Could not open run.'; }
    refresh();
  }
  async function delRun(id) {
    try { await apiDelete('/compare/runs/' + encodeURIComponent(id), { quietStatuses: [404] }); } catch (e) { if (e.message !== 'Run not found') { S.error = e.message || 'Could not delete.'; } }
    S.runs = S.runs.filter(r => r.run_id !== id); if (S.run && S.run.run_id === id) S.run = null; refresh();
  }
  async function load() {
    try {
      const [m, e] = await Promise.all([apiGet('/compare/models'), apiGet('/compare/evaluations').catch(() => null)]);
      const d = (m && m.data) || {};
      S.models = d.models || []; S.providers = d.providers || []; S.limits = d.limits || {}; S.disclosures = d.disclosures || [];
      S.stale = !!d.catalog_stale; S.verifiedOn = d.catalog_verified_on || null; S.evals = (e && e.data) || null;
      if (!S.selected.length) S.selected = S.models.filter(x => x.default && x.configured && x.available !== false).map(x => x.id).slice(0, S.limits.max_models || 4);
      S.loaded = true; S.error = null;
    } catch (err) { S.error = (err && err.message) || 'Compare could not load.'; S.loaded = true; }
    refresh(); loadRuns();
  }

  window.AtlasCompare = {
    render(main, ctx) { if (ctx) ctx.innerHTML = ''; mainEl = main; main.innerHTML = html(); bind(); if (!S.loaded) load(); },
    // pure helpers (also used by tests)
    formatUsd: fmtUsd, formatInt: fmtInt, formatMs: fmtMsv, formatRate: fmtRate,
    safeUrl, safeMarkdown, newIdempotencyKey, estimateRunCost, gateRun, explainError,
    resultCardHtml, summaryHtml, evaluationHtml, sourcesHtml, citationsHtml, groundingHtml, glossaryHtml, GLOSSARY,
    _state: S,
  };
})();
