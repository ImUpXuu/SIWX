'use strict';
const token=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
const $=id=>document.getElementById(id);
let offset=0,total=0,busy=false,stopped=false;
function message(text){$('status').textContent=text;}
async function api(path,body){
 const res=await fetch(path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',credentials:'omit'});
 if(!res.ok){const data=await res.json();throw new Error(data.error||'请求未完成');}
 return res;
}
async function run(task){if(busy||stopped)return;busy=true;document.querySelectorAll('button,input,select').forEach(b=>b.disabled=true);try{await task();}catch(e){message(e.message);}finally{busy=false;document.querySelectorAll('button,input,select').forEach(b=>b.disabled=stopped);}}
function options(id,items){$(id).replaceChildren();for(const item of items){const o=document.createElement('option');o.value=item.id;o.textContent=item.name;$(id).append(o);}}
function request(){return {chat:$('chat').value,query:$('query').value,since:$('since').value,until:$('until').value};}
function clearData(){options('chat',[]);$('messages').replaceChildren();$('page').textContent='';offset=0;total=0;}
async function show(){
 const data=await(await api('/api/messages',{...request(),offset})).json();total=data.total;$('messages').replaceChildren();
 for(const m of data.messages){const a=document.createElement('article'),b=document.createElement('b'),s=document.createElement('small'),p=document.createElement('pre');b.textContent=m.senderDisplayName;s.textContent='　'+m.createTime+' · '+m.typeName;p.textContent=m.content;a.append(b,s,p);$('messages').append(a);}
 $('messages').scrollTop=0;$('page').textContent=total?`${offset+1}–${Math.min(offset+200,total)} / ${total}`:'0 条';message('已显示当前筛选结果。');
}
$('open').onclick=()=>run(async()=>{clearData();message('正在提取和读取，请保持微信登录，稍候…');const state=await(await api('/api/open',{account:Number($('account').value),consent:$('consent').checked})).json();options('chat',await(await api('/api/chats')).json());if($('chat').options.length)await show();else message('未发现可读取会话。');if(state.warnings)message('部分数据库未能读取，当前显示可用结果。');});
$('search').onclick=()=>run(async()=>{offset=0;await show();});
$('chat').onchange=()=>run(async()=>{offset=0;await show();});
$('prev').onclick=()=>run(async()=>{offset=Math.max(0,offset-200);await show();});
$('next').onclick=()=>run(async()=>{if(offset+200<total){offset+=200;await show();}});
$('close').onclick=()=>run(async()=>{await api('/api/close',{});clearData();message('当前数据已关闭。');});
$('stop').onclick=()=>run(async()=>{await api('/api/stop',{});stopped=true;clearData();message('程序已停止，可关闭此网页。');});
$('export').onclick=()=>run(async()=>{message('正在生成导出…');const fmt=$('format').value,res=await api('/api/export',{...request(),format:fmt}),url=URL.createObjectURL(await res.blob()),a=document.createElement('a');a.href=url;a.download='chat-export.'+fmt;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);message('已生成文件，请在浏览器下载中查看。');});
run(async()=>{if(!token)throw new Error('请从程序窗口复制本次启动的完整网址。');const accounts=await(await api('/api/accounts')).json();options('account',accounts);$('empty').hidden=accounts.length>0;});
