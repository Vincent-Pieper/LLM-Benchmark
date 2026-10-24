'use strict';

/* Pure comparison logic; the browser view and offline checks share these rules. */
const BenchmarkComparison = (() => {
  const scaleStep = (value,min,max) => Number.isFinite(value)
    ? Math.round(Math.max(0,Math.min(1,(value-min)/(max-min)))*10) : null;
  const temperatureStep = value => scaleStep(value,0,2);
  const outputRange = runs => {
    let min=Infinity,max=-Infinity;
    for(const run of runs) {
      const value=run.setup?.options?.num_predict;
      if(Number.isFinite(value)&&value>=0) {
        min=Math.min(min,value);
        max=Math.max(max,value);
      }
    }
    return min===Infinity?null:{min,max};
  };
  const outputStep = (value,range) =>
    !Number.isFinite(value)||value<0||!range ? null
      : range.min===range.max ? 5 : scaleStep(value,range.min,range.max);
  const median = values => {
    if (!values.length) return null;
    const sorted = [...values].sort((a,b) => a-b);
    const middle = Math.floor(sorted.length/2);
    return sorted.length%2 ? sorted[middle] : (sorted[middle-1]+sorted[middle])/2;
  };
  const scoreOf = record => Number.isFinite(record?.formatReview?.contentScore)
    ? record.formatReview.contentScore : Number.isFinite(record?.score) ? record.score : null;
  const rateOf = records => {
    const count = records.reduce((sum,record) => sum+(Number(record.metrics?.evalCount)||0),0);
    const ns = records.reduce((sum,record) => sum+(Number(record.metrics?.evalNs)||0),0);
    return ns>0 ? count*1e9/ns : null;
  };
  function category(run, id) {
    const records = run.tasks.filter(record => record.category===id);
    const totals=[], times=[], levels=[];
    let pending=false;
    for(let rep=1;rep<=run.repetitions;rep++) {
      const tasks=records.filter(record => record.repetition===rep);
      const scores=tasks.map(scoreOf);
      if(tasks.length===5&&scores.every(Number.isFinite)) totals.push(scores.reduce((a,b)=>a+b,0));
      else pending=true;
      if(tasks.length===5&&tasks.every(record=>Number.isFinite(record.processingMs)))
        times.push(tasks.reduce((sum,record)=>sum+record.processingMs,0));
    }
    for(let level=1;level<=5;level++) {
      const scores=records.filter(record=>record.level===level).map(scoreOf).filter(Number.isFinite);
      levels.push(scores.length ? scores.reduce((a,b)=>a+b,0)/scores.length : null);
    }
    const score=totals.length ? totals.reduce((a,b)=>a+b,0)/totals.length : null;
    const deviation=score===null ? null : Math.sqrt(totals.reduce((sum,value)=>sum+(value-score)**2,0)/totals.length);
    const adaptedCount=records.filter(record=>record.formatReview?.originalFormatValid===false).length;
    return {score,deviation,pending,levels,timeMs:median(times),rate:rateOf(records),
      adapted:adaptedCount>0,adaptedCount,
      recordCount:records.length};
  }
  function summarize(run, categoryIds) {
    const categories=Object.fromEntries(categoryIds.map(id=>[id,category(run,id)]));
    const values=Object.values(categories);
    return {run,categories,total:values.every(value=>value.score!==null&&!value.pending)
      ? values.reduce((sum,value)=>sum+value.score,0) : null,
      rate:rateOf(run.tasks)};
  }
  function matches(run, filters) {
    const setup=run.setup, options=setup.options||{};
    const actual={think:setup.think===null?'default':String(setup.think),
      quant:setup.details?.quantization_level||'unknown',
      temperature:String(options.temperature), context:String(options.num_ctx),
      limit:String(options.num_predict), digest:setup.digest||''};
    return Object.entries(filters).every(([key,value])=>!value||value==='all'||actual[key]===value);
  }
  function value(summary,key) {
    if(key==='total')return summary.total;
    if(key==='speed')return summary.rate;
    if(key==='started')return Date.parse(summary.run.startedAt)||0;
    const [metric,categoryId]=key.split(':');
    const category=summary.categories[categoryId];
    if(!category)return null;
    return metric==='score'?category.score:metric==='time'?category.timeMs:metric==='rate'?category.rate:null;
  }
  function compare(a,b,sorts) {
    for(const sort of sorts) {
      const left=value(a,sort.key), right=value(b,sort.key);
      if(left===null&&right!==null)return 1;
      if(right===null&&left!==null)return -1;
      if(left!==right)return (left-right)*(sort.direction==='asc'?1:-1);
    }
    return (Date.parse(b.run.startedAt)||0)-(Date.parse(a.run.startedAt)||0);
  }
  function rows(runs,categoryIds,filters,sorts,grouped) {
    const summaries=runs.filter(run=>matches(run,filters)).map(run=>summarize(run,categoryIds));
    summaries.sort((a,b)=>compare(a,b,sorts));
    if(!grouped)return summaries.map(summary=>({kind:'variant',summary}));
    const groups=new Map();
    for(const summary of summaries) {
      const name=summary.run.setup.model;
      if(!groups.has(name))groups.set(name,[]);
      groups.get(name).push(summary);
    }
    return [...groups].map(([name,variants])=>({kind:'group',name,variants,
      representative:variants[0]})).sort((a,b)=>compare(a.representative,b.representative,sorts));
  }
  return {scoreOf,rateOf,category,summarize,matches,value,compare,rows,
    temperatureStep,outputRange,outputStep};
})();
if(typeof window!=='undefined')window.BenchmarkComparison=BenchmarkComparison;
if(typeof module!=='undefined')module.exports=BenchmarkComparison;
