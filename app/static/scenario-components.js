/* Repository scenarios, explicit-save secrets and execution inputs. No extra services. */
const fieldClass = 'w-full bg-slate-950 border border-slate-700 rounded p-2 text-xs';
function InputFields({definitions=[], defaults={}, values={}, onChange}) {
  const names = [...new Set([...definitions.map(v=>v.name), ...Object.keys(defaults), ...Object.keys(values)])];
  return e('div', {className:'space-y-2'}, names.length===0 && e('p',{className:'text-xs text-slate-400'},'No inputs defined. Add project variables or parameters in the Variables tab.'),
    names.map(name=>{const definition=definitions.find(v=>v.name===name)||{}; const secret=definition.is_secret || defaults[name]==='[encrypted]';
      const value=Object.hasOwn(values,name)?values[name]:(Object.hasOwn(defaults,name)?defaults[name]:definition.value||'');
      return e('label',{key:name,className:'block text-xs text-slate-400'},`${name} · ${definition.category==='Parameter'?'parameter':'variable'}${secret?' · encrypted':''}`,
        e('input',{className:fieldClass,'aria-label':name,type:secret?'password':'text',autoComplete:'off',value:secret && (!Object.hasOwn(values,name)||value==='[encrypted]')?'':value,
          placeholder:secret?'Stored encrypted value is retained unless replaced':'Value',onChange:ev=>{const next={...values}; if(secret && !ev.target.value) delete next[name]; else next[name]=ev.target.value; onChange(next);}}));}));
}

// The application provisions its own key when the environment has none, so this
// panel is normally informational. It still has to be candid about what that key
// does and does not protect, because a key stored beside its own ciphertext must
// never be presented as if it were an external secret.
function EncryptionNotice({status,onRecheck,onRotate,busy}) {
  const [copied,setCopied]=useState('');
  const [open,setOpen]=useState(false);
  if(!status) return null;
  const variable=status.variable||'TF_VARIABLE_ENCRYPTION_KEYS';
  const command=status.generate_command||'';
  const copy=async()=>{
    try{await navigator.clipboard.writeText(command);setCopied('Command copied.');}
    catch(_){setCopied('Clipboard blocked by the browser; select the text instead.');}
  };

  if(!status.ready) {
    return e('div',{role:'alert',className:'border border-amber-500/60 bg-amber-950/40 rounded-xl p-4 space-y-2'},
      e('p',{className:'text-xs font-bold text-amber-300'},'⚠ Encryption is unavailable on this server'),
      e('p',{className:'text-xs text-amber-100'},'Plaintext variables still save normally.'),
      status.reason && e('p',{className:'text-[11px] text-amber-200 break-words'},status.reason),
      command && e('div',{className:'flex items-center gap-2'},
        e('code',{className:'flex-1 block bg-slate-950 border border-slate-700 rounded p-2 text-[10px] break-all'},command),
        e('button',{type:'button',className:'key-btn shrink-0',onClick:copy},'COPY')),
      e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRecheck},busy?'CHECKING…':'RE-CHECK ENCRYPTION'),
      copied && e('p',{role:'status',className:'text-[10px] text-amber-300'},copied));
  }

  const managed = status.source === 'app-managed';
  return e('details',{open,onToggle:ev=>setOpen(ev.target.open),
      className:`border rounded-xl p-4 space-y-2 ${managed?'border-emerald-700/60 bg-emerald-950/20':'border-slate-700 bg-slate-900'}`},
    e('summary',{className:'text-xs font-bold cursor-pointer '+(managed?'text-emerald-300':'text-indigo-300')},
      managed
        ? `🔒 Encryption ready · key managed by the application (${status.keys} in the keyring)`
        : `🔒 Encryption ready · key supplied by the environment (${status.keys} in the keyring)`),
    managed && e('p',{className:'text-xs text-slate-300'},
      'Nothing to configure. The application generated the key itself on first use and keeps it in its own database, so encrypted values survive restarts and redeploys.'),
    status.note && e('p',{className:'text-[11px] text-amber-200/90'},status.note),
    !managed && e('p',{className:'text-xs text-slate-300'},
      `${variable} is set, so new values are sealed with its first key. Any key the application created earlier is kept in the ring so existing values still decrypt. Rotate by putting the new key first: ${variable}=new,old`),
    e('p',{className:'text-[11px] text-slate-400'},
      'Values are decrypted only while a step needs them and re-sealed immediately; nothing is ever written back in plaintext.'),
    e('div',{className:'flex flex-wrap gap-2 pt-1'},
      e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRecheck},busy?'CHECKING…':'RE-CHECK'),
      managed && e('button',{type:'button',className:'key-btn',disabled:busy,onClick:onRotate},
        busy?'WORKING…':'ROTATE KEY & RE-SEAL'),
      !managed && command && e('button',{type:'button',className:'key-btn',onClick:copy},'COPY KEY-GENERATION COMMAND')),
    managed && e('p',{className:'text-[10px] text-slate-500'},
      'Rotation mints a new key, decrypts each stored value with the ring that can still read it and re-encrypts it immediately. Superseded keys are kept so queued runs and older snapshots stay readable.'),
    copied && e('p',{role:'status',className:'text-[10px] text-emerald-300'},copied));
}

function SecureVariables({project}) {
  const [rows,setRows]=useState([]), [edited,setEdited]=useState({}), [name,setName]=useState(''), [value,setValue]=useState('');
  const [secret,setSecret]=useState(false), [category,setCategory]=useState('General'), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const [encryption,setEncryption]=useState(null), [checking,setChecking]=useState(false);
  const load=async()=>{if(project) setRows(await API.get(`/api/variables?project_id=${project.id}`));};
  // One cheap read of the readiness the server already computes, so the tab can
  // answer "can I tick Encrypt?" before a password is typed into a doomed field.
  const checkEncryption=async()=>{
    setChecking(true);
    try{setEncryption(await API.get('/api/encryption'));}
    catch(_){/* a failed probe must not block plaintext work */}
    finally{setChecking(false);}
  };
  const rotate=async()=>{
    if(!confirm('Mint a new encryption key and re-seal every stored value with it?\n\nSuperseded keys are kept, so nothing becomes unreadable. This publishes the re-encrypted library.')) return;
    setChecking(true);
    try{
      const result=await API.post('/api/encryption/rotate',{});
      setNotice(`Rotated. Re-sealed ${result.variables} variable(s), ${result.recording_defaults} recording default(s), ${result.scenario_entries} scenario input(s)`
        +(result.unreadable?`; ${result.unreadable} value(s) could not be opened and were left untouched`:'')
        +`. ${result.publish_message||''}`);
      await load();
    }catch(err){setNotice(err.message);}
    finally{setChecking(false);await checkEncryption();}
  };
  useEffect(()=>{checkEncryption();},[]);
  useEffect(()=>{setEdited({}); load().catch(err=>setNotice(err.message));},[project]);
  // Status can land after the name field already auto-ticked the box.
  useEffect(()=>{if(encryption && !encryption.ready) setSecret(false);},[encryption]);
  if(!project) return e('p',{},'SELECT PROJECT FIRST');
  const sealingReady = !encryption || encryption.ready;   // unknown stays permissive
  const sealingBlocked = !!encryption && !encryption.ready;
  const work=async(fn)=>{setBusy(true);try{const result=await fn();setNotice(result?.publish_message||'Saved. Persistent values are retained for subsequent executions.');setEdited({});await load();}catch(err){setNotice(err.message);if(/encryption|keyring|TF_VARIABLE_ENCRYPTION_KEYS/i.test(err.message||'')) checkEncryption();}finally{setBusy(false);}};
  return e('div',{className:'space-y-4'},
    e('h2',{className:'font-bold text-indigo-300'},`${project.name} · Variables & parameters`),
    e('p',{className:'text-xs text-slate-400'},'Use {{name}} in steps. Values are saved only when you press Save. Encrypted values stay masked; the server needs a stable TF_VARIABLE_ENCRYPTION_KEYS key outside Git. Plaintext exists briefly in memory and in the target app during use.'),
    e(EncryptionNotice,{status:encryption,onRecheck:checkEncryption,onRotate:rotate,busy:checking}),
    e('form',{className:'bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2',onSubmit:ev=>{ev.preventDefault();work(async()=>{const result=await API.post('/api/variables',{project_id:project.id,name,value,is_secret:secret&&sealingReady,category});setName('');setValue('');return result;});}},
      e('input',{className:fieldClass,required:true,'aria-label':'Variable name',placeholder:'Name, e.g. password or account',value:name,onChange:ev=>{setName(ev.target.value);setSecret(sealingReady && /password|secret|token/i.test(ev.target.value));}}),
      e('input',{className:fieldClass,'aria-label':'Variable value',type:secret?'password':'text',autoComplete:'off',placeholder:'Saved value',value,onChange:ev=>setValue(ev.target.value)}),
      e('select',{className:fieldClass,'aria-label':'Input category',value:category,onChange:ev=>setCategory(ev.target.value)},e('option',{value:'General'},'Variable'),e('option',{value:'Parameter'},'Parameter')),
      e('label',{className:`text-xs flex gap-2 ${sealingBlocked?'text-slate-500':''}`,title:sealingBlocked?`Unavailable until ${encryption.variable} is configured on this server`:''},
        e('input',{type:'checkbox',checked:secret,disabled:sealingBlocked,onChange:ev=>setSecret(ev.target.checked)}),
        sealingBlocked?'Encrypt at rest — unavailable until the server keyring is configured (see above)':'Encrypt at rest (password / token)'),
      e('button',{className:'key-btn',disabled:busy},'SAVE INPUT')),
    notice && e('p',{role:'status',className:'text-xs text-amber-300'},notice),
    rows.map(row=>{const changes=edited[row.id]||{}; const sealed=row.is_secret||changes.is_secret; return e('div',{key:row.id,className:'bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2'},
      e('strong',{className:'text-xs'},`{{${row.name}}} · ${row.category}${sealed?' · encrypted':''}`),
      e('input',{className:fieldClass,'aria-label':`Saved ${row.name}`,type:sealed?'password':'text',autoComplete:'off',value:Object.hasOwn(changes,'value')?changes.value:(row.is_secret?'':row.value),placeholder:sealed?'Stored value hidden; leave unchanged to retain':'Saved value',onChange:ev=>setEdited({...edited,[row.id]:{...changes,value:ev.target.value}})}),
      !row.is_secret && e('label',{className:`text-xs flex gap-2 ${sealingBlocked?'text-slate-500':''}`,title:sealingBlocked?`Unavailable until ${encryption.variable} is configured on this server`:''},
        e('input',{type:'checkbox',checked:!!changes.is_secret,disabled:sealingBlocked,onChange:ev=>setEdited({...edited,[row.id]:{...changes,is_secret:ev.target.checked}})}),
        sealingBlocked?'Encrypt this saved value — unavailable until the server keyring is configured':'Encrypt this saved value'),
      row.is_secret && sealingBlocked && e('p',{className:'text-[10px] text-amber-400'},'This value was sealed with a keyring this server cannot load. Restore the original key to read or replace it.'),
      e('button',{className:'key-btn',disabled:busy||!Object.keys(changes).length,onClick:()=>work(()=>API.patch(`/api/variables/${row.id}`,changes))},'SAVE CHANGES'),
      e('button',{className:'key-btn',disabled:busy,onClick:()=>{if(confirm(`Delete ${row.name}?`)) work(()=>API.del(`/api/variables/${row.id}`));}},'DELETE'));}));
}

function ExecutionInputs({recording,onClose,onRun}) {
  const [config,setConfig]=useState(null), [changes,setChanges]=useState({}), [note,setNote]=useState(''), [busy,setBusy]=useState(false);
  useEffect(()=>{API.get(`/api/recordings/${recording.id}/inputs`).then(setConfig).catch(err=>setNote(err.message));},[recording.id]);
  const work=async(fn)=>{setBusy(true);try{await fn();}catch(err){setNote(err.message);}finally{setBusy(false);}};
  return e('div',{role:'dialog','aria-label':'Execution inputs',className:'fixed inset-0 z-[110] overflow-y-auto bg-slate-950 p-6 space-y-4'},
    e('h2',{className:'font-bold text-indigo-300'},`Inputs · ${recording.name}`),
    e('p',{className:'text-xs text-slate-400'},'Run once uses these edits without changing persistent project values. Save recording defaults to reuse edits for this recording. These inputs also apply to imported/AI-authored recordings; this does not add an autonomous AI agent.'),
    config && e(InputFields,{definitions:config.project_variables,defaults:config.defaults,values:changes,onChange:setChanges}),
    e('div',{className:'flex flex-wrap gap-2'},
      e('button',{className:'key-btn',disabled:busy||!config,onClick:()=>work(async()=>{await onRun(recording,changes);onClose();})},'RUN ONCE'),
      e('button',{className:'key-btn',disabled:busy||!config,onClick:()=>work(async()=>{const result=await API.put(`/api/recordings/${recording.id}/inputs`,{variables:{...config.defaults,...changes}});setConfig({...config,defaults:result.defaults});setChanges({});setNote(result.publish_message||'Recording defaults saved.');})},'SAVE RECORDING DEFAULTS'),
      e('button',{className:'key-btn',onClick:onClose},'CLOSE')),
    note && e('p',{role:'alert',className:'text-xs text-amber-300'},note));
}

function ReportLinks({kind,id}) {
  const [notice,setNotice]=useState('');
  const [repo,setRepo]=useState(null);
  const url=kind==='batch'?`/api/runs/batch/${id}/report.pdf`:`/api/runs/${id}/report.pdf`;
  // The committed report is the durable copy: it is part of the library push and
  // does not expire with the PDF. Look it up so the row can link straight to it.
  useEffect(()=>{API.get(`/api/reports/${kind}/${id}`).then(setRepo).catch(()=>{});},[kind,id]);
  return e('div',{className:'flex flex-wrap gap-2 text-xs',onClick:ev=>ev.stopPropagation()},
    e('a',{href:url,target:'_blank',rel:'noopener noreferrer',className:'text-indigo-300 underline'},'VIEW PDF'),
    e('a',{href:url+'?download=true',className:'text-indigo-300 underline'},'DOWNLOAD PDF'),
    repo && repo.repository_url && e('a',{href:repo.repository_url,target:'_blank',rel:'noopener noreferrer',
      className:'text-emerald-300 underline',title:repo.repository_path},'REPORT IN REPOSITORY'),
    repo && !repo.repository_url && repo.repository_path && e('span',{className:'text-slate-500',
      title:'Set TF_GITHUB_REPO/TF_GITHUB_BRANCH, or mount a checkout, to link straight to GitHub'},
      `committed to ${repo.repository_path}`),
    e('button',{className:'text-indigo-300 underline',onClick:async()=>{try{const meta=await API.get(`/api/reports/${kind}/${id}`); if(meta.remote_status!=='published'||Date.now()/1000>=meta.expires_at) throw new Error('GitHub release attachment is not available (TF_REPORTS_GITHUB may be off, pending or expired). The report itself is still committed to the repository; download the PDF to share the evidence.');await navigator.clipboard.writeText(meta.url);setNotice('GitHub link copied. Expires 48h after completion; private repositories require access.');}catch(err){setNotice(err.message);}}},'COPY PDF SHARE LINK'),
    notice && e('span',{role:'status',className:'text-amber-300'},notice));
}

function LinkedInputs({entry,projectVariables,onChange}) {
  const [config,setConfig]=useState(null);
  useEffect(()=>{setConfig(null);if(entry.recording_id) API.get(`/api/recordings/${entry.recording_id}/inputs`).then(setConfig).catch(()=>{});},[entry.recording_id]);
  if(!config) return null;
  const definitions=[...config.project_variables.filter(v=>!projectVariables.some(p=>p.name===v.name)),...projectVariables];
  return e(InputFields,{definitions,defaults:config.defaults,values:entry.variables||{},onChange:values=>onChange({...entry,variables:values})});
}

function ScenarioPlans({project}) {
  const [scenarios,setScenarios]=useState([]), [recordings,setRecordings]=useState([]), [variables,setVariables]=useState([]), [draft,setDraft]=useState(null);
  const [execution,setExecution]=useState(null), [overrides,setOverrides]=useState({}), [batch,setBatch]=useState(null), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const endpoint=`/api/projects/${project.id}/scenarios`;
  const load=()=>API.get(endpoint).then(setScenarios);
  useEffect(()=>{setDraft(null);setExecution(null);setBatch(null);load().catch(err=>setNotice(err.message));API.get('/api/library/recordings').then(setRecordings);API.get(`/api/variables?project_id=${project.id}`).then(setVariables);},[project.id]);
  useEffect(()=>{if(!batch||!['queued','running'].includes(batch.status))return;const timer=setTimeout(()=>API.get(`/api/runs/batch/${batch.id}`).then(setBatch).catch(err=>setNotice(err.message)),1500);return()=>clearTimeout(timer);},[batch]);
  const work=async(fn)=>{setBusy(true);try{await fn();}catch(err){setNotice(err.message);}finally{setBusy(false);}};
  const updatePlan=(index,plan)=>setDraft({...draft,plans:draft.plans.map((p,i)=>i===index?plan:p)});
  const save=()=>work(async()=>{const result=draft.id?await API.put(endpoint+'/'+draft.id,draft):await API.post(endpoint,draft);setDraft(null);setNotice(result.publish_message||'Scenario saved.');await load();});
  return e('section',{className:'bg-slate-900 rounded-xl border border-slate-700 p-4 space-y-3'},
    e('h2',{className:'font-bold text-indigo-300'},`${project.name} / High-level scenarios / Execution plans`),
    e('p',{className:'text-xs text-slate-400'},'Link existing recordings in exact order. Login and logout share one browser session. A failed recording stops the plan; following recordings are skipped. No automatic retry or adaptive reordering.'),
    e('button',{className:'key-btn',onClick:()=>setDraft({name:'',plans:[{name:'Execution plan',entries:[]}]})},'NEW HIGH-LEVEL SCENARIO'),
    scenarios.map(scenario=>e('div',{key:scenario.id,className:'bg-slate-950 rounded p-3 space-y-2'},
      e('strong',{className:'text-sm'},scenario.name), e('button',{className:'key-btn',onClick:()=>setDraft(JSON.parse(JSON.stringify(scenario)))},'EDIT SCENARIO'),
      scenario.plans.map(plan=>e('div',{key:plan.id,className:'space-y-2'},e('p',{className:'text-xs text-indigo-300'},plan.name),
        e('ol',{className:'text-xs text-slate-400'},plan.entries.map((entry,index)=>e('li',{key:entry.id||index},`${index+1}. ${entry.name}`))),
        e('button',{className:'key-btn',disabled:busy,onClick:()=>{setOverrides({});setExecution({scenario,plan});}},'REVIEW INPUTS & RUN'))))),
    draft && e('div',{className:'space-y-3 border border-indigo-500 rounded p-3'},
      e('input',{className:fieldClass,'aria-label':'Scenario name',placeholder:'Validate Application login & logout',value:draft.name,onChange:ev=>setDraft({...draft,name:ev.target.value})}),
      draft.plans.map((plan,pi)=>e('div',{key:pi,className:'space-y-2 bg-slate-950 p-3 rounded'},
        e('input',{className:fieldClass,'aria-label':`Plan ${pi+1} name`,value:plan.name,onChange:ev=>updatePlan(pi,{...plan,name:ev.target.value})}),
        plan.entries.map((entry,index)=>e('div',{key:entry.id||index,className:'border border-slate-700 rounded p-3 space-y-2'},
          e('p',{className:'text-xs'},`Recording ${index+1}`),
          e('select',{className:fieldClass,'aria-label':`Recording ${pi+1}.${index+1}`,value:entry.recording_id,onChange:ev=>updatePlan(pi,{...plan,entries:plan.entries.map((v,i)=>i===index?{recording_id:ev.target.value,variables:{}}:v)})},
            e('option',{value:''},'Choose a repository recording'),recordings.map(r=>e('option',{key:r.id,value:r.id},`${r.project_name||r.project_id} / ${r.name}`))),
          e(LinkedInputs,{entry,projectVariables:variables,onChange:changed=>updatePlan(pi,{...plan,entries:plan.entries.map((v,i)=>i===index?changed:v)})}),
          e('div',{className:'flex gap-2'},['UP','DOWN','REMOVE'].map((label,n)=>e('button',{key:label,className:'key-btn',disabled:n===0&&index===0||n===1&&index===plan.entries.length-1,onClick:()=>{const entries=[...plan.entries];if(n===2)entries.splice(index,1);else{const to=index+(n===0?-1:1);[entries[index],entries[to]]=[entries[to],entries[index]];}updatePlan(pi,{...plan,entries});}},label))))),
        e('button',{className:'key-btn',onClick:()=>updatePlan(pi,{...plan,entries:[...plan.entries,{recording_id:'',variables:{}}]})},'LINK RECORDING'),
        draft.plans.length>1 && e('button',{className:'key-btn',onClick:()=>setDraft({...draft,plans:draft.plans.filter((_,i)=>i!==pi)})},'REMOVE PLAN'))),
      e('button',{className:'key-btn',onClick:()=>setDraft({...draft,plans:[...draft.plans,{name:'Execution plan',entries:[]}]})},'ADD PLAN'),
      e('button',{className:'key-btn',disabled:busy,onClick:save},'SAVE SCENARIO'),e('button',{className:'key-btn',onClick:()=>setDraft(null)},'CANCEL EDIT')),
    execution && e('div',{role:'dialog','aria-label':'Plan execution inputs',className:'space-y-3 border border-indigo-500 rounded p-3'},
      e('h3',{className:'text-sm font-bold'},`${execution.scenario.name} / ${execution.plan.name}`),
      e('p',{className:'text-xs text-slate-400'},'Only edits are sent as one-off overrides. Project and plan defaults remain unchanged.'),
      execution.plan.entries.map((entry,index)=>e('div',{key:entry.id||index,className:'space-y-2'},e('p',{className:'text-xs'},`${index+1}. ${entry.name}`),
        e(LinkedInputs,{entry:{...entry,variables:{...entry.variables,...(overrides[index]||{})}},projectVariables:variables,onChange:changed=>{const values={};for(const [name,value] of Object.entries(changed.variables))if(value!=='[encrypted]' && value!==entry.variables[name])values[name]=value;setOverrides({...overrides,[index]:values});}}))),
      e('button',{className:'key-btn',disabled:busy,onClick:()=>work(async()=>{const result=await API.post(`${endpoint}/${execution.scenario.id}/plans/${execution.plan.id}/run`,{entry_overrides:overrides});setBatch({id:result.batch_id,status:result.status});setExecution(null);setNotice('Ordered plan queued.');})},'EXECUTE ORDERED PLAN'),
      e('button',{className:'key-btn',onClick:()=>setExecution(null)},'CLOSE INPUTS')),
    batch && e('div',{className:'space-y-2'},e('p',{role:'status',className:'text-xs'},`Plan execution: ${batch.status} · ${batch.progress_pct||0}% · ${batch.passed||0} passed / ${batch.total||0}`),
      ['queued','running'].includes(batch.status)?e('button',{className:'key-btn',onClick:()=>work(()=>API.post(`/api/runs/batch/${batch.id}/cancel`,{}))},'CANCEL EXECUTION'):e(ReportLinks,{kind:'batch',id:batch.id})),
    notice && e('p',{role:'status',className:'text-xs text-amber-300'},notice));
}
