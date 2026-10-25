'use strict';

// These helpers build queue entries locally; only the Add action sends them to the backend.
(function(root) {
  const MODES = {
    'on-finite': {think:true, open:false},
    'on-open': {think:true, open:true},
    'off-finite': {think:false, open:false},
    'off-open': {think:false, open:true},
    'default-finite': {think:null, open:false},
    'default-open': {think:null, open:true}
  };
  function outputCap(value) {
    const cap=Number(value);
    if(!Number.isInteger(cap)||cap<32||cap>32768)throw new Error('Finite output cap must be 32–32768 tokens');
    return cap;
  }
  function expand(base,modes,temperatures,capValue) {
    const cap=outputCap(capValue);
    if(!modes.length||!temperatures.length)throw new Error('Select at least one mode and temperature');
    const uniqueModes=[...new Set(modes)],uniqueTemps=[...new Set(temperatures.map(Number))];
    if(uniqueModes.some(mode=>!MODES[mode])||uniqueTemps.some(temp=>!Number.isFinite(temp)||temp<0||temp>2))
      throw new Error('Invalid queue combination');
    return uniqueModes.flatMap(mode=>uniqueTemps.map(temperature=>({
      ...base,think:MODES[mode].think,temperature,
      numPredict:MODES[mode].open?-1:cap
    })));
  }
  function standard(base,cap) {
    return expand(base,['on-finite','on-open','off-finite'],[0],cap);
  }
  function label(config) {
    const thinking=config.think===true?'Thinking on':config.think===false?'Thinking off':'Model default';
    const output=config.numPredict===-1?'open output':`${config.numPredict} output tokens`;
    return `${thinking} · Temp ${config.temperature} · ${output} · ctx ${config.numCtx} · ${config.repetitions} rep`;
  }
  const api={expand,standard,label,outputCap};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.BenchmarkQueueUI=api;
})(typeof window==='undefined'?globalThis:window);
