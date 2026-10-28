/* AI Playground — uses the authenticated Atlas APIs and existing routed model service. */
(function () {
  'use strict';
  const esc = (v) => escapeHtml(v == null ? '' : String(v));
  const numericValue = (v) => {
    if (v === null || v === undefined || (typeof v === 'string' && v.trim() === '')) return null;
    const n = typeof v === 'number' ? v : (typeof v === 'string' ? Number(v.trim()) : NaN);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const usd = (v) => { const n = numericValue(v); return n === null ? 'Not reported' : '$' + n.toFixed(4); };
  const safeAnswer = (v) => mdToHtml(v || '').replace(/<a href="([^"]*)" target="_blank" rel="noopener">([\s\S]*?)<\/a>/g,
    (whole, href, label) => /^https?:\/\//i.test(href) ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label);
  // A view change invalidates any response started by the previous view.
  function createRequestEpoch() {
    let current = 0;
    return { begin: () => ++current, invalidate: () => ++current, isCurrent: (id) => id === current };
  }
  const requests = createRequestEpoch();
  const P = { models: [], conversations: [], conversationId: null, pendingConversationId: null, messages: [], selected: new Set(), busy: false, error: null, loaded: false };

  function beginConversationRequest(epoch, state, conversationId) {
    const requestId = epoch.begin();
    state.pendingConversationId = conversationId;
    return requestId;
  }
  function invalidateDeletedConversation(epoch, state, conversationId) {
    const isActive = state.conversationId === conversationId;
    const isPending = state.pendingConversationId === conversationId;
    if (!isActive && !isPending) return false;
    if (isPending) {
      epoch.invalidate();
      state.pendingConversationId = null;
      state.busy = false;
      state.error = null;
    }
    if (isActive) { state.conversationId = null; state.messages = []; }
    return true;
  }

  function resetForNewConversation(epoch, state, render) {
    epoch.invalidate(); state.busy = false; state.conversationId = null; state.pendingConversationId = null; state.messages = []; state.error = null; render();
  }
  function newConversation() {
    resetForNewConversation(requests, P, refresh);
  }

  function sourceHtml(s) {
    const title = esc(s.title || s.domain || 'Source');
    const meta = s.kind === 'web'
      ? (/^https?:\/\//i.test(s.url || '') ? `<a href="${escapeAttr(s.url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : `<span>${title}</span>`)+`<span>${esc(s.domain || '')}</span>`
      : `<span>${title}</span><span>Research library · ${esc(s.similarity_score == null ? 'match' : Math.round(s.similarity_score * 100) + '% match')}</span>`;
    const titleHtml = s.kind === 'web' ? meta : `<button class="pg-report-link" data-pg-report="${escapeAttr(s.report_id || '')}">${title} · open research ↗</button>`;
    return `<article class="pg-source"><div class="pg-source-head"><b>[${esc(s.id)}]</b>${titleHtml}</div><p>${esc((s.excerpt || '').slice(0, 360))}${(s.excerpt || '').length > 360 ? '…' : ''}</p></article>`;
  }
  function outputHtml(o) {
    const error = o.status !== 'ok';
    const usage = o.usage || {};
    const statusLabel = o.status === 'blocked' ? 'Limit reached' : o.status === 'timeout' ? 'Timed out' : o.status === 'failed' ? 'Provider failed' : error ? 'Unavailable' : 'Complete';
    return `<article class="pg-output ${error ? 'pg-output-error' : ''}">
      <header><div><span class="pg-provider-dot"></span><strong>${esc(o.label || o.model)}</strong><span class="pg-model-id">${esc(o.model)}</span></div><span class="pg-state ${error ? 'is-error' : ''}" aria-label="${esc(statusLabel)}">${esc(statusLabel)}</span></header>
      ${error ? `<div class="pg-error-copy" role="status">${esc(o.error || 'This model could not complete the request.')}</div>` : `<div class="pg-answer md-body">${safeAnswer(o.answer || '')}</div>`}
      <footer><span>${numericValue(usage.total_tokens) === null || !Number.isInteger(numericValue(usage.total_tokens)) ? 'Tokens not reported' : `${numericValue(usage.total_tokens).toLocaleString()} tokens`}</span><span>Pre-call cost estimate: ${usd(usage.estimated_cost_usd)}</span><span>Usage-based cost estimate: ${usd(usage.actual_cost_usd)}</span><span>${numericValue(usage.latency_ms) === null ? '—' : Math.round(numericValue(usage.latency_ms)) + ' ms'}</span></footer>
    </article>`;
  }
  function messageHtml(m) {
    if (m.role === 'user') return `<div class="pg-user-message"><span>You</span><p>${esc(m.content)}</p></div>`;
    const outputs = m.outputs || [];
    return `<section class="pg-turn-output"><div class="pg-turn-label">Atlas response</div>
      ${outputs.length ? `<div class="pg-output-grid">${outputs.map(outputHtml).join('')}</div>` : `<article class="pg-output"><div class="pg-answer md-body">${mdToHtml(m.content || '')}</div></article>`}
      ${(m.sources || []).length ? `<div class="pg-sources"><h3>Evidence provided <span>${m.sources.length}</span></h3><div class="pg-source-grid">${m.sources.map(sourceHtml).join('')}</div></div>` : ''}
      ${(m.warnings || []).map(w => `<div class="pg-warning" role="status">${esc(w)}</div>`).join('')}
    </section>`;
  }
  function html() {
    const modelList = P.models.map(m => `<label class="pg-model-choice"><input type="checkbox" value="${esc(m.model)}" data-pg-model ${P.selected.has(m.model) ? 'checked' : ''}><span><b>${esc(m.label)}</b><small>${esc(m.model)} · ${m.price_in == null ? 'price unknown' : '$' + m.price_in + ' / $' + m.price_out + ' per 1M tokens'}</small></span></label>`).join('');
    const history = P.conversations.map(c => `<div class="pg-history-row"><button class="pg-history-item ${P.conversationId === c.conversation_id ? 'active' : ''}" data-pg-open="${esc(c.conversation_id)}"><b>${esc(c.title)}</b><small>${Number(c.message_count) || 0} messages · ${esc(new Date(c.updated_at).toLocaleDateString())}</small></button><button class="pg-history-delete" title="Delete conversation" aria-label="Delete ${esc(c.title)}" data-pg-delete="${esc(c.conversation_id)}">×</button></div>`).join('');
    const content = P.messages.map(messageHtml).join('');
    const pending = P.busy ? `<section class="pg-turn-output"><div class="pg-turn-label">Running ${P.selected.size} model${P.selected.size === 1 ? '' : 's'}</div><div class="pg-output-grid">${[...P.selected].map(model => `<article class="pg-output pg-pending"><header><strong>${esc(P.models.find(m => m.model === model)?.label || model)}</strong><span class="pg-spinner"></span></header><p>Generating a response…</p></article>`).join('')}</div></section>` : '';
    return `<div class="pg-page">
      <div class="pg-page-heading"><div><div class="eyebrow">Compare models</div><h1>AI Playground</h1><p>Ask once, compare model responses, and keep the sources and usage in view.</p></div><button class="btn btn-primary" data-pg-new>＋ New conversation</button></div>
      ${P.error ? `<div class="pg-warning" role="alert">${esc(P.error)}</div>` : ''}
      <div class="pg-layout">
        <aside class="pg-history card"><div class="pg-side-title"><span>Your conversations</span><span>${P.conversations.length}</span></div><div class="pg-history-list">${history || '<div class="pg-history-empty">Your saved conversations will appear here.</div>'}</div></aside>
        <section class="pg-workspace card">
          <div class="pg-toolbar"><div class="pg-toolbar-main"><div class="pg-kicker">Model selection <span>Select up to 3</span></div><div class="pg-model-list">${modelList || '<span class="muted">Loading available models…</span>'}</div></div>
            <div class="pg-context"><div class="pg-kicker">Context</div><label><input type="checkbox" id="pgDocs" checked> My research library</label><label><input type="checkbox" id="pgWeb"> Web search</label><div class="pg-context-note">Research context is private to your account. Web results are cited with links.</div></div></div>
          <div class="pg-limit-note">${P.selected.size || 0} selected · Explicit model choices apply to this comparison; the saved M9 policy continues to govern other calls. Fallback is off here and account limits still apply server-side.</div>
          <div class="pg-conversation" id="pgConversation">${content || '<div class="pg-welcome"><div class="pg-orbit">◉</div><h2>What are you exploring?</h2><p>Compare up to three models against the same question and retrieved context.</p><div class="pg-prompts"><button data-pg-prompt="Compare the strongest evidence on this question">Compare the strongest evidence on a question</button><button data-pg-prompt="What remains uncertain, and which sources support your answer?">Find evidence and remaining uncertainty</button></div></div>'}${pending}</div>
          <form class="pg-composer" id="pgForm"><textarea id="pgMessage" maxlength="4000" rows="2" placeholder="Ask a question… (⌘/Ctrl + Enter to send)" required></textarea><div class="pg-composer-footer"><span>Responses cite evidence as [D1] or [W1]. Pre-call estimates use catalog prices and estimated token usage; usage-based estimates use reported token counts and catalog prices.</span><button class="btn btn-primary" type="submit" ${P.busy ? 'disabled' : ''}>${P.busy ? 'Comparing…' : 'Run comparison'} <span>↑</span></button></div></form>
        </section>
      </div>
    </div>`;
  }
  function bind() {
    document.querySelectorAll('[data-pg-model]').forEach(input => input.addEventListener('change', () => {
      if (input.checked && P.selected.size >= 3) { input.checked = false; toast('Choose up to three models.', 'err'); return; }
      input.checked ? P.selected.add(input.value) : P.selected.delete(input.value);
      refresh();
    }));
    document.querySelectorAll('[data-pg-new]').forEach(b => b.addEventListener('click', newConversation));
    document.querySelectorAll('[data-pg-open]').forEach(b => b.addEventListener('click', () => loadConversation(b.dataset.pgOpen)));
    document.querySelectorAll('[data-pg-delete]').forEach(b => b.addEventListener('click', () => deleteConversation(b.dataset.pgDelete)));
    document.querySelectorAll('[data-pg-report]').forEach(b => b.addEventListener('click', () => { if (b.dataset.pgReport) openHistoryCase(b.dataset.pgReport); }));
    document.querySelectorAll('[data-pg-prompt]').forEach(b => b.addEventListener('click', () => { document.querySelector('#pgMessage').value = b.dataset.pgPrompt; document.querySelector('#pgMessage').focus(); }));
    document.querySelector('#pgForm')?.addEventListener('submit', send);
    document.querySelector('#pgMessage')?.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); document.querySelector('#pgForm').requestSubmit(); } });
  }
  function refresh() {
    if (Atlas.state.view !== 'playground') return;
    const main = document.querySelector('#mainView'), ctx = document.querySelector('#contextPanel');
    main.innerHTML = html(); ctx.innerHTML = ''; document.querySelector('#appRoot').classList.add('ctx-collapsed'); bind();
    const box = document.querySelector('#pgConversation'); if (box) box.scrollTop = box.scrollHeight;
  }
  async function load() {
    try {
      const [models, conversations] = await Promise.all([apiGet('/playground/models'), apiGet('/playground/conversations')]);
      P.models = models?.data?.models || []; P.conversations = conversations?.data?.items || [];
      if (!P.selected.size && P.models.length) P.selected.add(P.models[0].model);
      P.error = null; P.loaded = true;
    } catch (e) { P.error = e.message || 'Playground could not load.'; }
    refresh();
  }
  async function loadConversation(id) {
    const requestId = beginConversationRequest(requests, P, id); P.busy = false; P.error = null; refresh();
    try {
      const result = await apiGet('/playground/conversations/' + encodeURIComponent(id));
      if (!requests.isCurrent(requestId)) return;
      P.conversationId = id; P.messages = result?.data?.messages || []; P.error = null;
    } catch (e) { if (requests.isCurrent(requestId)) P.error = e.message || 'Could not open conversation.'; }
    finally { if (requests.isCurrent(requestId)) { P.pendingConversationId = null; refresh(); } }
  }
  async function deleteConversation(id) {
    if (invalidateDeletedConversation(requests, P, id)) refresh();
    try {
      await apiDelete('/playground/conversations/' + encodeURIComponent(id), { quietStatuses: [404] });
      P.conversations = P.conversations.filter(c => c.conversation_id !== id);
      if (P.conversationId === id) { P.conversationId = null; P.messages = []; }
      refresh();
    } catch (e) {
      if (e.message === 'Conversation not found') {
        P.conversations = P.conversations.filter(c => c.conversation_id !== id);
        refresh();
        return;
      }
      P.error = e.message || 'Could not delete conversation.'; refresh();
    }
  }
  async function send(event) {
    event.preventDefault();
    const textarea = document.querySelector('#pgMessage'); const message = textarea?.value.trim();
    if (!message || P.busy) return;
    if (!P.selected.size) { toast('Select at least one model.', 'err'); return; }
    const includeDocuments = document.querySelector('#pgDocs')?.checked !== false;
    const includeWeb = !!document.querySelector('#pgWeb')?.checked;
    const conversationId = P.conversationId;
    const requestId = beginConversationRequest(requests, P, conversationId);
    P.busy = true; P.error = null; P.messages = [...P.messages, { role: 'user', content: message }]; textarea.value = ''; refresh();
    try {
      const result = await apiPost('/playground/chat', { message, models: [...P.selected], conversation_id: conversationId,
        include_documents: includeDocuments, include_web: includeWeb });
      const data = result?.data;
      if (!data) throw new Error('Playground returned an empty response.');
      if (!requests.isCurrent(requestId)) {
        // A response to an abandoned unsaved chat would otherwise leave an
        // unreachable conversation in history after New Conversation/switch.
        if (!conversationId && data.conversation_id) {
          try { await apiDelete('/playground/conversations/' + encodeURIComponent(data.conversation_id), { quietStatuses: [404] }); }
          catch (_) { /* stale requests must not change the current view */ }
        }
        return;
      }
      P.conversationId = data.conversation_id; P.messages = data.messages || [...P.messages, { role: 'assistant', content: '', outputs: data.outputs, sources: data.sources, warnings: data.warnings }];
      const conversations = await apiGet('/playground/conversations');
      if (!requests.isCurrent(requestId)) return;
      P.conversations = conversations?.data?.items || P.conversations;
    } catch (e) { if (requests.isCurrent(requestId)) P.error = e.message || 'The Playground request failed.'; }
    finally { if (requests.isCurrent(requestId)) { P.busy = false; P.pendingConversationId = null; refresh(); } }
  }
  window.AtlasPlayground = {
    openConversation(id) { if (id && typeof loadConversation === 'function') loadConversation(id); },
    render(main, ctx) { ctx.innerHTML = ''; main.innerHTML = html(); bind(); if (!P.loaded) load(); },
    formatCost: usd,
    numericValue,
    safeAnswerHtml: safeAnswer,
    outputHtml,
    messageHtml,
    createRequestEpoch,
    isRequestCurrent: (epoch, id) => epoch.isCurrent(id),
    beginConversationRequest,
    invalidateDeletedConversation,
    resetForNewConversation,
  };
})();
