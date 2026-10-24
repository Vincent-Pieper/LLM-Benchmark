'use strict';
const assert = require('node:assert/strict');
const C = require('../public/comparison.js');

function run(model, score, ms, speed, think=false, startedAt='2026-10-24T12:00:00Z') {
  return {id:`${model}-${score}-${speed}`,startedAt,repetitions:1,
    setup:{model,digest:model,think,details:{quantization_level:'Q4_K_M'},
      options:{num_ctx:4096,temperature:0,num_predict:4096}},
    tasks:Array.from({length:5},(_,index)=>({category:'ocr',level:index+1,repetition:1,
      score:score/5,processingMs:ms/5,metrics:{evalCount:speed,evalNs:1e9}}))};
}
const a=run('a:1',40,5000,20);
const b=run('b:1',40,10000,50);
const c=run('c:1',30,1000,100,true);
const review=run('a:1',0,4000,30);
review.id='adapted';
review.tasks[0].formatReview={contentScore:10,originalFormatValid:false};
assert.equal(C.scoreOf(review.tasks[0]),10);
assert.equal(C.category(review,'ocr').score,10);
assert.equal(C.category(review,'ocr').adaptedCount,1);
const byScoreThenRate=C.rows([a,b,c],['ocr'],{},[
  {key:'score:ocr',direction:'desc'},{key:'rate:ocr',direction:'desc'}],false);
assert.deepEqual(byScoreThenRate.map(item=>item.summary.run.id),[b.id,a.id,c.id]);
const byScoreThenTime=C.rows([a,b,c],['ocr'],{},[
  {key:'score:ocr',direction:'desc'},{key:'time:ocr',direction:'asc'}],false);
assert.deepEqual(byScoreThenTime.map(item=>item.summary.run.id),[a.id,b.id,c.id]);
assert.equal(C.rows([a,b,c],['ocr'],{think:'true'},[],true).length,1);
const groups=C.rows([a,review,b],['ocr'],{},[{key:'score:ocr',direction:'desc'}],true);
assert.equal(groups.length,2);
assert.equal(groups.find(item=>item.name==='a:1').variants.length,2);
const low=run('low',0,1000,1);
const newMinimum=run('new-minimum',0,1000,1);
const middle=run('middle',0,1000,1);
const high=run('high',0,1000,1);
const open=run('open',0,1000,1);
low.setup.options.num_predict=1024;
newMinimum.setup.options.num_predict=512;
middle.setup.options.num_predict=4096;
high.setup.options.num_predict=8192;
open.setup.options.num_predict=-1;
const outputRange=C.outputRange([low,high,open]);
assert.deepEqual(outputRange,{min:1024,max:8192});
assert.equal(C.outputStep(1024,outputRange),0);
assert.equal(C.outputStep(8192,outputRange),10);
assert.equal(C.outputStep(1024,C.outputRange([low,middle,high,open])),
  C.outputStep(1024,outputRange));
assert.notEqual(C.outputStep(1024,C.outputRange([newMinimum,low,high,open])),
  C.outputStep(1024,outputRange));
assert.equal(C.outputStep(1024,C.outputRange([low,open])),5);
assert.equal(C.temperatureStep(0),0);
assert.equal(C.temperatureStep(1),5);
assert.equal(C.temperatureStep(2),10);
console.log('Comparison sorting, grouping, filters and adapted scores: OK');
