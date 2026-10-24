'use strict';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number.isFinite(n) ? n.toLocaleString('en-US',{maximumFractionDigits:2}) : '—';
const when = value => value ? new Date(value).toLocaleString() : '—';
let state = null, library = null, system = null, selectedRun = null, lastJobStatus = null;

async function get(url) {
  const response = await fetch(url,{cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function post(url,body={}) {
  const response = await fetch(url,{method:'POST',headers:{'content-type':'application/json','x-benchmark-token':state.token},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.errors?.join('; ') || `HTTP ${response.status}`);
  return data;
}
function notice(message,isError=false) { const el=$('notice'); el.textContent=message; el.classList.toggle('error',isError); if(message) setTimeout(() => { if(el.textContent===message) el.textContent=''; },12000); }
function guard(fn) { return async (...args) => { try { await fn(...args); } catch(e) { notice(e.message,true); } }; }

function renderSystem() {
  if (!system) return;
  const gpu = system.gpu;
  $('system-content').innerHTML = `<div class="row"><div class="stat"><strong>${esc(system.cores)}</strong><span>Logical CPU cores</span></div><div class="stat"><strong>${esc(system.freeRamGiB)} / ${esc(system.totalRamGiB)} GiB</strong><span>Free / total RAM</span></div><div class="stat"><strong>${gpu ? `${esc(gpu.usedMiB)} / ${esc(gpu.totalMiB)} MiB` : 'Unavailable'}</strong><span>GPU memory in use</span></div></div><p class="hint">${esc(system.cpu)}${gpu ? ` · ${esc(gpu.name)}` : ''}</p>${system.warnings.length ? `<p>${system.warnings.map(w=>`<span class="tag warn">${esc(w)}</span>`).join(' ')}</p>` : '<p><span class="tag good">Ready for a comparable run</span></p>'}<details><summary>Top RAM users</summary><div>${system.processes.map(p=>`<div>${esc(p.ProcessName)} · ${esc(p.ramMiB)} MiB</div>`).join('') || 'Unavailable'}</div></details>`;
}
function renderModels() {
  const status=$('ollama-status');
  if (!library) { status.className='pill bad';status.textContent='Ollama unavailable';$('models-content').textContent='Ollama is not responding on localhost:11434. Start Ollama, then click Refresh.';$('model-select').innerHTML='<option value="">No models found</option>';return; }
  status.className='pill good'; status.textContent=`Ollama ${library.version || 'online'}`;
  const models=library.models || [], loaded=new Set((library.running || []).map(m=>m.name));
  $('models-content').innerHTML=models.length ? `<div>${models.map(m=>`<div class="task"><strong>${esc(m.name)}</strong> ${loaded.has(m.name) ? `<span class="tag good">loaded</span> <button class="secondary unload-model" data-model="${esc(m.name)}">Unload</button>` : `<button class="secondary danger delete-model" data-model="${esc(m.name)}" data-digest="${esc(m.digest)}">Delete model</button>`}<small>${fmt(m.size/2**30)} GiB · ${esc(m.details?.quantization_level || 'unknown quant')} · digest ${esc(m.digest?.slice(0,12))}</small></div>`).join('')}</div>` : 'No installed models. Pull one below.';
  $('models-content').querySelectorAll('button.unload-model').forEach(button=>button.onclick=guard(async()=>{const model=button.dataset.model;await post('/api/models/unload',{model});notice(`${model} unloaded from memory.`);await refreshModels();}));
  $('models-content').querySelectorAll('button.delete-model').forEach(button=>button.onclick=guard(async()=>{const model=button.dataset.model,digest=button.dataset.digest;const typed=prompt(`Delete the installed Ollama model ${model}? This removes its local files. Saved benchmark results remain. Type the full model name to confirm:`);if(typed!==model)return;await post('/api/models/delete',{model,digest});notice(`${model} deleted from Ollama. Saved results remain.`);await refreshModels();}));
  const previous=$('model-select').value;
  $('model-select').innerHTML=[...models].sort((a,b)=>a.size-b.size).map(m=>`<option value="${esc(m.name)}">${esc(m.name)} · ${esc(m.details?.quantization_level || '')}</option>`).join('');
  if(models.some(m=>m.name===previous))$('model-select').value=previous;
}
function chosenPack() { return state?.packs.find(p=>p.id===$('pack-select').value); }
function chosenModel() { return library?.models.find(m=>m.name===$('model-select').value); }
function renderPacks() {
  const select=$('pack-select'), previous=select.value;
  select.innerHTML=state.packs.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.version)}${p.approved ? ' · approved' : ' · draft'}</option>`).join('');
  if(state.packs.some(p=>p.id===previous))select.value=previous;
  renderPackDetail();
  $('cloud-btn').disabled=!state.codexAvailable;
  if(!state.codexAvailable)$('cloud-btn').title='Codex CLI was not found on this PC';
}
function renderPackDetail() {
  const pack=chosenPack();if(!pack){$('pack-detail').textContent='No pack available.';return;}
  $('pack-detail').innerHTML=`<p>${pack.approved?'<span class="tag good">Approved</span>':'<span class="tag warn">Draft — cannot run yet</span>'}<span class="tag">${esc(pack.source)}</span></p><p class="hint">${esc(pack.description || '')}</p><p class="hint">Created ${esc(when(pack.createdAt))} · SHA-256 ${esc(pack.hash.slice(0,16))}</p><details><summary>Review all ${pack.tasks.length} tasks and reference rubrics</summary>${pack.tasks.map(t=>`<details><summary>${esc(t.category)} · level ${esc(t.level)} · ${esc(t.id)}</summary>${t.image?`<img src="/${esc(t.image)}" alt="Fixture for ${esc(t.id)}" style="max-width:100%;height:auto">`:''}<pre>${esc(JSON.stringify(t,null,2))}</pre></details>`).join('')}</details><button id="pack-approve" class="${pack.approved?'secondary':'primary'}">${pack.approved?'Unapprove pack':'Approve this pack'}</button>`;
  $('pack-approve').onclick=guard(async()=>{const approved=!pack.approved;await post('/api/packs/approve',{id:pack.id,approved});await refreshState();notice(approved?'Pack approved.':'Pack unapproved.');});
}
function renderLimits() {
  const values=state.settings?.limitsSec || [3600,7200,14400,21600,28800];
  $('limits').innerHTML=values.map((seconds,i)=>`<label>Level ${i+1}<input id="limit-${i+1}" type="number" min="1" max="1440" value="${Math.round(seconds/60)}"></label>`).join('');
  $('repetitions').value=String(state.settings?.repetitions || 1);
}
function readLimits() { const values=[1,2,3,4,5].map(i=>Number($(`limit-${i}`).value)*60); if(values.some(n=>!Number.isInteger(n)||n<1||n>86400))throw new Error('Each task limit must be between 1 and 1440 minutes');return values; }
async function saveSettings() { await post('/api/settings',{limitsSec:readLimits(),repetitions:+$('repetitions').value,judgeModel:state.settings.judgeModel || ''}); }

function catScore(run,category) {
  const totals=[];let observed=0,pending=false;
  for(let rep=1;rep<=run.repetitions;rep++){
    const records=run.tasks.filter(t=>t.repetition===rep&&t.category===category);
    observed+=records.filter(t=>Number.isFinite(t.score)).reduce((sum,t)=>sum+t.score,0);
    if(records.length===5&&records.every(t=>Number.isFinite(t.score)))totals.push(records.reduce((sum,t)=>sum+t.score,0));
    else pending=true;
  }
  if(!totals.length)return {number:null,label:`${fmt(observed)} · pending`,pending:true};
  const mean=totals.reduce((a,b)=>a+b,0)/totals.length;
  const sd=Math.sqrt(totals.reduce((s,n)=>s+(n-mean)**2,0)/totals.length);
  return {number:mean,label:`${fmt(mean)}${totals.length>1?` ± ${fmt(sd)}`:''}${pending?' · partial':''}`,pending};
}
function speed(run){const rates=run.tasks.filter(t=>t.metrics?.evalCount&&t.metrics?.evalNs).map(t=>t.metrics.evalCount/(t.metrics.evalNs/1e9));return rates.length?rates.reduce((a,b)=>a+b,0)/rates.length:null;}
function renderResults() {
  const rows=$('result-rows');
  const pack=chosenPack();
  const categoryIds=pack?.categoryIds || [...new Set((pack?.tasks||[]).map(t=>t.category))];
  const categories=(state.categories || []).filter(c=>categoryIds.includes(c.id));
  const runs=pack ? state.runs.filter(run=>run.packHash===pack.hash) : [];
  $('result-head').innerHTML=`<th>Setup</th><th>Pack</th>${categories.map(c=>`<th>${esc(c.label)} / 50</th>`).join('')}<th>Total / ${categories.length*50}</th><th>Speed</th><th>Run</th>`;
  $('result-hint').textContent=`Showing runs for the selected pack. Unweighted quality: 10 points × 5 levels × ${categories.length} categories = ${categories.length*50}. Speed and cold load are separate. Pending grading is not zero.`;
  if(!runs.length){rows.innerHTML=`<tr><td colspan="${categories.length+5}" class="muted">No runs for this pack yet. Select another pack to see its saved results.</td></tr>`;$('result-detail').innerHTML='';return;}
  if(!runs.some(run=>run.id===selectedRun))selectedRun=null;
  rows.innerHTML=[...runs].reverse().map(run=>{
    const cats=categories.map(c=>catScore(run,c.id));
    const all=cats.every(c=>!c.pending&&c.number!==null);
    const total=cats.reduce((sum,c)=>sum+(c.number||0),0);
    const setup=`${run.setup.model} · ${run.setup.details?.quantization_level || '?'} · ${run.setup.think===null?'default':run.setup.think?'think':'no think'} · ctx ${run.setup.options.num_ctx}`;
    const different=chosenPack()?.hash!==run.packHash;
    return `<tr data-run="${esc(run.id)}" class="${selectedRun===run.id?'active':''}"><td><strong>${esc(run.setup.model)}</strong><small>${esc(setup)}</small><small>digest ${esc(run.setup.digest?.slice(0,12))}</small></td><td>${esc(run.packName)}<small>v${esc(run.packVersion)}${different?' · different from selected pack':''}</small></td>${cats.map(c=>`<td>${esc(c.label)}</td>`).join('')}<td><strong>${fmt(total)}</strong>${all?'':'<small>incomplete</small>'}</td><td>${speed(run)?`${fmt(speed(run))} tok/s`:'—'}<small>load ${run.coldLoadMs===null?'—':fmt(run.coldLoadMs/1000)+' s'}</small></td><td><span class="tag ${run.status==='completed'?'good':run.status==='failed'?'bad':'warn'}">${esc(run.status)}</span><small>${esc(when(run.startedAt))}</small></td></tr>`;
  }).join('');
  rows.querySelectorAll('tr[data-run]').forEach(tr=>tr.onclick=()=>{selectedRun=tr.dataset.run;renderResults();});
  renderResultDetail();
}
function renderResultDetail() {
  const el=$('result-detail'),run=state.runs.find(r=>r.id===selectedRun);
  if(!run){el.innerHTML='';return;}
  const pack=state.packs.find(p=>p.id===run.packId);
  const pending=run.tasks.filter(t=>t.status==='subjective_pending'||t.status==='judge_error').length;
  const retryable=run.status!=='completed'||run.tasks.some(t=>['timed_out','skipped_due_to_limit','technical_error','aborted'].includes(t.status));
  el.innerHTML=`<div class="divider"></div><h3>${esc(run.setup.model)} · ${esc(run.id.slice(0,8))}</h3><p class="hint">${esc(run.repetitions)} repetition(s) · ${esc(run.ollamaVersion||'Ollama version unknown')} · ${esc(run.packHash.slice(0,16))} · started ${esc(when(run.startedAt))}</p>${run.error?`<p class="tag bad">${esc(run.error)}</p>`:''}<div class="inline">${pending?`<select id="judge-model">${(library?.models||[]).map(m=>`<option value="${esc(m.name)}">Judge with ${esc(m.name)}</option>`).join('')}</select><button id="judge-btn">Judge ${pending} answer(s)</button>`:''}<button id="project-btn" class="secondary">Preview current time limits</button>${retryable?'<button id="retry-btn" class="secondary">Retry incomplete tasks with current limits</button>':''}</div><div id="projection-content"></div><details open><summary>Task records and raw answers</summary>${[...run.tasks].sort((a,b)=>a.repetition-b.repetition||(state.categories.findIndex(c=>c.id===a.category)-state.categories.findIndex(c=>c.id===b.category))||a.level-b.level).map(t=>{const task=pack?.tasks.find(x=>x.id===t.taskId);return `<details class="task"><summary>Rep ${esc(t.repetition)} · ${esc(t.category)} ${esc(t.level)} · <span class="status ${esc(t.status)}">${esc(t.status)}</span> · ${Number.isFinite(t.score)?esc(t.score)+'/10':'unscored'}</summary><p class="hint">${esc(t.reason||'')} · processing ${Number.isFinite(t.processingMs)?fmt(t.processingMs/1000)+' s':'—'} · ${t.metrics?.evalCount?`${esc(t.metrics.evalCount)} output tokens`:''}</p>${task?`<details><summary>Task and reference</summary><pre>${esc(JSON.stringify(task,null,2))}</pre></details>`:''}${t.raw!==undefined?`<details><summary>Raw model answer</summary><pre>${esc(t.raw)}</pre></details>`:''}${t.breakdown?`<pre>${esc(JSON.stringify(t.breakdown,null,2))}</pre>`:''}</details>`;}).join('')}</details>`;
  $('project-btn').onclick=guard(async()=>{const projection=await post('/api/runs/project',{runId:run.id,limitsSec:readLimits()});$('projection-content').innerHTML=`<p class="hint">Preview only; original run data is unchanged. ${projection.categories.map(c=>`${esc(c.category)}: ${c.score===null?'needs retry':fmt(c.score)+'/50'}`).join(' · ')} · Total: ${projection.total===null?'incomplete':fmt(projection.total)}</p><details><summary>Projected task statuses</summary><pre>${esc(JSON.stringify(projection.records,null,2))}</pre></details>`;});
  if(pending){$('judge-btn').onclick=guard(async()=>{const model=$('judge-model').value;if(!model)throw new Error('Choose an installed judge model');if(!confirm(`Run the local judge ${model}? This unloads loaded Ollama models and scores the saved answers.`))return;await post('/api/judge',{runId:run.id,judgeModel:model});notice('Local judging started.');});}
  if(run.tasks.some(t=>state.categories.some(c=>c.id===t.category&&c.type==='subjective')&&typeof t.raw==='string'&&t.raw.trim())){
    const actions=el.querySelector('.inline');
    if(!$('judge-model')){const select=document.createElement('select');select.id='judge-model';for(const model of library?.models||[]){const option=document.createElement('option');option.value=model.name;option.textContent=`Judge with ${model.name}`;select.append(option);}actions.prepend(select);}
    const button=document.createElement('button');button.className='secondary';button.textContent='Rejudge saved subjective answers';actions.append(button);
    button.onclick=guard(async()=>{const model=$('judge-model').value;if(!model)throw new Error('Choose an installed judge model');if(!confirm(`Rejudge saved subjective answers with ${model}? Previous grades remain in history.`))return;await post('/api/judge',{runId:run.id,judgeModel:model,rejudge:true});notice('Rejudging started.');});
  }
  const gradeHistory=run.tasks.filter(t=>t.gradeHistory?.length).map(t=>({taskId:t.taskId,repetition:t.repetition,previousGrades:t.gradeHistory}));
  if(gradeHistory.length)el.insertAdjacentHTML('beforeend',`<details><summary>Previous subjective grades</summary><pre>${esc(JSON.stringify(gradeHistory,null,2))}</pre></details>`);
  if(retryable){$('retry-btn').onclick=guard(async()=>{const limitsSec=readLimits();if(!confirm('Retry incomplete tasks from this run? Completed answers will be reused; loaded Ollama models will be unloaded.'))return;await post('/api/runs/retry',{runId:run.id,limitsSec,overrideWarnings:$('override-warnings').checked});notice('Retry started.');await refreshState();});}
}
function renderJob(job) {
  const el=$('job-content');
  if(!job){el.textContent='No job yet.';$('after-btn').disabled=true;$('abort-btn').disabled=true;$('check-btn').disabled=true;return;}
  const active=job.status==='running';
  const progress=job.progress;
  let extra='';
  if(job.type==='pull'&&progress)extra=`<p>${esc(progress.status||'Downloading')} · ${progress.total?Math.round(100*progress.completed/progress.total)+'%':'working'} ${progress.total?`(${fmt(progress.completed/2**30)} / ${fmt(progress.total/2**30)} GiB)`:''}</p>`;
  if(job.type==='benchmark'&&progress)extra=`<p>Repetition ${esc(progress.repetition)} · ${esc(progress.category)} level ${esc(progress.level)} · ${esc(progress.taskId)} · started ${esc(when(progress.startedAt))}</p>`;
  if(job.type==='judge'&&progress)extra=`<p>Judging ${esc(progress.taskId)} · repetition ${esc(progress.repetition)}</p>`;
  if(job.log)extra+=`<details><summary>Codex CLI log</summary><pre>${esc(job.log)}</pre></details>`;
  el.innerHTML=`<p><span class="tag ${job.status==='completed'?'good':job.status==='failed'?'bad':'warn'}">${esc(job.status)}</span> <strong>${esc(job.type)}</strong> · ${esc(job.detail||'')}</p>${extra}<p class="hint">Started ${esc(when(job.startedAt))}${job.endedAt?' · ended '+esc(when(job.endedAt)):''}</p>`;
  $('after-btn').disabled=!active||job.type!=='benchmark';$('abort-btn').disabled=!active;$('check-btn').disabled=!active||job.type!=='benchmark';
}
async function refreshState() { state=await get('/api/state');$('profile-status').textContent=`PC: ${state.profiles?.find(p=>p.id==='local')?.name||'Local'}`;renderPacks();renderResults();if(!window._limitsReady){renderLimits();window._limitsReady=true;}renderJob(state.job); }
async function refreshModels() { try{library=await get('/api/models');renderModels();renderResults();}catch(e){library=null;renderModels();notice(e.message,true);} }
async function refreshSystem(){system=await get('/api/system');renderSystem();}
async function poll(){try{const job=await get('/api/job');renderJob(job);if(job?.status!==lastJobStatus&&job?.status!=='running'){await refreshState();await refreshModels();}lastJobStatus=job?.status||null;}catch{}}

async function init(){
  await refreshState();
  await Promise.all([refreshModels(),refreshSystem()]);
  $('cloud-prompt').value=(await get('/api/prompt')).prompt;
  $('system-refresh').onclick=guard(refreshSystem);
  $('models-refresh').onclick=guard(refreshModels);
  $('results-refresh').onclick=guard(refreshState);
  $('pack-select').onchange=()=>{renderPackDetail();renderResults();};
  $('export-btn').onclick=()=>{window.location.href='/api/export';};
  $('pull-btn').onclick=guard(async()=>{const model=$('pull-name').value.trim();if(!model)throw new Error('Enter an Ollama model name');if(!confirm(`Download ${model} through Ollama now? The model may be large and the progress will be shown here.`))return;await post('/api/models/pull',{model});notice(`Download of ${model} started.`);});
  $('cloud-btn').onclick=guard(async()=>{const model=$('cloud-model').value.trim(),effort=$('cloud-effort').value;if(!confirm(`Send the generic task-generation prompt to Codex CLI using ${model} / ${effort}?`))return;await post('/api/cloud/generate',{model,effort});notice('Codex is generating a draft. Review and approve it when complete.');});
  $('pack-import').onclick=guard(async()=>{const raw=$('pack-json').value;const result=await post('/api/packs/import',{pack:raw});await refreshState();$('pack-select').value=result.pack.id;renderPackDetail();notice('Draft imported. Review every task before approval.');});
  $('run-btn').onclick=guard(async()=>{const pack=chosenPack(),model=chosenModel();if(!pack?.approved)throw new Error('Approve a pack first');if(!model)throw new Error('Select an installed model');if(system?.warnings.length&&!$('override-warnings').checked)throw new Error('System Check has warnings. Close other programs and refresh, or explicitly check the override box.');await saveSettings();const config={packId:pack.id,model:model.name,digest:model.digest,details:model.details,think:$('think').value==='default'?null:$('think').value==='true',numCtx:+$('num-ctx').value,temperature:+$('temperature').value,numPredict:+$('num-predict').value,limitsSec:readLimits(),repetitions:+$('repetitions').value,overrideWarnings:$('override-warnings').checked};if(!confirm(`Run ${model.name} on ${pack.name} for ${config.repetitions} repetition(s)? Ollama will unload all currently loaded models first.`))return;await post('/api/run',config);notice('Benchmark started. Completed task records are saved automatically.');await refreshState();});
  $('check-btn').onclick=guard(async()=>{const check=await get('/api/job/check');const eta=check.eta;$('eta-content').innerHTML=eta?`<p><strong>Estimated remaining:</strong> ${fmt(eta.remainingMs/60000)} min · finish around ${esc(when(eta.finishAt))} · confidence ${esc(eta.confidence)} (${esc(eta.confidenceScore)}/100) · ${esc(eta.completedSamples)} historical task samples.</p><details><summary>Estimate detail</summary><pre>${esc(JSON.stringify(eta.details,null,2))}</pre></details>`:'<p class="hint">No benchmark is running.</p>';});
  $('after-btn').onclick=guard(async()=>{await post('/api/job/abort',{mode:'after-task'});notice('Will stop after the current task; completed results stay saved.');});
  $('abort-btn').onclick=guard(async()=>{const job=await get('/api/job');const messages={benchmark:'Abort now? The current partial answer will not be scored; completed tasks stay saved.',pull:'Stop this download request? Ollama may retain partial data and resume later.',cloud:'Stop Codex CLI draft generation now?',judge:'Stop local judging now? Already graded answers stay saved.'};if(!confirm(messages[job?.type]||'Abort this job now?'))return;await post('/api/job/abort',{mode:'now'});notice('Abort requested.');});
  setInterval(poll,2500);
}
init().catch(e=>notice(`App initialization failed: ${e.message}`,true));
