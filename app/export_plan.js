// Portable canonical plan runner. One browser/context at a time, no fixed sleeps.
const fs=require('node:fs'), path=require('node:path');
const {replay,launchOptions,resolve}=require('./testforge_runtime.js');
const {DesktopSimulator}=require('./testforge_desktop.js');
const {MobileSimulator}=require('./testforge_mobile.js');
const devices={desktop:{viewport:{width:1024,height:640}},tablet:{viewport:{width:820,height:1180},hasTouch:true},mobile:{viewport:{width:390,height:844},hasTouch:true}};
function values(raw){
 const result={};
 for(const [key,item] of Object.entries(raw || {})){
  if(item && typeof item==='object'){
   if(Object.keys(item).length!==1 || !/^TF_SECRET_[A-Z0-9_]{1,64}$/.test(item.env || '') || process.env[item.env]===undefined) throw new Error('Missing/invalid secret reference');
   result[key]=process.env[item.env];
  }else result[key]=item;
 }
 return result;
}
function storage(alias){
 if(!alias) return undefined;
 if(!/^[A-Z0-9_]{1,64}$/.test(alias)) throw new Error('Invalid auth profile');
 const raw=process.env['TF_AUTH_STATE_'+alias];
 if(!raw || Buffer.byteLength(raw)>262144) throw new Error('Missing/oversized auth profile');
 const p=JSON.parse(raw);
 if(!Number.isFinite(Number(p.expires_at)) || Number(p.expires_at)<=Date.now()/1000 || !Array.isArray(p.state?.cookies) || !Array.isArray(p.state?.origins)) throw new Error('Invalid/expired auth profile');
 return p.state;
}
async function replayPlan(pw, plan, output){
 const stages=['setup','steps','teardown'];
 if(plan.version!==1 || !Array.isArray(plan.dataset) || !plan.dataset.length || !Array.isArray(plan.matrix) || !plan.matrix.length || plan.matrix.length>6 || plan.dataset.length*plan.matrix.length>25 || ![0,1].includes(plan.retries)) throw new Error('Invalid canonical plan');
 let actions=0,downloads=0,authored=0;
 for(const stage of stages){
  if(!Array.isArray(plan[stage])) throw new Error('Invalid plan stage');
  for(const step of plan[stage]){
   const n=step.repeat_count ?? 1;
   if(!Number.isInteger(n) || n<1 || n>100) throw new Error('Invalid repeat count');
   actions+=n;authored++;if(step.action==='download')downloads+=n;
  }
 }
 const multiplier=plan.dataset.length*plan.matrix.length*(plan.retries+1);
 if(authored>500 || !plan.steps.length || actions*multiplier>2000 || downloads*multiplier>16) throw new Error('Plan resource limit exceeded');
 fs.mkdirSync(output,{recursive:true});
 const cases=[];
 for(const profile of plan.matrix){
  if(profile.runner && profile.runner!=='web'){
   // Desktop/mobile profiles run against the agent/hub bridge in the Python
   // export; here the deterministic simulators provide the same contract.
   for(let row=0;row<plan.dataset.length;row++){
    const testCase={case:cases.length+1,row:row+1,...profile,status:'failed',attempts:[]};
    for(let attempt=0;attempt<=plan.retries;attempt++){
     const vars={...Object.fromEntries(Object.entries(process.env).filter(([k])=>k.startsWith('TF_VAR_')).map(([k,v])=>[k.slice(7),v])),...values(plan.variables),...values(plan.dataset[row])};
     const sim=profile.runner==='windows_desktop' ? new DesktopSimulator(profile.desktop_app) : new MobileSimulator(profile.platform || 'android');
     const foreign=profile.runner==='windows_desktop' ? ['app_launch','tap_element','long_press','swipe','mobile_type','device_key','rotate_orientation','toggle_network_mode','scan_barcode','assert_mobile_text','assert_mobile_visible'] : ['window_attach','ui_click','ui_double_click','ui_right_click','ui_type','ui_select','ui_check','ui_menu_invoke','ui_grid_read','assert_control_text','assert_control_state','wait_window_ready'];
     const record={attempt:attempt+1,status:'passed',steps:[]};let failed=false;
     for(const stage of stages){
      if(failed && stage!=='teardown')continue;
      for(let i=0;i<plan[stage].length;i++){
       const step=plan[stage][i];
       if(foreign.includes(step.action)){record.steps.push({stage,step:i+1,action:step.action,status:'skipped'});continue;}
       const resolved={action:step.action,value:['ui_grid_read','table_lookup','capture_text'].includes(step.action)?step.value:resolve(step.value || '',vars),selector:resolve(step.selector || {},vars)};
       try{sim.perform(resolved,vars);record.steps.push({stage,step:i+1,action:step.action,status:'passed',mode:'simulator'});}
       catch(error){failed=true;record.status='failed';record.steps.push({stage,step:i+1,action:step.action,status:'failed',error_type:error.name});if(stage!=='teardown')break;}
      }
     }
     testCase.attempts.push(record);
     if(record.status==='passed'){testCase.status=attempt?'flaky':'passed';break;}
    }
    cases.push(testCase);
   }
   continue;
  }
  if(!['chromium','firefox','webkit'].includes(profile.browser) || !Object.hasOwn(devices,profile.device)) throw new Error('Invalid browser/device');
  const browser=await pw[profile.browser].launch(profile.browser==='chromium' ? launchOptions() : {headless:true});
  try {
   for(let row=0;row<plan.dataset.length;row++){
    const testCase={case:cases.length+1,row:row+1,...profile,status:'failed',attempts:[]};
    for(let attempt=0;attempt<=plan.retries;attempt++){
     const vars={...Object.fromEntries(Object.entries(process.env).filter(([k])=>k.startsWith('TF_VAR_')).map(([k,v])=>[k.slice(7),v])),...values(plan.variables),...values(plan.dataset[row])};
     const context=await browser.newContext({...devices[profile.device],storageState:storage(profile.auth_profile),acceptDownloads:true});
     const record={attempt:attempt+1,status:'passed',steps:[]};let page,state;
     try {
      page=await context.newPage();state={keepOpen:true,speedFactor:profile.execution_speed ?? plan.execution_speed,mobilePlatform:profile.platform || 'android'};
      const folder=path.join(output,`case-${testCase.case}-${attempt+1}`);
      let failed=false;
      for(const stage of stages){
       if(failed && stage!=='teardown')continue;
       for(let i=0;i<plan[stage].length;i++){
        const step=plan[stage][i];
        try{await replay(page,[step],folder,vars,state);record.steps.push({stage,step:i+1,action:step.action,status:'passed'});}
        catch(error){failed=true;record.status='failed';record.steps.push({stage,step:i+1,action:step.action,status:'failed',error_type:error.name});if(stage!=='teardown')break;}
       }
      }
     }finally{
      try{if(state){state.keepOpen=false;await replay(page,[],output,vars,state);}}
      finally{await context.close();}
     }
     testCase.attempts.push(record);
     if(record.status==='passed'){testCase.status=attempt?'flaky':'passed';break;}
    }
    cases.push(testCase);
   }
  }finally{await browser.close();}
 }
 const result={status:cases.every(c=>c.status==='passed')?'passed':'failed',cases,total:cases.length,sequential:true};
 fs.writeFileSync(path.join(output,'plan-result.json'),JSON.stringify(result,null,2));
 if(result.status!=='passed')throw new Error('Execution plan failed or was flaky; inspect plan-result.json');
 return result;
}
module.exports={replayPlan};
