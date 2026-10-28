// Node-run tests for frontend/js/routing.js: HTML builders, honest rendering of unknown cost/tokens,
// unmissable fallbacks, form coercion, and XSS safety.
const assert = require('assert');
const path = require('path');
const R = require(path.join(__dirname, '..', '..', '..', 'frontend', 'js', 'routing.js'));
const XSS = '<img src=x onerror=alert(1)>';
const noRaw = (h) => { assert(!/<img\b/i.test(h), 'unescaped <img>: ' + h.slice(0, 160)); assert(!/<script/i.test(h)); };
let n = 0, failed = 0;
const t = (name, fn) => { try { fn(); n++; } catch (e) { failed++; console.error('FAIL ' + name + '\n  ' + String(e.message).split('\n').slice(0, 3).join('\n  ')); } };

const row = (o) => Object.assign({ usage_id: 1, created_at: '2026-10-09T10:15:00+00:00', purpose: 'research', mode: 'autopilot', reason_code: 'autopilot_selected',
  reason: 'Autopilot: cheapest of 2 qualifying models', requested_model: 'openai/gpt-oss-20b', actual_model: 'openai/gpt-oss-20b', status: 'ok', fallback_used: false, fallback_reason: null,
  attempts: [{ model: 'openai/gpt-oss-20b', outcome: 'ok' }], prompt_tokens: 1000, completion_tokens: 200, total_tokens: 1200, tokens_reported: true,
  est_cost_usd: 0.0003, actual_cost_usd: 0.00028, latency_ms: 1350 }, o);

t('formatters never invent numbers', () => {
  assert.strictEqual(R.ms(420), '420 ms'); assert.strictEqual(R.ms(1350), '1.4 s'); assert.strictEqual(R.ms(null), '—'); assert.strictEqual(R.ms(NaN), '—');
  assert.strictEqual(R.usd(0.00028), '$0.0003'); assert.strictEqual(R.usd(null), '—'); assert.strictEqual(R.usd(undefined), '—'); assert.strictEqual(R.usd(0), '$0.0000');
  assert.strictEqual(R.num(12345), '12,345'); assert.strictEqual(R.num(null), '—');
});
t('reason badges explain the decision type, with a safe fallback for unknown codes', () => {
  assert(R.reasonBadge('autopilot_selected', 'why').includes('Autopilot') && R.reasonBadge('autopilot_selected', 'why').includes('title="why"'));
  assert(R.reasonBadge('autopilot_no_evidence').includes('no benchmark yet') && R.reasonBadge('autopilot_no_qualifying_model').includes('nothing qualified'));
  assert(R.reasonBadge('routing_unavailable').includes('badge-terracotta') && R.reasonBadge('explicit').includes('Explicit'));
  assert(R.reasonBadge('something_new').includes('something_new') && R.reasonBadge(null).includes('Unknown'));
});
t('a normal call shows the answering model; a fallback shows requested → actual with a Fallback marker and the reason', () => {
  assert(R.modelFlow(row()).includes('<strong>openai/gpt-oss-20b</strong>') && !R.modelFlow(row()).includes('Fallback'));
  const fb = R.modelFlow(row({ fallback_used: true, actual_model: 'llama-3.3-70b-versatile', fallback_reason: 'openai/gpt-oss-20b: rate limited (HTTP 429) → llama-3.3-70b-versatile' }));
  assert(fb.includes('rt-strike') && fb.includes('→') && fb.includes('llama-3.3-70b-versatile') && fb.includes('Fallback') && fb.includes('rate limited (HTTP 429)'));
  const failed = R.modelFlow(row({ status: 'failed', actual_model: null }));
  assert(failed.includes('Failed') && !failed.includes('<strong>'));
});
t('cost cells distinguish pre-call and usage-based estimates without claiming provider-billed cost', () => {
  const c = R.costCell(row());
  assert(c.includes('$0.0003') && c.includes('Pre-call estimate: $0.0003') && c.includes('Usage-based estimate') && c.includes('not provider-billed') === false);
  const unknown = R.costCell(row({ actual_cost_usd: null, tokens_reported: false }));
  assert(unknown.includes('Not reported') && !unknown.includes('$0.0000'));
  assert(!R.costCell(row({ status: 'failed', actual_cost_usd: null })).includes('unknown'));
});
t('usage table: model, reason, tokens, cost, time, result — and every unknown is labelled', () => {
  const h = R.usageTable([row(), row({ usage_id: 2, tokens_reported: false, total_tokens: null, actual_cost_usd: null, attempts: [{}, {}] }), row({ usage_id: 3, status: 'failed', actual_model: null, fallback_reason: 'Fallback is disabled in this policy.' })]);
  assert(h.includes('Why this model') && h.includes('Research run') && h.includes('1,200') && h.includes('not reported') && h.includes('2 attempts') && h.includes('2026-10-09 10:15'));
  assert(h.includes('Fallback is disabled in this policy.') && h.includes('badge-terracotta') && h.includes('badge-jade'));
  assert(R.usageTable([]).includes('No model calls recorded yet') && R.usageTable(null).includes('No model calls recorded yet'));
});
t('usage summary is honest about incomplete cost and tokens', () => {
  const s = { total: { calls: 10, failed: 1, fallbacks: 2, fallback_rate: 0.2, total_tokens: 9000, cost_usd: 0.0123, cost_complete: false },
    by_model: [{ model: 'm1', calls: 6, failed: 1, fallbacks: 0, total_tokens: 5000, tokens_complete: false, cost_usd: 0.01, cost_complete: false, avg_latency_ms: 900 },
               { model: 'm2', calls: 4, failed: 0, fallbacks: 2, total_tokens: null, tokens_complete: false, cost_usd: null, cost_complete: false, avg_latency_ms: null }] };
  const h = R.summaryHtml(s);
  assert(h.includes('some calls could not be priced') && h.includes('20% of calls') && h.includes('$0.012') && h.includes('title="Some calls reported no tokens"') && h.includes('not provider-billed charges'));
  assert(h.includes('<th scope="row">m2</th>') && h.includes('—'));
  assert(R.summaryHtml({ total: { calls: 3, failed: 0, fallbacks: 0, fallback_rate: 0, total_tokens: null, cost_usd: null, cost_complete: false }, by_model: [] }).includes('not reported'));
  assert(R.summaryHtml({ total: { calls: 0 } }).includes('No usage') && R.summaryHtml(null).includes('No usage'));
});
const preview = (o) => Object.assign({ source: 'saved', policy_name: 'Cheap', mode: 'autopilot', prompt_tokens_assumed: 4000, fallback: { enabled: true, models: ['a', 'b'] },
  decision: { requested_model: 'openai/gpt-oss-20b', reason_code: 'autopilot_selected', reason: 'Autopilot: cheapest of 2 qualifying models. Cheaper but rejected: llama-3.1-8b-instant (quality 0.55 < required 0.80).', est_cost_usd: 0.0021,
    candidates: [{ model: 'openai/gpt-oss-20b', eligible: true, est_cost_usd: 0.002, rejected: [], evidence: { quality: 0.9, mean_call_latency_ms: 800, run_id: 'run_' + 'a'.repeat(32) } },
                 { model: 'llama-3.1-8b-instant', eligible: false, est_cost_usd: 0.0003, rejected: ['quality 0.55 < required 0.80'], evidence: { quality: 0.55, mean_call_latency_ms: 300, run_id: 'run_' + 'b'.repeat(32) } },
                 { model: 'openai/gpt-oss-120b', eligible: false, est_cost_usd: 0.004, rejected: ['no usable AtlasBench result for this model'], evidence: null }] } }, o);
t('a decision preview names the choice, the reason, the rejected cheaper model and says no model was called', () => {
  const h = R.decisionHtml(preview());
  assert(h.includes('openai/gpt-oss-20b') && h.includes('Cheaper but rejected') && h.includes('Qualifies') && h.includes('Rejected') && h.includes('quality 0.55 &lt; required 0.80'));
  assert(h.includes('0.90') && h.includes('800 ms') && h.includes('est. $0.0021') && h.includes('Fallback on: a → b') && h.includes('no model was called') && h.includes('run_aaaaaaaa'));
  assert(R.decisionHtml(preview({ fallback: { enabled: false, models: [] } })).includes('Fallback off') && R.decisionHtml(null) === '');
});
t('policies show their requirements and fallback; the active one cannot be activated again or deleted', () => {
  const p = { policy_id: 'pol_' + 'a'.repeat(32), name: 'Cheap', mode: 'autopilot', model: 'm', min_quality: 0.8, max_latency_ms: 8000, max_cost_per_call_usd: 0.02, fallback_enabled: true, fallback_models: ['x', 'y'], active: false };
  const idle = R.policyRow(p);
  assert(idle.includes('Autopilot') && idle.includes('quality ≥ 0.80') && idle.includes('≤ 8,000 ms/call') && idle.includes('≤ $0.020/call') && idle.includes('fallback → x → y') && idle.includes('data-act="rt-activate"') && !idle.includes('disabled'));
  const active = R.policyRow(Object.assign({}, p, { active: true, mode: 'explicit', fallback_enabled: false, fallback_models: [] }));
  assert(active.includes('Active') && !active.includes('rt-activate') && active.includes('disabled') && active.includes('no fallback') && !active.includes('quality ≥'));
});
t('the active banner says what happens with and without a policy', () => {
  assert(R.activeBanner({ default_model: 'm0', active_policy: null }).includes('exactly how Atlas behaved before routing') && R.activeBanner({ default_model: 'm0', active_policy: null }).includes('m0'));
  const a = R.activeBanner({ default_model: 'm0', active_policy: { name: 'P', mode: 'autopilot', model: 'm1', fallback_enabled: true } });
  assert(a.includes('Active policy') && a.includes('fallback on') && a.includes('rt-deactivate'));
});
t('the policy form lists allowed models, flags unpriced ones, and starts with autopilot/fallback sections hidden', () => {
  const h = R.policyFormHtml({ models: [{ model: 'm1', label: 'Model One', price_in: 0.5 }, { model: 'm2', label: 'Private', price_in: null }] });
  assert(h.includes('Model One') && h.includes('Private (no price)') && h.includes('id="rtAuto" hidden') && h.includes('id="rtFbPick" hidden') && h.includes('never used during AtlasBench runs') && h.includes('rt-save'));
  assert((h.match(/class="rtFb"/g) || []).length === 2 && h.includes('Preview without saving') && h.includes('~4k tokens'));
});
t('form values are coerced and fallback models are dropped unless fallback is on', () => {
  const p = R.parsePolicyForm({ name: '  P  ', mode: 'autopilot', model: 'm', min_quality: '0.9', max_latency_ms: '4500.6', max_cost_per_call_usd: 'abc', fallback_enabled: true, fallback_models: ['a', '', 'b'] });
  assert.deepStrictEqual(p, { name: 'P', mode: 'autopilot', model: 'm', min_quality: 0.9, max_latency_ms: 4501, max_cost_per_call_usd: 0.02, fallback_enabled: true, fallback_models: ['a', 'b'] });
  const off = R.parsePolicyForm({ name: '', mode: 'weird', model: 'm', fallback_enabled: false, fallback_models: ['a'] });
  assert(off.mode === 'explicit' && off.name === 'Untitled policy' && off.fallback_enabled === false && off.fallback_models.length === 0 && off.min_quality === 0.8);
});
t('hostile text in any field is escaped everywhere', () => {
  noRaw(R.usageTable([row({ requested_model: XSS, actual_model: XSS, reason: XSS, fallback_reason: XSS, purpose: XSS, reason_code: XSS, fallback_used: true })]));
  noRaw(R.usageTable([row({ status: 'failed', requested_model: XSS, fallback_reason: XSS })]));
  noRaw(R.summaryHtml({ total: { calls: 1, failed: 0, fallbacks: 0, total_tokens: 1, cost_usd: 0.1, cost_complete: true }, by_model: [{ model: XSS, calls: 1, failed: 0, fallbacks: 0, total_tokens: 1, tokens_complete: true, cost_usd: 0.1, cost_complete: true, avg_latency_ms: 1 }] }));
  const p = preview(); p.policy_name = XSS; p.source = XSS; p.fallback.models = [XSS]; p.decision.requested_model = XSS; p.decision.reason = XSS; p.decision.reason_code = XSS;
  p.decision.candidates[0].model = XSS; p.decision.candidates[1].rejected = [XSS]; p.decision.candidates[0].evidence.run_id = XSS;
  noRaw(R.decisionHtml(p));
  noRaw(R.policyRow({ policy_id: XSS, name: XSS, mode: 'autopilot', model: XSS, min_quality: 1, max_latency_ms: 1, max_cost_per_call_usd: 1, fallback_enabled: true, fallback_models: [XSS], active: false }));
  noRaw(R.activeBanner({ default_model: XSS, active_policy: { name: XSS, mode: 'explicit', model: XSS, fallback_enabled: false } }));
  noRaw(R.policyFormHtml({ models: [{ model: XSS, label: XSS, price_in: 1 }] }));
});
t('attribute values cannot break out of data-id', () => {
  const evil = '" onmouseover="alert(1)';
  assert(!R.policyRow({ policy_id: evil, name: 'x', mode: 'explicit', model: 'm', active: false, fallback_models: [] }).includes('onmouseover="alert'));
});

t('model mix ribbon shows share of calls per answering model, escaped, with a text legend', () => {
  const s = { total: { calls: 4, failed: 1, fallbacks: 1, fallback_rate: 0.25, total_tokens: 10, cost_usd: 0.1, cost_complete: true },
    by_model: [{ model: 'model-a', calls: 3, failed: 0, fallbacks: 0, total_tokens: 6, tokens_complete: true, cost_usd: 0.05, cost_complete: true, avg_latency_ms: 10 },
               { model: '<img src=x onerror=alert(1)>', calls: 1, failed: 1, fallbacks: 1, total_tokens: 4, tokens_complete: true, cost_usd: 0.05, cost_complete: true, avg_latency_ms: 20 }] };
  const h = R.summaryHtml(s);
  assert(h.includes('class="rt-mix"') && h.includes('model-a: 75% of calls'), 'ribbon and aria summary present');
  assert(h.includes('flex:3') && h.includes('flex:1'), 'segment widths are proportional to calls');
  assert(h.includes('75%') && h.includes('25%') && h.includes('1 failed') && h.includes('via fallback'), 'legend states the numbers in text');
  assert(!h.includes('<img src=x'), 'model names are escaped');
  assert(!R.summaryHtml({ total: { calls: 0 } }).includes('rt-mix'), 'no ribbon without calls');
});

console.log(`routing_ui: ${n} passed${failed ? `, ${failed} FAILED` : ''}`);
if (failed) process.exit(1);
