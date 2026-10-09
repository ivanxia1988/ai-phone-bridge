'use strict';
const $ = id => document.getElementById(id);
const labels = {preparing:'准备模型',dialing:'等待接听',talking:'正在通话',summarizing:'生成总结',completed:'已完成',failed:'未完成',cancelled_before_dial:'已取消',cancelled_before_answer:'接听前取消'};
let lastCurrentId = null;
let snapshot = null, selectedId = null, detailState = '', records = [], working = false, initialized = false, requestSerial = 0;
let liveId=null, liveRows=[], liveState='', liveSignature='', followLive=true, finishedLiveId=null, refreshing=false;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作未完成');
  return value;
}
function notice(text, error=false) { $('notice').textContent=text; $('notice').className=text?'notice'+(error?' error':''):'notice hidden'; }
function settings() {
  const custom = $('voice').value === 'custom';
  return {provider:$('provider').value,api_key:$('apiKey').value,voice_id:custom?$('voiceId').value.trim():$('voice').value,voice_name:custom?$('voiceName').value.trim():$('voice').selectedOptions[0].text.split(' · ')[0],port:$('port').value};
}
function check(id, title, good, note) {
  const root=$(id); root.replaceChildren();
  const dot=document.createElement('span'); dot.className='dot '+(good?'green':'amber');
  const name=document.createElement('span'); name.textContent=title;
  const info=document.createElement('span'); info.className='check-note'; info.textContent=note;
  root.append(dot,name,info);
}
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.getTime())?value || '':date.toLocaleString('zh-CN',{hour12:false}); }
function seconds(value) { const n=Math.max(0,Math.round(value||0)); return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0'); }
function applyStatus(s) {
  snapshot=s;
  if (!initialized) {
    $('provider').value=s.provider;
    const option=Array.from($('voice').options).find(o=>o.value===s.voice_id);
    $('voice').value=option?s.voice_id:'custom'; $('voiceId').value=s.voice_id; $('voiceName').value=s.voice_name;
    $('customVoice').classList.toggle('hidden',!!option); initialized=true;
  }
  check('usbCheck','USB 设备',s.usb_connected,s.usb_connected?'已发现 ESP32':'请插上 USB-C');
  check('firmwareCheck','通话固件',s.firmware_ready,s.firmware_ready?'响应正常':'等待连接检测');
  check('phoneCheck','iPhone 蓝牙',s.phone_connected,s.phone_connected?'已连接': '请在手机设置中连接');
  const doubao=s.provider==='doubao';
  check('apiCheck',s.provider_name,s.api_ready,s.api_ready?(doubao?s.voice_name+' 已验证':'会话已验证'):s.api_error||'等待会话验证');
  $('keyStatus').textContent=s.credential_saved?'已保存 · 可留空':s.credentials_available?'临时配置 · 尚未保存':'尚未保存';
  $('apiKeyLabel').textContent=doubao?'豆包语音 API Key':'OpenAI API Key';
  $('apiKey').placeholder=s.credential_saved?'已保存，留空继续使用；输入新 Key 可替换':doubao?'豆包语音控制台的 Key，无需 APP ID':'OpenAI API Key';
  $('keyHint').textContent=s.credential_error||(!s.credential_saved&&s.credentials_available?'旧版本 Key 尚未持久保存，请在这里保存一次。之后关闭页面或重启也可复用。':'Key 保存在 macOS 钥匙串。各渠道独立保存，关闭页面或重启后仍可使用。');
  $('voiceControls').classList.toggle('hidden',!doubao);
  $('openaiVoiceInfo').classList.toggle('hidden',doubao);
  for(const p of s.providers||[]) {
    const option=Array.from($('provider').options).find(o=>o.value===p.id);
    if(option) option.textContent=p.name+(p.saved?' · Key 已保存':' · 待保存 Key');
  }
  $('portWrap').classList.toggle('hidden',s.ports.length<2);
  const oldPort=$('port').value;
  if (Array.from($('port').options).map(o=>o.value).join()!==s.ports.join()) {
    $('port').replaceChildren(...s.ports.map(p=>{const o=document.createElement('option');o.value=p;o.textContent=p.includes('SLAB')?'ESP32 · USB-C':p;o.selected=p===oldPort;return o;}));
  }
  const selectedVoice=settings().voice_id;
  $('start').disabled=!s.ready||working||$('provider').value!==s.provider||(doubao&&selectedVoice!==s.voice_id);
  $('connect').disabled=working||s.busy||s.checking;
  $('voice').disabled=!!s.current_call; $('apiKey').disabled=working||s.busy||s.checking;
  $('provider').disabled=working||s.busy||s.checking; $('saveKey').disabled=working||s.busy||s.checking;
  $('hangup').disabled=!s.current_call||s.current_call.state==='summarizing';
  $('topStatus').textContent=s.current_call?labels[s.current_call.state]||'处理中':s.ready?'● 准备就绪':s.usb_connected?'等待完成检测':'等待连接设备';
  $('startHint').textContent=s.ready?'检测通过。确认号码和目的后，点击开始。':'完成左侧检测后即可开始。失败不会自动重拨。';
  $('connectHint').textContent=!s.usb_connected?'未发现板子，请通过 USB-C 接电脑。填写 Key 后仍可验证音色。':!s.phone_connected?'检测可能使板子重启。请在 iPhone「设置 → 蓝牙」连接 ESP32 Phone Bridge，再检测一次。':'手机已连接。切换渠道或音色后，请重新检测会话。';
  const current=s.current_call;
  if(current) renderLive(current);
  const state=current?.state;
  $('progressSteps').replaceChildren(...['preparing','dialing','talking','summarizing','completed'].map((x,i)=>{const div=document.createElement('div');div.className='progress-step'+(state===x?' active':'');div.textContent=['准备','拨号','通话','总结','完成'][i];return div;}));
  $('callStatus').textContent=current?(labels[state]||state)+' · '+current.number:'尚未发起通话 / 上次通话已结束';
  const elapsed=current?.elapsed_seconds||current?.duration_seconds||0;
  $('callTimer').textContent=current?seconds(elapsed):'—';
  $('audioEvidence').textContent=current?'收到对方音频 '+Math.round((current.audio_bytes?.remote||0)/1024)+' KB · 发出 AI 音频 '+Math.round((current.audio_bytes?.agent||0)/1024)+' KB':'通话结束后可在下方播放录音和查看结果。';
  if(current && lastCurrentId!==current.id) {lastCurrentId=current.id;selectCall(current.id);}
}
function renderHistory() {
  const root=$('history'); root.replaceChildren();
  if(!records.length) {root.textContent='还没有通话记录。完成第一次通话后会显示在这里。';return;}
  for(const record of records) {
    const button=document.createElement('button'); button.className='history-item'+(record.id===selectedId?' selected':'');
    const top=document.createElement('div');top.className='history-top';
    const number=document.createElement('span');number.textContent=record.number;
    const state=document.createElement('span');state.className='state-pill'+(record.state==='failed'?' failed':'');state.textContent=labels[record.state]||record.state;
    top.append(number,state);const date=document.createElement('div');date.className='history-date';date.textContent=formatDate(record.created_at);button.append(top,date);button.onclick=()=>selectCall(record.id);root.append(button);
  }
}
function element(tag,text,className) {const e=document.createElement(tag);if(text)e.textContent=text;if(className)e.className=className;return e;}
function renderLive(call) {
  if(liveId!==call.id) {liveId=call.id;liveSignature='';followLive=true;finishedLiveId=null;}
  liveRows=call.live_transcripts||[];liveState=call.state;
  const signature=JSON.stringify([call.id,call.state,liveRows]);
  if(signature===liveSignature)return;
  liveSignature=signature;
  const closed=['completed','failed','cancelled_before_dial','cancelled_before_answer'].includes(call.state);
  $('liveStatus').textContent=closed?'通话已结束 · 可在下方查看完整转写':call.state==='talking'?'双方文字持续更新 · 约每秒刷新':call.state==='summarizing'?'通话已结束 · 正在整理结果':'等待接听 · 接通后显示文字';
  const root=$('liveTranscript'),top=root.scrollTop;
  root.replaceChildren();
  if(!liveRows.length)root.append(element('p',closed?'本次没有收到转写文字。':'等待双方说话…','empty'));
  for(const row of liveRows) {
    const agent=row.role==='agent_generated';
    const bubble=element('div',null,'live-message '+(agent?'agent':'remote')+(!row.final?' pending':'')+(row.interrupted?' interrupted':''));
    const meta=element('div',null,'live-meta');meta.append(element('span',agent?'AI · 生成文字':'对方'),element('span',seconds(row.seconds)));bubble.append(meta,element('p',row.text||'…'));
    const state=row.interrupted?'已打断 · 可能未完整播出':row.final?'文字已完成':closed||call.state==='summarizing'?'未收到最终文字':agent?'生成中…':'识别中…';
    bubble.append(element('div',state+(row.truncated?' · 长句节选':''),'live-state'));root.append(bubble);
  }
  root.scrollTop=followLive?root.scrollHeight:top;
  $('followLive').textContent=followLive?'已跟随最新':'跟随最新';
}
function completedLive(detail) {
  const rows=new Map(liveRows.map(r=>[r.role+'\0'+r.item_id,{...r}]));
  const interrupted=new Set(detail.transcripts.filter(e=>e.role==='interrupt').map(e=>e.item_id));
  for(const [index,entry] of detail.transcripts.entries()) {
    const item=entry.item_id||'history-'+index,key=entry.role+'\0'+item;
    if(entry.role==='interrupt') {const row=rows.get('agent_generated\0'+entry.item_id);if(row)row.interrupted=true;continue;}
    if(!['remote','agent_generated'].includes(entry.role))continue;
    const old=rows.get(key);
    rows.set(key,{...old,...entry,item_id:item,seconds:old?.seconds??entry.seconds,final:true,
                  text:entry.text.slice(0,2000),truncated:entry.text.length>2000,
                  interrupted:!!old?.interrupted||entry.role==='agent_generated'&&interrupted.has(item)});
  }
  renderLive({...detail.result,live_transcripts:Array.from(rows.values()).sort((a,b)=>a.seconds-b.seconds).slice(-60)});
  finishedLiveId=['completed','failed','cancelled_before_dial','cancelled_before_answer'].includes(detail.result.state)?detail.result.id:null;
}
$('liveTranscript').onscroll=()=>{const root=$('liveTranscript');followLive=root.scrollHeight-root.scrollTop-root.clientHeight<40;$('followLive').textContent=followLive?'已跟随最新':'跟随最新';};
$('followLive').onclick=()=>{followLive=true;$('liveTranscript').scrollTop=$('liveTranscript').scrollHeight;$('followLive').textContent='已跟随最新';};
function mediaUrl(id,file) {return '/media/'+encodeURIComponent(id)+'/'+encodeURIComponent(file);}
async function selectCall(id, force=false) {
  const serial=++requestSerial;
  const previous=selectedId;selectedId=id;renderHistory();
  try {
    const detail=await api('/api/calls/'+id); if(serial!==requestSerial||selectedId!==id)return;
    const signature=JSON.stringify([detail.result.state,detail.summary,detail.transcripts.length,detail.files]);
    if(!force&&previous===id&&signature===detailState)return;
    detailState=signature;
    const r=detail.result, root=$('detail');root.replaceChildren();
    const title=element('div',null,'detail-title');title.append(element('h3',r.number),element('span',labels[r.state]||r.state,'state-pill'+(r.state==='failed'?' failed':'')));root.append(title);
    root.append(element('div',formatDate(r.created_at)+' · '+seconds(r.duration_seconds)+' · '+(r.voice_id=== 'zh_female_vv_jupiter_bigtts'?'VV':r.voice_provider==='doubao'?'豆包 O2':'OpenAI'),'detail-meta'));
    if(r.hangup_warning)root.append(element('div',r.hangup_warning,'notice error'));
    if(r.error)root.append(element('div','通话未完成：'+(r.error==='RuntimeError'?'旧记录未保存具体原因，请重新检测后测试。':r.error),'notice error'));
    if(r.task)root.append(element('p','通话目的：'+r.task,'source-note'));
    if(r.state==='completed')root.append(element('p',r.hangup_confirmed?'手机已确认挂断':'请检查手机是否已经挂断','source-note'));
    const closed=['completed','failed','summarizing','cancelled_before_dial','cancelled_before_answer'].includes(r.state);
    const audioFinal=closed && r.state!=='summarizing';
    const recordings=element('div',null,'recordings');
    for(const [file,label] of [['remote.wav','对方录音'],['agent.wav','AI 录音']]) {
      const card=element('div',null,'audio-card');card.append(element('h4',label));
      if(audioFinal&&detail.files.includes(file)&&r.audio_bytes?.[file==='remote.wav'?'remote':'agent']>0) {
        const player=document.createElement('audio');player.controls=true;player.preload='metadata';player.src=mediaUrl(id,file);card.append(player);
        const download=element('a','下载录音');download.href=player.src;download.download=file;card.append(download);
      }else card.append(element('p',audioFinal?'没有有效音频':r.state==='summarizing'?'正在保存结果，完成后可播放':'通话结束后可播放','hint'));
      recordings.append(card);
    }root.append(recordings,element('h3','沟通总结'));
    root.append(element('p',r.voice_provider==='doubao'?'根据实时转写生成，未经录音二次转写核对。':'总结来源请查看正文中的转写说明。','source-note'));
    root.append(element('div',detail.summary||(r.state==='summarizing'?'正在生成总结…':closed?'本次未生成总结。':'通话结束后自动生成。'),'summary'));
    const disclosure=document.createElement('details');disclosure.open=!closed;
    disclosure.append(element('summary','对话文字 · '+detail.transcripts.length+' 条'));
    disclosure.append(element('p','AI 文字为模型生成内容；打断时可能未完整播出。','source-note'));
    const transcripts=element('div',null,'transcripts');
    for(const entry of detail.transcripts) {
      const row=element('div',null,'transcript');row.append(element('span',entry.role==='remote'?'对方':entry.role==='agent'?'AI 生成':entry.role||'记录'),element('p',entry.text||JSON.stringify(entry)));transcripts.append(row);
    }disclosure.append(transcripts);root.append(disclosure);
    const downloads=element('div',null,'downloads');
    for(const [file,label] of [['summary.md','下载总结'],['transcripts.jsonl','下载转写'],['result.json','下载通话结果']])if(detail.files.includes(file)){const a=element('a',label);a.href=mediaUrl(id,file);a.download=file;downloads.append(a);}root.append(downloads);
  } catch(e) {if(previous!==id)notice(e.message,true);}
}
async function refresh() {
  if(refreshing)return;refreshing=true;
  try {
    const s=await api('/api/status');applyStatus(s);
    if(!s.current_call&&liveId&&finishedLiveId!==liveId)completedLive(await api('/api/calls/'+liveId));
    records=await api('/api/calls');renderHistory();
    if(!selectedId&&records.length)await selectCall(records[0].id);else if(selectedId)await selectCall(selectedId);
  }catch(e){$('topStatus').textContent='控制台连接中断';$('start').disabled=true;notice('本机服务没有响应，请重新打开启动文件。',true);}
  finally{refreshing=false;}
}
async function poll() {await refresh();setTimeout(poll,snapshot?.current_call?1000:2000);}
$('connect').onclick=async()=> { working=true;$('connect').textContent='正在检测，请稍候…';if(snapshot)applyStatus(snapshot);notice('正在检测设备和所选渠道，这一步不会拨号。');try{await api('/api/connect',settings());$('apiKey').value='';notice('渠道会话验证成功。若手机蓝牙尚未连接，请连接后再检测。');}catch(e){notice(e.message,true);}finally{working=false;$('connect').textContent='连接设备并检测渠道';await refresh();}};
$('saveKey').onclick=async()=>{
  if(!$('apiKey').value.trim()&&!snapshot?.credential_saved) {notice('请输入该渠道的 API Key 再保存。',true);return;}
  working=true;if(snapshot)applyStatus(snapshot);
  try{await api('/api/settings',settings());$('apiKey').value='';notice('Key 已保存到 macOS 钥匙串。请检测渠道会话，确认可用后再通话。');}
  catch(e){notice(e.message,true);}finally{working=false;await refresh();}
};
$('provider').onchange=async()=>{
  working=true;$('apiKey').value='';$('start').disabled=true;
  try{const s=settings();delete s.api_key;await api('/api/settings',s);notice('已切换渠道。每个渠道独立保存 Key，请重新检测。');}
  catch(e){if(snapshot)$('provider').value=snapshot.provider;notice(e.message,true);}
  finally{working=false;await refresh();}
};
$('voice').onchange=async()=>{$('customVoice').classList.toggle('hidden',$('voice').value!=='custom');$('start').disabled=true;if($('voice').value!=='custom'){try{const s=settings();delete s.api_key;await api('/api/settings',s);await refresh();}catch(e){notice(e.message,true);}}};
for(const id of ['voiceId','voiceName'])$(id).oninput=()=>{$('start').disabled=true;};
$('callForm').onsubmit=async event=>{event.preventDefault();working=true;$('start').disabled=true;notice('正在创建通话任务…');try{const result=await api('/api/call',{number:$('number').value.trim(),task:$('task').value,max_seconds:Number($('limit').value)});notice('已创建通话任务，请等待另一部手机响铃并接听。');await selectCall(result.id,true);}catch(e){notice(e.message,true);}finally{working=false;await refresh();}};
$('hangup').onclick=async()=>{try{await api('/api/hangup',{});notice('已请求结束通话，录音与总结仍会继续保存。');await refresh();}catch(e){notice(e.message,true);}};
const HEADHUNTER_CASE = `【人设】
你是猎头顾问的 AI 助手，负责候选人的首次电话沟通与意向初聊。专业、亲切、有分寸，认真听取对方的情况，帮助顾问判断后续是否值得进一步联系。坦诚说明 AI 身份，不把自己介绍成真人，不把对方的客气回应当成求职意愿。

【沟通风格与声音表达】
用自然的中文交流，每轮说一两句话，只问一个问题，等对方回答完再继续。根据对方刚说的具体内容回应，再顺势追问；不要机械地逐项念问题，也不要每句都以“好的”“明白”开头。
语调随内容变化：开场亲切、轻松；提问时带一点好奇，问句自然上扬；听到具体成果时适度肯定，声音稍有精神；对方提到压力或顾虑时，语速略放缓、语气柔和；确认安排时清楚利落；结束时带一点笑意，轻松道别。句子长短和停顿有变化，重要信息稍作强调，不用始终一致的音高和节奏，不使用播报腔或夸张的推销腔。
肯定必须有具体依据。例如对方讲完某段项目经历，可说“这块听起来挺有挑战的，最难推进的是哪一步？”遇到顾虑，可说“嗯，这个顾虑我理解，你更希望下一份工作在哪方面有变化？”这些只是表达参考，根据回答自然措辞，不固定复读。不要把语气要求、括号动作或情绪标签读出来。

【任务】
这次电话先了解候选人是否考虑新机会、相关经历、下一份工作的偏好，以及是否愿意进一步沟通。当前没有已确认的具体公司、岗位、薪资或待遇资料，不介绍未经确认的机会，不承诺面试、录用或资料已经发送。

【对话顺序】
1. 完成 AI 身份和录音用途说明并得到同意后，简短说明来意：“我这边想先了解一下你目前的工作情况，看看之后有合适的机会时，是否方便再跟你联系。”随后问：“你最近会考虑新的工作机会吗？”现在不方便时先询问合适的联系时间；不愿继续则结束。
2. 若愿意聊，先听对方当前负责的方向，再围绕一项有代表性的项目或成果追问。一次只问一个问题，不假定其经历，不做盘问式的信息采集。
3. 顺着对方的回答了解下一份机会最看重什么：工作内容、业务方向、成长空间、城市或工作方式。优先聊对方自己提出的关注点，不为了问完清单忽略刚才的回答。
4. 若对方只愿意看特别合适的机会，了解什么条件值得进一步聊；若暂不考虑，询问是否愿意以后有匹配机会时再联系，不强劝，不要求当场作决定。
5. 对方有继续交流的意愿时，确认是否可以把刚才的情况整理给猎头顾问，以及什么时间方便进一步沟通。后续联系或岗位资料由顾问确认，不说已经替对方安排好了。
6. 在通话时限内简短复述已确认的意向、相关背景、关键偏好与后续沟通意愿，问“我这样理解对吗，还有什么你希望我一并记下的？”等待回答，再感谢并道别。

【结束与记录】
对方明确拒绝、不愿录音或想结束时，及时礼貌结束。不要为了问完所有问题延长通话。没有回答或不确定的信息标注未确认；区分对方实际表达、顾问待确认信息和你的推测，便于通话后总结。`;
$('savedNumber').onchange=()=>{
  const number=$('savedNumber').value;
  if(number) $('number').value=number;
};
$('number').oninput=()=>{ $('savedNumber').value=''; };
$('headhunterDemo').onclick=()=>{
  $('task').value=HEADHUNTER_CASE;
  $('task').rows=10;
  $('task').focus();
  $('task').setSelectionRange(0,0);
  $('task').scrollTop=0;
  notice('已填入猎头顾问案例。可继续编辑，确认后再开始通话。');
};
$('refresh').onclick=refresh;
poll();
