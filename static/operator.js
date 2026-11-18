(()=>{
'use strict';
const $=s=>document.querySelector(s), screen=$('#screen'),canvas=$('#draw'),ctx=canvas.getContext('2d');
let token='',state=null,mode='view',anchor=null,imgUrl=null,lastPreview=0;
const names=['score','header','timer','road'];
function show(s){$('#actionResult').textContent=s}
async function req(path, data){
 const res=await fetch(path,{method:data?'POST':'GET',cache:'no-store',headers:{'Authorization':`Bearer ${token}`,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});
 if(!res.ok){let m='HTTP '+res.status;try{m=(await res.json()).error||m}catch{};throw Error(m)}
 return path.endsWith('preview')?res.blob():res.json();
}
async function command(action,data={}){try{await req('/api/action',{action,data});show('已送出：'+action);setTimeout(refresh,1000)}catch(e){show('錯誤：'+e.message)}}
function toPixels(e){const r=canvas.getBoundingClientRect();return {x:Math.max(0,Math.min(canvas.width,(e.clientX-r.left)/r.width*canvas.width)),y:Math.max(0,Math.min(canvas.height,(e.clientY-r.top)/r.height*canvas.height))}}
function renderRois(){if(!canvas.width)return;ctx.clearRect(0,0,canvas.width,canvas.height);if(!state)return;
 const colors={score:'#6db7ff',header:'#ffc16c',timer:'#68e4ae',road:'#d49cff'};
 for(const n of names){const r=state.rois?.[n];if(!r)continue;ctx.strokeStyle=colors[n];ctx.lineWidth=3;ctx.strokeRect(r.x*canvas.width,r.y*canvas.height,r.w*canvas.width,r.h*canvas.height);ctx.fillStyle=colors[n];ctx.font='18px sans-serif';ctx.fillText(n,r.x*canvas.width+6,r.y*canvas.height+20)}
 if(anchor && anchor.to){const x=Math.min(anchor.from.x,anchor.to.x),y=Math.min(anchor.from.y,anchor.to.y),w=Math.abs(anchor.to.x-anchor.from.x),h=Math.abs(anchor.to.y-anchor.from.y);ctx.strokeStyle='#fff';ctx.lineWidth=3;ctx.strokeRect(x,y,w,h)}
}
canvas.addEventListener('pointerdown',e=>{if(!canvas.width || !names.includes(mode))return;anchor={from:toPixels(e),to:null};canvas.setPointerCapture(e.pointerId)});
canvas.addEventListener('pointermove',e=>{if(!anchor)return;anchor.to=toPixels(e);renderRois()});
canvas.addEventListener('pointerup',async e=>{
 if(mode==='click'){const p=toPixels(e);return command('click',{x:p.x,y:p.y})}
 if(!anchor || !names.includes(mode))return;anchor.to=toPixels(e);
 const a=anchor.from,b=anchor.to;anchor=null;renderRois();
 const x=Math.min(a.x,b.x),y=Math.min(a.y,b.y),w=Math.abs(a.x-b.x),h=Math.abs(a.y-b.y);
 if(w<15||h<12)return show('選取區域太小，請重畫');
 await command('set_roi',{name:mode,roi:{x:x/canvas.width,y:y/canvas.height,w:w/canvas.width,h:h/canvas.height}});
});
$('#mode').addEventListener('change',e=>mode=e.target.value);
$('#connect').addEventListener('click',()=>{token=$('#token').value;$('#token').value='';refresh()});
for(const [id,action] of [['start','start_browser'],['home','go_home'],['stop','stop_browser'],['shot','capture_once'],['monitor','start_monitor'],['pause','stop_monitor']]){$('#'+id).addEventListener('click',()=>command(action))}
$('#entry').addEventListener('click',()=>{const e=$('#entryUrl'),url=e.value.trim();e.value='';if(!url)return show('請先貼上當次授權連結');command('open_authorized_link',{url})});
$('#type').addEventListener('click',()=>{const v=$('#textInput').value;$('#textInput').value='';command('type_text',{text:v})});
$('#press').addEventListener('click',()=>command('press_key',{key:$('#key').value}));
$('#refresh').addEventListener('click',refresh);
async function refresh(){if(!token)return show('請先輸入管理金鑰');try{
 state=await req('/api/status');
 const copy={...state};delete copy.rois;
 $('#status').textContent=JSON.stringify(copy,null,2);
 $('#rois').textContent='校準：'+names.map(n=>(state.rois?.[n]?`✓ ${n}`:`• ${n} 未設定`)).join(' ｜ ');
 renderRois();
 try{const result=await req('/api/candidates');$('#candidates').textContent=JSON.stringify(result.items?.slice(-8)||[],null,2)}catch{}
 if(state.preview_at&&state.preview_at!==lastPreview){
  const blob=await req('/api/preview'),url=URL.createObjectURL(blob);if(imgUrl)URL.revokeObjectURL(imgUrl);imgUrl=url;
  screen.onload=()=>{canvas.width=screen.naturalWidth;canvas.height=screen.naturalHeight;$('#empty').style.display='none';renderRois()};screen.src=url;lastPreview=state.preview_at;
 }
}catch(e){show('狀態連線失敗：'+e.message)}}
setInterval(()=>{if(token)refresh()},2800);
})();
