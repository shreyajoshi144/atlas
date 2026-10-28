/* ══════════════════════════════════════════════════════════════════════
   ATLASBENCH (M8)
   Test the same research task and the same frozen evidence against different
   models and agent settings, then compare how well each one grounded its
   answer: right verdicts, real citations, evidence covered, conflicts handled,
   sensible tool use — plus latency, tokens and estimated cost.

   Structure mirrors knowledge.js: pure helpers first (no DOM, unit-tested under
   Node), then views. All dynamic text goes through esc(); interactions use
   data-* attributes and one delegated listener — no inline JS built from data.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const esc = (typeof escapeHtml === 'function') ? escapeHtml : function (s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  };

  /* ── vocabulary ───────────────────────────────────────────────────── */
  const METRICS = [
    ['claim_correctness', 'Claim correctness'], ['citation_accuracy', 'Citation accuracy'], ['evidence_grounding', 'Evidence grounding'],
    ['evidence_coverage', 'Evidence coverage'], ['contradiction_handling', 'Contradiction handling'], ['trajectory_quality', 'Tool use'],
  ];
  const METRIC_HELP = {
    claim_correctness: 'Did each claim get the right verdict?',
    citation_accuracy: 'Are cited sources real, retrieved, relevant and on the right side?',
    evidence_grounding: 'Is every cited passage really in its source, and every "verified" backed by one?',
    evidence_coverage: 'How many of the required documents were cited for the right claim?',
    contradiction_handling: 'Was a conflict detected and were both sides shown? Only tasks with a conflict count.',
    trajectory_quality: 'Required tools used, in order, without duplicates, errors or overspend.',
  };
  const RUN_STATUS = {
    pending: { label: 'Not started', cls: 'badge-neutral' }, running: { label: 'Running', cls: 'badge-sage' },
    completed: { label: 'Completed', cls: 'badge-jade' }, failed: { label: 'Did not finish', cls: 'badge-terracotta' },
  };
  const CASE_STATUS = { pending: { label: 'Waiting', cls: 'badge-neutral' }, completed: { label: 'Done', cls: 'badge-jade' }, failed: { label: 'Failed', cls: 'badge-terracotta' } };
  const VERDICT = {
    verified: 'Verified', partially_verified: 'Partially verified', not_verified: 'Not verified', conflicting: 'Conflicting',
    insufficient_evidence: 'Insufficient evidence', abstained: 'Abstained (no evidence found)',
  };
  const STRATEGY = { full_corpus: 'Full corpus', single_pass: 'Single pass', iterative: 'Iterative gap-fill' };
  const FLAG_TEXT = {
    cited_injected_document: 'Cited a document that tried to give the model instructions',
    fabricated_or_ungrounded_evidence: 'Cited text that is not in the source it names',
    one_sided_contradiction: 'Saw the conflict but cited only one side',
    tool_budget_exhausted: 'Ran out of tool budget before finishing',
    extra_claims: 'Produced claims that were not asked for',
    case_failed: 'This task did not finish',
  };
  const ERROR_TEXT = {
    verification_failed: 'The model call failed or returned output Atlas could not use.',
    search_failed: 'Searching the frozen evidence failed.',
    no_evidence_retrieved: 'No evidence could be fetched for the verifier.',
    model_unavailable: 'The model could not be started (check the model configuration).',
    cases_failed: 'Some tasks failed. Run again to retry only those.',
    all_cases_failed: 'Every task failed, even after retrying.',
  };
  const ROLE_TEXT = { support: 'supports', contradict: 'contradicts' };
  const TASK_TEMPLATE = {
    title: 'Does the sample pass?', question: 'Does the sample pass the check every time it is run?',
    claims: [{ claim_id: 'c1', text: 'The sample passes the check every time.', expected_status: 'verified', supporting_docs: ['d1'], contradicting_docs: [] }],
    corpus: [{ doc_id: 'd1', url: 'https://example.com/report', title: 'Check report', publish_date: '2026-01-15', content: 'The sample passes the check every time it is run, according to the lab.' }],
  };

  /* ── pure helpers ─────────────────────────────────────────────────── */
  function badge(meta, extra) { return `<span class="badge ${meta.cls}"${extra || ''}>${esc(meta.label)}</span>`; }
  function runStatusBadge(s) { return badge(RUN_STATUS[s] || { label: String(s || 'Unknown'), cls: 'badge-neutral' }); }
  function caseStatusBadge(s) { return badge(CASE_STATUS[s] || { label: String(s || 'Unknown'), cls: 'badge-neutral' }); }
  function verdictLabel(v) { return VERDICT[v] || (v ? String(v) : 'No verdict'); }
  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function score(x) { return isNum(x) ? x.toFixed(2) : '—'; }
  function pct(x) { return isNum(x) ? Math.round(x * 100) + '%' : '—'; }
  function ms(x) { if (!isNum(x)) return '—'; return x < 1000 ? Math.round(x) + ' ms' : (x / 1000).toFixed(x < 10000 ? 1 : 0) + ' s'; }
  function num(x) { return isNum(x) ? Math.round(x).toLocaleString('en-US') : '—'; }
  function usd(x) { return isNum(x) ? '$' + (x < 0.01 ? x.toFixed(4) : x.toFixed(3)) : '—'; }
  function shortHash(h) { return h ? String(h).slice(0, 8) : '—'; }
  function fmtDate(iso) { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? '—' : d.toISOString().slice(0, 10); }
  function strategyLabel(s) { return STRATEGY[s] || String(s || '—'); }
  function metricLabel(k) { const m = METRICS.find(x => x[0] === k); return m ? m[1] : k; }

  /** Score bar. A missing score reads "not measured" — never zero. */
  function scoreBar(v, opts) {
    const o = opts || {};
    if (!isNum(v)) return `<span class="ab-na" title="Not measured for this task">n/a</span>`;
    const w = Math.max(0, Math.min(1, v));
    return `<span class="ab-bar" role="img" aria-label="${esc(o.label || 'Score')} ${score(w)} out of 1"><span class="ab-bar-fill${o.best ? ' best' : ''}" style="width:${Math.round(w * 100)}%"></span></span><span class="ab-num">${score(w)}</span>`;
  }
  function flagList(flags) {
    const f = (flags || []).filter(x => FLAG_TEXT[x]);
    return f.length ? `<ul class="ab-flags">${f.map(x => `<li>${esc(FLAG_TEXT[x])}</li>`).join('')}</ul>` : '';
  }
  function errorText(code) { return code ? (ERROR_TEXT[code] || 'Something went wrong while running this.') : ''; }
  function banner(kind, html) {
    return `<div class="km-banner ${kind === 'warn' ? 'km-banner-warn' : 'km-banner-neutral'}"${kind === 'warn' ? ' role="alert"' : ''}>${html}</div>`;
  }
  function tile(label, value, sub) {
    return `<div class="card km-tile"><div class="km-tile-num">${value}</div><div class="small muted">${esc(label)}</div>${sub ? `<div class="small muted">${sub}</div>` : ''}</div>`;
  }

  /* ── runs ─────────────────────────────────────────────────────────── */
  function configLine(run) {
    const c = run.config || {};
    return `${esc(c.model || '—')} · ${esc(strategyLabel(c.strategy))}`;
  }
  function runRow(run, selected) {
    const comp = run.summary && run.summary.composite ? run.summary.composite.mean : null;
    const checkable = run.status === 'completed';
    return `<div class="km-row ab-run" data-act="run-open" data-id="${esc(run.run_id)}" tabindex="0" role="button">
      <div class="km-row-title">${esc(run.label || run.config_name)}</div>
      <div class="km-row-sub">${runStatusBadge(run.status)} ${isNum(comp) ? `<span class="ab-num">${score(comp)}</span>` : ''}
        <span>${configLine(run)}</span><span>${esc(run.cases_done)}/${esc(run.case_count)} tasks</span><span>${esc(fmtDate(run.created_at))}</span>
        ${run.reproduces_run_id ? '<span class="badge badge-neutral">Reproduction</span>' : ''}
        ${checkable ? `<label class="km-check ab-pick" title="Select to compare"><input type="checkbox" data-act="pick" data-id="${esc(run.run_id)}"${selected ? ' checked' : ''}> Compare</label>` : ''}</div></div>`;
  }
  function metricTable(summary) {
    const rows = METRICS.map(([k, label]) => {
      const m = (summary.metrics || {})[k] || {};
      return `<tr><th scope="row">${esc(label)}<div class="small muted">${esc(METRIC_HELP[k])}</div></th><td>${scoreBar(m.mean, { label })}</td>
        <td class="small muted">${m.n ? `${esc(m.n)} of ${esc(summary.cases_completed)} tasks` : 'no applicable tasks'}</td></tr>`;
    }).join('');
    return `<table class="km-table ab-table"><thead><tr><th>Measure</th><th>Average score (0–1)</th><th>Based on</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  function perfTiles(summary) {
    const p = summary.performance || {};
    const tokens = p.total_tokens === null || p.total_tokens === undefined ? 'not reported' : num(p.total_tokens);
    const costNote = p.cost_usd === null || p.cost_usd === undefined ? 'unknown for this model' : (p.cost_complete ? 'estimate' : 'estimate · some tasks could not be priced');
    return `<div class="grid grid-4 km-tiles">${tile('Total time', ms(p.latency_ms_total), `p50 ${ms(p.latency_ms_p50)} · p95 ${ms(p.latency_ms_p95)} per task`)}
      ${tile('Tokens', tokens, `${num(p.llm_calls)} model calls`)}${tile('Cost', usd(p.cost_usd), esc(costNote))}${tile('Tool calls', num(p.tool_calls), `${ms(p.tool_latency_ms)} in tools`)}</div>`;
  }
  function casesTable(run) {
    const rows = (run.cases || []).map(c => `<tr class="ab-case" data-act="case-open" data-id="${esc(c.task_id)}" tabindex="0" role="button">
      <td>${esc(c.title)}<div class="small muted">${esc(c.task_id)}</div>${flagList(c.flags)}${c.error_type ? `<div class="small ab-err">${esc(errorText(c.error_type))}</div>` : ''}</td>
      <td>${caseStatusBadge(c.status)}</td><td>${scoreBar(c.composite, { label: 'Overall' })}</td><td class="small">${ms(c.latency_ms)}</td></tr>`).join('');
    return `<table class="km-table ab-table"><thead><tr><th>Task</th><th>Status</th><th>Overall</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  function runDetailHtml(run) {
    const s = run.summary;
    let alert = '';
    if (run.status === 'failed') {
      alert = banner('warn', `This run did not finish. ${esc(errorText(run.error_type))} ${run.retryable ? 'Run it again to continue with the unfinished tasks.' : 'No more automatic retries are available — create a new run.'}`);
    } else if (run.status === 'running') {
      alert = banner('neutral', 'This run is executing. Refresh to see progress.');
    }
    if (s && run.summary_partial) alert += banner('warn', `Scores below cover only the ${esc(s.cases_completed)} of ${esc(run.case_count)} tasks that finished.`);
    const actions = [
      (run.status === 'pending' || run.retryable) ? `<button class="btn btn-primary" data-act="run-execute" data-id="${esc(run.run_id)}"${AB.busy ? ' disabled' : ''}>${AB.busy ? 'Running…' : (run.status === 'pending' ? 'Run now' : 'Run again')}</button>` : '',
      `<button class="btn" data-act="run-reproduce" data-id="${esc(run.run_id)}">Reproduce</button>`,
      `<button class="btn" data-act="run-refresh" data-id="${esc(run.run_id)}">Refresh</button>`,
      run.status !== 'running' ? `<button class="btn btn-danger" data-act="run-delete" data-id="${esc(run.run_id)}">Delete</button>` : '',
    ].join(' ');
    const overall = s && s.composite && isNum(s.composite.mean)
      ? `<div class="ab-overall"><span class="ab-overall-num">${score(s.composite.mean)}</span><span class="small muted">overall, a weighted blend of the six measures — read the measures, not just this${isNum(s.composite_incl_failures) && s.cases_failed ? ` · ${score(s.composite_incl_failures)} counting failed tasks as 0` : ''}</span></div>` : '';
    return `<div class="card km-detail ab-detail"><div class="km-claim-text">${esc(run.label || run.config_name)}</div>
      <div class="km-row-sub">${runStatusBadge(run.status)} <span>${configLine(run)}</span><span>${esc(run.cases_done)}/${esc(run.case_count)} tasks done</span>
        <span title="Runs with the same fingerprint measured the same thing">fingerprint ${esc(shortHash(run.fingerprint))}</span><span>scorer ${esc(run.evaluator_version)}</span>
        ${run.reproduces_run_id ? `<span class="badge badge-neutral">Reproduces ${esc(run.reproduces_run_id.slice(0, 12))}…</span>` : ''}</div>
      <div class="km-toolbar" style="margin-top:10px;">${actions}</div>${alert}${overall}
      ${s ? `<div class="km-group-label">Measures</div>${metricTable(s)}<div class="km-group-label">Speed, tokens and cost</div>${perfTiles(s)}${flagSummary(s)}` : ''}
      <div class="km-group-label">Tasks (${esc((run.cases || []).length)})</div>${casesTable(run)}
      <div id="abCase">${AB.caseDetail ? caseDetailHtml(AB.caseDetail) : ''}</div></div>`;
  }
  function flagSummary(s) {
    const f = Object.entries(s.flags || {}).filter(([k]) => FLAG_TEXT[k] && k !== 'case_failed');
    return f.length ? `<div class="km-group-label">Things to look at</div><ul class="ab-flags">${f.map(([k, n]) => `<li>${esc(FLAG_TEXT[k])} <span class="muted">(${esc(n)} task${n === 1 ? '' : 's'})</span></li>`).join('')}</ul>` : '';
  }

  /* ── one task's result ────────────────────────────────────────────── */
  function yesNo(v) { return v === null || v === undefined ? '<span class="muted">n/a</span>' : (v ? 'Yes' : '<strong>No</strong>'); }
  function verdictTable(detail) {
    const rows = ((detail.claim_correctness || {}).details || []).map(d => `<tr><td>${esc(d.text)}</td><td>${esc(verdictLabel(d.expected))}</td>
      <td>${esc(d.matched || d.got === 'abstained' ? verdictLabel(d.got) : 'Missing')}</td><td>${d.credit === 1 ? 'Correct' : d.credit > 0 ? 'Same direction' : '<strong>Wrong</strong>'}</td></tr>`).join('');
    return `<table class="km-table ab-table"><thead><tr><th>Claim</th><th>Expected</th><th>Model said</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  function citationTable(detail, task) {
    const docs = {}; ((task && task.corpus) || []).forEach(d => { docs[d.doc_id] = d; });
    const rows = (detail.citations || []).map(c => {
      const d = c.doc_id ? docs[c.doc_id] : null;
      return `<tr><td>${d ? esc(d.title) : '<em>not in the evidence</em>'}<div class="small muted ab-url">${esc(c.url)}</div>
        ${c.injection ? '<span class="badge badge-terracotta">Tried to instruct the model</span>' : c.distractor ? '<span class="badge badge-neutral">Distractor</span>' : ''}</td>
        <td>${esc(ROLE_TEXT[c.stance] || c.stance || '—')}</td><td>${yesNo(c.provenance_valid)}</td><td>${yesNo(c.grounded)}</td><td>${yesNo(c.relevant)}</td><td>${yesNo(c.stance_correct)}</td></tr>`;
    }).join('');
    if (!rows) return '<div class="km-empty">The model cited no sources.</div>';
    return `<table class="km-table ab-table"><thead><tr><th>Cited source</th><th>Stance</th><th title="The agent actually retrieved this page">Retrieved</th><th title="The quoted text is in the page">Quote is real</th><th>Needed</th><th>Right side</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  function trajectoryList(out, traj) {
    const calls = (out.calls || []).map(c => `<li class="km-trail-step"><span class="km-trail-name">${esc(c.tool.replace(/_/g, ' '))}</span>
      ${c.ok ? 'ok' : `<strong>failed</strong> (${esc(c.error_code || 'error')})`} <span class="small muted">${ms(c.duration_ms)}${c.doc_id ? ' · ' + esc(c.doc_id) : ''}</span></li>`).join('');
    const t = traj || {};
    const facts = [`${esc(t.tool_calls)} calls (ideal ${esc(t.ideal_calls)})`, `${esc(t.duplicate_calls)} duplicate`, `order ${t.order_valid ? 'valid' : 'out of order'}`,
      t.retrieval ? `retrieval recall ${pct(t.retrieval.recall_at_k)}` : null, out.rounds > 1 ? `${esc(out.rounds)} verification rounds` : null].filter(Boolean).join(' · ');
    return `<div class="small muted">${facts}</div><ol class="km-trail">${calls || '<li class="km-trail-step">No tools were called.</li>'}</ol>`;
  }
  function caseDetailHtml(c) {
    const d = c.detail || {}, out = c.output || {};
    if (c.status === 'failed') {
      return `<div class="card km-challenge-detail"><div class="km-claim-text">${esc(c.title)}</div>${banner('warn', `This task did not finish. ${esc(errorText(c.error_type))}`)}
        <div class="km-group-label">What it did before failing</div>${trajectoryList(out, d.trajectory)}</div>`;
    }
    const sc = c.scores || {};
    const bars = METRICS.map(([k, label]) => `<tr><th scope="row">${esc(label)}</th><td>${scoreBar(sc[k], { label })}</td></tr>`).join('');
    const u = c.usage || {}, l = c.latency || {};
    const models = (c.model_outputs || []).map((t, i) => `<details class="ab-raw"><summary>Model output ${i + 1}</summary><pre>${esc(t)}</pre></details>`).join('');
    const cov = d.evidence_coverage || {};
    const missing = (cov.missing || []).length ? `<div class="small">Required but not cited: ${cov.missing.map(m => esc(m.doc_id + ' for ' + m.claim_id)).join(', ')}</div>` : '';
    return `<div class="card km-challenge-detail"><div class="km-claim-text">${esc(c.title)}</div>
      <div class="km-row-sub">${caseStatusBadge(c.status)} ${scoreBar(c.composite, { label: 'Overall' })}<span>${ms(l.total_ms)}</span><span>${isNum(u.total_tokens) ? num(u.total_tokens) + ' tokens' : 'tokens not reported'}</span><span>${usd(u.cost_usd)}</span></div>
      ${flagList(c.flags)}
      <div class="km-group-label">Verdicts</div>${verdictTable(d)}
      <div class="km-group-label">Citations checked</div>${citationTable(d, c.task)}${missing}
      <div class="km-group-label">Scores</div><table class="km-table ab-table"><tbody>${bars}</tbody></table>
      <div class="km-group-label">What the agent did</div>${trajectoryList(out, d.trajectory)}
      ${models ? `<div class="km-group-label">Raw model output</div>${models}` : ''}</div>`;
  }

  /* ── comparison ───────────────────────────────────────────────────── */
  function deltaText(v) { return isNum(v) && v !== 0 ? `<span class="ab-delta ${v > 0 ? 'up' : 'down'}">${v > 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}</span>` : ''; }
  function compareHtml(cmp) {
    if (!cmp) return '';
    const runs = cmp.runs || [];
    const head = runs.map((r, i) => `<th scope="col">${esc(r.label || r.config_name)}<div class="small muted">${esc(r.model)} · ${esc(strategyLabel(r.strategy))}</div>${i === 0 ? '<div class="small muted">baseline</div>' : ''}</th>`).join('');
    const cell = (run, values, best, delta, fmt) => {
      const v = values[run.run_id];
      return `<td>${fmt ? fmt(v) : scoreBar(v, { best: best === run.run_id })}${best === run.run_id ? ' <span class="ab-best">best</span>' : ''}${delta ? deltaText(delta[run.run_id]) : ''}</td>`;
    };
    const metricRows = (cmp.metrics || []).map(m => `<tr><th scope="row">${esc(m.label)}</th>${runs.map(r => cell(r, m.values, m.best, m.delta_vs_first)).join('')}</tr>`).join('');
    const comp = cmp.composite || {};
    const compRow = `<tr class="ab-total"><th scope="row">Overall<div class="small muted">failed tasks count as 0</div></th>${runs.map(r => cell(r, comp.values_incl_failures || {}, comp.best, comp.delta_vs_first)).join('')}</tr>`;
    const perfRows = (cmp.performance || []).map(p => {
      const fmt = p.metric.startsWith('latency') ? ms : p.metric === 'cost_usd' ? usd : num;
      return `<tr><th scope="row">${esc(p.label)}</th>${runs.map(r => cell(r, p.values, p.best, null, fmt)).join('')}</tr>`;
    }).join('');
    const tasks = (cmp.per_task || []).map(t => `<tr><th scope="row">${esc(t.title)}</th>${runs.map(r => {
      const v = t.composite[r.run_id];
      return `<td>${t.status[r.run_id] === 'failed' ? '<span class="badge badge-terracotta">Failed</span>' : scoreBar(v, { best: t.winner === r.run_id })}${t.winner === r.run_id ? ' <span class="ab-best">best</span>' : ''}</td>`;
    }).join('')}</tr>`).join('');
    const warns = (cmp.warnings || []).map(w => banner('warn', esc(w))).join('');
    const same = cmp.identical_setup ? banner('neutral', 'These runs used the same configuration and tasks — differences come from run-to-run variation.') : '';
    const wins = runs.map(r => `${esc(r.label || r.config_name)}: ${esc((cmp.wins || {})[r.run_id] || 0)}`).join(' · ');
    return `<div class="card km-detail ab-detail">${warns}${same}
      <div class="km-group-label">Quality on ${esc((cmp.common_tasks || []).length)} identical tasks</div>
      <div class="ab-scroll"><table class="km-table ab-table ab-compare"><thead><tr><th></th>${head}</tr></thead><tbody>${metricRows}${compRow}</tbody></table></div>
      <div class="km-group-label">Speed, tokens and cost (whole runs; lower is better)</div>
      <div class="ab-scroll"><table class="km-table ab-table ab-compare"><thead><tr><th></th>${head}</tr></thead><tbody>${perfRows}</tbody></table></div>
      <div class="km-group-label">Task by task <span class="muted">· tasks won: ${wins}</span></div>
      <div class="ab-scroll"><table class="km-table ab-table ab-compare"><thead><tr><th></th>${head}</tr></thead><tbody>${tasks}</tbody></table></div></div>`;
  }

  /* ── tasks ────────────────────────────────────────────────────────── */
  function taskRow(t) {
    return `<div class="km-row" data-act="task-open" data-id="${esc(t.task_id)}" tabindex="0" role="button">
      <div class="km-row-title">${esc(t.title)}</div>
      <div class="km-row-sub"><span class="badge ${t.builtin ? 'badge-jade' : 'badge-neutral'}">${t.builtin ? 'Built-in' : 'Yours'}</span>
        ${t.gold_quality === 'derived' ? '<span class="badge badge-amber" title="Expected answers were derived from Atlas\'s own earlier results, not reviewed by a person">Derived answers</span>' : ''}
        <span>${esc(t.claim_count)} claim${t.claim_count === 1 ? '' : 's'}</span><span>${esc(t.doc_count)} documents</span>
        ${(t.tags || []).map(x => `<span class="badge badge-neutral">${esc(x)}</span>`).join('')}</div></div>`;
  }
  function docRoles(def) {
    const roles = {};
    (def.claims || []).forEach(c => {
      (c.supporting_docs || []).forEach(d => { (roles[d] = roles[d] || []).push(`supports ${c.claim_id}`); });
      (c.contradicting_docs || []).forEach(d => { (roles[d] = roles[d] || []).push(`contradicts ${c.claim_id}`); });
    });
    return roles;
  }
  function taskDetailHtml(t) {
    const def = t.definition || {}, roles = docRoles(def);
    const claims = (def.claims || []).map(c => `<tr><td>${esc(c.claim_id)}</td><td>${esc(c.text)}</td><td>${esc(verdictLabel(c.expected_status))}</td></tr>`).join('');
    const docs = (def.corpus || []).map(d => `<div class="km-row ab-doc ${(roles[d.doc_id] || []).some(r => r.startsWith('contradicts')) ? 'km-ev-conflict' : (roles[d.doc_id] || []).length ? 'km-ev-support' : 'km-ev-mixed'}">
      <div class="km-row-title">${esc(d.title)} <span class="small muted">(${esc(d.doc_id)})</span></div>
      <div class="km-row-sub">${(roles[d.doc_id] || []).map(r => `<span class="badge badge-neutral">${esc(r)}</span>`).join('')}
        ${d.injection ? '<span class="badge badge-terracotta">Contains instructions aimed at the model</span>' : ''}${d.distractor && !d.injection ? '<span class="badge badge-neutral">Distractor</span>' : ''}
        <span class="ab-url">${esc(d.url)}</span>${d.publish_date ? `<span>${esc(d.publish_date)}</span>` : ''}</div>
      <div class="small ab-doc-text">${esc(d.content)}</div></div>`).join('');
    const derived = t.gold_quality === 'derived'
      ? banner('warn', 'The expected answers for this task were derived from Atlas\'s own earlier evidence and verdicts, not reviewed by a person. Use it to track changes over time, not as an absolute measure of accuracy.') : '';
    const del = t.builtin ? '' : `<button class="btn btn-danger" data-act="task-delete" data-id="${esc(t.task_id)}">Delete task</button>`;
    return `<div class="card km-detail ab-detail"><div class="km-claim-text">${esc(t.title)}</div>
      <div class="km-row-sub"><span>hash ${esc(shortHash(t.task_hash))}</span>${t.suite_id ? `<span>suite ${esc(t.suite_id)}</span>` : ''}</div>
      ${t.description ? `<p class="small">${esc(t.description)}</p>` : ''}${derived}
      <div class="km-group-label">Question given to the agent</div><p>${esc(t.question)}</p>
      <div class="km-group-label">Claims and expected verdicts</div><table class="km-table ab-table"><thead><tr><th>#</th><th>Claim</th><th>Expected</th></tr></thead><tbody>${claims}</tbody></table>
      <div class="km-group-label">Frozen evidence (the agent never reaches the live web)</div>${docs}
      <div class="km-toolbar" style="margin-top:12px;">${del}</div></div>`;
  }

  /* ── configs ──────────────────────────────────────────────────────── */
  function configRow(c) {
    return `<div class="km-row" tabindex="0">
      <div class="km-row-title">${esc(c.name)}</div>
      <div class="km-row-sub"><span class="badge ${c.builtin ? 'badge-jade' : 'badge-neutral'}">${c.builtin ? 'Preset' : 'Yours'}</span><span>${esc(c.model)}</span><span>${esc(strategyLabel(c.strategy))}</span>
        <span>temp ${esc(c.temperature)}</span><span>top ${esc(c.top_k)} sources</span>${c.max_rounds > 1 && c.strategy === 'iterative' ? `<span>${esc(c.max_rounds)} rounds</span>` : ''}
        ${c.price_known ? '' : '<span class="badge badge-amber" title="Cost cannot be estimated for this model">No price</span>'}
        ${c.builtin ? '' : `<button class="btn btn-sm btn-danger" data-act="cfg-delete" data-id="${esc(c.config_id)}">Delete</button>`}</div></div>`;
  }
  function configFormHtml(ov) {
    const models = ((ov && ov.models) || []).map(m => `<option value="${esc(m.model)}">${esc(m.label)}${m.price_in === null ? '' : ` — $${esc(m.price_in)}/$${esc(m.price_out)} per 1M tokens`}</option>`).join('');
    const strategies = ((ov && ov.strategies) || []).map(s => `<option value="${esc(s.strategy)}" title="${esc(s.description)}">${esc(s.label)}</option>`).join('');
    return `<div class="card km-detail"><div class="km-claim-text">New configuration</div>
      <div class="km-toolbar"><div class="km-sel"><label class="field-label" for="abCfgName">Name</label><input class="input" id="abCfgName" maxlength="80" placeholder="e.g. Llama 8B, iterative"></div>
        <div class="km-sel"><label class="field-label" for="abCfgModel">Model</label><select class="input" id="abCfgModel">${models}</select></div>
        <div class="km-sel"><label class="field-label" for="abCfgStrategy">Strategy</label><select class="input" id="abCfgStrategy">${strategies}</select></div></div>
      <div class="km-toolbar"><div class="km-sel"><label class="field-label" for="abCfgTemp">Temperature (0–1)</label><input class="input" id="abCfgTemp" type="number" min="0" max="1" step="0.1" value="0"></div>
        <div class="km-sel"><label class="field-label" for="abCfgTopK">Sources to retrieve (1–10)</label><input class="input" id="abCfgTopK" type="number" min="1" max="10" value="5"></div>
        <div class="km-sel"><label class="field-label" for="abCfgRounds">Gap-fill rounds (1–3)</label><input class="input" id="abCfgRounds" type="number" min="1" max="3" value="2"></div>
        <div class="km-sel"><label class="field-label" for="abCfgSeed">Seed</label><input class="input" id="abCfgSeed" type="number" min="0" value="0"></div></div>
      <button class="btn btn-primary" data-act="cfg-create">Save configuration</button></div>`;
  }
  function parseConfigForm(v) {
    const n = (x, d) => { const k = Number(x); return isFinite(k) ? k : d; };
    return { name: String(v.name || '').trim(), model: v.model, strategy: v.strategy, temperature: n(v.temperature, 0), top_k: Math.round(n(v.top_k, 5)),
             max_rounds: Math.round(n(v.max_rounds, 2)), seed: Math.round(n(v.seed, 0)) };
  }
  /** Parse pasted task JSON. Returns { ok, task } or { ok:false, error } — never throws. */
  function parseTaskJson(text) {
    let o;
    try { o = JSON.parse(text); } catch (e) { return { ok: false, error: 'That is not valid JSON.' }; }
    if (!o || typeof o !== 'object' || Array.isArray(o)) return { ok: false, error: 'A task must be a JSON object.' };
    ['task_id', 'gold_quality', 'provenance'].forEach(k => { delete o[k]; });
    return { ok: true, task: o };
  }

  /* ── state, API and views (browser only) ──────────────────────────── */
  const AB = { tab: 'runs', overview: null, runs: null, run: null, caseDetail: null, tasks: null, task: null, configs: null, picked: [], compare: null, busy: false, error: null };
  const API = '/atlasbench';
  const get = (path, params) => apiGet(API + path, params).then(r => (r && r.success ? r.data : null));
  const root = () => document.getElementById('abRoot');

  function tabsHtml() {
    const tabs = [['runs', 'Runs'], ['compare', 'Compare'], ['tasks', 'Tasks'], ['configs', 'Configurations'], ['routing', 'Routing & cost']];
    return `<div class="km-tabs" role="tablist">${tabs.map(([k, l]) => `<button class="km-tab${AB.tab === k ? ' active' : ''}" role="tab" aria-selected="${AB.tab === k}" data-act="tab" data-tab="${k}">${esc(l)}${k === 'compare' && AB.picked.length ? ` (${AB.picked.length})` : ''}</button>`).join('')}</div>`;
  }
  function intro() {
    const ov = AB.overview;
    if (!ov) return '';
    const s = (ov.suites || [])[0];
    return `<div class="small muted">Every task is a question plus a fixed set of documents — nothing is fetched from the web during a run, so any difference between two runs comes from the model or settings, not from the internet. Scoring is rule-based; no AI judges another AI. Costs are estimates.</div>
      ${s ? `<div class="small muted" style="margin-top:4px;">${esc(s.title)}: ${esc(s.task_count)} tasks · suite hash ${esc(shortHash(s.suite_hash))}</div>` : ''}`;
  }
  function draw() { const r = root(); if (!r) return; r.innerHTML = tabsHtml() + `<div id="abPane"></div>`; drawPane(); }
  function drawRouting() { const p = pane(); if (p && window.AtlasRouting) window.AtlasRouting.mount(p); }
  function drawPane() { ({ runs: drawRuns, compare: drawCompare, tasks: drawTasks, configs: drawConfigs, routing: drawRouting }[AB.tab] || drawRuns)(); }
  const pane = () => document.getElementById('abPane');

  function newRunForm() {
    const ov = AB.overview, cfgs = (AB.configs && AB.configs.items) || [];
    const custom = ((AB.tasks && AB.tasks.items) || []).filter(t => !t.builtin);
    const cfgOpts = cfgs.map(c => `<option value="${esc(c.config_id)}">${esc(c.name)} — ${esc(c.model)}, ${esc(strategyLabel(c.strategy))}</option>`).join('');
    const taskChecks = custom.map(t => `<label class="km-check"><input type="checkbox" class="abTaskPick" value="${esc(t.task_id)}"> ${esc(t.title)}</label>`).join('');
    return `<div class="card km-detail"><div class="km-claim-text">New run</div>
      <div class="km-toolbar"><div class="km-sel"><label class="field-label" for="abRunCfg">Configuration</label><select class="input" id="abRunCfg">${cfgOpts}</select></div>
        <div class="km-sel"><label class="field-label" for="abRunLabel">Label (optional)</label><input class="input" id="abRunLabel" maxlength="120" placeholder="e.g. before prompt change"></div></div>
      <div class="field-label">Tasks</div>
      <label class="km-check"><input type="radio" name="abScope" value="suite" checked> ${esc(ov && ov.suites[0] ? ov.suites[0].title : 'Built-in suite')} (${esc(ov && ov.suites[0] ? ov.suites[0].task_count : 8)} tasks)</label>
      <label class="km-check"><input type="radio" name="abScope" value="custom"${custom.length ? '' : ' disabled'}> Only my own tasks${custom.length ? '' : ' (none yet — add one in Tasks)'}</label>
      ${custom.length ? `<div class="ab-checks">${taskChecks}</div>` : ''}
      <div class="km-toolbar" style="margin-top:10px;"><button class="btn btn-primary" data-act="run-new"${AB.busy ? ' disabled' : ''}>Create run</button></div></div>`;
  }
  async function drawRuns() {
    const p = pane(); if (!p) return;
    if (!AB.runs) { p.innerHTML = skeletonBlock('200px'); try { AB.runs = await get('/runs', { limit: 50 }); } catch (e) { AB.error = 'Could not load runs.'; } }
    if (!pane()) return;
    const items = (AB.runs && AB.runs.items) || [];
    const list = items.length ? items.map(r => runRow(r, AB.picked.includes(r.run_id))).join('')
      : '<div class="km-empty">No runs yet. Create one above — the built-in suite takes a few minutes and uses your model quota.</div>';
    p.innerHTML = `${AB.error ? banner('warn', esc(AB.error)) : ''}${newRunForm()}<div class="km-group-label" style="margin-top:16px;">Your runs (${esc(AB.runs ? AB.runs.total : 0)})</div>
      ${AB.picked.length >= 2 ? '<div class="km-toolbar"><button class="btn" data-act="compare-go">Compare selected</button></div>' : AB.picked.length === 1 ? '<div class="small muted">Select one more completed run to compare.</div>' : ''}
      ${list}<div id="abRun">${AB.run ? runDetailHtml(AB.run) : ''}</div>`;
  }
  function drawCompare() {
    const p = pane(); if (!p) return;
    const picked = AB.picked.length;
    p.innerHTML = `${picked < 2 && !AB.compare ? '<div class="km-empty">Select two to four completed runs in the Runs tab (tick "Compare"), then compare them here.</div>' : ''}
      ${picked >= 2 ? `<div class="km-toolbar"><button class="btn btn-primary" data-act="compare-go">Compare ${esc(picked)} runs</button></div>` : ''}${AB.compare ? compareHtml(AB.compare) : ''}`;
  }
  async function drawTasks() {
    const p = pane(); if (!p) return;
    if (!AB.tasks) { p.innerHTML = skeletonBlock('200px'); try { AB.tasks = await get('/tasks'); } catch (e) { AB.error = 'Could not load tasks.'; } }
    if (!pane()) return;
    const items = (AB.tasks && AB.tasks.items) || [];
    p.innerHTML = `${items.map(taskRow).join('') || '<div class="km-empty">No tasks.</div>'}
      <div id="abTask">${AB.task ? taskDetailHtml(AB.task) : ''}</div>
      <div class="card km-detail"><div class="km-claim-text">Add your own task</div>
        <div class="small muted">Paste a task as JSON: a question, 1–5 claims with the verdict you expect, and 1–12 documents (up to 6,000 characters each). You can also freeze a claim from the Knowledge map — open a claim there and choose "Benchmark this claim".</div>
        <label class="field-label" for="abTaskJson" style="margin-top:8px;">Task JSON</label>
        <textarea class="input ab-json" id="abTaskJson" rows="12" spellcheck="false">${esc(JSON.stringify(TASK_TEMPLATE, null, 2))}</textarea>
        <div class="km-toolbar" style="margin-top:8px;"><button class="btn btn-primary" data-act="task-import">Add task</button></div></div>`;
  }
  async function drawConfigs() {
    const p = pane(); if (!p) return;
    if (!AB.configs) { p.innerHTML = skeletonBlock('200px'); try { AB.configs = await get('/configs'); } catch (e) { AB.error = 'Could not load configurations.'; } }
    if (!pane()) return;
    p.innerHTML = `${((AB.configs && AB.configs.items) || []).map(configRow).join('')}${configFormHtml(AB.overview)}`;
  }

  async function loadOverview() { AB.overview = await get('/overview'); }
  async function openRun(id) {
    AB.caseDetail = null;
    try { AB.run = (await get('/runs/' + encodeURIComponent(id))).run; } catch (e) { return; }
    const box = document.getElementById('abRun'); if (box) { box.innerHTML = runDetailHtml(AB.run); box.scrollIntoView && box.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  }
  async function openCase(taskId) {
    if (!AB.run) return;
    try { AB.caseDetail = (await get('/runs/' + encodeURIComponent(AB.run.run_id) + '/cases/' + encodeURIComponent(taskId))).case; } catch (e) { return; }
    const box = document.getElementById('abCase'); if (box) box.innerHTML = caseDetailHtml(AB.caseDetail);
  }
  async function createRun() {
    const cfg = document.getElementById('abRunCfg').value;
    const label = document.getElementById('abRunLabel').value;
    const scope = (document.querySelector('input[name="abScope"]:checked') || {}).value;
    const body = { config_id: cfg, label };
    if (scope === 'custom') {
      body.task_ids = Array.from(document.querySelectorAll('.abTaskPick:checked')).map(x => x.value);
      if (!body.task_ids.length) { toast('Pick at least one of your tasks.', 'err'); return; }
    } else body.suite_id = AB.overview.suites[0].suite_id;
    try {
      const r = await apiPost(API + '/runs', body);
      AB.runs = null; AB.run = r.data.run; toast('Run created — press "Run now" to start it', 'ok'); await drawRuns();
    } catch (e) { /* apiCall toasted the reason */ }
  }
  async function executeRun(id) {
    if (AB.busy) return;
    AB.busy = true; if (AB.run) { const b = document.getElementById('abRun'); if (b) b.innerHTML = runDetailHtml(AB.run); }
    toast('Running — this can take a few minutes. You can keep this page open.', 'ok');
    try {
      const r = await apiPost(API + '/runs/' + encodeURIComponent(id) + '/execute', {});
      AB.run = (await get('/runs/' + encodeURIComponent(id))).run;
      toast(r.success ? 'Run completed' : 'Run did not finish — see the notice on the run', r.success ? 'ok' : 'err');
    } catch (e) { try { AB.run = (await get('/runs/' + encodeURIComponent(id))).run; } catch (e2) { /* keep old view */ } }
    AB.busy = false; AB.runs = null; AB.caseDetail = null; await drawRuns();
  }
  async function reproduce(id) {
    try { const r = await apiPost(API + '/runs/' + encodeURIComponent(id) + '/reproduce', {}); AB.runs = null; AB.run = r.data.run; toast('Reproduction created — press "Run now"', 'ok'); await drawRuns(); } catch (e) { /* toasted */ }
  }
  async function deleteRun(id) {
    if (!window.confirm('Delete this run and its results?')) return;
    try { await apiDelete(API + '/runs/' + encodeURIComponent(id)); AB.picked = AB.picked.filter(x => x !== id); AB.run = null; AB.runs = null; AB.caseDetail = null; toast('Run deleted', 'ok'); await drawRuns(); } catch (e) { /* toasted */ }
  }
  async function runCompare() {
    try { AB.compare = await get('/compare', { run_ids: AB.picked.join(',') }); AB.tab = 'compare'; draw(); } catch (e) { /* toasted */ }
  }
  async function openTask(id) { try { AB.task = (await get('/tasks/' + encodeURIComponent(id))).task; } catch (e) { return; } const b = document.getElementById('abTask'); if (b) { b.innerHTML = taskDetailHtml(AB.task); b.scrollIntoView && b.scrollIntoView({ behavior: 'smooth', block: 'start' }); } }
  async function importTask() {
    const parsed = parseTaskJson(document.getElementById('abTaskJson').value);
    if (!parsed.ok) { toast(parsed.error, 'err'); return; }
    try { const r = await apiPost(API + '/tasks', parsed.task); AB.tasks = null; AB.task = r.data.task; toast('Task added', 'ok'); await drawTasks(); } catch (e) { /* toasted with the reason */ }
  }
  async function deleteTask(id) {
    if (!window.confirm('Delete this task? Runs that already used it keep their results.')) return;
    try { await apiDelete(API + '/tasks/' + encodeURIComponent(id)); AB.tasks = null; AB.task = null; toast('Task deleted', 'ok'); await drawTasks(); } catch (e) { /* toasted */ }
  }
  async function createConfig() {
    const v = parseConfigForm({ name: val('abCfgName'), model: val('abCfgModel'), strategy: val('abCfgStrategy'), temperature: val('abCfgTemp'), top_k: val('abCfgTopK'), max_rounds: val('abCfgRounds'), seed: val('abCfgSeed') });
    if (!v.name) { toast('Give the configuration a name.', 'err'); return; }
    try { await apiPost(API + '/configs', v); AB.configs = null; toast('Configuration saved', 'ok'); await drawConfigs(); } catch (e) { /* toasted */ }
  }
  async function deleteConfig(id) { try { await apiDelete(API + '/configs/' + encodeURIComponent(id)); AB.configs = null; toast('Configuration deleted', 'ok'); await drawConfigs(); } catch (e) { /* toasted */ } }
  const val = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };

  /** Called from the Knowledge map: freeze a claim (and its completed challenges) into a task, then show it. */
  async function fromClaim(investigationId, claimId, includeChallenges) {
    try {
      const r = await apiPost(API + '/tasks/from-claim', { investigation_id: investigationId, claim_id: claimId, include_challenges: includeChallenges !== false });
      toast(r.data.created ? 'Benchmark task created from this claim' : 'A task for this claim already exists', 'ok');
      AB.tasks = null; AB.task = r.data.task;
      Atlas.nav('atlasbench', { tab: 'tasks' });
    } catch (e) { /* apiCall toasted the reason */ }
  }

  function onClick(ev) {
    const t = ev.target.closest('[data-act]'); if (!t) return;
    const act = t.dataset.act, id = t.dataset.id;
    if (ev.target.closest('.ab-pick') && act !== 'pick') return;   // the "Compare" label must not also open the run
    if (act === 'pick') {
      if (t.checked) {
        if (AB.picked.length >= 4 && !AB.picked.includes(id)) { t.checked = false; toast('You can compare up to four runs.', 'err'); return; }
        if (!AB.picked.includes(id)) AB.picked.push(id);
      } else AB.picked = AB.picked.filter(x => x !== id);
      AB.compare = null; draw(); return;
    }
    if (act === 'tab') { AB.tab = t.dataset.tab; draw(); }
    else if (act === 'run-open') openRun(id);
    else if (act === 'case-open') openCase(id);
    else if (act === 'run-new') createRun();
    else if (act === 'run-execute') executeRun(id);
    else if (act === 'run-reproduce') reproduce(id);
    else if (act === 'run-refresh') openRun(id);
    else if (act === 'run-delete') deleteRun(id);
    else if (act === 'compare-go') runCompare();
    else if (act === 'task-open') openTask(id);
    else if (act === 'task-import') importTask();
    else if (act === 'task-delete') deleteTask(id);
    else if (act === 'cfg-create') createConfig();
    else if (act === 'cfg-delete') deleteConfig(id);
  }
  function onKey(ev) {
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches && ev.target.matches('[role="button"][data-act]')) { ev.preventDefault(); ev.target.click(); }
  }

  async function viewAtlasBench(main, ctx, opts) {
    main.innerHTML = `
      <div class="page">
        <div class="crumbs">${crumbsHtml('atlasbench')}</div>
        <div class="page-title">AtlasBench</div>
        <div class="page-desc">Run the same research task and evidence through different models and agent settings, and compare how well each one stays grounded in the evidence.</div>
        <div class="hr"></div>
        <div id="abRoot">${skeletonBlock('90px')}${skeletonBlock('260px')}</div>
      </div>`;
    ctx.innerHTML = '';
    const r = root();
    r.addEventListener('click', onClick); r.addEventListener('keydown', onKey);
    if (opts && opts.tab) AB.tab = opts.tab;
    AB.error = null;
    try { await loadOverview(); AB.configs = AB.configs || await get('/configs'); AB.tasks = AB.tasks || await get('/tasks'); } catch (e) { AB.error = 'AtlasBench could not be loaded. Try again in a moment.'; }
    if (!root()) return;
    draw();
    const intro_ = document.createElement('div'); intro_.innerHTML = intro(); root().parentNode.insertBefore(intro_, root());
  }

  /* ── exports ──────────────────────────────────────────────────────── */
  const helpers = { runRow, runDetailHtml, caseDetailHtml, compareHtml, taskRow, taskDetailHtml, configRow, configFormHtml, scoreBar, metricTable, perfTiles, casesTable,
    verdictTable, citationTable, trajectoryList, flagList, runStatusBadge, caseStatusBadge, verdictLabel, errorText, parseTaskJson, parseConfigForm,
    ms, usd, num, score, pct, shortHash, strategyLabel, metricLabel, METRICS, FLAG_TEXT, ERROR_TEXT, TASK_TEMPLATE, esc, fromClaim };
  if (typeof window !== 'undefined') { window.viewAtlasBench = viewAtlasBench; window.AtlasBench = helpers; }
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
})();
