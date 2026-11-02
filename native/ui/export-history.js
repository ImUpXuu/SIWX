/* Native edition extension: disk-backed export history; original export module unchanged. */
(() => {
  'use strict';
  const style=elStyle();
  function elStyle(){const n=document.createElement('style');n.textContent=`
    .export-wrap .e-side > .card {flex-shrink:0}
    .export-wrap .e-side > .e-logcard {flex:0 0 auto;overflow:visible}
    .export-wrap #e-log {flex:none;height:clamp(320px,48vh,640px);min-height:320px;max-height:none;resize:vertical;overflow:auto;overflow-wrap:anywhere}
    .export-wrap #e-log.native-expanded {height:75vh;min-height:320px}
    .export-wrap .e-log-actions {flex-wrap:wrap}
    .export-wrap .e-logcard .card-head {flex-wrap:wrap}
  `;document.head.append(n);return n;}
  async function copyDirectory(directory){
    try{const ok=await window.SX.copyText(directory);window.SX.toast(ok?'目录路径已复制':'复制失败，请手动选择目录路径复制。',!ok);}
    catch(error){window.SX.toast('复制失败，请手动选择目录路径复制。',true);}
  }
  function enhanceResults(){
    const log=document.getElementById('e-log'),actions=document.querySelector('.e-log-actions');
    if(log&&actions&&!document.getElementById('e-log-expand')){
      const expand=el('button','mini-btn','展开日志');expand.type='button';expand.id='e-log-expand';expand.setAttribute('aria-expanded','false');
      expand.onclick=()=>{const on=log.classList.toggle('native-expanded');log.style.height='';expand.textContent=on?'收起日志':'展开日志';expand.setAttribute('aria-expanded',String(on));};actions.append(expand);
    }
    document.querySelectorAll('#e-result [data-open-path]').forEach(open=>{
      if(open.parentElement.querySelector('[data-native-copy-dir]'))return;
      const copy=el('button','btn','复制目录路径');copy.type='button';copy.dataset.nativeCopyDir='';copy.onclick=()=>copyDirectory(open.dataset.openPath);open.after(copy);
    });
  }
  let offset=0,query='',busy=false,lastKey='',mounted=null;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  function size(bytes){return bytes>=1048576?(bytes/1048576).toFixed(1)+' MB':bytes>=1024?(bytes/1024).toFixed(1)+' KB':bytes+' B';}
  async function load(force=false){
    const card=document.getElementById('e-history');if(!card||busy)return;
    busy=true;const result=card.querySelector('[data-history-list]');
    try{
      const response=await fetch('/api/native/exports?'+new URLSearchParams({offset,limit:30,q:query}),{cache:'no-store'});
      if(!response.ok)throw new Error('导出文件列表读取失败，请刷新重试。');
      const data=await response.json();if(!card.isConnected)return;
      if(offset&&offset>=data.total){offset=Math.max(0,Math.floor((data.total-1)/30)*30);busy=false;return load(true);}
      const key=JSON.stringify(data);if(!force&&lastKey===key)return;lastKey=key;
      result.replaceChildren();
      if(!data.files.length)result.append(el('div','empty',query?'没有匹配的导出文件。':'暂无导出文件，完成导出后会在这里保留展示。'));
      for(const file of data.files){
        const row=el('div');row.style.display='flex';row.style.padding='12px 0';row.style.borderBottom='1px solid var(--line)';row.style.fontSize='13px';row.style.alignItems='start';row.style.gap='12px';
        const meta=el('div');meta.style.minWidth='0';meta.style.flex='1';meta.style.overflowWrap='anywhere';
        meta.append(el('b','',file.name),el('div','dim',file.path),el('div','dim',file.format+' · '+size(file.bytes)+' · '+file.modified.replace('T',' ')));
        const link=el('a','btn','下载');link.href=file.download;link.setAttribute('download','');const actions=el('div');actions.style.display='flex';actions.style.gap='8px';actions.style.flexWrap='wrap';const copy=el('button','btn','复制目录路径');copy.type='button';copy.dataset.historyCopyDir='';copy.onclick=()=>copyDirectory(file.directory);actions.append(link,copy);row.append(meta,actions);result.append(row);
      }
      card.querySelector('[data-history-count]').textContent='共 '+data.total+' 个文件'+(data.truncated?'（目录过大，已限制扫描范围）':'');
      card.querySelector('[data-history-page]').textContent=data.total?(Math.floor(offset/30)+1)+' / '+Math.ceil(data.total/30):'0 / 0';
      card.querySelector('[data-history-prev]').disabled=offset===0;card.querySelector('[data-history-next]').disabled=!data.has_more;
    }catch(error){if(card.isConnected)result.replaceChildren(el('div','empty',error.message));}
    finally{busy=false;}
  }
  function mount(){
    const host=document.querySelector('.export-wrap .e-side');if(!host||document.getElementById('e-history'))return;
    offset=0;query='';lastKey='';
    const card=el('section','card');card.id='e-history';const head=el('div','card-head'),count=el('span','hint');count.dataset.historyCount='';
    head.append(el('h2','','已导出文件'),count);card.append(head,el('p','dim','导出文件保存在本机磁盘，刷新、重启或更新软件后仍可查看；删除原文件后列表会同步更新。'));
    const controls=el('div'),search=el('input');search.type='search';search.placeholder='按文件名或目录搜索';search.setAttribute('aria-label','搜索已导出文件');search.style.minWidth='0';search.style.flex='1';
    const refresh=el('button','btn','刷新列表');refresh.type='button';refresh.id='e-history-refresh';
    const list=el('div');list.dataset.historyList='';list.style.maxHeight='420px';list.style.overflow='auto';
    const nav=el('div'),prev=el('button','btn','上一页'),next=el('button','btn','下一页'),page=el('span','dim');prev.type=next.type='button';prev.dataset.historyPrev='';next.dataset.historyNext='';page.dataset.historyPage='';
    let timer;search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{query=search.value.trim();offset=0;load(true);},250);});refresh.onclick=()=>load(true);prev.onclick=()=>{offset=Math.max(0,offset-30);load(true);};next.onclick=()=>{offset+=30;load(true);};
    for(const bar of [controls,nav]){bar.style.display='flex';bar.style.alignItems='center';bar.style.gap='8px';bar.style.flexWrap='wrap';bar.style.marginTop='12px';}controls.append(search,refresh);nav.append(prev,page,next);card.append(controls,list,nav);host.append(card);mounted=card;load(true);
  }
  const observer=new MutationObserver(()=>{mount();enhanceResults();});observer.observe(document.getElementById('view'),{childList:true,subtree:true});mount();enhanceResults();
  setInterval(()=>{if(mounted?.isConnected&&!document.hidden)load();},3000);
  window.addEventListener('focus',()=>load());
})();
