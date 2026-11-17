# -*- coding: utf-8 -*-
html_js = """
<script>
const API='';
const tags={traits:[],style:[],topics:[]};
let currentId=null;
async function loadList(){
  const r=await fetch(API+'/admin/personas');const list=await r.json();
  const el=document.getElementById('botList');
  if(!list.length){el.innerHTML='<div class=\"empty-list\">暂无配置</div>';return;}
  el.innerHTML=list.map(p=>`<div class=\"bot-item${p.id===currentId?' active':''}\" onclick=\"loadBot('${p.id}')\"><div class=\"bi-name\">${p.name||'未命名'} <span style=\"font-size:.7rem;opacity:.6\">${p.role||''}</span></div><div class=\"bi-meta\">${p.brand||''}</div><div class=\"bi-col\">&#x1F4DA; ${p.knowledge_collection||'agent_memory'}</div><div class=\"bi-id\">${p.id}</div></div>`).join('');
}
function showEditor(show){document.getElementById('emptyState').style.display=show?'none':'flex';const w=document.getElementById('editorWrapper');w.style.display=show?'flex':'none';}
function newBot(){
  currentId=null;
  document.getElementById('botId').value='';document.getElementById('botId').removeAttribute('readonly');
  document.getElementById('editorTitle').textContent='新建机器人';
  document.getElementById('btnDelete').style.display='none';
  document.getElementById('f_collection').value='agent_memory';
  ['f_name','f_role','f_brand','f_brand_intro','f_extra'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('previewBody').innerHTML='<span style=\"color:var(--muted);font-style:italic\">点击「预览 Prompt」...</span>';
  ['traits','style','topics'].forEach(k=>{tags[k]=[];renderTags(k);});showEditor(true);
}
async function loadBot(id){
  const r=await fetch(API+'/admin/personas/'+id);if(!r.ok){toast('加载失败','err');return;}
  const p=await r.json();currentId=id;
  document.getElementById('botId').value=id;document.getElementById('botId').setAttribute('readonly','readonly');
  document.getElementById('editorTitle').textContent=(p.name||id)+' ('+id+')';
  document.getElementById('btnDelete').style.display='';
  document.getElementById('f_collection').value=p.knowledge_collection||'agent_memory';
  document.getElementById('f_name').value=p.name||'';document.getElementById('f_role').value=p.role||'';
  document.getElementById('f_brand').value=p.brand||'';document.getElementById('f_brand_intro').value=p.brand_intro||'';
  document.getElementById('f_extra').value=p.extra_instructions||'';
  tags.traits=p.personality_traits||[];renderTags('traits');
  tags.style=p.style_guidelines||[];renderTags('style');
  tags.topics=p.chitchat_topics||[];renderTags('topics');
  document.getElementById('previewBody').innerHTML='<span style=\"color:var(--muted);font-style:italic\">点击「预览 Prompt」...</span>';
  showEditor(true);
  document.querySelectorAll('.bot-item').forEach(e=>{e.classList.remove('active');if(e.querySelector('.bi-id')&&e.querySelector('.bi-id').textContent===id)e.classList.add('active');});
}
function setCollection(col){document.getElementById('f_collection').value=col;}
function buildPayload(){return{knowledge_collection:document.getElementById('f_collection').value.trim()||'agent_memory',name:document.getElementById('f_name').value.trim(),role:document.getElementById('f_role').value.trim(),brand:document.getElementById('f_brand').value.trim(),brand_intro:document.getElementById('f_brand_intro').value.trim(),personality_traits:[...tags.traits],style_guidelines:[...tags.style],chitchat_topics:[...tags.topics],extra_instructions:document.getElementById('f_extra').value.trim()};}
async function saveBot(){
  const id=document.getElementById('botId').value.trim();
  if(!id){toast('请填写 Bot ID','err');return;}
  const payload=buildPayload();if(!payload.name){toast('请填写客服名字','err');return;}
  const r=await fetch(API+'/admin/personas/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(r.ok){currentId=id;toast('保存成功','ok');loadList();document.getElementById('editorTitle').textContent=payload.name+' ('+id+')';document.getElementById('btnDelete').style.display='';document.getElementById('botId').setAttribute('readonly','readonly');}
  else{const e=await r.json();toast('保存失败: '+(e.detail||''),'err');}
}
async function deleteBot(){
  if(!currentId)return;if(!confirm('确定删除 "'+currentId+'"？'))return;
  const r=await fetch(API+'/admin/personas/'+currentId,{method:'DELETE'});
  if(r.ok){toast('已删除','ok');currentId=null;showEditor(false);loadList();}else toast('删除失败','err');
}
async function previewPrompt(){
  const payload=buildPayload();
  const r=await fetch(API+'/admin/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:payload})});
  const d=await r.json();const el=document.getElementById('previewBody');
  if(d.ok)el.textContent=d.preview;else el.textContent='预览失败: '+(d.detail||'');
}
function copyPreview(){navigator.clipboard.writeText(document.getElementById('previewBody').textContent).then(()=>toast('已复制','ok'));}
function copyApiSnippet(){
  const id=currentId||document.getElementById('botId').value.trim();
  const col=document.getElementById('f_collection').value.trim()||'agent_memory';
  const snippet=JSON.stringify({message:'用户消息',session_id:'session-001',collection:col,persona:{name:document.getElementById('f_name').value||'小美',role:document.getElementById('f_role').value||'客服助手',brand:document.getElementById('f_brand').value||'品牌'}},null,2);
  navigator.clipboard.writeText(snippet).then(()=>toast('API 示例已复制','ok'));
}
function addTag(e,key){if(e.key!=='Enter')return;e.preventDefault();const v=e.target.value.trim();if(v&&!tags[key].includes(v)){tags[key].push(v);renderTags(key);}e.target.value='';}
function renderTags(key){const map={traits:'tags_traits',style:'tags_style',topics:'tags_topics'};const el=document.getElementById(map[key]);el.innerHTML=tags[key].map((t,i)=>`<span class=\"tag\">${t}<span class=\"rm\" onclick=\"removeTag('${key}',${i})\">x</span></span>`).join('');}
function removeTag(key,idx){tags[key].splice(idx,1);renderTags(key);}
function toast(msg,type){const el=document.getElementById('toast');el.textContent=msg;el.className=type;el.style.display='block';setTimeout(()=>el.style.display='none',2500);}
loadList();
</script></body></html>
"""
open('api/static/bots_p3.html','w',encoding='utf-8').write(html_js)
print('p3:', len(html_js))
