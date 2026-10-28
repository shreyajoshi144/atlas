// Smoke-test the real Workflow Studio renderer with API responses injected at its boundary.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'frontend', 'js', 'workflow-studio.js'), 'utf8');
const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
const templates = { items: [{ template_id: 'text-cleanup', name: 'Text cleanup', steps: [{ type: 'input_text', name: 'Load input' }, { type: 'normalize_whitespace', name: 'Normalize whitespace' }] }] };
const workflows = { items: [{ workflow_id: 'wf_' + 'a'.repeat(32), name: 'Saved cleanup', definition: { name: 'Saved cleanup', steps: [{ type: 'input_text', name: 'Load input' }, { type: 'normalize_whitespace', name: 'Normalize whitespace' }] } }] };
const context = { console, document: { getElementById: () => main }, window: {}, escapeHtml: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), apiGet: async p => p.endsWith('/templates') ? { data: templates } : { data: workflows }, apiPost: async () => ({ data: { workflow: workflows.items[0] } }), apiCall: async () => ({ data: { run: { status: 'completed', output: { text: 'done' }, steps: [] } } }), toast: () => {} };
vm.runInNewContext(source, context, { filename: 'workflow-studio.js' });
(async () => {
  await context.window.viewWorkflowStudio(main, {});
  assert(main.innerHTML.includes('Workflow Studio'));
  assert(main.innerHTML.includes('Text cleanup'));
  assert(main.innerHTML.includes('Saved cleanup'));
  assert(main.innerHTML.includes('Save workflow'));
  assert(main.innerHTML.includes('Unsupported actions are rejected'));
  assert(main.innerHTML.includes('data-wf-edit='));
  assert(main.innerHTML.includes('data-wf-duplicate='));
  assert(main.innerHTML.includes('data-wf-delete='));
  console.log('workflow_studio_ui: 8 assertions passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
