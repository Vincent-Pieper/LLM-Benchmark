'use strict';
const assert=require('node:assert/strict');
const Q=require('../public/queue.js');
const base={packId:'p',model:'fake:1',digest:'digest',think:false,numCtx:4096,
  temperature:1.5,numPredict:1024,limitsSec:[60,60,60,60,60],repetitions:5};
const standard=Q.standard(base,4096);
assert.deepEqual(standard.map(x=>[x.think,x.temperature,x.numPredict]),
  [[true,0,4096],[true,0,-1],[false,0,4096]]);
assert.equal(standard[0].repetitions,5);
const custom=Q.expand(base,['off-finite','on-open'],[0,1,2],8192);
assert.equal(custom.length,6);
assert.deepEqual(custom.map(x=>x.temperature),[0,1,2,0,1,2]);
assert.equal(custom[0].numPredict,8192);
assert.equal(custom[3].numPredict,-1);
assert.throws(()=>Q.expand(base,[],[0],4096),/Select/);
assert.throws(()=>Q.standard(base,-1),/Finite output cap/);
