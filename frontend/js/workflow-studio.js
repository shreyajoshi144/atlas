/* Minimal Workflow Studio vertical slice. Only deterministic server-supported steps are offered. */
(function () {
  'use strict';
  const esc = s => typeof escapeHtml === 'function' ? escapeHtml(s) : String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = { templates: [], workflows: [], selected: null, run: null, history: [] };
  async function load() {
    const [t, w] = await Promise.all([apiGet('/workflows/templates'), apiGet('/workflows')]);
    state.templates = t.data.items || []; state.workflows = w.data.items || [];
    if (state.selected && !state.workflows.some(x => x.workflow_id === state.selected)) state.selected = null;
    render(document.getElementById('mainView'), document.getElementById('contextPanel'));
  }
  function render(main) {
    if (!main) return;
    const chosen = state.workflows.find(w => w.workflow_id === state.selected);
    const statusCls = s => /fail|error|reject|invalid/i.test(String(s)) ? 'bad' : /success|complete|ok|done/i.test(String(s)) ? 'ok' : '';
    const stepLi = s => `<li class="${statusCls(s.status)}"><strong>${esc(s.name)}</strong> <span class="wf-type">${esc(s.type || '')}</span>${s.status ? ` <span class="wf-status ${statusCls(s.status)}">${esc(s.status)}</span>` : ''}${s.error ? `<div class="small" style="color:var(--wine)">${esc(s.error)}</div>` : ''}${s.output ? `<pre>${esc(s.output.text)}</pre>` : ''}</li>`;
    main.innerHTML = `<div class="wf-page">
      <section class="page-head"><div><div class="eyebrow">Build with AI</div><h1 class="page-title">Workflow Studio</h1><p class="page-desc">Build and run bounded, deterministic workflows. Unsupported actions are rejected by the API.</p></div></section>
      <div class="wf-grid">
        <aside class="wf-side">
          <div class="wf-block"><h2>New from template</h2>
            <div class="form-row" style="display:flex;flex-direction:column;gap:12px;">
              <label>Template<select id="wfTemplate" class="form-control">${state.templates.map(t=>`<option value="${esc(t.template_id)}">${esc(t.name)}</option>`).join('')}</select></label>
              <label>Workflow name<input id="wfName" class="form-control" maxlength="100" placeholder="My workflow"></label>
              <button id="wfCreate" class="btn btn-primary">Save workflow</button>
            </div>
            <p class="small muted" style="margin-top:12px">Supported steps: input text, normalize whitespace, append text, uppercase and require phrase.</p>
          </div>
          <div class="wf-block"><h2>Saved workflows</h2>${state.workflows.length ? `<div class="wf-list">${state.workflows.map(w=>`<div class="wf-item ${w.workflow_id===state.selected?'sel':''}"><button class="wf-name" data-wf-select="${esc(w.workflow_id)}" aria-pressed="${w.workflow_id===state.selected}">${esc(w.name)}</button><button class="btn btn-secondary btn-sm" data-wf-edit="${esc(w.workflow_id)}" aria-label="Edit ${esc(w.name)}">Edit</button><button class="btn btn-secondary btn-sm" data-wf-duplicate="${esc(w.workflow_id)}" aria-label="Duplicate ${esc(w.name)}">Duplicate</button><button class="btn btn-danger btn-sm" data-wf-delete="${esc(w.workflow_id)}" aria-label="Delete ${esc(w.name)}">Delete</button></div>`).join('')}</div>` : '<p class="muted">No workflows saved yet. Pick a template above and save your first one.</p>'}</div>
        </aside>
        <div class="wf-main">
          ${chosen ? `<div class="wf-block"><div class="eyebrow">Selected workflow</div><h2>${esc(chosen.name)}</h2><p class="small muted">${chosen.definition.steps.length} validated steps · owned by your account</p><ol class="wf-steps">${chosen.definition.steps.map(s=>`<li><strong>${esc(s.name)}</strong> <span class="wf-type">${esc(s.type)}</span></li>`).join('')}</ol>
            <label class="f-label" for="wfInput" style="margin-top:16px">Input text</label><textarea id="wfInput" class="form-control" rows="4" maxlength="10000" placeholder="Enter text to process"></textarea>
            <div class="flex gap-10 mt-14"><button id="wfExecute" class="btn btn-primary">Execute workflow</button><button id="wfHistory" class="btn btn-secondary">Refresh run history</button></div></div>`
          : `<div class="wf-block"><div class="empty"><div class="empty-icon">№</div><div class="empty-title">${state.workflows.length ? 'Select a workflow' : 'Nothing to run yet'}</div><div class="empty-sub">${state.workflows.length ? 'Choose a saved workflow to see its steps and run it.' : 'Save a workflow from a template, then run it on any text to see each step’s outcome.'}</div></div></div>`}
          ${state.run ? `<div class="wf-block"><div class="eyebrow">Latest execution</div><h2>Run result</h2><p>Status: <span class="wf-status ${statusCls(state.run.status)}">${esc(state.run.status)}</span>${state.run.error ? ` <span style="color:var(--wine)">— ${esc(state.run.error)}</span>` : ''}</p>${state.run.output ? `<label class="f-label">Output<textarea class="form-control" rows="4" readonly>${esc(state.run.output.text)}</textarea></label>` : ''}<h3>Step outcomes</h3><ol class="wf-steps">${state.run.steps.map(stepLi).join('')}</ol></div>` : ''}
          ${state.history.length ? `<div class="wf-block"><h2>Run history</h2>${state.history.map(r=>`<span class="wf-run-row"><button class="btn btn-secondary btn-sm" data-wf-run="${esc(r.run_id)}">${esc(r.created_at)} · ${esc(r.status)}</button></span>`).join('')}</div>` : ''}
        </div>
      </div>
    </div>`;
    main.querySelector('#wfCreate')?.addEventListener('click', async()=>{ try { const tid=main.querySelector('#wfTemplate').value; const name=main.querySelector('#wfName').value.trim(); const t=state.templates.find(x=>x.template_id===tid); const res=await apiPost('/workflows',{template_id:tid,name:name||t.name,steps:t.steps}); state.selected=res.data.workflow.workflow_id; state.run=null; state.history=[]; await load(); toast('Workflow saved.'); } catch(e) { toast(e.message||'Could not save workflow.','err'); } });
    main.querySelectorAll('[data-wf-select]').forEach(b=>b.addEventListener('click',async()=>{state.selected=b.dataset.wfSelect;state.run=null;await refreshHistory();render(main);}));
    main.querySelectorAll('[data-wf-edit]').forEach(b=>b.addEventListener('click',async()=>{
      const wf=state.workflows.find(x=>x.workflow_id===b.dataset.wfEdit); if(!wf)return;
      const name=window.prompt('Workflow name',wf.name); if(name===null)return;
      const raw=window.prompt('Edit step configuration as JSON',JSON.stringify(wf.definition.steps,null,2)); if(raw===null)return;
      let steps; try { steps=JSON.parse(raw); if(!Array.isArray(steps))throw new Error('Steps must be a JSON array.'); }
      catch(e){toast(e.message||'Invalid step JSON.','err');return;}
      try { const res=await apiCall('PUT','/workflows/'+encodeURIComponent(wf.workflow_id),{name:name.trim(),steps}); state.selected=wf.workflow_id; state.run=null; await load(); await refreshHistory(); render(main); toast('Workflow updated.'); }
      catch(e){toast(e.message||'Could not update workflow.','err');}
    }));
    main.querySelectorAll('[data-wf-duplicate]').forEach(b=>b.addEventListener('click',async()=>{
      try { const res=await apiCall('POST','/workflows/'+encodeURIComponent(b.dataset.wfDuplicate)+'/duplicate',{}); state.selected=res.data.workflow.workflow_id; state.run=null; await load(); await refreshHistory(); render(main); toast('Workflow duplicated.'); }
      catch(e){toast(e.message||'Could not duplicate workflow.','err');}
    }));
    main.querySelectorAll('[data-wf-delete]').forEach(b=>b.addEventListener('click',async()=>{
      const wf=state.workflows.find(x=>x.workflow_id===b.dataset.wfDelete); if(!wf)return;
      if(!window.confirm(`Delete “${wf.name}” and its run history? This cannot be undone.`))return;
      try { await apiCall('DELETE','/workflows/'+encodeURIComponent(wf.workflow_id)); if(state.selected===wf.workflow_id){state.selected=null;state.run=null;state.history=[];} await load(); toast('Workflow deleted.'); }
      catch(e){toast(e.message||'Could not delete workflow.','err');}
    }));
    main.querySelector('#wfExecute')?.addEventListener('click',async()=>{ try { const text=main.querySelector('#wfInput').value; const key=(window.crypto?.randomUUID?.()||('wf-'+Date.now()+'-'+Math.random().toString(16).slice(2))); const res=await apiCall('POST','/workflows/'+encodeURIComponent(state.selected)+'/execute',{text},{headers:{'Idempotency-Key':key}}); state.run=res.data.run; await refreshHistory(); render(main); } catch(e) { toast(e.message||'Workflow execution failed.','err'); } });
    main.querySelector('#wfHistory')?.addEventListener('click',async()=>{await refreshHistory();render(main);});
    main.querySelectorAll('[data-wf-run]').forEach(b=>b.addEventListener('click',async()=>{try{const res=await apiGet('/workflows/runs/'+encodeURIComponent(b.dataset.wfRun));state.run=res.data.run;render(main);}catch(e){toast(e.message||'Could not load run.','err');}}));
  }
  async function refreshHistory(){ if(!state.selected)return; const res=await apiGet('/workflows/'+encodeURIComponent(state.selected)+'/runs'); state.history=res.data.items||[]; }
  async function viewWorkflowStudio(main, ctx) { try { await load(); if(state.selected) await refreshHistory(); render(main,ctx); } catch(e) { main.innerHTML=`<section class="card" style="padding:20px"><h1>Workflow Studio unavailable</h1><p>${esc(e.message||'Could not load workflows.')}</p><button class="btn btn-secondary" id="wfRetry">Retry</button></section>`; main.querySelector('#wfRetry')?.addEventListener('click',()=>viewWorkflowStudio(main,ctx)); } }
  window.viewWorkflowStudio = viewWorkflowStudio;
})();
