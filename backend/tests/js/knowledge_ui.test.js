// Node-run tests for frontend/js/knowledge.js (no browser needed): pure helpers,
// HTML builders, XSS safety, and explicit-knowledge-state rendering.
const assert = require('assert');
const path = require('path');
const K = require(path.join(__dirname, '..', '..', '..', 'frontend', 'js', 'knowledge.js'));

const XSS = '<img src=x onerror=alert(1)>';
const noRawTags = (html) => { assert(!/<img\b/i.test(html), 'unescaped <img> in: ' + html.slice(0, 200)); assert(!/onerror=alert/.test(html.replace(/&lt;[^>]*?&gt;/g, ''))); };
let n = 0; const t = (name, fn) => { fn(); n++; };

// ── knowledge state is explicit and never defaults to complete ───────────────
t('state meta', () => {
  assert.strictEqual(K.stateMeta('complete').label, 'Complete');
  assert.strictEqual(K.stateMeta('failed').cls, 'badge-terracotta');
  assert.strictEqual(K.stateMeta('partial').cls, 'badge-amber');
  for (const bogus of [undefined, null, '', 'done', 'COMPLETE', '<script>']) assert.strictEqual(K.stateMeta(bogus).label, 'Not recorded');
  assert.strictEqual(K.isUntrusted('complete'), false);
  for (const s of ['partial', 'failed', 'not_recorded', undefined, 'whatever']) assert.strictEqual(K.isUntrusted(s), true);
});
t('state badge explains itself', () => {
  assert(/title="[^"]*rebuild/i.test(K.stateBadge('failed')));
  assert(K.stateBadge('failed').includes('Failed') && !K.stateBadge('failed').includes('Complete'));
});

// ── banner ───────────────────────────────────────────────────────────────────
const counts = { entities: 3, claims: 6, evidence: 7, sources: 3 };
const mkSummary = (inv, complete) => ({ counts, investigations: Object.assign({ total: 0, complete: 0, partial: 0, failed: 0, not_recorded: 0 }, inv), map_is_complete: complete, stale_claims: 1, conflicting_claims: 1, integrity: { ok: true, problem_count: 0 } });
t('banner warns when the map is incomplete and names the states', () => {
  const html = K.banner(mkSummary({ total: 3, complete: 1, failed: 1, partial: 1 }, false));
  assert(html.includes('not complete') && html.includes('1 failed') && html.includes('1 partial') && html.includes('role="alert"'));
});
t('banner shows no warning only when the map is complete', () => {
  const ok = K.banner(mkSummary({ total: 2, complete: 2 }, true));
  assert(!ok.includes('not complete') && !ok.includes('km-banner-warn'));
  assert(K.banner(mkSummary({ total: 0 }, false)).includes('No investigations yet'));
});
t('banner surfaces integrity problems', () => {
  const s = mkSummary({ total: 1, complete: 1 }, true); s.integrity = { ok: false, problem_count: 2 };
  assert(K.banner(s).includes('2 problem'));
});

// ── rows ─────────────────────────────────────────────────────────────────────
const claim = (o) => Object.assign({ claim_id: 'clm_' + 'a'.repeat(32), text: 'A claim', status: 'verified', confidence: 0.8,
  evidence_counts: { supporting: 1, conflicting: 0, mixed: 0 }, stale: { is_stale: false, reasons: [] }, graph_state: 'complete' }, o);
t('claim row shows status, confidence, stale and conflicting flags', () => {
  const h = K.claimRow(claim({ status: 'conflicting', confidence: 0.3, evidence_counts: { supporting: 0, conflicting: 2, mixed: 0 }, stale: { is_stale: true, reasons: [{ code: 'age' }] } }));
  assert(h.includes('Conflicting') && h.includes('30%') && h.includes('Possibly stale') && h.includes('2 conflicting') && h.includes('Not re-verified recently'));
});
t('a failed-state claim is flagged, a complete one is not', () => {
  assert(K.claimRow(claim({ graph_state: 'failed' })).includes('Failed'));
  assert(K.claimRow(claim({ graph_state: 'partial' })).includes('Partial'));
  assert(!K.claimRow(claim()).includes('Complete'));  // complete is the unmarked default; only deviations are badged
});
t('evidence row distinguishes supporting / conflicting and changed sources', () => {
  const ev = { evidence_id: 'evd_' + 'b'.repeat(32), passage: 'p', strength: 0.7, source: { domain: 'a.com', freshness: { label: 'old', age_days: 900 }, changed_since_verified: true } };
  assert(K.evidenceRow(ev, 'conflicting').includes('km-ev-conflict'));
  assert(K.evidenceRow(ev, 'supporting').includes('km-ev-support'));
  const h = K.evidenceRow(ev, 'supporting'); assert(h.includes('Old') && h.includes('900d') && h.includes('Source changed since') && h.includes('70%'));
});

// ── XSS: every dynamic field is escaped ──────────────────────────────────────
t('claim / entity / evidence / change rows escape hostile text', () => {
  noRawTags(K.claimRow(claim({ text: XSS })));
  noRawTags(K.entityRow({ entity_id: 'ent_' + 'c'.repeat(32), name: XSS, claim_count: 1, source_count: 1, graph_state: 'complete' }));
  noRawTags(K.evidenceRow({ evidence_id: 'evd_1', passage: XSS, strength: 1, source: { domain: XSS, freshness: { label: 'fresh' } } }, 'mixed'));
  noRawTags(K.changeRow('new', { claim_id: 'clm_1', text: XSS, base: null, target: { status: 'verified', confidence: 1 }, reasons: [XSS] }));
});
t('attribute injection through ids is escaped', () => {
  const h = K.claimRow(claim({ claim_id: '"><script>alert(1)</script>' }));
  assert(!h.includes('<script>') && h.includes('&quot;&gt;&lt;script&gt;'));
});
t('claim detail escapes history topics and shows state banner for untrusted claims', () => {
  const h = K.claimDetailHtml(Object.assign(claim({ graph_state: 'failed', text: XSS }), {
    last_verified_at: '2026-06-01T00:00:00+00:00', first_seen_at: '2026-01-10T00:00:00+00:00', entities: [{ entity_id: 'ent_1', name: XSS }],
    history: [{ observed_at: '2026-06-01T00:00:00+00:00', topic: XSS, status: 'verified', confidence: 0.9, graph_state: 'failed' }] }));
  noRawTags(h);
  assert(h.includes('Knowledge state: Failed') && h.includes('role="alert"') && h.includes('2026-06-01'));
});
t('claim detail shows stale reasons and last verified', () => {
  const h = K.claimDetailHtml(Object.assign(claim({ stale: { is_stale: true, reasons: [{ code: 'source_changed' }, { code: 'sources_old' }] } }), { last_verified_at: null, first_seen_at: null, entities: [], history: [] }));
  assert(h.includes('A supporting source has changed') && h.includes('All supporting sources are old'));
  assert(h.includes('Last verified') && h.includes('—'));
});

// ── what changed ─────────────────────────────────────────────────────────────
const diff = () => ({ base: { topic: 'Energy', created_at: '2026-01-10T09:00:00+00:00' }, target: { topic: 'Energy', created_at: '2026-06-01T09:00:00+00:00' }, warnings: [],
  summary: { new: 1, strengthened: 1, weakened: 1, contradicted: 1, unchanged: 0, not_reobserved: 1, stale: 1 },
  claims: { new: [{ claim_id: 'clm_n', text: 'New one', base: null, target: { status: 'verified', confidence: 0.8 } }],
    strengthened: [{ claim_id: 'clm_s', text: 'Up', base: { status: 'partially_verified', confidence: 0.5 }, target: { status: 'verified', confidence: 0.9 }, reasons: ['confidence 0.50 → 0.90'] }],
    weakened: [{ claim_id: 'clm_w', text: 'Down', base: { status: 'verified', confidence: 0.9 }, target: { status: 'partially_verified', confidence: 0.4 }, reasons: [] }],
    contradicted: [{ claim_id: 'clm_c', text: 'Contra', base: { status: 'verified', confidence: 0.8 }, target: { status: 'conflicting', confidence: 0.3 }, reasons: ['status became conflicting'] }],
    unchanged: [], not_reobserved: [{ claim_id: 'clm_g', text: 'Gone', base: { status: 'verified', confidence: 0.7 }, target: null }],
    stale: [{ claim_id: 'clm_g', text: 'Gone', base: null, target: null, stale: { is_stale: true, reasons: [{ code: 'age' }] } }] },
  evidence: { added: [1], removed: [1], stance_changed: [1], stale: [1] }, sources: { changed: [1] } });
t('diff renders every class, before→after, and orders contradicted first', () => {
  const h = K.diffHtmlFor(diff());
  for (const label of ['Contradicted', 'Weakened', 'Strengthened', 'New', 'Possibly stale', 'Not re-observed']) assert(h.includes(label), label);
  assert(h.includes('Partially verified · 50% → Verified · 90%') && h.includes('status became conflicting'));
  assert(h.indexOf('Contra') < h.indexOf('Down') && h.indexOf('Down') < h.indexOf('Up'));
  assert(h.includes('1 source(s) changed content') && !h.includes('Unchanged (0)'));
});
t('diff shows partial-input warnings prominently and an escaped error for refusals', () => {
  const d = diff(); d.warnings = ['Investigation ' + XSS + ' has partial knowledge'];
  const h = K.diffHtmlFor(d); noRawTags(h); assert(h.includes('role="alert"'));
  const e = K.diffHtmlFor(null, 'Knowledge for investigation ' + XSS + " is 'failed'");
  noRawTags(e); assert(e.includes('role="alert"') && e.includes('failed'));
  assert(K.diffHtmlFor(null).includes('Pick two investigations'));
});

// ── investigations table ─────────────────────────────────────────────────────
const inv = (o) => Object.assign({ report_id: 'report_1', topic: 'T', created_at: '2026-01-10T00:00:00', knowledge_state: 'complete', is_complete: true, comparable: true,
  claim_count: 2, source_count: 3, dropped: { sources: 0, evidence: 0, claims: 0 }, error_type: null }, o);
t('investigations table: only non-complete rows offer rebuild; failed shows error type only', () => {
  const h = K.investigationsHtml([inv(), inv({ report_id: 'report_f', knowledge_state: 'failed', is_complete: false, error_type: 'IntegrityError' }),
    inv({ report_id: 'report_p', knowledge_state: 'partial', is_complete: false, dropped: { sources: 1, evidence: 2, claims: 0 } }),
    inv({ report_id: 'report_n', knowledge_state: 'not_recorded', is_complete: false })]);
  assert.strictEqual((h.match(/data-act="rebuild"/g) || []).length, 3);
  assert(!h.includes('data-id="report_1"'));
  assert(h.includes('IntegrityError') && h.includes('3 input(s) dropped'));
  noRawTags(K.investigationsHtml([inv({ topic: XSS, report_id: XSS, error_type: XSS, knowledge_state: 'failed', is_complete: false })]));
  assert(K.investigationsHtml([]).includes('No investigations yet'));
});

// ── graph layout ─────────────────────────────────────────────────────────────
const nodes = [
  { id: 's1', type: 'source', label: 'src', graph_state: 'complete' }, { id: 'c2', type: 'claim', label: 'b', graph_state: 'complete' },
  { id: 'e1', type: 'entity', label: 'ent', graph_state: 'complete' }, { id: 'c1', type: 'claim', label: 'a', graph_state: 'failed' },
  { id: 'v1', type: 'evidence', label: 'ev', graph_state: 'complete' }];
const edges = [{ source: 'c1', target: 'e1', type: 'mentions' }, { source: 'c1', target: 'v1', type: 'supported_by' }, { source: 'v1', target: 's1', type: 'from_source' }, { source: 'ghost', target: 'e1', type: 'mentions' }];
t('layout: columns follow Entity → Claim → Evidence → Source and are deterministic', () => {
  const L = K.layoutGraph(nodes, edges), by = Object.fromEntries(L.nodes.map(x => [x.id, x]));
  assert(by.e1.x < by.c1.x && by.c1.x < by.v1.x && by.v1.x < by.s1.x);
  assert(by.c1.y < by.c2.y);  // alphabetical within a column
  assert.deepStrictEqual(K.layoutGraph(nodes, edges), K.layoutGraph(nodes.slice().reverse(), edges));
});
t('layout: edges never reference missing nodes and always run left→right', () => {
  const L = K.layoutGraph(nodes, edges);
  assert.strictEqual(L.edges.length, 3);  // 'ghost' edge dropped
  for (const e of L.edges) assert(e.x1 < e.x2);
  assert(L.width > 0 && L.height > 0);
  assert.deepStrictEqual(K.layoutGraph([], []).nodes, []);
});

// ── misc helpers ─────────────────────────────────────────────────────────────
t('confidence bar clamps and formats', () => {
  assert(K.confidenceBar(2).includes('width:100%') && K.confidenceBar(-1).includes('width:0%') && K.confidenceBar('x').includes('0%'));
  assert.strictEqual(K.pct(0.456), '46%'); assert.strictEqual(K.pct(null), '—');
});
t('dates', () => { assert.strictEqual(K.fmtDate('2026-06-01T09:00:00+00:00'), '2026-06-01'); for (const bad of [null, '', 'nope']) assert.strictEqual(K.fmtDate(bad), '—'); });
t('freshness badge', () => { assert(K.freshnessBadge({ label: 'unknown', age_days: null }).includes('Date unknown')); assert(K.freshnessBadge(undefined).includes('Date unknown')); });

// ══ M7: challenges ═══════════════════════════════════════════════════════════
const detail = (o) => Object.assign(claim({ claim_id: 'clm_' + 'd'.repeat(32), investigation: { report_id: 'report_abc', topic: 'T' }, challenges_detail: [] }), o);
t('challenge box: six types, run button wired to claim + investigation', () => {
  const h = K.challengeBoxHtml(detail());
  for (const [k] of K.CHALLENGE_TYPES) assert(h.includes(`value="${k}"`), k);
  assert.strictEqual(K.CHALLENGE_TYPES.length, 6);
  assert(h.includes('data-act="challenge-run"') && h.includes('data-inv="report_abc"') && h.includes('data-id="clm_' + 'd'.repeat(32) + '"'));
  assert(!/data-act="challenge-run"[^>]*disabled/.test(h));
});
t('challenge box: partial may be challenged; failed / unrecorded may not', () => {
  assert(K.challengeBoxHtml(detail({ graph_state: 'partial' })).includes('data-act="challenge-run"'));
  for (const st of ['failed', 'not_recorded']) {
    const h = K.challengeBoxHtml(detail({ graph_state: st }));
    assert(!h.includes('data-act="challenge-run"') && h.includes('role="alert"') && /Rebuild it before challenging/.test(h), st);
  }
  assert(!K.challengeBoxHtml(detail({ investigation: undefined })).includes('data-act="challenge-run"'));  // nothing to target
});
t('challenge box lists past challenges with status, result and counter-evidence count', () => {
  const h = K.challengeBoxHtml(detail({ challenges_detail: [
    { challenge_id: 'chl_' + 'a'.repeat(32), challenge_type: 'counter_evidence', status: 'completed', result: 'contradicted', completed_at: '2026-06-01T00:00:00+00:00', counter_evidence_count: 2 },
    { challenge_id: 'chl_' + 'b'.repeat(32), challenge_type: 'temporal_validity', status: 'failed', result: null, created_at: '2026-06-02T00:00:00+00:00', counter_evidence_count: 0 }] }));
  assert(h.includes('Past challenges (2)') && h.includes('Contradicted') && h.includes('2 counter') && h.includes('Failed') && h.includes('Temporal validity'));
  assert((h.match(/data-act="challenge-open"/g) || []).length === 2);
});
t('all six results have distinct, labelled badges; unknown values are neutral', () => {
  const labels = { supported: 'Supported', weakened: 'Weakened', contradicted: 'Contradicted', unresolved: 'Unresolved', stale: 'Stale', source_concern: 'Source concern' };
  const seen = new Set();
  for (const [k, l] of Object.entries(labels)) { const h = K.resultBadge(k); assert(h.includes(l), k); seen.add(h); }
  assert.strictEqual(seen.size, 6);
  assert(K.resultBadge('contradicted').includes('badge-terracotta') && K.resultBadge('supported').includes('badge-jade'));
  assert(K.resultBadge('<img src=x onerror=alert(1)>').includes('badge-neutral')); noRawTags(K.resultBadge(XSS));
  assert(K.challengeStatusBadge('failed').includes('badge-terracotta') && K.challengeStatusBadge('running').includes('Running'));
});
t('interpretation block: unchallenged vs challenged, and says adjusted confidence is a heuristic', () => {
  assert(K.interpretationHtml({ label: 'unchallenged' }).includes('Not challenged yet') && K.interpretationHtml(undefined).includes('Not challenged yet'));
  const h = K.interpretationHtml({ label: 'contradicted', summary: 'Contradicted by challenge evidence (counter evidence).', original_status: 'verified', original_confidence: 0.9, adjusted_confidence: 0.5 });
  assert(h.includes('UPDATED INTERPRETATION') && h.includes('Contradicted') && h.includes('90%') && h.includes('50%') && /heuristic/.test(h) && /original record is unchanged/.test(h));
  noRawTags(K.interpretationHtml({ label: 'weakened', summary: XSS, original_status: XSS, original_confidence: 1, adjusted_confidence: 1 }));
});
t('claim row shows a Challenged badge only when challenged', () => {
  assert(!K.claimRow(claim({ interpretation: { label: 'unchallenged', summary: '' } })).includes('Challenged:'));
  assert(K.claimRow(claim({ interpretation: { label: 'weakened', summary: 'Weakened by x' } })).includes('Challenged: Weakened'));
  noRawTags(K.claimRow(claim({ interpretation: { label: 'weakened', summary: XSS } })));
});
const completed = (o) => Object.assign({ challenge_id: 'chl_' + 'c'.repeat(32), challenge_type: 'counter_evidence', status: 'completed', result: 'contradicted', error_type: null, retryable: false,
  dropped_unverified_evidence: 0, sources: [{}, {}], attempts: [{ attempt_no: 1, status: 'completed', started_at: '2026-06-01T00:00:00+00:00', finished_at: '2026-06-01T00:01:00+00:00', error_type: null }],
  evidence: [{ evidence_id: 'evd_1', role: 'counter', stance: 'contradict', strength: 0.9, passage: 'Output fell.', source: { url: 'https://news.example.io/a', domain: 'news.example.io', independent: true, publish_date: '2026-09-01' } },
             { evidence_id: 'evd_2', role: 'supporting', stance: 'support', strength: 0.4, passage: 'It held.', source: { url: 'https://a.example.com/x', domain: 'a.example.com', independent: false, publish_date: null } }],
  trail: [{ step: 'claim', text: 'A claim', status: 'verified', confidence: 0.9 }, { step: 'evidence', items: [{ url: 'u' }] }, { step: 'challenge', challenge_type: 'counter_evidence', queries: ['A claim — evidence against'] },
          { step: 'counter_evidence', items: [{ role: 'counter' }, { role: 'supporting' }] }, { step: 'result', value: 'contradicted', rationale: 'Independent evidence contradicts it.' },
          { step: 'updated_interpretation', label: 'contradicted', summary: 'Contradicted.', adjusted_confidence: 0.5 }] }, o);
t('inspect: the trail reads Claim → Evidence → Challenge → Counter-evidence → Result → Updated interpretation, in order', () => {
  const h = K.challengeInspectHtml(completed());
  const order = ['Claim', 'Evidence', 'Challenge', 'Counter-evidence', 'Result', 'Updated interpretation'].map(x => h.indexOf(`<span class="km-trail-name">${x}</span>`));
  assert(order.every(i => i >= 0) && order.every((v, i) => i === 0 || v > order[i - 1]), JSON.stringify(order));
  assert(h.includes('1 counter · 1 supporting · 0 qualifying') && h.includes('Independent evidence contradicts it.') && h.includes('adjusted confidence 50%'));
});
t('inspect: evidence shows role, independence and a safe external link', () => {
  const h = K.challengeInspectHtml(completed());
  assert(h.includes('Counter-evidence</span>') && h.includes('Supporting</span>') && h.includes('Independent source') && h.includes('Same domain as original'));
  assert(/<a href="https:\/\/news\.example\.io\/a" target="_blank" rel="noopener noreferrer">/.test(h) && h.includes('every item cites a retrieved source'));
});
t('inspect: a failed challenge shows the failure and Retry, never a result or trail', () => {
  const h = K.challengeInspectHtml(completed({ status: 'failed', result: null, error_type: 'ChallengeToolFailure', retryable: true, evidence: [], trail: [],
    attempts: [{ attempt_no: 1, status: 'failed', started_at: '2026-06-01T00:00:00+00:00', finished_at: '2026-06-01T00:00:30+00:00', error_type: 'ChallengeToolFailure' }] }));
  assert(h.includes('This challenge failed') && h.includes('ChallengeToolFailure') && h.includes('data-act="challenge-retry"') && h.includes('role="alert"'));
  assert(!h.includes('km-trail') && !h.includes('Contradicted</span>') && !h.includes('Evidence found'));
});
t('inspect: no retry once attempts are exhausted', () => {
  const h = K.challengeInspectHtml(completed({ status: 'failed', result: null, retryable: false, error_type: 'ChallengeToolFailure', evidence: [], trail: [] }));
  assert(!h.includes('data-act="challenge-retry"') && /maximum number of attempts/.test(h));
});
t('inspect: empty and discarded evidence are explained', () => {
  assert(K.challengeInspectHtml(completed({ evidence: [], result: 'unresolved' })).includes('No evidence was found'));
  assert(K.challengeInspectHtml(completed({ dropped_unverified_evidence: 2 })).includes('2 item(s) were discarded because they did not cite a page that was actually retrieved'));
});
t('inspect: attempt history is listed', () => {
  const h = K.challengeInspectHtml(completed({ attempts: [{ attempt_no: 1, status: 'failed', started_at: '2026-06-01T00:00:00+00:00', finished_at: null, error_type: 'X' },
    { attempt_no: 2, status: 'completed', started_at: '2026-06-02T00:00:00+00:00', finished_at: '2026-06-02T00:01:00+00:00', error_type: null }] }));
  assert(h.includes('Attempts') && h.includes('2026-06-02') && (h.match(/<tr><td>\d<\/td>/g) || []).length === 2);
});
t('inspect escapes every dynamic field', () => {
  const evil = completed({ challenge_type: XSS, error_type: XSS, status: 'completed' });
  evil.evidence[0].passage = XSS; evil.evidence[0].source.domain = XSS; evil.evidence[0].source.url = '"><script>alert(1)</script>';
  evil.trail[0].text = XSS; evil.trail[4].rationale = XSS; evil.trail[2].queries = [XSS];
  const h = K.challengeInspectHtml(evil); noRawTags(h); assert(!h.includes('<script>'));
  const f = K.challengeInspectHtml(completed({ status: 'failed', result: null, retryable: true, error_type: XSS, challenge_id: XSS, evidence: [], trail: [] })); noRawTags(f);
  noRawTags(K.challengeBoxHtml(detail({ claim_id: XSS, investigation: { report_id: XSS } })));
});
t('graph layout gives challenge and counter-evidence their own columns, left to right', () => {
  const ns = [{ id: 'c', type: 'claim', label: 'c' }, { id: 's', type: 'source', label: 's' }, { id: 'h', type: 'challenge', label: 'h' }, { id: 'k', type: 'counter', label: 'k' }];
  const es = [{ source: 'c', target: 'h', type: 'challenged_by' }, { source: 'h', target: 'k', type: 'counter_evidence' }];
  const L = K.layoutGraph(ns, es), by = Object.fromEntries(L.nodes.map(x => [x.id, x]));
  assert(by.c.x < by.s.x && by.s.x < by.h.x && by.h.x < by.k.x);
  assert.strictEqual(L.edges.length, 2); for (const e of L.edges) assert(e.x1 < e.x2);
  assert(L.width > K.layoutGraph(ns.slice(0, 2), []).width);
});

console.log('knowledge_ui: ' + n + ' passed');
