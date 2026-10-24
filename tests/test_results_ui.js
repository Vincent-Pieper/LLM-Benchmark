'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const comparison=require('../public/comparison.js');

const elements=new Map();
function element(id) {
  if(!elements.has(id))elements.set(id,{
    innerHTML:'',textContent:'',onclick:null,
    addEventListener:()=>{},querySelectorAll:()=>[]
  });
  return elements.get(id);
}
global.window={BenchmarkComparison:comparison};
global.document={getElementById:element,querySelectorAll:()=>[]};
require('../public/results_ui.js');

function run(model,think,temperature,num_predict) {
  return {id:model,startedAt:'2026-10-24T12:00:00Z',status:'completed',repetitions:1,
    setup:{model,digest:model,think,details:{quantization_level:'Q4_K_M'},
      options:{num_ctx:4096,temperature,num_predict}},
    tasks:Array.from({length:5},(_,index)=>({category:'ocr',level:index+1,
      repetition:1,score:10,processingMs:1000,metrics:{evalCount:10,evalNs:1e9}}))};
}
const low=run('low',false,0,1024);
low.tasks[0].formatReview={contentScore:10,originalFormatValid:false};
const high=run('high',true,1,8192);
const open=run('open',false,2,-1);
window.BenchmarkResultsUI.render({runs:[low,high,open],allRuns:[low,high,open],
  categories:[{id:'ocr',label:'OCR'}],selectedRun:null,onSelect:()=>{}});
element('group-toggle').onclick();
const markup=element('result-rows').innerHTML;
assert.match(markup,/class="setup-tag think-off"/);
assert.match(markup,/class="setup-tag think-on"/);
assert.match(markup,/class="setup-tag temp-0"/);
assert.match(markup,/class="setup-tag temp-5"/);
assert.match(markup,/class="setup-tag output-0"/);
assert.match(markup,/class="setup-tag output-10"/);
assert.match(markup,/class="setup-tag output-open"/);
assert.match(markup,/class="format-marker"/);
assert.doesNotMatch(markup,/\bstyle=/);
const css=fs.readFileSync(path.join(__dirname,'../public/results.css'),'utf8');
for(const tone of ['think-off','think-on','temp-0','temp-5','temp-10',
  'output-0','output-10','output-open'])
  assert.ok(css.includes(`.setup-tag.${tone} { --tag-color:`),`Missing CSS color for ${tone}`);
console.log('Result tags have CSP-compatible colors and format marker: OK');
