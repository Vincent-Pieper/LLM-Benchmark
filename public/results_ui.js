'use strict';

const BenchmarkResultsUI = (() => {
  const C = window.BenchmarkComparison;
  const view = {grouped:true,expanded:new Set(),sorts:[],filters:{},wide:false};
  const esc = value => String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = value => Number.isFinite(value) ? value.toLocaleString('en-US',{maximumFractionDigits:2}) : '—';
  const seconds = ms => Number.isFinite(ms) ? `${number(ms/1000)}s` : '—';
  const sortLabel = (key,categories) => {
    if(key==='total')return 'Total quality';
    if(key==='speed')return 'Overall tok/s';
    if(key==='started')return 'Run date';
    const [metric,id]=key.split(':');
    const category=categories.find(item=>item.id===id);
    return `${category?.label||id} ${metric==='score'?'score':metric==='time'?'time':'tok/s'}`;
  };
  function toggleSort(key) {
    const current=view.sorts.find(sort=>sort.key===key);
    if(current)current.direction=current.direction==='asc'?'desc':'asc';
    else view.sorts.push({key,direction:key.startsWith('time:')?'asc':'desc'});
  }
  function optionsFor(runs,key) {
    const values=new Map();
    for(const run of runs) {
      const setup=run.setup,options=setup.options||{};
      const item={think:[setup.think===null?'default':String(setup.think),setup.think===null?'Default':setup.think?'Thinking on':'Thinking off'],
        quant:[setup.details?.quantization_level||'unknown',setup.details?.quantization_level||'Unknown'],
        temperature:[String(options.temperature),`Temperature ${options.temperature}`],
        context:[String(options.num_ctx),`Context ${options.num_ctx}`],
        limit:[String(options.num_predict),options.num_predict===-1?'Output open-ended':`Output ${options.num_predict}`],
        digest:[setup.digest||'',`Digest ${(setup.digest||'').slice(0,12)}`]}[key];
      if(item)values.set(item[0],item[1]);
    }
    return [...values].sort((a,b)=>a[1].localeCompare(b[1]));
  }
  function controls(runs,categories,rerender) {
    const fields=[['think','Thinking'],['quant','Quantization'],['temperature','Temperature'],
      ['context','Context'],['limit','Output limit'],['digest','Model version']];
    document.getElementById('result-controls').innerHTML=`<div class="result-filters">
      <button id="group-toggle" class="secondary">${view.grouped?'Show individual runs':'Group by model'}</button>
      ${fields.map(([key,label])=>`<label>${label}<select data-filter="${key}"><option value="all">All</option>${optionsFor(runs,key).map(([value,title])=>`<option value="${esc(value)}" ${view.filters[key]===value?'selected':''}>${esc(title)}</option>`).join('')}</select></label>`).join('')}
      <button id="clear-filters" class="secondary">Clear filters</button></div>
      <div class="sort-order">${view.sorts.map((sort,index)=>`<button class="secondary" data-remove-sort="${index}" title="Remove this sort">${index+1}. ${esc(sortLabel(sort.key,categories))} ${sort.direction==='asc'?'↑':'↓'} ×</button>`).join('')}${view.sorts.length?'<button id="clear-sorts" class="secondary">Clear sorting</button>':'<span class="hint">Click a column heading to sort; click another for a tie-breaker.</span>'}</div>`;
    document.getElementById('group-toggle').onclick=()=>{view.grouped=!view.grouped;rerender();};
    document.getElementById('clear-filters').onclick=()=>{view.filters={};rerender();};
    document.getElementById('clear-sorts')?.addEventListener('click',()=>{view.sorts=[];rerender();});
    document.querySelectorAll('#result-controls select[data-filter]').forEach(select=>select.onchange=()=>{view.filters[select.dataset.filter]=select.value;rerender();});
    document.querySelectorAll('#result-controls button[data-remove-sort]').forEach(button=>button.onclick=()=>{view.sorts.splice(Number(button.dataset.removeSort),1);rerender();});
  }
  function scoreCell(summary,id) {
    const item=summary.categories[id];
    const bars=item.levels.map(value=>`<span class="${value===null?'missing':value>=9.99?'filled':value>0?'partial':''}${value===null?'':` shade-${Math.max(0,Math.min(10,Math.round(value)))}`}" title="Level score ${esc(number(value))}/10"></span>`).join('');
    const label=item.score===null?'—':`${number(item.score)}${item.deviation>0?` ± ${number(item.deviation)}`:''}`;
    const formatMarker=item.adaptedCount
      ? `<span class="format-marker" title="${item.adaptedCount} answer(s) scored after a simple format repair; original strict scores remain available" aria-label="${item.adaptedCount} format-adjusted answer(s)">F</span>`:'';
    return `<td class="category-cell" title="${esc(id)} · median category time ${seconds(item.timeMs)} · ${number(item.rate)} tok/s">
      <span class="score-chip">${esc(label)}</span>${formatMarker}${item.pending?'<small>partial</small>':''}<div class="level-bars">${bars}</div><small class="category-speed">${seconds(item.timeMs)} · ${number(item.rate)} tok/s</small></td>`;
  }
  function tags(run,outputRange) {
    const setup=run.setup,opts=setup.options||{};
    const thinking=setup.think===true?['Thinking on','think-on']:
      setup.think===false?['Thinking off','think-off']:['Think default','unknown'];
    const temperature=Number.isFinite(opts.temperature)
      ? [`Temp ${number(opts.temperature)}`,`temp-${C.temperatureStep(opts.temperature)}`] : ['Temp unknown','unknown'];
    const output=opts.num_predict===-1?['Output open','output-open']:
      Number.isFinite(opts.num_predict)
        ? [`Output ${number(opts.num_predict)}`,outputRange?`output-${C.outputStep(opts.num_predict,outputRange)}`:'unknown']
        : ['Output unknown','unknown'];
    return `<span class="setup-tags">${[thinking,temperature,output].map(([label,tone])=>
      `<span class="setup-tag ${tone}" title="${esc(label)}">${esc(label)}</span>`).join('')}</span>`;
  }
  function cells(summary,categories) {
    return `${categories.map(item=>scoreCell(summary,item.id)).join('')}<td><strong>${number(summary.total)}</strong>${summary.total===null?'<small>incomplete</small>':''}</td><td>${number(summary.rate)} tok/s<small>load ${seconds(summary.run.coldLoadMs)}</small></td><td><span class="tag ${summary.run.status==='completed'?'good':summary.run.status==='failed'?'bad':'warn'}">${esc(summary.run.status)}</span><small>${esc(new Date(summary.run.startedAt).toLocaleString())}</small></td>`;
  }
  function variantRow(summary,categories,selectedRun,outputRange) {
    const run=summary.run;
    const quant=run.setup.details?.quantization_level||'Unknown quantization';
    return `<tr class="variant-row ${selectedRun===run.id?'active':''}" data-run="${esc(run.id)}"><td class="setup-cell"><strong>${esc(run.setup.model)}</strong><span class="quant-label">${esc(quant)}</span><div class="result-tags">${tags(run,outputRange)}</div></td>${cells(summary,categories)}</tr>`;
  }
  function groupRows(group,categories,selectedRun,outputRange) {
    const top=group.representative,open=view.expanded.has(group.name);
    return `<tr class="group-row" data-group="${esc(group.name)}"><td class="setup-cell"><button class="group-toggle" aria-label="${open?'Collapse':'Expand'} ${esc(group.name)}">${open?'▾':'▸'}</button><strong>${esc(group.name)}</strong><small>${group.variants.length} variant(s) · values from top sorted run</small></td>${cells(top,categories)}</tr>${open?group.variants.map(summary=>variantRow(summary,categories,selectedRun,outputRange)).join(''):''}`;
  }
  function render({runs,allRuns=runs,categories,selectedRun,onSelect}) {
    const rerender=()=>render({runs,allRuns,categories,selectedRun,onSelect});
    const outputRange=C.outputRange(allRuns);
    controls(runs,categories,rerender);
    document.getElementById('result-legend').innerHTML=`<span class="format-marker" aria-hidden="true">F</span> Content scored after a simple, versioned format repair. Original output and strict score remain saved. Draft pack scores are provisional.`;
    const sorted=C.rows(runs,categories.map(item=>item.id),view.filters,view.sorts,view.grouped);
    const sortedButton=(key,label)=>`<button data-sort="${esc(key)}" class="${view.sorts.some(sort=>sort.key===key)?'is-sorted':''}">${esc(label)}</button>`;
    document.getElementById('result-head').innerHTML=`<th>Model / setup</th>${categories.map(item=>`<th>${sortedButton(`score:${item.id}`,item.label)}<br>${sortedButton(`time:${item.id}`,'Time')} ${sortedButton(`rate:${item.id}`,'tok/s')}</th>`).join('')}<th>${sortedButton('total','Total')}</th><th>${sortedButton('speed','Speed')}</th><th>${sortedButton('started','Run')}</th>`;
    const body=document.getElementById('result-rows');
    body.innerHTML=sorted.length?sorted.map(item=>item.kind==='group'?groupRows(item,categories,selectedRun,outputRange):variantRow(item.summary,categories,selectedRun,outputRange)).join(''):`<tr><td colspan="${categories.length+4}" class="muted">No runs match these setup filters.</td></tr>`;
    document.querySelectorAll('#result-head button[data-sort]').forEach(button=>button.onclick=()=>{toggleSort(button.dataset.sort);rerender();});
    body.querySelectorAll('tr[data-group]').forEach(row=>row.onclick=()=>{const name=row.dataset.group;view.expanded.has(name)?view.expanded.delete(name):view.expanded.add(name);rerender();});
    body.querySelectorAll('tr[data-run]').forEach(row=>row.onclick=()=>onSelect(row.dataset.run));
  }
  function setWide() {
    view.wide=!view.wide;
    document.body.classList.toggle('wide-results',view.wide);
    document.getElementById('wide-results').textContent=view.wide?'Standard width':'Wide view';
  }
  return {render,setWide};
})();
window.BenchmarkResultsUI=BenchmarkResultsUI;
