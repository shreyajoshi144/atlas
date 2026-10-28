/* ══════════════════════════════════════════════════════════════════════
   KNOWLEDGE MAP (M6)
   Explore Entity → Claim → Evidence → Source, see confidence / verification /
   supporting vs conflicting evidence / freshness / last-verified, compare two
   investigations ("What changed?"), and see the explicit knowledge state
   (complete / partial / failed / not recorded) of every investigation.

   Structure: pure helpers first (no DOM, unit-tested under Node), then views.
   All dynamic text goes through esc(); interactions use data-* attributes and
   one delegated listener — no inline JS built from data.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const esc = (typeof escapeHtml === 'function') ? escapeHtml : function (s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  };

  /* ── pure helpers ─────────────────────────────────────────────────── */
  const STATE_META = {
    complete:     { label: 'Complete',     cls: 'badge-jade',       hint: 'All knowledge for this investigation was saved.' },
    partial:      { label: 'Partial',      cls: 'badge-amber',      hint: 'Saved, but some inputs were dropped. Treat as incomplete.' },
    failed:       { label: 'Failed',       cls: 'badge-terracotta', hint: 'Saving failed. Anything shown may be out of date — rebuild it.' },
    not_recorded: { label: 'Not recorded', cls: 'badge-neutral',    hint: 'No knowledge has been saved for this investigation yet.' },
  };
  const STATUS_META = {
    verified:              { label: 'Verified',              cls: 'badge-jade' },
    partially_verified:    { label: 'Partially verified',    cls: 'badge-sage' },
    insufficient_evidence: { label: 'Insufficient evidence', cls: 'badge-neutral' },
    not_verified:          { label: 'Not verified',          cls: 'badge-amber' },
    conflicting:           { label: 'Conflicting',           cls: 'badge-terracotta' },
  };
  const FRESH_META = {
    fresh:   { label: 'Fresh',   cls: 'badge-jade' },
    aging:   { label: 'Aging',   cls: 'badge-amber' },
    old:     { label: 'Old',     cls: 'badge-terracotta' },
    unknown: { label: 'Date unknown', cls: 'badge-neutral' },
  };
  const STALE_TEXT = { age: 'Not re-verified recently', sources_old: 'All supporting sources are old', source_changed: 'A supporting source has changed' };
  const COLUMN = { entity: 0, claim: 1, evidence: 2, source: 3, challenge: 4, counter: 5 };
  const COLUMN_LABEL = ['Entities', 'Claims', 'Evidence', 'Sources', 'Challenges', 'Counter-evidence'];
  const CHALLENGE_TYPES = [
    ['counter_evidence', 'Counter-evidence', 'Look for evidence against the claim'],
    ['contradiction_search', 'Contradiction search', 'Search for sources that contradict it'],
    ['source_quality', 'Source quality', 'Check how reliable and independent its sources are'],
    ['temporal_validity', 'Temporal validity', 'Check whether it is still current'],
    ['assumption_check', 'Assumption check', 'Test the assumptions it depends on'],
    ['alternative_explanation', 'Alternative explanation', 'Look for other explanations'],
  ];
  const RESULT_META = {
    supported: { label: 'Supported', cls: 'badge-jade' }, weakened: { label: 'Weakened', cls: 'badge-amber' },
    contradicted: { label: 'Contradicted', cls: 'badge-terracotta' }, unresolved: { label: 'Unresolved', cls: 'badge-neutral' },
    stale: { label: 'Stale', cls: 'badge-brass' }, source_concern: { label: 'Source concern', cls: 'badge-amber' },
  };
  const CH_STATUS_META = {
    pending: { label: 'Pending', cls: 'badge-neutral' }, running: { label: 'Running', cls: 'badge-sage' },
    completed: { label: 'Completed', cls: 'badge-jade' }, failed: { label: 'Failed', cls: 'badge-terracotta' },
  };
  const ROLE_META = { counter: { label: 'Counter-evidence', cls: 'badge-terracotta' }, supporting: { label: 'Supporting', cls: 'badge-jade' }, qualifying: { label: 'Qualifying', cls: 'badge-amber' } };
  const TRAIL_LABEL = { claim: 'Claim', evidence: 'Evidence', challenge: 'Challenge', counter_evidence: 'Counter-evidence', result: 'Result', updated_interpretation: 'Updated interpretation' };

  function stateMeta(state) { return STATE_META[state] || STATE_META.not_recorded; }
  function badge(meta, extra) { return `<span class="badge ${meta.cls}"${extra || ''}>${esc(meta.label)}</span>`; }
  function stateBadge(state) { const m = stateMeta(state); return badge(m, ` title="${esc(m.hint)}"`); }
  function statusBadge(status) { return badge(STATUS_META[status] || { label: String(status || 'Unknown'), cls: 'badge-neutral' }); }
  function freshnessBadge(f) {
    const m = FRESH_META[(f && f.label) || 'unknown'] || FRESH_META.unknown;
    const age = f && f.age_days !== null && f.age_days !== undefined ? ` · ${f.age_days}d` : '';
    return `<span class="badge ${m.cls}">${esc(m.label)}${esc(age)}</span>`;
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d) ? '—' : d.toISOString().slice(0, 10);
  }
  function pct(x) { return x === null || x === undefined ? '—' : Math.round(Number(x) * 100) + '%'; }
  function confidenceBar(c) {
    const v = Math.max(0, Math.min(1, Number(c) || 0));
    return `<span class="km-conf" title="Confidence ${pct(v)}"><span class="km-conf-fill" style="width:${Math.round(v * 100)}%"></span></span><span class="km-conf-num">${pct(v)}</span>`;
  }
  function staleReasonText(reason) { return STALE_TEXT[reason.code] || reason.message || reason.code; }
  function isUntrusted(state) { return state !== 'complete'; }

  /** Deterministic 4-column layout: Entity | Claim | Evidence | Source. */
  function layoutGraph(nodes, edges, opts) {
    const o = Object.assign({ colW: 230, rowH: 54, padX: 16, padY: 34, nodeW: 190, nodeH: 38 }, opts || {});
    const cols = [[], [], [], [], [], []];
    (nodes || []).forEach(n => cols[COLUMN[n.type] !== undefined ? COLUMN[n.type] : 1].push(n));
    const placed = {};
    cols.forEach((col, ci) => {
      col.sort((a, b) => String(a.label).localeCompare(String(b.label)) || String(a.id).localeCompare(String(b.id)));
      col.forEach((n, ri) => { placed[n.id] = Object.assign({}, n, { x: o.padX + ci * o.colW, y: o.padY + ri * o.rowH, w: o.nodeW, h: o.nodeH }); });
    });
    const out = Object.values(placed);
    const laidEdges = (edges || []).filter(e => placed[e.source] && placed[e.target]).map(e => {
      const a = placed[e.source], b = placed[e.target];
      const left = a.x <= b.x ? a : b, right = a.x <= b.x ? b : a;
      return Object.assign({}, e, { x1: left.x + left.w, y1: left.y + left.h / 2, x2: right.x, y2: right.y + right.h / 2 });
    });
    const maxRows = Math.max(1, ...cols.map(c => c.length));
    const maxCol = Math.max(3, ...out.map(n => COLUMN[n.type] !== undefined ? COLUMN[n.type] : 1));
    return { nodes: out, edges: laidEdges, width: o.padX * 2 + maxCol * o.colW + o.nodeW, height: o.padY + maxRows * o.rowH + 12 };
  }

  const CHANGE_SECTIONS = [
    ['contradicted', 'Contradicted', 'badge-terracotta'], ['weakened', 'Weakened', 'badge-amber'],
    ['strengthened', 'Strengthened', 'badge-jade'], ['new', 'New', 'badge-sage'],
    ['stale', 'Possibly stale', 'badge-brass'], ['not_reobserved', 'Not re-observed', 'badge-neutral'],
    ['unchanged', 'Unchanged', 'badge-neutral'],
  ];

  /* ── state ────────────────────────────────────────────────────────── */
  const KM = {
    tab: 'explore', summary: null, investigations: [],
    sel: { entity: null, claim: null, evidence: null, source: null },
    entities: null, entityDetail: null, claimDetail: null, evidenceDetail: null,
    claimsFilter: { query: '', status: '', stale: false, conflicting: false },
    graph: null, showGraph: false, diff: null, diffError: null, base: '', target: '',
    challengeDetail: null, challengeBusy: false, challengeType: 'counter_evidence',
  };
  const root = () => document.getElementById('kmRoot');
  const API = '/knowledge-map';
  const get = (path, params) => apiGet(API + path, params).then(r => (r && r.success ? r.data : null));

  /* ── views ────────────────────────────────────────────────────────── */
  function viewKnowledge(main, ctx, opts) {
    main.innerHTML = `
      <div class="page">
        <div class="crumbs">${crumbsHtml('knowledge')}</div>
        <div class="page-title">Knowledge</div>
        <div class="page-desc">What Atlas knows across all your investigations — claims, the evidence behind them, and where it came from.</div>
        <div class="hr"></div>
        <div id="kmRoot">${skeletonBlock('90px')}${skeletonBlock('260px')}</div>
      </div>`;
    ctx.innerHTML = '';
    root().addEventListener('click', onClick);
    root().addEventListener('input', onInput);
    root().addEventListener('change', onChange);
    root().addEventListener('keydown', onKeyboardActivate);
    if (opts && opts.tab) KM.tab = opts.tab;
    loadAll();
  }

  async function loadAll() {
    try {
      const [summary, inv] = await Promise.all([get('/summary'), get('/investigations')]);
      KM.summary = summary; KM.investigations = inv ? inv.items : [];
      KM.entities = await get('/entities', { limit: 50 });
      draw();
    } catch (e) { root().innerHTML = emptyState('⚠', 'Could not load your knowledge map', ''); }
  }

  function banner(summary) {
    const s = summary === undefined ? KM.summary : summary; if (!s) return '';
    const inv = s.investigations;
    const tiles = [['Entities', s.counts.entities], ['Claims', s.counts.claims], ['Evidence', s.counts.evidence], ['Sources', s.counts.sources],
                   ['Possibly stale', s.stale_claims], ['Conflicting', s.conflicting_claims]]
      .map(([k, v]) => `<div class="stat-tile km-tile"><div class="field-label">${esc(k)}</div><div class="km-tile-num">${esc(v)}</div></div>`).join('');
    let warn = '';
    if (inv.total === 0) {
      warn = `<div class="km-banner km-banner-neutral">No investigations yet. Run a research investigation and its knowledge will appear here.</div>`;
    } else if (!s.map_is_complete) {
      const parts = ['failed', 'partial', 'not_recorded'].filter(k => inv[k] > 0).map(k => `${inv[k]} ${stateMeta(k).label.toLowerCase()}`);
      warn = `<div class="km-banner km-banner-warn" role="alert"><strong>This knowledge map is not complete.</strong> ${esc(parts.join(', '))}.
        Items from those investigations are marked and may be out of date. <a href="#" data-act="tab" data-tab="investigations">Review investigations</a></div>`;
    }
    const integrity = s.integrity && !s.integrity.ok ? `<div class="km-banner km-banner-warn" role="alert">Integrity check found ${esc(s.integrity.problem_count)} problem(s) in the stored graph.</div>` : '';
    return `<div class="grid grid-6 km-tiles">${tiles}</div>${warn}${integrity}`;
  }

  function draw() {
    const tabs = [['explore', 'Explore'], ['claims', 'Claims'], ['changes', 'What changed'], ['investigations', 'Investigations']];
    root().innerHTML = `
      ${banner()}
      <div class="km-tabs" role="tablist">${tabs.map(([k, l]) => `<button class="km-tab${KM.tab === k ? ' active' : ''}" role="tab" aria-selected="${KM.tab === k}" data-act="tab" data-tab="${k}">${l}</button>`).join('')}</div>
      <div id="kmBody"></div>`;
    ({ explore: drawExplore, claims: drawClaims, changes: drawChanges, investigations: drawInvestigations }[KM.tab] || drawExplore)();
  }

  /* Explore: four drill-down columns + detail */
  function entityRow(e) {
    return `<div class="km-row${KM.sel.entity === e.entity_id ? ' sel' : ''}" data-act="entity" data-id="${esc(e.entity_id)}" tabindex="0" role="button">
      <div class="km-row-title">${esc(e.name)}</div>
      <div class="km-row-sub">${esc(e.claim_count)} claim${e.claim_count === 1 ? '' : 's'} · ${esc(e.source_count)} source${e.source_count === 1 ? '' : 's'} ${isUntrusted(e.graph_state) ? stateBadge(e.graph_state) : ''}</div></div>`;
  }
  function claimRow(c) {
    const stale = c.stale && c.stale.is_stale ? `<span class="badge badge-brass" title="${esc(c.stale.reasons.map(staleReasonText).join('; '))}">Possibly stale</span>` : '';
    const cf = c.evidence_counts.conflicting ? `<span class="badge badge-terracotta">${esc(c.evidence_counts.conflicting)} conflicting</span>` : '';
    return `<div class="km-row${KM.sel.claim === c.claim_id ? ' sel' : ''}" data-act="claim" data-id="${esc(c.claim_id)}" tabindex="0" role="button">
      <div class="km-row-title">${esc(c.text)}</div>
      <div class="km-row-sub">${statusBadge(c.status)} ${confidenceBar(c.confidence)} ${stale} ${cf} ${challengedBadge(c)} ${isUntrusted(c.graph_state) ? stateBadge(c.graph_state) : ''}</div></div>`;
  }
  function evidenceRow(e, group) {
    const cls = group === 'conflicting' ? 'km-ev-conflict' : group === 'supporting' ? 'km-ev-support' : 'km-ev-mixed';
    const changed = e.source.changed_since_verified ? '<span class="badge badge-amber">Source changed since</span>' : '';
    return `<div class="km-row ${cls}${KM.sel.evidence === e.evidence_id ? ' sel' : ''}" data-act="evidence" data-id="${esc(e.evidence_id)}" tabindex="0" role="button">
      <div class="km-row-title">“${esc(e.passage)}”</div>
      <div class="km-row-sub">${esc(e.source.domain || '')} ${freshnessBadge(e.source.freshness)} ${changed} <span class="km-strength">strength ${pct(e.strength)}</span></div></div>`;
  }

  function drawExplore() {
    const body = document.getElementById('kmBody');
    const ents = KM.entities ? KM.entities.items : [];
    const claims = KM.entityDetail ? KM.entityDetail.claims : null;
    const ev = KM.claimDetail ? KM.claimDetail.evidence : null;
    const evCol = ev ? ['supporting', 'conflicting', 'mixed'].map(g => ev[g].length
      ? `<div class="km-group-label">${g === 'supporting' ? 'Supporting' : g === 'conflicting' ? 'Conflicting' : 'Mixed / neutral'} (${ev[g].length})</div>${ev[g].map(e => evidenceRow(e, g)).join('')}` : '').join('') || '<div class="km-empty">No evidence recorded for this claim.</div>' : '<div class="km-empty">Select a claim.</div>';
    body.innerHTML = `
      <div class="km-toolbar">
        <input class="input" id="kmEntitySearch" placeholder="Search entities…" maxlength="200" aria-label="Search entities" />
        <button class="btn btn-ghost" data-act="graph" ${KM.sel.entity || KM.sel.claim ? '' : 'disabled'}>${KM.showGraph ? 'Hide graph' : 'Show graph'}</button>
      </div>
      <div class="km-cols">
        <div class="km-col"><div class="km-col-head">${COLUMN_LABEL[0]}</div><div id="kmEntities">${ents.length ? ents.map(entityRow).join('') : '<div class="km-empty">No entities yet.</div>'}</div></div>
        <div class="km-col"><div class="km-col-head">${COLUMN_LABEL[1]}</div>${claims ? (claims.length ? claims.map(claimRow).join('') : '<div class="km-empty">No claims.</div>') : '<div class="km-empty">Select an entity.</div>'}</div>
        <div class="km-col"><div class="km-col-head">${COLUMN_LABEL[2]}</div>${evCol}</div>
        <div class="km-col"><div class="km-col-head">${COLUMN_LABEL[3]}</div>${sourcePanel()}</div>
      </div>
      ${KM.showGraph ? graphPanel() : ''}
      ${claimDetailPanel()}`;
  }

  function sourcePanel() {
    const d = KM.evidenceDetail; if (!d) return '<div class="km-empty">Select evidence.</div>';
    const s = d.source;
    return `<div class="km-card"><div class="km-row-title">${esc(s.title || s.domain || 'Source')}</div>
      <div class="km-row-sub"><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.domain || s.url)}</a></div>
      <div class="km-kv"><span>Freshness</span><span>${freshnessBadge(s.freshness)}</span></div>
      <div class="km-kv"><span>Published</span><span>${esc(fmtDate(s.publish_date))}</span></div>
      <div class="km-kv"><span>Last seen</span><span>${esc(fmtDate(s.last_seen_at))}</span></div>
      <div class="km-kv"><span>Supports</span><span>${esc(d.claims.length)} claim${d.claims.length === 1 ? '' : 's'}</span></div>
      <div class="km-kv"><span>Knowledge state</span><span>${stateBadge(d.graph_state)}</span></div></div>`;
  }

  /* ── challenge builders (pure) ────────────────────────────────────── */
  function challengeTypeLabel(t) { const m = CHALLENGE_TYPES.find(x => x[0] === t); return m ? m[1] : String(t || 'Challenge'); }
  function resultBadge(r) { return badge(RESULT_META[r] || { label: String(r || 'Unknown'), cls: 'badge-neutral' }); }
  function challengeStatusBadge(s) { return badge(CH_STATUS_META[s] || { label: String(s || 'Unknown'), cls: 'badge-neutral' }); }
  function interpretationHtml(i) {
    if (!i || i.label === 'unchallenged') return '<div class="km-kv"><span>Challenges</span><span class="muted">Not challenged yet</span></div>';
    return `<div class="km-interp"><div class="field-label">UPDATED INTERPRETATION</div>
      <div>${resultBadge(i.label)} <span>${esc(i.summary)}</span></div>
      <div class="small muted">Original verification: ${esc((STATUS_META[i.original_status] || {}).label || i.original_status)} · ${pct(i.original_confidence)} →
        adjusted confidence ${pct(i.adjusted_confidence)} (a heuristic view; the original record is unchanged)</div></div>`;
  }
  function challengeBoxHtml(c) {
    const invId = (c.investigation && c.investigation.report_id) || '';
    const canRun = (c.graph_state === 'complete' || c.graph_state === 'partial') && !!invId;
    const opts = CHALLENGE_TYPES.map(([k, l, h]) => `<option value="${k}" title="${esc(h)}"${KM.challengeType === k ? ' selected' : ''}>${esc(l)}</option>`).join('');
    const past = c.challenges_detail || [];
    const rows = past.map(ch => `<div class="km-row" data-act="challenge-open" data-id="${esc(ch.challenge_id)}" tabindex="0" role="button">
      <div class="km-row-title">${esc(challengeTypeLabel(ch.challenge_type))}</div>
      <div class="km-row-sub">${challengeStatusBadge(ch.status)} ${ch.result ? resultBadge(ch.result) : ''} ${esc(fmtDate(ch.completed_at || ch.created_at))}
        ${ch.counter_evidence_count ? `<span class="badge badge-terracotta">${esc(ch.counter_evidence_count)} counter</span>` : ''}</div></div>`).join('');
    const control = canRun
      ? `<div class="km-toolbar"><select class="input" id="kmChallengeType" aria-label="Challenge type">${opts}</select>
         <button class="btn btn-primary" data-act="challenge-run" data-id="${esc(c.claim_id)}" data-inv="${esc(invId)}"${KM.challengeBusy ? ' disabled' : ''}>${KM.challengeBusy ? 'Running…' : 'Run challenge'}</button></div>`
      : `<div class="km-banner km-banner-warn" role="alert">Knowledge for this investigation is ${esc(stateMeta(c.graph_state).label.toLowerCase())}. Rebuild it before challenging this claim.</div>`;
    return `<div class="km-challenge-box"><div class="field-label" style="margin-top:14px;">CHALLENGE THIS CLAIM</div>${control}
      ${KM.challengeBusy ? '<div class="small muted">Searching for independent evidence and verifying it — this can take up to a minute.</div>' : ''}
      ${past.length ? `<div class="km-group-label">Past challenges (${past.length})</div>${rows}` : ''}
      ${canRun ? benchBoxHtml(c, invId, past.some(ch => ch.status === 'completed')) : ''}
      <div id="kmChallengeDetail">${KM.challengeDetail ? challengeInspectHtml(KM.challengeDetail) : ''}</div></div>`;
  }
  /** AtlasBench: freeze this claim (and its completed challenges' evidence) into a benchmark task. */
  function benchBoxHtml(c, invId, hasChallenges) {
    return `<div class="km-toolbar" style="margin-top:12px;"><button class="btn" data-act="bench-claim" data-id="${esc(c.claim_id)}" data-inv="${esc(invId)}" data-ch="${hasChallenges ? '1' : '0'}"
      title="Freeze this claim and its evidence into a task you can test different models on">Benchmark this claim</button>
      <span class="small muted">${hasChallenges ? 'Includes the evidence from its completed challenges.' : 'Uses the evidence Atlas already holds for it.'}</span></div>`;
  }
  function trailStep(s) {
    if (s.step === 'claim') return `${esc(s.text)} ${statusBadge(s.status)} ${confidenceBar(s.confidence)}`;
    if (s.step === 'evidence') return s.items ? `${esc(s.items.length)} source(s) the claim relied on` : `${esc(s.supporting)} supporting · ${esc(s.conflicting)} conflicting`;
    if (s.step === 'challenge') return `${esc(challengeTypeLabel(s.challenge_type))}${s.queries && s.queries.length ? `<div class="small muted">${s.queries.map(esc).join(' · ')}</div>` : ''}`;
    if (s.step === 'counter_evidence') {
      const by = (r) => s.items.filter(i => (i.role || ({ contradict: 'counter', support: 'supporting' }[i.stance] || 'qualifying')) === r).length;
      return `${esc(by('counter'))} counter · ${esc(by('supporting'))} supporting · ${esc(by('qualifying'))} qualifying`;
    }
    if (s.step === 'result') return `${s.value ? resultBadge(s.value) : '<span class="muted">pending</span>'} ${s.rationale ? esc(s.rationale) : ''}`;
    if (s.step === 'updated_interpretation') return `${resultBadge(s.label)} ${esc(s.summary)} <span class="small muted">adjusted confidence ${pct(s.adjusted_confidence)}</span>`;
    return '';
  }
  function trailHtml(trail) {
    return `<ol class="km-trail">${(trail || []).map(s => `<li class="km-trail-step"><span class="km-trail-name">${esc(TRAIL_LABEL[s.step] || s.step)}</span><div>${trailStep(s)}</div></li>`).join('')}</ol>`;
  }
  function challengeInspectHtml(d) {
    const failed = d.status === 'failed';
    const failure = failed ? `<div class="km-banner km-banner-warn" role="alert"><strong>This challenge failed</strong> (${esc(d.error_type || 'unknown error')}); no result was recorded.
      ${d.retryable ? `<button class="btn btn-ghost" data-act="challenge-retry" data-id="${esc(d.challenge_id)}"${KM.challengeBusy ? ' disabled' : ''}>Retry</button>` : 'The maximum number of attempts was reached — start a new challenge.'}</div>` : '';
    const ev = (d.evidence || []).map(e => {
      const role = ROLE_META[e.role] || ROLE_META.qualifying;
      return `<div class="km-row km-ev-${e.role === 'counter' ? 'conflict' : e.role === 'supporting' ? 'support' : 'mixed'}"><div class="km-row-title">“${esc(e.passage)}”</div>
        <div class="km-row-sub">${badge(role)} <a href="${esc(e.source.url)}" target="_blank" rel="noopener noreferrer">${esc(e.source.domain)}</a>
        ${e.source.independent ? '<span class="badge badge-sage">Independent source</span>' : '<span class="badge badge-amber">Same domain as original</span>'}
        <span class="km-strength">strength ${pct(e.strength)}</span> ${esc(fmtDate(e.source.publish_date))}</div></div>`;
    }).join('');
    const attempts = (d.attempts || []).map(a => `<tr><td>${esc(a.attempt_no)}</td><td>${challengeStatusBadge(a.status)}</td><td>${esc(fmtDate(a.started_at))}</td><td>${esc(fmtDate(a.finished_at))}</td><td>${esc(a.error_type || '')}</td></tr>`).join('');
    const dropped = d.dropped_unverified_evidence ? `<div class="small muted">${esc(d.dropped_unverified_evidence)} item(s) were discarded because they did not cite a page that was actually retrieved.</div>` : '';
    return `<div class="km-challenge-detail card"><div class="field-label">${esc(challengeTypeLabel(d.challenge_type)).toUpperCase()} · ${challengeStatusBadge(d.status)} ${d.result ? resultBadge(d.result) : ''}</div>
      ${failure}${d.status === 'completed' ? trailHtml(d.trail) : ''}
      ${d.evidence && d.evidence.length ? `<div class="km-group-label">Evidence found (${d.evidence.length}) — every item cites a retrieved source</div>${ev}` : (d.status === 'completed' ? '<div class="km-empty">No evidence was found that bears on this claim.</div>' : '')}
      ${dropped}<div class="small muted" style="margin-top:6px;">${esc((d.sources || []).length)} source(s) retrieved</div>
      <div class="km-group-label">Attempts</div><table class="km-table"><thead><tr><th>#</th><th>Status</th><th>Started</th><th>Finished</th><th>Error</th></tr></thead><tbody>${attempts}</tbody></table></div>`;
  }

  function challengedBadge(c) {
    const i = c.interpretation;
    return i && i.label !== 'unchallenged' ? `<span class="badge ${(RESULT_META[i.label] || {}).cls || 'badge-neutral'}" title="${esc(i.summary)}">Challenged: ${esc((RESULT_META[i.label] || {}).label || i.label)}</span>` : '';
  }

  function claimDetailPanel() {
    const c = KM.claimDetail; if (!c) return '';
    const stale = c.stale.is_stale
      ? `<div class="km-banner km-banner-warn"><strong>Possibly stale.</strong> ${esc(c.stale.reasons.map(staleReasonText).join(' · '))}</div>` : '';
    const untrusted = isUntrusted(c.graph_state)
      ? `<div class="km-banner km-banner-warn" role="alert"><strong>Knowledge state: ${esc(stateMeta(c.graph_state).label)}.</strong> ${esc(stateMeta(c.graph_state).hint)}</div>` : '';
    const hist = c.history.map(h => `<tr><td>${esc(fmtDate(h.observed_at))}</td><td>${esc(h.topic)}</td><td>${statusBadge(h.status)}</td><td>${confidenceBar(h.confidence)}</td><td>${stateBadge(h.graph_state)}</td></tr>`).join('');
    return `<div class="km-detail card">
      <div class="field-label">CLAIM</div><div class="km-claim-text">${esc(c.text)}</div>
      ${untrusted}${stale}
      <div class="km-kv"><span>Verification</span><span>${statusBadge(c.status)}</span></div>
      <div class="km-kv"><span>Confidence</span><span>${confidenceBar(c.confidence)}</span></div>
      <div class="km-kv"><span>Evidence</span><span>${esc(c.evidence_counts.supporting)} supporting · ${esc(c.evidence_counts.conflicting)} conflicting · ${esc(c.evidence_counts.mixed)} mixed</span></div>
      <div class="km-kv"><span>Last verified</span><span>${esc(fmtDate(c.last_verified_at))}</span></div>
      <div class="km-kv"><span>First seen</span><span>${esc(fmtDate(c.first_seen_at))}</span></div>
      <div class="km-kv"><span>Knowledge state</span><span>${stateBadge(c.graph_state)}</span></div>
      ${c.entities.length ? `<div class="km-kv"><span>Entities</span><span>${c.entities.map(e => `<a href="#" data-act="entity" data-id="${esc(e.entity_id)}" class="chip">${esc(e.name)}</a>`).join(' ')}</span></div>` : ''}
      ${interpretationHtml(c.interpretation)}
      ${challengeBoxHtml(c)}
      <div class="field-label" style="margin-top:14px;">HISTORY</div>
      <table class="km-table"><thead><tr><th>Date</th><th>Investigation</th><th>Status</th><th>Confidence</th><th>State</th></tr></thead><tbody>${hist}</tbody></table>
    </div>`;
  }

  function graphPanel() {
    const g = KM.graph;
    if (!g) return '<div class="km-empty">Loading graph…</div>';
    const L = layoutGraph(g.nodes, g.edges);
    const edgeSvg = L.edges.map(e => {
      const mx = (e.x1 + e.x2) / 2;
      return `<path class="km-edge km-edge-${esc(e.type)}" d="M${e.x1},${e.y1} C${mx},${e.y1} ${mx},${e.y2} ${e.x2},${e.y2}" fill="none"><title>${esc(e.type.replace('_', ' '))}</title></path>`;
    }).join('');
    const nodeSvg = L.nodes.map(n => `<g class="km-node km-node-${esc(n.type)} km-st-${esc(n.graph_state)}${n.id === g.focus ? ' focus' : ''}" data-act="node" data-id="${esc(n.id)}" data-type="${esc(n.type)}" tabindex="0" role="button">
      <rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="6"/><text x="${n.x + 8}" y="${n.y + 16}">${esc(String(n.label).slice(0, 28))}</text>
      <text class="km-node-sub" x="${n.x + 8}" y="${n.y + 30}">${esc(n.type)}${isUntrusted(n.graph_state) ? ' · ' + esc(n.graph_state) : ''}</text><title>${esc(n.label)}</title></g>`).join('');
    const used = new Set(L.nodes.map(n => COLUMN[n.type]));
    const heads = COLUMN_LABEL.map((l, i) => used.has(i) || i < 4 ? `<text class="km-colhead" x="${16 + i * 230}" y="18">${l}</text>` : '').join('');
    return `<div class="km-graph card"><div class="field-label">GRAPH${g.truncated ? ' · truncated to fit' : ''}</div>
      <div class="km-graph-scroll"><svg viewBox="0 0 ${L.width} ${L.height}" width="${L.width}" height="${L.height}" role="img" aria-label="Knowledge graph">${heads}${edgeSvg}${nodeSvg}</svg></div></div>`;
  }

  /* Claims tab */
  async function drawClaims() {
    const body = document.getElementById('kmBody');
    const f = KM.claimsFilter;
    body.innerHTML = `<div class="km-toolbar">
      <input class="input" id="kmClaimQuery" placeholder="Search claims…" value="${esc(f.query)}" maxlength="200" aria-label="Search claims" />
      <select class="input" id="kmClaimStatus" aria-label="Verification status"><option value="">Any status</option>${Object.keys(STATUS_META).map(k => `<option value="${k}"${f.status === k ? ' selected' : ''}>${esc(STATUS_META[k].label)}</option>`).join('')}</select>
      <label class="km-check"><input type="checkbox" id="kmClaimStale"${f.stale ? ' checked' : ''}/> Possibly stale</label>
      <label class="km-check"><input type="checkbox" id="kmClaimConflict"${f.conflicting ? ' checked' : ''}/> Has conflicting evidence</label></div>
      <div id="kmClaimList">${skeletonBlock('160px')}</div>`;
    const params = { limit: 50, query: f.query || undefined, status: f.status || undefined, stale: f.stale ? true : undefined, conflicting: f.conflicting ? true : undefined };
    try {
      const res = await get('/claims', params);
      const list = document.getElementById('kmClaimList'); if (!list) return;
      list.innerHTML = res && res.items.length ? `<div class="small muted" style="margin-bottom:8px;">${esc(res.total)} claim${res.total === 1 ? '' : 's'}</div>${res.items.map(claimRow).join('')}` : '<div class="km-empty">No claims match.</div>';
    } catch (e) { const l = document.getElementById('kmClaimList'); if (l) l.innerHTML = '<div class="km-empty">Could not load claims.</div>'; }
  }

  /* What changed */
  function drawChanges() {
    const body = document.getElementById('kmBody');
    const opt = (sel) => KM.investigations.map(i => `<option value="${esc(i.report_id)}"${sel === i.report_id ? ' selected' : ''}${i.comparable ? '' : ' disabled'}>${esc(i.topic)} · ${esc(fmtDate(i.created_at))} · ${esc(stateMeta(i.knowledge_state).label)}</option>`).join('');
    body.innerHTML = `<div class="km-toolbar">
        <label class="km-sel"><span class="field-label">BASE (older)</span><select class="input" id="kmBase"><option value="">Choose…</option>${opt(KM.base)}</select></label>
        <label class="km-sel"><span class="field-label">TARGET (newer)</span><select class="input" id="kmTarget"><option value="">Choose…</option>${opt(KM.target)}</select></label>
        <button class="btn btn-primary" data-act="compare" ${KM.base && KM.target ? '' : 'disabled'}>Compare</button></div>
      <div class="small muted" style="margin-bottom:10px;">Investigations whose knowledge failed to save, or was never saved, can't be compared until rebuilt.</div>
      <div id="kmDiff">${diffHtml()}</div>`;
  }
  function diffHtml() {
    if (KM.diffError) return `<div class="km-banner km-banner-warn" role="alert">${esc(KM.diffError)}</div>`;
    const d = KM.diff; if (!d) return '<div class="km-empty">Pick two investigations to see what changed.</div>';
    const s = d.summary;
    const warn = d.warnings.map(w => `<div class="km-banner km-banner-warn" role="alert">${esc(w)}</div>`).join('');
    const chips = CHANGE_SECTIONS.map(([k, l, cls]) => `<span class="badge ${cls}">${esc(l)} ${esc(s[k])}</span>`).join(' ');
    const sections = CHANGE_SECTIONS.map(([k, label]) => {
      const items = d.claims[k]; if (!items.length) return '';
      return `<div class="km-group-label">${esc(label)} (${items.length})</div>${items.map(it => changeRow(k, it)).join('')}`;
    }).join('');
    const ev = d.evidence, extra = [];
    if (ev.added.length) extra.push(`${ev.added.length} evidence item(s) added`);
    if (ev.removed.length) extra.push(`${ev.removed.length} removed`);
    if (ev.stance_changed.length) extra.push(`${ev.stance_changed.length} changed stance`);
    if (ev.stale.length) extra.push(`${ev.stale.length} stale`);
    if (d.sources.changed.length) extra.push(`${d.sources.changed.length} source(s) changed content`);
    return `${warn}<div class="km-diff-head">${esc(d.base.topic)} <span class="muted">${esc(fmtDate(d.base.created_at))}</span> → ${esc(d.target.topic)} <span class="muted">${esc(fmtDate(d.target.created_at))}</span></div>
      <div class="km-chips">${chips}</div>${extra.length ? `<div class="small muted" style="margin:8px 0;">${esc(extra.join(' · '))}</div>` : ''}${sections || '<div class="km-empty">No claim-level differences.</div>'}`;
  }
  function changeRow(kind, it) {
    const before = it.base ? `${esc(STATUS_META[it.base.status] ? STATUS_META[it.base.status].label : it.base.status)} · ${pct(it.base.confidence)}` : '—';
    const after = it.target ? `${esc(STATUS_META[it.target.status] ? STATUS_META[it.target.status].label : it.target.status)} · ${pct(it.target.confidence)}` : '—';
    const why = (it.reasons || []).concat(it.stale ? it.stale.reasons.map(staleReasonText) : []);
    return `<div class="km-row" data-act="claim-open" data-id="${esc(it.claim_id)}" tabindex="0" role="button"><div class="km-row-title">${esc(it.text)}</div>
      <div class="km-row-sub">${before} → ${after}</div>${why.length ? `<div class="km-row-sub muted">${esc(why.join(' · '))}</div>` : ''}</div>`;
  }

  /* Investigations (knowledge states) */
  function investigationsHtml(list) {
    if (!list.length) return '<div class="km-empty">No investigations yet.</div>';
    return `<table class="km-table"><thead><tr><th>Investigation</th><th>Date</th><th>Knowledge state</th><th>Claims</th><th>Sources</th><th></th></tr></thead><tbody>
      ${list.map(i => {
        const dropped = i.dropped.sources + i.dropped.evidence + i.dropped.claims;
        const note = i.knowledge_state === 'partial' ? `<div class="small muted">${esc(dropped)} input(s) dropped</div>`
          : i.knowledge_state === 'failed' ? `<div class="small muted">Error: ${esc(i.error_type || 'unknown')}</div>` : '';
        const action = i.is_complete ? '' : `<button class="btn btn-ghost" data-act="rebuild" data-id="${esc(i.report_id)}">${i.knowledge_state === 'partial' ? 'Rebuild' : 'Rebuild knowledge'}</button>`;
        return `<tr><td>${esc(i.topic)}</td><td>${esc(fmtDate(i.created_at))}</td><td>${stateBadge(i.knowledge_state)}${note}</td><td>${esc(i.claim_count)}</td><td>${esc(i.source_count)}</td><td>${action}</td></tr>`;
      }).join('')}</tbody></table>`;
  }
  function drawInvestigations() { document.getElementById('kmBody').innerHTML = investigationsHtml(KM.investigations); }

  /* ── interaction ──────────────────────────────────────────────────── */
  async function selectEntity(id) {
    KM.sel = { entity: id, claim: null, evidence: null, source: null };
    KM.entityDetail = null; KM.claimDetail = null; KM.evidenceDetail = null; KM.graph = null;
    drawExplore();
    KM.entityDetail = await get('/entities/' + encodeURIComponent(id));
    drawExplore();
    if (KM.showGraph) loadGraph(id);
  }
  async function selectClaim(id) {
    KM.sel.claim = id; KM.sel.evidence = null; KM.evidenceDetail = null; KM.claimDetail = null;
    drawExplore();
    KM.claimDetail = await get('/claims/' + encodeURIComponent(id));
    drawExplore();
    if (KM.showGraph) loadGraph(id);
  }
  async function selectEvidence(id) {
    KM.sel.evidence = id; KM.evidenceDetail = null; drawExplore();
    KM.evidenceDetail = await get('/evidence/' + encodeURIComponent(id));
    drawExplore();
  }
  async function loadGraph(focus) {
    KM.graph = null; if (KM.tab === 'explore') drawExplore();
    KM.graph = await get('/graph', { focus, depth: 2, limit: 60 });
    if (KM.tab === 'explore') drawExplore();
  }
  async function openClaim(id) {
    KM.tab = 'explore'; KM.sel = { entity: null, claim: id, evidence: null, source: null };
    KM.entityDetail = null; KM.evidenceDetail = null;
    draw();
    KM.claimDetail = await get('/claims/' + encodeURIComponent(id));
    drawExplore();
  }
  async function compare() {
    KM.diff = null; KM.diffError = null;
    try {
      KM.diff = await get('/changes', { base: KM.base, target: KM.target });
    } catch (e) {
      KM.diffError = (e && e.message) || 'These investigations could not be compared.';
    }
    const el = document.getElementById('kmDiff'); if (el) el.innerHTML = diffHtml();
  }
  async function rebuild(id) {
    try {
      await apiPost(API + '/investigations/' + encodeURIComponent(id) + '/rebuild', {});
      toast('Knowledge rebuilt', 'ok');
    } catch (e) { /* apiCall already toasted */ }
    KM.diff = null; KM.diffError = null;
    await loadAll();
  }

  async function refreshAfterChallenge(claimId) {
    try { KM.summary = await get('/summary'); } catch (e) { /* non-fatal */ }
    if (claimId) { try { KM.claimDetail = await get('/claims/' + encodeURIComponent(claimId)); } catch (e) { /* non-fatal */ } }
    KM.graph = null;
    if (KM.tab === 'explore') draw();
  }
  async function executeChallenge(id) {
    try { await apiPost('/challenges/' + encodeURIComponent(id) + '/execute', {}); } catch (e) { /* apiCall toasted; inspect shows state */ }
    KM.challengeDetail = await get('/challenges/' + encodeURIComponent(id) + '/inspect');
  }
  async function runChallenge(claimId, invId) {
    if (KM.challengeBusy) return;
    KM.challengeBusy = true; KM.challengeDetail = null; drawExplore();
    try {
      const created = await apiPost('/challenges', { investigation_id: invId, claim_id: claimId, challenge_type: KM.challengeType });
      await executeChallenge(created.data.challenge.challenge_id);
    } catch (e) { /* apiCall already toasted the reason */ }
    KM.challengeBusy = false;
    await refreshAfterChallenge(claimId);
  }
  async function retryChallenge(id) {
    if (KM.challengeBusy) return;
    KM.challengeBusy = true; drawExplore();
    try { await executeChallenge(id); } catch (e) { /* toasted */ }
    KM.challengeBusy = false;
    await refreshAfterChallenge(KM.sel.claim);
  }
  async function openChallenge(id) {
    try { KM.challengeDetail = await get('/challenges/' + encodeURIComponent(id) + '/inspect'); } catch (e) { return; }
    drawExplore();
  }

  function onClick(ev) {
    const t = ev.target.closest('[data-act]'); if (!t) return;
    const act = t.dataset.act, id = t.dataset.id;
    if (t.tagName === 'A') ev.preventDefault();
    if (act === 'tab') { KM.tab = t.dataset.tab; draw(); }
    else if (act === 'entity') { if (KM.tab !== 'explore') { KM.tab = 'explore'; draw(); } selectEntity(id); }
    else if (act === 'claim') selectClaim(id);
    else if (act === 'claim-open') openClaim(id);
    else if (act === 'evidence') selectEvidence(id);
    else if (act === 'graph') { KM.showGraph = !KM.showGraph; if (KM.showGraph) loadGraph(KM.sel.claim || KM.sel.entity); else drawExplore(); }
    else if (act === 'node') {
      const type = t.dataset.type;
      if (type === 'entity') selectEntity(id); else if (type === 'claim') selectClaim(id); else if (type === 'evidence') selectEvidence(id);
      else loadGraph(id);
    }
    else if (act === 'compare') compare();
    else if (act === 'rebuild') rebuild(id);
    else if (act === 'challenge-run') runChallenge(id, t.dataset.inv);
    else if (act === 'challenge-retry') retryChallenge(id);
    else if (act === 'challenge-open') openChallenge(id);
    else if (act === 'bench-claim') { if (window.AtlasBench) window.AtlasBench.fromClaim(t.dataset.inv, id, t.dataset.ch === '1'); }
  }

  function onKeyboardActivate(ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const target = ev.target.closest('[data-act][role="button"]');
    if (!target || target.tagName === 'BUTTON' || target.tagName === 'A') return;
    ev.preventDefault();
    onClick({ target, preventDefault() {} });
  }

  let searchTimer = null;
  function onInput(ev) {
    const id = ev.target.id;
    if (id === 'kmEntitySearch') {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        KM.entities = await get('/entities', { limit: 50, query: ev.target.value || undefined });
        const box = document.getElementById('kmEntities');
        if (box) box.innerHTML = KM.entities.items.length ? KM.entities.items.map(entityRow).join('') : '<div class="km-empty">No entities match.</div>';
      }, 250);
    } else if (id === 'kmClaimQuery') {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { KM.claimsFilter.query = ev.target.value; drawClaims(); }, 300);
    }
  }
  function onChange(ev) {
    const id = ev.target.id;
    if (id === 'kmBase') { KM.base = ev.target.value; KM.diff = null; KM.diffError = null; drawChanges(); }
    else if (id === 'kmTarget') { KM.target = ev.target.value; KM.diff = null; KM.diffError = null; drawChanges(); }
    else if (id === 'kmClaimStatus') { KM.claimsFilter.status = ev.target.value; drawClaims(); }
    else if (id === 'kmClaimStale') { KM.claimsFilter.stale = ev.target.checked; drawClaims(); }
    else if (id === 'kmClaimConflict') { KM.claimsFilter.conflicting = ev.target.checked; drawClaims(); }
    else if (id === 'kmChallengeType') { KM.challengeType = ev.target.value; }
  }

  /* ── exports ──────────────────────────────────────────────────────── */
  function diffHtmlFor(d, err) {  // render a diff without touching live UI state
    const sd = KM.diff, se = KM.diffError;
    KM.diff = d; KM.diffError = err || null;
    try { return diffHtml(); } finally { KM.diff = sd; KM.diffError = se; }
  }
  function claimDetailHtml(c) {
    const sv = KM.claimDetail; KM.claimDetail = c;
    try { return claimDetailPanel(); } finally { KM.claimDetail = sv; }
  }
  const helpers = { challengeBoxHtml, challengeInspectHtml, interpretationHtml, trailHtml, resultBadge, challengeStatusBadge, challengeTypeLabel, CHALLENGE_TYPES, banner, investigationsHtml, diffHtmlFor, claimDetailHtml, claimRow, entityRow, evidenceRow, changeRow, stateMeta, stateBadge, statusBadge, freshnessBadge, confidenceBar, fmtDate, pct, layoutGraph, staleReasonText, isUntrusted, esc, CHANGE_SECTIONS };
  if (typeof window !== 'undefined') { window.viewKnowledge = viewKnowledge; window.AtlasKnowledge = helpers; }
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
})();
