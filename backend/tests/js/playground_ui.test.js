// Focused helpers for the Playground's transparent cost and safe citation links.
const assert = require('assert');
global.window = {};
global.escapeHtml = (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.escapeAttr = global.escapeHtml;
global.mdToHtml = (value) => String(value);
require('../../../frontend/js/playground.js');
const P = global.window.AtlasPlayground;
let passed = 0;
function test(name, fn) { fn(); passed++; }

test('unknown cost is never rendered as free', () => {
  assert.strictEqual(P.formatCost(null), 'Not reported');
  assert.strictEqual(P.formatCost(undefined), 'Not reported');
  assert.strictEqual(P.formatCost(0), '$0.0000');
  assert.strictEqual(P.formatCost(0.00125), '$0.0013');
});
test('rendered response distinguishes pre-call and usage-based estimates', () => {
  const html = P.outputHtml({ status: 'ok', model: 'model-a', label: 'Model A', answer: 'answer', usage: {
    total_tokens: 120, estimated_cost_usd: 0.0012, actual_cost_usd: 0.0008, latency_ms: 12
  }});
  assert(html.includes('Pre-call cost estimate: $0.0012'));
  assert(html.includes('Usage-based cost estimate: $0.0008'));
  assert(html.includes('120 tokens'));
  assert(!html.includes('List estimate'));
  assert(!html.includes('actual provider-billed'));
});
test('rendered response shows usage-based estimate when pre-call estimate is missing', () => {
  const html = P.outputHtml({ status: 'ok', model: 'model-a', answer: 'answer', usage: {
    estimated_cost_usd: null, actual_cost_usd: 0.0007, total_tokens: 42
  }});
  assert(html.includes('Pre-call cost estimate: Not reported'));
  assert(html.includes('Usage-based cost estimate: $0.0007'));
});
test('missing usage metadata is not presented as zero cost or zero tokens', () => {
  const html = P.outputHtml({ status: 'ok', model: 'model-a', answer: 'answer' });
  assert(html.includes('Tokens not reported'));
  assert(html.includes('Pre-call cost estimate: Not reported'));
  assert(html.includes('Usage-based cost estimate: Not reported'));
  assert(!html.includes('$0.0000'));
});
test('legitimate zero costs and zero token counts remain valid', () => {
  const html = P.outputHtml({ status: 'ok', model: 'free-model', answer: 'answer', usage: {
    total_tokens: 0, estimated_cost_usd: 0, actual_cost_usd: 0, latency_ms: 0
  }});
  assert(html.includes('0 tokens'));
  assert(html.includes('Pre-call cost estimate: $0.0000'));
  assert(html.includes('Usage-based cost estimate: $0.0000'));
  assert(html.includes('0 ms'));
});
test('null, malformed, negative and non-finite cost/usage values are unavailable', () => {
  for (const value of [null, undefined, '', 'not-a-number', 'Infinity', Infinity, -1, NaN]) {
    assert.strictEqual(P.formatCost(value), 'Not reported');
  }
  const html = P.outputHtml({ status: 'ok', model: 'model-a', answer: 'answer', usage: {
    total_tokens: 'oops', estimated_cost_usd: 'Infinity', actual_cost_usd: 'bad', latency_ms: NaN
  }});
  assert(html.includes('Tokens not reported'));
  assert(html.includes('Pre-call cost estimate: Not reported'));
  assert(html.includes('Usage-based cost estimate: Not reported'));
  assert(!/NaN|Infinity|\$undefined/.test(html));
});
test('provider failure remains visible beside successful response and its cost metadata', () => {
  const html = P.messageHtml({ role: 'assistant', outputs: [
    { status: 'ok', model: 'model-a', label: 'Model A', answer: 'successful answer', usage: { total_tokens: 10, estimated_cost_usd: 0, actual_cost_usd: 0 } },
    { status: 'failed', model: 'model-b', label: 'Model B', error: 'Provider unavailable', usage: { estimated_cost_usd: null, actual_cost_usd: null } }
  ]});
  assert(html.includes('successful answer'));
  assert(html.includes('Provider failed'));
  assert(html.includes('Provider unavailable'));
  assert(html.includes('Usage-based cost estimate: $0.0000'));
  assert(html.includes('Usage-based cost estimate: Not reported'));
});
test('citations keep only http(s) links from model output', () => {
  const html = P.safeAnswerHtml('<a href="javascript:alert(1)" target="_blank" rel="noopener">bad</a> <a href="https://example.org/a" target="_blank" rel="noopener">good</a>');
  assert(!html.includes('javascript:'));
  assert(html.includes('bad') && !html.includes('<a href="javascript:'));
  assert(html.includes('href="https://example.org/a"') && html.includes('rel="noopener noreferrer"'));
});
test('switching to a new conversation invalidates an in-flight response', () => {
  const requests = P.createRequestEpoch();
  const state = { busy: true, conversationId: 'old-id', pendingConversationId: 'old-id', messages: [{ role: 'user', content: 'stale' }], error: 'stale error' };
  const oldConversationRequest = P.beginConversationRequest(requests, state, 'old-id');
  let rendered = false;
  P.resetForNewConversation(requests, state, () => { rendered = true; });
  assert.strictEqual(P.isRequestCurrent(requests, oldConversationRequest), false);
  assert.deepStrictEqual(state, { busy: false, conversationId: null, pendingConversationId: null, messages: [], error: null });
  assert.strictEqual(rendered, true);
  if (P.isRequestCurrent(requests, oldConversationRequest)) state.conversationId = 'stale-response';
  assert.strictEqual(state.conversationId, null);
  const newConversationRequest = requests.begin();
  assert.strictEqual(P.isRequestCurrent(requests, newConversationRequest), true);
  assert.strictEqual(P.isRequestCurrent(requests, oldConversationRequest), false);
});
test('deleting the active conversation invalidates its in-flight chat', () => {
  const requests = P.createRequestEpoch();
  const state = { busy: true, conversationId: 'active', pendingConversationId: null, messages: [{ role: 'user', content: 'pending' }], error: null };
  const chat = P.beginConversationRequest(requests, state, state.conversationId);
  assert.strictEqual(P.invalidateDeletedConversation(requests, state, 'active'), true);
  assert.strictEqual(P.isRequestCurrent(requests, chat), false);
  assert.strictEqual(state.conversationId, null);
  assert.deepStrictEqual(state.messages, []);
  assert.strictEqual(state.busy, false);
});
test('deleting a conversation invalidates a load targeting it without restoring it', () => {
  const requests = P.createRequestEpoch();
  const state = { busy: false, conversationId: 'other', pendingConversationId: null, messages: [{ role: 'assistant', content: 'other' }], error: null };
  const load = P.beginConversationRequest(requests, state, 'deleted');
  assert.strictEqual(P.invalidateDeletedConversation(requests, state, 'deleted'), true);
  assert.strictEqual(P.isRequestCurrent(requests, load), false);
  assert.strictEqual(state.conversationId, 'other');
  assert.deepStrictEqual(state.messages, [{ role: 'assistant', content: 'other' }]);
});
test('switching conversations during an in-flight chat only accepts the new load', () => {
  const requests = P.createRequestEpoch();
  const state = { busy: true, conversationId: 'first', pendingConversationId: null, messages: [], error: null };
  const chat = P.beginConversationRequest(requests, state, 'first');
  const load = P.beginConversationRequest(requests, state, 'second');
  if (P.isRequestCurrent(requests, chat)) state.messages = [{ role: 'assistant', content: 'stale chat' }];
  assert.strictEqual(P.isRequestCurrent(requests, chat), false);
  assert.strictEqual(P.isRequestCurrent(requests, load), true);
  assert.deepStrictEqual(state.messages, []);
  assert.strictEqual(state.pendingConversationId, 'second');
});
console.log(`playground_ui: ${passed} passed`);
