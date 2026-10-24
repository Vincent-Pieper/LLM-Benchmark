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

function renderResults() {
  const pack=chosenPack();
  const categoryIds=pack?.categoryIds || [...new Set((pack?.tasks||[]).map(t=>t.category))];
  const categories=(state.categories || []).filter(c=>categoryIds.includes(c.id));
  const runs=pack ? state.runs.filter(run=>run.packHash===pack.hash) : [];
  $('result-hint').textContent=`${pack?.approved?'Approved':'Draft'} pack · ${categories.length} areas × 50 points. A small F marks content scores after a safe format repair; raw grades remain available. Speed and cold load are separate.`;
  if(!runs.some(run=>run.id===selectedRun))selectedRun=null;
  window.BenchmarkResultsUI.render({runs,allRuns:state.runs,categories,selectedRun,onSelect:id=>{selectedRun=id;renderResults();}});
  renderResultDetail();
}
function taskMetrics(record) {
  const metrics=record.metrics||{};
  const tokens=Number(metrics.evalCount),ns=Number(metrics.evalNs);
  const rate=tokens>0&&ns>0?tokens*1e9/ns:null;
  return `<div class="task-metrics"><span>Total ${Number.isFinite(record.processingMs)?fmt(record.processingMs/1000)+'s':'—'}</span><span>Input ${fmt(metrics.promptEvalCount)} tokens · ${Number.isFinite(metrics.promptEvalNs)?fmt(metrics.promptEvalNs/1e9)+'s':'—'}</span><span>Output ${fmt(metrics.evalCount)} tokens · ${Number.isFinite(metrics.evalNs)?fmt(metrics.evalNs/1e9)+'s':'—'}</span><span>${fmt(rate)} tok/s</span></div>`;
}
function judgeFailureDetails(record) {
  const failures=record.judgeFailures||[];
  if(!failures.length)return '';
  return `<details><summary>Judge failures (${failures.length})</summary>${failures.map((failure,index)=>`<details><summary>Attempt ${index+1} · ${esc(failure.model)} · ${esc(failure.kind)} · ${esc(when(failure.failedAt))}</summary><p class="hint">${esc(failure.message)} · done reason ${esc(failure.metrics?.doneReason||'unknown')} · output tokens ${fmt(failure.metrics?.evalCount)} · format ${esc(failure.schemaVersion||'unknown')}</p><details><summary>Raw judge response</summary><pre>${esc(failure.raw??'(no response)')}</pre></details><details><summary>Judge thinking output</summary><pre>${esc(failure.thinking??'(not returned)')}</pre></details></details>`).join('')}</details>`;
}
function taskDetails(run,pack) {
  const order=state.categories.map(category=>category.id);
  const records=[...run.tasks].sort((a,b)=>a.repetition-b.repetition||order.indexOf(a.category)-order.indexOf(b.category)||a.level-b.level);
  return `<details open><summary>Tasks · time, tokens and raw answers</summary>${records.map(record=>{
    const task=pack?.tasks.find(item=>item.id===record.taskId);
    const score=window.BenchmarkComparison.scoreOf(record);
    const review=record.formatReview;
    const scoreLabel=score===null?'unscored':`${fmt(score)}/10`;
    return `<details class="task"><summary>Rep ${esc(record.repetition)} · ${esc(record.category)} ${esc(record.level)} · <span class="status ${esc(record.status)}">${esc(record.status)}</span> · <span class="score-chip">${esc(scoreLabel)}</span>${review?.originalFormatValid===false?'<span class="format-marker" title="Content scored after a simple format repair" aria-label="Format-adjusted answer">F</span>':''}</summary>${taskMetrics(record)}<p class="hint">${esc(record.reason||'')}</p>${review?`<p class="hint">Format repair ${esc(review.adapter)} · Original score ${fmt(review.strictScore)}/10 · content score ${fmt(review.contentScore)}/10 · review ${esc(review.version)}</p>`:''}${judgeFailureDetails(record)}${task?`<details><summary>Task and reference</summary><pre>${esc(JSON.stringify(task,null,2))}</pre></details>`:''}${record.raw!==undefined?`<details><summary>Raw model answer</summary><pre>${esc(record.raw)}</pre></details>`:''}${record.breakdown?`<pre>${esc(JSON.stringify(record.breakdown,null,2))}</pre>`:''}</details>`;
  }).join('')}</details>`;
}
async function loadTelemetry(run) {
  const holder=$('telemetry-content');
  if(!holder)return;
  try {
    const {samples}=await get(`/api/telemetry?runId=${encodeURIComponent(run.id)}`);
    if(!$('telemetry-content'))return;
    if(!samples.length){holder.textContent='No samples recorded for this run.';return;}
    holder.innerHTML=`<p class="hint">${samples.length} local samples. GPU energy covers the whole GPU, not just Ollama. Short tasks may finish between samples.</p><div class="telemetry-scroll"><table><thead><tr><th>Time</th><th>CPU</th><th>Free RAM</th><th>GPU</th><th>VRAM</th><th>Clock</th><th>GPU power</th><th>Temp</th><th>Sample cost</th></tr></thead><tbody>${samples.map(sample=>`<tr><td>${esc(when(sample.at))}</td><td>${fmt(sample.cpuPercent)}%</td><td>${fmt(sample.freeRamGiB)} GiB</td><td>${fmt(sample.gpu?.utilizationPercent)}%</td><td>${fmt(sample.gpu?.usedMiB)} MiB</td><td>${fmt(sample.gpu?.clockMHz)} MHz</td><td>${fmt(sample.gpu?.powerW)} W</td><td>${fmt(sample.gpu?.temperatureC)} °C</td><td>${fmt(sample.sampleMs)} ms</td></tr>`).join('')}</tbody></table></div>`;
  } catch(error) { if(holder)holder.textContent=error.message; }
}
function renderResultDetail() {
  const el=$('result-detail'),run=state.runs.find(r=>r.id===selectedRun);
  if(!run){el.innerHTML='';return;}
  const pack=state.packs.find(p=>p.id===run.packId);
  const pending=run.tasks.filter(t=>t.status==='subjective_pending'||t.status==='judge_error').length;
  const retryable=run.status!=='completed'||run.tasks.some(t=>['timed_out','skipped_due_to_limit','technical_error','aborted'].includes(t.status));
  const telemetry=run.telemetry;
  const telemetryLabel=telemetry?`${esc(telemetry.status)} · ${fmt(telemetry.sampleCount)} samples · median query ${fmt(telemetry.medianSampleMs)} ms · observed whole-GPU energy ${fmt(telemetry.gpuEnergyWh)} Wh`:'No telemetry was collected for this historical run';
  el.innerHTML=`<div class="divider"></div><h3>${esc(run.setup.model)} · ${esc(run.id.slice(0,8))}</h3><p class="hint">${esc(run.repetitions)} repetition(s) · ${esc(run.ollamaVersion||'Ollama version unknown')} · ${esc(run.packHash.slice(0,16))} · started ${esc(when(run.startedAt))}</p><p class="hint">Quantization ${esc(run.setup.details?.quantization_level||'unknown')} · Context ${esc(run.setup.options?.num_ctx??'unknown')} · Model digest ${esc(run.setup.digest||'unknown')}</p>${pending?`<p class="hint">${pending} subjective answer(s) still need a valid judge grade; the final score remains incomplete until they are graded.</p>`:''}${run.error?`<p class="tag bad">${esc(run.error)}</p>`:''}<div class="inline">${pending?`<select id="judge-model">${(library?.models||[]).map(m=>`<option value="${esc(m.name)}">Judge with ${esc(m.name)}</option>`).join('')}</select><button id="judge-btn">Judge ${pending} answer(s)</button>`:''}<button id="project-btn" class="secondary">Preview current time limits</button>${retryable?'<button id="retry-btn" class="secondary">Retry incomplete tasks with current limits</button>':''}</div><div id="projection-content"></div><details id="telemetry-detail"><summary>System samples · ${telemetryLabel}</summary><div id="telemetry-content">Open to load local samples.</div></details>${taskDetails(run,pack)}`;
  $('telemetry-detail').ontoggle=()=>{if($('telemetry-detail').open)loadTelemetry(run);};
  $('project-btn').onclick=guard(async()=>{const projection=await post('/api/runs/project',{runId:run.id,limitsSec:readLimits()});$('projection-content').innerHTML=`<p class="hint">Preview only; original run data is unchanged. ${projection.categories.map(c=>`${esc(c.category)}: ${c.score===null?'needs retry':fmt(c.score)+'/50'}`).join(' · ')} · Total: ${projection.total===null?'incomplete':fmt(projection.total)}</p><details><summary>Projected task statuses</summary><pre>${esc(JSON.stringify(projection.records,null,2))}</pre></details>`;});
  if(pending){$('judge-btn').onclick=guard(async()=>{const model=$('judge-model').value;if(!model)throw new Error('Choose an installed judge model');if(!confirm(`Run the local judge ${model} on ${pending} saved answer(s)? This unloads loaded Ollama models. Each grading request uses a JSON schema and may generate up to 4096 tokens.`))return;await post('/api/judge',{runId:run.id,judgeModel:model});notice('Local judging started.');});}
  if(run.tasks.some(t=>state.categories.some(c=>c.id===t.category&&c.type==='subjective')&&typeof t.raw==='string'&&t.raw.trim())){
    const regradeCount=run.tasks.filter(t=>state.categories.some(c=>c.id===t.category&&c.type==='subjective')&&typeof t.raw==='string'&&t.raw.trim()).length;
    const actions=el.querySelector('.inline');
    if(!$('judge-model')){const select=document.createElement('select');select.id='judge-model';for(const model of library?.models||[]){const option=document.createElement('option');option.value=model.name;option.textContent=`Judge with ${model.name}`;select.append(option);}actions.prepend(select);}
    const button=document.createElement('button');button.className='secondary';button.textContent='Rejudge saved subjective answers';actions.append(button);
    button.onclick=guard(async()=>{const model=$('judge-model').value;if(!model)throw new Error('Choose an installed judge model');if(!confirm(`Rejudge all ${regradeCount} saved subjective answers with ${model}? Loaded Ollama models will be unloaded. Each request uses a JSON schema and may generate up to 4096 tokens. Previous grades remain in history.`))return;await post('/api/judge',{runId:run.id,judgeModel:model,rejudge:true});notice('Rejudging started.');});
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
  $('wide-results').onclick=()=>window.BenchmarkResultsUI.setWide();
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
