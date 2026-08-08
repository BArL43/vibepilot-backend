const $=(s)=>document.querySelector(s), $$=(s)=>[...document.querySelectorAll(s)];
const defaultApi=location.pathname.startsWith('/app')?location.origin:'https://vibepilot-backend.onrender.com';
const state={
  api:sessionStorage.getItem('vp_api')||defaultApi,
  key:sessionStorage.getItem('vp_key')||'', priority:'balanced', contract:null, workflow:null, receipt:null,
  pollTimer:null, pollBusy:false
};
const money=v=>`${Number(v??0).toLocaleString('ru-RU',{maximumFractionDigits:2})} ₽`;
const terminal=new Set(['complete','cancelled','blocked','error']);
const doneStates=new Set(['complete','simulated','skipped']);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function toast(msg,error=false){const el=$('#toast');el.textContent=msg;el.className=`toast show${error?' error':''}`;clearTimeout(el.t);el.t=setTimeout(()=>el.className='toast',3600)}
function headers(json=false){const h={};if(json)h['Content-Type']='application/json';if(state.key)h['X-VibePilot-Live-Key']=state.key;return h}
async function api(path,opt={}){
  const r=await fetch(`${state.api.replace(/\/$/,'')}${path}`,{...opt,headers:{...headers(!!opt.body),...(opt.headers||{})}});
  let data={};try{data=await r.json()}catch{}
  if(!r.ok){const d=data.detail||data;throw new Error(typeof d==='string'?d:(d.message||d.code||`HTTP ${r.status}`))}
  return data;
}
function switchView(name){
  $$('.view').forEach(v=>v.classList.toggle('active',v.id===`view-${name}`));
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
  const titles={planner:'Кампания от идеи до видео',workspace:'Производство кампании',receipt:'Проверяемая квитанция',settings:'Подключение к API'};
  $('#pageTitle').textContent=titles[name];window.scrollTo({top:0,behavior:'smooth'});
}
$$('.nav-item').forEach(b=>b.onclick=()=>switchView(b.dataset.view));$('#openSettings').onclick=()=>switchView('settings');

function syncForm(){
  const b=Number($('#budget').value||0),r=Number($('#reserve').value||0),video=$('#video').checked;
  $('#previewBudget').textContent=b.toLocaleString('ru-RU');$('#previewReserve').textContent=money(r);$('#usableBudget').textContent=money(Math.max(0,b-r));
  $('#reserveMeter').style.width=`${Math.max(0,Math.min(100,(b-r)/Math.max(b,1)*100))}%`;
  $('#globalMode').textContent=$('#liveMode').checked?'LIVE':'DEMO';$('#globalMode').classList.toggle('live',$('#liveMode').checked);
  $('#videoChoice').classList.toggle('active',video);$('#videoChoice').classList.toggle('disabled',!video);$('#previewVideoStep').classList.toggle('hidden',!video);
  $('#videoSection').classList.toggle('hidden',!video && !state.workflow?.include_video);
  $('#formHint').textContent=$('#liveMode').checked?'Live: после сметы начнутся реальные генерации и списания в пределах лимита.':'Demo: безопасная симуляция без генераций и списаний.';
}
['budget','reserve'].forEach(id=>$(`#${id}`).oninput=syncForm);$('#video').onchange=syncForm;$('#liveMode').onchange=syncForm;
$('#brief').oninput=e=>$('#briefCount').textContent=e.target.value.length;
$$('#priority button').forEach(b=>b.onclick=()=>{$$('#priority button').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');state.priority=b.dataset.value});
$('#fillExample').onclick=()=>{
  $('#brief').value='Запуск нового холодного кофе возле МГУ. Целевая аудитория — студенты 18–25 лет. Нужна энергичная рекламная кампания для Telegram и VK: три разные идеи, затем один квадратный баннер и короткое видео. Не использовать неподтверждённые обещания.';
  $('#budget').value='60';$('#brief').dispatchEvent(new Event('input'));syncForm();
};
function payload(budgets){return{brief:$('#brief').value.trim(),budgets_rub:budgets,execution_mode:$('#liveMode').checked?'live':'demo',approval_required_above_rub:Number($('#approval').value),reserve_rub:Number($('#reserve').value),price_drift_tolerance_rub:Number($('#drift').value),include_video:$('#video').checked}}

async function compare(){
  if($('#brief').value.trim().length<20)return toast('Добавьте задачу длиной от 20 символов',true);
  $('#compareBtn').disabled=true;
  try{const base=Number($('#budget').value),vals=[Math.max(10,Math.round(base*.45)),Math.max(10,Math.round(base*.7)),base,Math.round(base*1.4)];const data=await api('/api/v1/budget/compare',{method:'POST',body:JSON.stringify(payload(vals))});renderScenarios(data.scenarios);$('#comparisonPanel').classList.remove('hidden');toast('Сценарии рассчитаны')}
  catch(e){toast(e.message,true)}finally{$('#compareBtn').disabled=false}
}
$('#compareBtn').onclick=compare;$('#closeCompare').onclick=()=>$('#comparisonPanel').classList.add('hidden');
function renderScenarios(items){$('#scenarioGrid').innerHTML=items.map(s=>`<article class="scenario ${s.feasible?'feasible':''}"><small>${s.feasible?'ПОМЕЩАЕТСЯ':'НЕ ХВАТАЕТ БЮДЖЕТА'}</small><h4>${money(s.budget_rub)}</h4><span class="tag">${esc(s.priority)} · ${s.include_video?'баннер + видео':'баннер'}</span><ul>${s.steps.map(x=>`<li>${esc(x.media_type)}: ${esc(x.model)} — ${money(x.estimated_cost_rub)}</li>`).join('')}</ul><small>${esc(s.reason)}</small></article>`).join('')}

$('#campaignForm').onsubmit=async e=>{
  e.preventDefault();const btn=e.submitter;
  if($('#brief').value.trim().length<20)return toast('Опишите задачу чуть подробнее',true);
  if($('#liveMode').checked&&!state.key){toast('Для Live укажите Live control key в разделе «Подключение»',true);switchView('settings');return}
  btn.disabled=true;btn.querySelector('span').textContent='Собираем смету…';
  stopPolling();
  try{
    state.contract=await api('/api/v1/contracts/compile',{method:'POST',body:JSON.stringify(payload([Number($('#budget').value)]))});
    btn.querySelector('span').textContent='Активируем…';
    state.workflow=await api(`/api/v1/contracts/${state.contract.id}/activate`,{method:'POST'});
    renderWorkflow();switchView('workspace');toast(`Кампания ${state.workflow.id} создана`);
    await executeCurrent(true);
  }catch(err){toast(err.message,true)}finally{btn.disabled=false;btn.querySelector('span').textContent='Создать кампанию'}
};

function getStep(id){return state.workflow?.steps?.find(s=>s.id===id)}
function stepClass(s){if(!s)return'';if(s.status==='error'||s.status==='blocked')return'error';if(s.status==='awaiting_approval')return'wait';if(doneStates.has(s.status))return'done';if(s.status==='running'||s.status==='estimating')return'active';return''}
function stateLabel(s){if(!s)return'ожидает';const map={planned:'ожидает',estimating:'смета',running:'в работе',complete:'готово',simulated:'симуляция',awaiting_approval:'подтверждение',skipped:'пропущено',blocked:'заблокировано',error:'ошибка'};return map[s.status]||s.status}
function setStepState(id,step){const el=$(`#${id}State`);if(!el)return;el.textContent=stateLabel(step);el.className=`step-state ${stepClass(step)}`}
function jsonMaybe(text){if(!text||typeof text!=='string')return null;let x=text.trim().replace(/^```json\s*/i,'').replace(/^```\s*/,'').replace(/```$/,'').trim();try{return JSON.parse(x)}catch{return null}}
function conceptData(step){
  const structured=step?.structured_result||jsonMaybe(step?.text_result);
  if(Array.isArray(structured?.concepts))return structured.concepts.slice(0,3);
  const raw=step?.text_result||'';if(!raw)return[];
  const chunks=raw.split(/\n(?=(?:#{0,3}\s*)?(?:Концепция\s*)?[1-3][\).:\-]|\n---)/i).filter(x=>x.trim()).slice(0,3);
  return chunks.map((x,i)=>{const lines=x.replace(/^#+\s*/,'').trim().split('\n').filter(Boolean);return{id:`concept-${i+1}`,name:(lines[0]||`Концепция ${i+1}`).replace(/^(?:Концепция\s*)?\d+[\).:\-]?\s*/i,''),idea:lines.slice(1).join(' ')||x}})
}
function selectionData(step){return step?.structured_result||jsonMaybe(step?.text_result)||{}}
function renderConcepts(){
  const step=getStep('concepts'), selection=selectionData(getStep('selection'));setStepState('concepts',step);
  const cards=conceptData(step);const holder=$('#conceptCards');
  if(!step||(!step.text_result&&!doneStates.has(step.status))){holder.className='concept-grid placeholders loading';holder.innerHTML='<div></div><div></div><div></div>';return}
  if(!cards.length){holder.className='concept-grid';holder.innerHTML=`<div class="winner-empty">${step?.status==='simulated'?'В demo-режиме концепции не генерируются. Включите Live для реального результата.':'Модель завершила шаг без структурированных карточек.'}</div>`;return}
  const winner=String(selection.winner||'').toLowerCase();holder.className='concept-grid';holder.innerHTML=cards.map((c,i)=>{const id=String(c.id||`concept-${i+1}`);const isWinner=winner&&(winner===id.toLowerCase()||winner.includes(String(i+1))||winner===String(c.name||'').toLowerCase());return `<article class="concept-card ${isWinner?'winner':''}"><span class="concept-no">0${i+1}</span><h4>${esc(c.name||c.title||`Концепция ${i+1}`)}</h4><p>${esc(c.idea||c.description||c.hook||'')}</p><footer>${c.hook?`<span>${esc(c.hook)}</span>`:''}${c.audience?`<span>${esc(c.audience)}</span>`:''}${c.visual_direction?`<span>${esc(c.visual_direction)}</span>`:''}</footer></article>`}).join('')
}
function renderSelection(){
  const step=getStep('selection');setStepState('selection',step);const d=selectionData(step),box=$('#winnerCard');
  if(!step?.text_result&&!step?.structured_result){box.className='winner-empty';box.textContent=step?.status==='simulated'?'Demo показывает план без AI-ответов.':'Победитель появится после оценки концепций.';return}
  const winner=d.winner_name||d.winner||'Выбранная концепция';const reason=d.winner_reason||d.reason||(Array.isArray(d.risks)&&!d.risks.length?'Лучший баланс ясности, релевантности и безопасности обещаний.':'Независимая модель завершила оценку.');
  const rubric=Array.isArray(d.rubric)?d.rubric:[];let selected=rubric.find(x=>String(x.concept||'').toLowerCase()===String(d.winner||'').toLowerCase())||rubric[0];
  const metrics=selected?[['Ясность',selected.clarity],['ЦА',selected.audience_fit],['Отличимость',selected.distinctiveness],['Безопасность',selected.claims_safety]]:[];
  box.className='winner-card';box.innerHTML=`<div class="winner-main"><span class="pick-label">AI CREATIVE DIRECTOR PICK</span><h4>${esc(winner)}</h4><p>${esc(reason)}</p>${d.banner_prompt?`<small>Визуальное направление: ${esc(d.banner_prompt)}</small>`:''}</div><div class="rubric-list">${metrics.map(([n,v])=>`<div class="rubric-line"><span>${n}</span><b>${Number(v||0)}/10</b><i style="--score:${Math.max(0,Math.min(100,Number(v||0)*10))}%"></i></div>`).join('')}</div>`;
}
function mediaUrl(step){return step?.result_url||step?.result_urls?.[0]||null}
function renderBanner(){
  const step=getStep('banner');setStepState('banner',step);const box=$('#bannerMedia'),url=mediaUrl(step);
  if(url){box.className='media-stage with-meta';box.innerHTML=`<img src="${esc(url)}" alt="Сгенерированный рекламный баннер" loading="lazy"><div class="media-meta"><span>${esc(step.model)}</span><span>${money(step.actual_cost_rub||step.estimated_cost_rub)}</span><a href="${esc(url)}" target="_blank" rel="noopener">Открыть оригинал ↗</a></div>`;return}
  box.className='media-stage';const msg=step?.status==='simulated'?'Demo не запускает реальную генерацию изображения':step?.status==='running'?'Vibe Marketolog генерирует баннер…':step?.error_message||'Изображение появится здесь';box.innerHTML=`<div class="media-placeholder"><b>${step?.status==='running'?'◌':'▣'}</b><span>${esc(msg)}</span></div>`;
}
function renderQA(){
  const qa=getStep('qa')||getStep('quality_check'),banner=getStep('banner'),preflight=getStep('preflight');setStepState('qa',qa||preflight);const box=$('#qaCard');
  let d=qa?.structured_result||jsonMaybe(qa?.text_result)||preflight?.structured_result||jsonMaybe(preflight?.text_result);
  if(d){const checks=Array.isArray(d.checks)?d.checks:[];const passed=d.passed!==false&&d.decision!=='reject';const score=d.score??(checks.length?Math.round(checks.filter(x=>x.passed!==false).length/checks.length*100):100);box.className=`qa-card ${passed?'':'fail'}`;box.innerHTML=`<div class="qa-score">${esc(score)}${Number.isFinite(Number(score))?'%':''}</div><div><h4>${passed?'Креатив допущен к видео':'Нужно исправление'}</h4><p>${esc(d.summary||d.reason||'QA завершён.')}</p><div class="qa-checks">${checks.slice(0,6).map(x=>`<span>${x.passed===false?'×':'✓'} ${esc(x.name||x.check||x.label||'проверка')}</span>`).join('')}</div></div><b>${passed?'PASS':'REVIEW'}</b>`;return}
  if(!qa&&preflight&&doneStates.has(preflight.status)&&banner?.result_url){box.className='qa-card';box.innerHTML='<div class="qa-score">✓</div><div><h4>Технический QA пройден</h4><p>Баннер получен, результат доступен, а цена следующего шага повторно проверена перед видео. В backend 0.4 этот блок дополняется AI QA.</p><div class="qa-checks"><span>✓ media ready</span><span>✓ budget guard</span><span>✓ fresh estimate</span></div></div><b>PASS</b>';return}
  box.className='qa-empty';box.innerHTML=`<span>◎</span><div><b>${qa?.status==='running'?'QA выполняется…':'Проверка ещё не запущена'}</b><p>${esc(qa?.error_message||'После баннера здесь появятся проверки и решение: продолжать, исправить или остановиться.')}</p></div>`;
}
function renderVideo(){
  const step=getStep('video');setStepState('video',step);const box=$('#videoMedia'),url=mediaUrl(step);$('#videoSection').classList.toggle('hidden',state.workflow?.include_video===false);
  if(state.workflow?.include_video===false)return;
  if(url){box.className='media-stage video-stage with-meta';box.innerHTML=`<video src="${esc(url)}" controls playsinline preload="metadata"></video><div class="media-meta"><span>${esc(step.model)}</span><span>${money(step.actual_cost_rub||step.estimated_cost_rub)}</span><a href="${esc(url)}" target="_blank" rel="noopener">Открыть оригинал ↗</a></div>`;return}
  box.className='media-stage video-stage';const msg=step?.status==='running'?'Видео создаётся. Страница обновится автоматически…':step?.status==='skipped'?'Видео пропущено, чтобы не выйти за бюджет':step?.error_message||'Видео появится здесь';box.innerHTML=`<div class="media-placeholder"><b>▶</b><span>${esc(msg)}</span></div>`;
}
function renderStageRail(){
  const desired=[['concepts','Концепции'],['selection','Отбор'],['banner','Баннер'],[getStep('qa')?'qa':'preflight','QA'],['video','Видео']];
  $('#stageRail').innerHTML=desired.filter(([id])=>id!=='video'||state.workflow?.include_video!==false).map(([id,label])=>{const s=getStep(id);return `<div class="stage-chip ${stepClass(s)}"><b>${label}</b><small>${stateLabel(s)}</small></div>`}).join('');
}
function renderTech(){const w=state.workflow;$('#stepsTimeline').classList.remove('empty');$('#stepsTimeline').innerHTML=w.steps.map((s,i)=>`<div class="timeline-item ${esc(s.status)}"><div class="timeline-icon">${doneStates.has(s.status)?'✓':s.status==='awaiting_approval'?'!':i+1}</div><div><b>${esc(s.title)}</b><small>${esc(s.model)} · ${esc(stateLabel(s))}${s.applied_fallback?` · fallback: ${esc(s.applied_fallback)}`:''}</small>${s.error_message?`<small class="error-text">${esc(s.error_message)}</small>`:''}</div><div class="cost"><b>${money(s.actual_cost_rub||s.estimated_cost_rub)}</b><small>${esc(s.pricing_source)}</small></div></div>`).join('');const ev=w.audit?.at(-1);$('#auditMini').innerHTML=`<b>Последнее событие</b><p>${ev?`${esc(ev.event)}<br>${new Date(ev.at).toLocaleString('ru-RU')}`:'Нет событий'}</p>`}
function renderWorkflow(){
  const w=state.workflow;if(!w)return;
  $('#workflowStatus').textContent=w.status.toUpperCase();$('#workflowStatus').className=`pill ${w.status}`;$('#workflowTitle').textContent=w.brief;$('#workflowId').textContent=w.id;
  $('#statBudget').textContent=money(w.budget_rub);$('#statSpent').textContent=money(w.actual_spend_rub);$('#statRemaining').textContent=money(w.remaining_budget_rub);$('#statRecon').textContent=w.reconciliation_status;
  $('#executeBtn').disabled=terminal.has(w.status)||w.status==='running'||w.status==='awaiting_approval';$('#executeBtn').textContent=w.status==='planned'?'Запустить':'Продолжить';$('#refreshBtn').disabled=false;
  const waiting=w.status==='awaiting_approval',waitStep=w.steps.find(x=>x.status==='awaiting_approval');$('#approvalBanner').classList.toggle('hidden',!waiting);if(waitStep)$('#approvalText').textContent=`«${waitStep.title}» оценён в ${money(waitStep.estimated_cost_rub)}. Порог ручного подтверждения — ${money(w.approval_required_above_rub)}.`;
  renderStageRail();renderConcepts();renderSelection();renderBanner();renderQA();renderVideo();renderTech();
  const complete=w.status==='complete';$('#campaignSummary').classList.toggle('hidden',!complete);if(complete)$('#summaryLine').textContent=`Потрачено ${money(w.actual_spend_rub-w.refunded_rub)} из ${money(w.budget_rub)} · осталось ${money(w.remaining_budget_rub)} · сверка: ${w.reconciliation_status}.`;
  updatePolling();
}

async function executeCurrent(silent=false){
  if(!state.workflow)return;
  try{state.workflow=await api(`/api/v1/workflows/${state.workflow.id}/execute`,{method:'POST'});renderWorkflow();if(!silent)toast('Workflow продолжен');if(state.workflow.status==='complete')await maybeFetchReceipt()}
  catch(e){toast(e.message,true);await safeReloadWorkflow()}
}
async function refreshCurrent(silent=false){
  if(!state.workflow||state.pollBusy)return;state.pollBusy=true;
  try{const endpoint=state.workflow.mode==='live'?'refresh':null;state.workflow=endpoint?await api(`/api/v1/workflows/${state.workflow.id}/${endpoint}`,{method:'POST'}):await api(`/api/v1/workflows/${state.workflow.id}`);renderWorkflow();if(!silent)toast('Статус обновлён');if(state.workflow.status==='complete')await maybeFetchReceipt()}
  catch(e){if(!silent)toast(e.message,true);$('#autoStatus').textContent=`Автообновление: ${e.message}`}
  finally{state.pollBusy=false}
}
async function safeReloadWorkflow(){try{if(state.workflow?.id){state.workflow=await api(`/api/v1/workflows/${state.workflow.id}`);renderWorkflow()}}catch{}}
async function approve(approved){try{state.workflow=await api(`/api/v1/workflows/${state.workflow.id}/approve`,{method:'POST',body:JSON.stringify({approved,note:approved?'Approved in VibePilot UI':'Skipped in VibePilot UI'})});renderWorkflow();toast(approved?'Подтверждено. Производство продолжается.':'Шаг отклонён.')}catch(e){toast(e.message,true)}}
$('#executeBtn').onclick=()=>executeCurrent();$('#refreshBtn').onclick=()=>refreshCurrent();$('#approveBtn').onclick=()=>approve(true);$('#rejectBtn').onclick=()=>approve(false);

function stopPolling(){if(state.pollTimer){clearInterval(state.pollTimer);state.pollTimer=null}}
function updatePolling(){
  stopPolling();const enabled=$('#autoRefresh').checked,w=state.workflow;
  if(!enabled||!w){$('#autoStatus').textContent='Автообновление выключено';return}
  if(w.mode!=='live'){$('#autoStatus').textContent='Demo не требует polling';return}
  if(terminal.has(w.status)||w.status==='awaiting_approval'){$('#autoStatus').textContent=w.status==='awaiting_approval'?'Ожидаем вашего решения':'Производство завершено';return}
  $('#autoStatus').textContent='Следим за генерациями · каждые 4 сек';state.pollTimer=setInterval(()=>refreshCurrent(true),4000);
}
$('#autoRefresh').onchange=updatePolling;

async function reconcile(){try{state.workflow=await api(`/api/v1/workflows/${state.workflow.id}/reconcile`,{method:'POST'});renderWorkflow();toast(`Сверка: ${state.workflow.reconciliation_status}`)}catch(e){toast(e.message,true)}}
$('#reconcileBtn').onclick=reconcile;
async function maybeFetchReceipt(){if(!state.workflow)return;try{state.receipt=await api(`/api/v1/workflows/${state.workflow.id}/receipt`);renderReceipt()}catch{}}
$('#receiptBtn').onclick=async()=>{try{if(!state.receipt)await maybeFetchReceipt();if(!state.receipt)throw new Error('Квитанция пока недоступна');renderReceipt();switchView('receipt')}catch(e){toast(e.message,true)}};
function renderReceipt(){const r=state.receipt;if(!r)return;$('#receiptJson').textContent=JSON.stringify(r,null,2);$('#receiptWorkflow').textContent=`workflow_id: ${r.workflow_id}`;$('#receiptMode').textContent=r.mode;$('#receiptStatus').textContent=r.status;$('#receiptSpend').textContent=money(r.budget?.net_spend_rub);$('#receiptDigest').textContent=r.integrity?.digest||'—';$('#receiptVerifyBadge').textContent=String(r.verification||'').toUpperCase();$('#verifyBtn').disabled=false}
$('#verifyBtn').onclick=async()=>{try{const v=await api('/api/v1/receipts/verify',{method:'POST',body:JSON.stringify(state.receipt)});$('#receiptVerifyBadge').textContent=v.valid?'ПОДПИСЬ ВЕРНА':'ОШИБКА ПОДПИСИ';$('#receiptVerifyBadge').style.background=v.valid?'#e5f8f1':'#fde9ed';$('#receiptVerifyBadge').style.color=v.valid?'#138962':'#d43d59';toast(v.message,!v.valid)}catch(e){toast(e.message,true)}};
$('#copyReceipt').onclick=()=>navigator.clipboard?.writeText($('#receiptJson').textContent).then(()=>toast('JSON скопирован'));

$('#loadWorkflow').onclick=()=>$('#idModal').classList.add('show');$('.modal-close').onclick=()=>$('#idModal').classList.remove('show');
$('#confirmLoad').onclick=async()=>{try{state.workflow=await api(`/api/v1/workflows/${$('#workflowIdInput').value.trim()}`);renderWorkflow();$('#idModal').classList.remove('show');switchView('workspace');toast('Workflow загружен')}catch(e){toast(e.message,true)}};

$('#apiBase').value=state.api;$('#liveKey').value=state.key;
$('#saveSettings').onclick=()=>{state.api=$('#apiBase').value.trim().replace(/\/$/,'');state.key=$('#liveKey').value.trim();sessionStorage.setItem('vp_api',state.api);sessionStorage.setItem('vp_key',state.key);toast('Настройки сохранены');checkHealth()};$('#testConnection').onclick=checkHealth;
async function checkHealth(){try{const h=await api('/health');$('#apiDot').className='ok';$('#apiLabel').textContent='API подключён';$('#apiMeta').textContent=`v${h.version} · ${h.state_store}`;$('#serverInfo').innerHTML=Object.entries(h).map(([k,v])=>`<p><b>${esc(k)}</b><br>${esc(String(v))}</p>`).join('');return h}catch(e){$('#apiDot').className='bad';$('#apiLabel').textContent='API недоступен';$('#apiMeta').textContent=e.message;$('#serverInfo').innerHTML=`<p>${esc(e.message)}</p>`}}

function showOnboarding(force=false){if(force||!localStorage.getItem('vp_onboarded'))$('#onboardingModal').classList.add('show')}
function closeOnboarding(){localStorage.setItem('vp_onboarded','1');$('#onboardingModal').classList.remove('show')}
$('#startOnboarding').onclick=closeOnboarding;$('#closeOnboarding').onclick=closeOnboarding;$('#showHow').onclick=()=>showOnboarding(true);

const previewMode=new URLSearchParams(location.search).has('preview');
syncForm();
if(previewMode){$('#apiLabel').textContent='Preview mode';$('#apiMeta').textContent='без сетевых вызовов';}else{checkHealth();}
showOnboarding();
