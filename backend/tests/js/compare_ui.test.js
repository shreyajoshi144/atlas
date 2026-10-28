// Pure-function tests for the Compare view: safe rendering, missing values, gating, honest wording.
const assert = require('assert');
global.window = {};
global.escapeHtml = (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.escapeAttr = global.escapeHtml;
require('../../../frontend/js/compare.js');
const C = global.window.AtlasCompare;
let passed = 0;
function test(name, fn) { try { fn(); passed++; } catch (e) { console.error('FAIL: ' + name); throw e; } }
const noExec = (html) => {
  assert(!/<script/i.test(html), 'raw <script>');
  assert(!/<img/i.test(html), 'raw <img>');
  assert(!/<[^>]*\son\w+\s*=/i.test(html.replace(/"[^"]*"/g, '""')), 'inline handler attribute inside a real tag');
  assert(!/href="\s*javascript:/i.test(html), 'javascript: href');
  assert(!/href="data:/i.test(html), 'data: href');
};
const XSS = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '"><svg onload=alert(1)>', '[x](javascript:alert(1))', '[x](JaVaScRiPt:alert(1))', '[x](data:text/html,<script>1</script>)'];
const okResult = (over) => Object.assign({ id: 'openai:m', provider: 'openai', model: 'm', label: 'M', status: 'ok', answer: 'hello', citations: [], usage: {}, cost: {}, latency: {}, safety: { flags: [] } }, over);

test('markdown: raw HTML and unsafe links never survive', () => {
  for (const x of XSS) noExec(C.safeMarkdown(x + '\n\n- ' + x + '\n\n**' + x + '**'));
  assert(C.safeMarkdown('[ok](https://example.com/a?b=1&c=2)').includes('href="https://example.com/a?b=1&amp;c=2"'));
  assert(C.safeMarkdown('[ok](https://example.com)').includes('rel="noopener noreferrer"'));
  assert(!C.safeMarkdown('[bad](javascript:alert(1))').includes('<a '));
});
test('markdown renders basic structure', () => {
  const h = C.safeMarkdown('# Title\n\n- a\n- b\n\n1. one\n\nplain **bold** `code`');
  assert(h.includes('<ul><li>a</li><li>b</li></ul>') && h.includes('<ol>') && h.includes('<strong>bold</strong>') && h.includes('<code>code</code>'));
});
test('safeUrl allows only http(s)', () => {
  assert.strictEqual(C.safeUrl('https://a.com/x'), 'https://a.com/x');
  for (const u of ['javascript:alert(1)', 'data:text/html,x', 'ftp://a.com', '//a.com', 'https://a.com/" onclick="x', null, undefined, 5, 'https://a.com/<b>']) assert.strictEqual(C.safeUrl(u), null, String(u));
});
test('XSS payloads in answer, labels, errors, citations, sources are escaped', () => {
  for (const x of XSS) {
    const h = C.resultCardHtml(okResult({ answer: x, label: x, model: x, provider: x, id: x,
      citations: [{ kind: 'provider_web', url: x, title: x, snippet: x, valid: true }, { kind: 'atlas_source', source_id: x, title: x, valid: false }],
      safety: { flags: [x] }, structured: { valid: false, errors: [x], data: { claims: [{ text: x, sources: [x] }] } },
      grounding: { invalid_labels: [x], notes: [x], method: x } }));
    noExec(h);
    const e = C.resultCardHtml(okResult({ status: 'failed', answer: null, error: { category: x, message: x, retryable: true } }));
    noExec(e);
    noExec(C.sourcesHtml([{ id: x, kind: 'web', title: x, url: x, domain: x, excerpt: x, risk_flags: [x] }]));
    noExec(C.summaryHtml({ measurements: [{ key: x, label: x, winner_id: x, value: 1, unit: x }], notes: [x] }));
    noExec(C.evaluationHtml({ message: x }, { explanation: x, runs: [{ dataset: { name: x, version: x, n_cases: 3 }, ran_at: x, models: [{ id: x, n: 3, pass_rate: 0.5, ci95: [0.1, 0.9] }], caveats: [x] }] }));
  }
});
test('citation links are filtered to http/https', () => {
  const h = C.citationsHtml([{ kind: 'provider_web', url: 'javascript:alert(1)', title: 'bad' }, { kind: 'provider_web', url: 'https://ok.example/p', title: 'good' }]);
  assert(!h.includes('javascript:'));
  assert(h.includes('href="https://ok.example/p"') && h.includes('noopener noreferrer'));
  assert(C.citationsHtml([]).includes('No citations returned'));
});
test('missing values render as Not reported, never zero', () => {
  const h = C.resultCardHtml(okResult());
  assert(h.includes('Not reported'));
  assert(!/\$0\.0000|>0 ms|>0<\/dd>/.test(h), 'invented zero');
  for (const v of [null, undefined, '', 'x', -1, NaN, Infinity]) { assert.strictEqual(C.formatUsd(v), 'Not reported'); assert.strictEqual(C.formatInt(v), 'Not reported'); assert.strictEqual(C.formatMs(v), 'Not reported'); assert.strictEqual(C.formatRate(v), 'Not reported'); }
  assert.strictEqual(C.formatUsd(0), '$0.0000'); assert.strictEqual(C.formatInt(0), '0'); assert.strictEqual(C.formatMs(0), '0 ms');
  assert.strictEqual(C.formatRate(0.5), '50%'); assert.strictEqual(C.formatUsd(0.00002), '<$0.0001');
});
test('usage block shows real numbers, basis and retries', () => {
  const h = C.resultCardHtml(okResult({ usage: { input_tokens: 1200, output_tokens: 300, total_tokens: 1500 }, cost: { usd: 0.0123, complete: true, basis: 'catalog_price_estimate', price_verified_on: '2026-10-20' },
    latency: { total_ms: 2300, retries: 1, attempts: [{ n: 1, ms: 900, outcome: 'error', category: 'rate_limit' }, { n: 2, ms: 1400, outcome: 'ok' }] } }));
  assert(h.includes('1,200') && h.includes('$0.0123') && h.includes('2.3 s') && h.includes('2026-10-20') && h.includes('not an invoice') && h.includes('rate_limit') && h.includes('estimated from token counts'));
});
test('truncation, failure explanation and status labels', () => {
  assert(C.resultCardHtml(okResult({ truncated: true })).includes('cut off'));
  const f = C.resultCardHtml(okResult({ status: 'timeout', answer: null, error: { category: 'timeout', message: 'slow', retryable: true } }));
  assert(f.includes('Timed out') && f.includes('did not answer within the time limit') && f.includes('Retrying may work'));
  assert(C.resultCardHtml(okResult({ status: 'blocked', answer: null, error: { category: 'budget', message: 'cap' } })).includes('Blocked'));
});
test('grounding block is labelled as a heuristic and handles absence', () => {
  assert(C.groundingHtml(null).includes('No grounding check ran'));
  const h = C.groundingHtml({ valid_citation_rate: 1, sentence_coverage: 0.5, support_rate: null, supported: 0, checked: 0, invalid_labels: ['D9'], method: 'lexical-overlap heuristic' });
  assert(h.includes('D9') && h.includes('not proof') && h.includes('Not reported'));
});
test('summary never claims a best model', () => {
  const html = C.summaryHtml({ measurements: [{ key: 'fastest', label: 'Fastest response', winner_id: 'a:x', value: 800, unit: 'ms' }, { key: 'cheapest', label: 'Lowest estimated cost', winner_id: 'b:y', value: 0.002, unit: 'usd' }], notes: ['n'] });
  assert(/not a verdict/.test(html) && html.includes('800 ms') && html.includes('$0.0020'));
  const all = [html, C.evaluationHtml({ message: '' }, null), C.glossaryHtml(), C.resultCardHtml(okResult())].join(' ').toLowerCase();
  for (const w of ['best model', 'winner', 'smartest', 'most accurate', 'recommended model', 'wins']) assert(!all.includes(w), w);
  assert.strictEqual(C.summaryHtml({}), '');
});
test('evaluation evidence: none, overlapping, separated', () => {
  const none = C.evaluationHtml({ message: 'No evaluation has been run.' }, { available: false, runs: [] });
  assert(none.includes('No measured evaluation evidence yet') && none.includes('no ranking is shown') && none.includes('No evaluation has been run.'));
  const run = (rank) => ({ available: true, runs: [{ dataset: { name: 'ds', version: '1', n_cases: 24 }, ran_at: '2026-10-20', ranking_supported: rank, caveats: ['small set'], models: [{ id: 'a:x', n: 24, pass_rate: 0.7, ci95: [0.5, 0.85] }, { id: 'b:y', n: 24, pass_rate: 0.6, ci95: [0.4, 0.78] }] }] });
  const ov = C.evaluationHtml(null, run(false));
  assert(ov.includes('cannot rank') && ov.includes('70%') && ov.includes('50% to 85%') && ov.includes('24 cases') && ov.includes('small set'));
  const sep = C.evaluationHtml(null, run(true));
  assert(sep.includes('do not overlap') && !sep.includes('cannot rank'));
});
test('sources panel shows risk flags and filters urls', () => {
  const h = C.sourcesHtml([{ id: 'W1', kind: 'web', title: 'T', url: 'javascript:alert(1)', excerpt: 'e', risk_flags: ['instruction_override'] }, { id: 'D1', kind: 'document', title: 'Doc', excerpt: 'x', risk_flags: [] }]);
  assert(h.includes('instruction-like text') && h.includes('instruction_override') && !h.includes('javascript:') && h.includes('Your library'));
  assert.strictEqual(C.sourcesHtml([]), '');
});
const models = [
  { id: 'a:x', configured: true, est_max_cost_usd: 0.10 }, { id: 'b:y', configured: true, est_max_cost_usd: 0.20 },
  { id: 'c:z', configured: false, est_max_cost_usd: 0.01 }, { id: 'd:u', configured: true, est_max_cost_usd: null },
];
const base = (o) => Object.assign({ models, selected: ['a:x', 'b:y'], prompt: 'What is up?', consent: true, mode: 'compare', maxOut: '',
  limits: { min_models: 1, max_models: 4, max_prompt_chars: 100, default_max_output_tokens: 1000, max_output_tokens: 4000, max_cost_per_request_usd: 1 } }, o);
test('cost-estimate gating', () => {
  assert(C.gateRun(base()).ok);
  assert.strictEqual(Math.round(C.gateRun(base()).estimate.usd * 100) / 100, 0.3);
  const scaled = C.gateRun(base({ maxOut: '4000' })); // 4x -> 1.2 > 1
  assert(!scaled.ok && scaled.reasons.some(r => r.includes('per-request ceiling')));
  assert(!C.gateRun(base({ limits: { ...base().limits, max_cost_per_request_usd: 0.25 } })).ok);
  const unk = C.gateRun(base({ selected: ['a:x', 'd:u'] }));
  assert.strictEqual(unk.estimate.usd, null); assert(unk.ok, 'unknown price is left to the server');
  assert.strictEqual(C.estimateRunCost(models, [], '', 1000).usd, null);
});
test('consent, selection and prompt gating', () => {
  assert(!C.gateRun(base({ consent: false })).ok);
  assert(C.gateRun(base({ consent: false })).reasons.some(r => r.includes('Confirm')));
  assert(!C.gateRun(base({ selected: [] })).ok);
  assert(!C.gateRun(base({ selected: ['a:x', 'b:y', 'c:z', 'd:u', 'a:x'] })).ok);
  assert(!C.gateRun(base({ prompt: '  ' })).ok);
  assert(!C.gateRun(base({ prompt: 'x'.repeat(101) })).ok);
  assert(!C.gateRun(base({ selected: ['c:z'] })).ok, 'unconfigured provider blocks');
  assert(!C.gateRun(base({ maxOut: '5000' })).ok);
  assert(C.gateRun(base({ mode: 'ask', selected: ['a:x'] })).ok);
  assert(!C.gateRun(base({ mode: 'ask', selected: ['a:x', 'b:y', 'd:u', 'a:x'] })).ok);
});
test('idempotency keys are unique, well-formed and not guessable from time alone', () => {
  const keys = new Set(); for (let i = 0; i < 200; i++) keys.add(C.newIdempotencyKey());
  assert.strictEqual(keys.size, 200);
  for (const k of keys) assert(/^cmp-[A-Za-z0-9-]{12,}$/.test(k), k);
});
test('glossary covers required concepts', () => {
  const t = C.GLOSSARY.map(g => g[0].toLowerCase()).join('|');
  for (const w of ['token', 'latency', 'invoice', 'grounding', 'citation', 'reliability', 'structured', 'anecdote', 'evidence modes']) assert(t.includes(w), w);
  assert(C.glossaryHtml().includes('How to read this page'));
});
console.log(`compare_ui.test.js: ${passed} tests passed`);
