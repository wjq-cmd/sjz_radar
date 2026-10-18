/* Keep the existing controls and listeners; only reorganize the settings panel. */
(() => {
  const dock = document.getElementById('dock');
  if (!dock) return;
  const body = dock.querySelector(':scope > .pan-body');
  const pages = [
    {key:'display',label:'显示',title:'人物与标记',hint:'选择地图上需要的信息',groups:[
      ['地图',['s-map']],
      ['外观主题',['panelthemeseg']],
      ['人物显示',['s-mate','s-ai','s-name','s-wpn','s-gear','s-dist','s-hp','s-foe','s-cone','s-trail','s-roster'],true],
      ['标记样式',['s-dotsize','s-fontsize','s-tagw','s-tagop','s-gunlen']]]},
    {key:'scene',label:'3D 场景',title:'3D 场景',hint:'拖动即生效 · 自动保存',groups:[
      ['建筑材质',['s-walltrans','s-floortrans']],
      ['人物模型与朝向',['model3dseg','direction3dseg','directionanchor3dseg','s-charsize']],
      ['人物颜色',['s-visiblecolor3d','s-occludedcolor3d']],
      ['第一视角',['s-fov','s-fpvheight','s-fpvtau']],
      ['渲染性能',['q3dseg','fpscapseg']]]},
    {key:'loot',label:'物资',title:'物资与容器',hint:'按需显示，减少地图遮挡',groups:[
      ['显示类型',['s-loot','s-box','s-container','s-aibox'],true],
      ['物资品质',['pick-loot']]]},
    {key:'alerts',label:'预警',title:'距离与预警',hint:'视图顶部可开关屏外预警',groups:[
      ['接近提醒',['s-alert']],
      ['屏外预警',['s-warnd','s-warnr','s-warnsz']]]},
  ];
  const shell = document.createElement('div'); shell.className='dock-layout';
  const nav = document.createElement('div');nav.className='dock-tabs';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','设置分类');nav.setAttribute('aria-orientation','vertical');
  const content=document.createElement('div');content.className='dock-content';
  const tabs=[],panels=[];
  for (const page of pages) {
    const tab=document.createElement('button');tab.type='button';tab.id='dock-tab-'+page.key;tab.textContent=page.label;
    tab.setAttribute('role','tab');tab.setAttribute('aria-controls','dock-page-'+page.key);tab.dataset.page=page.key;nav.append(tab);tabs.push(tab);
    const panel=document.createElement('section');panel.id='dock-page-'+page.key;panel.className='dock-page';panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tab.id);
    const head=document.createElement('header');head.className='dock-page-head';
    const title=document.createElement('h2');title.textContent=page.title;const hint=document.createElement('p');hint.textContent=page.hint;head.append(title,hint);panel.append(head);
    for(const [name,ids,grid] of page.groups){
      const section=document.createElement('section');section.className='dock-group';const label=document.createElement('h3');label.textContent=name;section.append(label);
      const controls=document.createElement('div');controls.className=grid?'dock-switches':'dock-controls';
      for(const id of ids){
        const el=document.getElementById(id);if(!el)continue;
        const row=el.closest('.row,.sld,.pick') || el;
        if(el.classList.contains('sw')){const caption=row.querySelector('.row-t')?.textContent || id;el.setAttribute('aria-label',caption);row.title=row.querySelector('.row-s')?.textContent || caption;}
        if(el.matches('input[type="range"]')) el.setAttribute('aria-label',row.querySelector('span')?.textContent || id);
        controls.append(row);
      }
      section.append(controls);panel.append(section);
    }
    if(page.key==='scene'){
      const note=document.createElement('p');note.className='dock-note';note.textContent='透明度 0% 为实心，100% 为全透明。仅改变模型外观。';panel.append(note);
    }
    content.append(panel);panels.push(panel);
  }
  // Unexpected future controls stay reachable rather than disappearing silently.
  const remaining=[...body.children].filter(el=>el.querySelector('button,input,select')||el.matches('button,input,select'));
  if(remaining.length){const group=document.createElement('section');group.className='dock-group';group.append(...remaining);panels[0].append(group);}
  shell.append(nav,content);body.replaceWith(shell);
  const diagnostics=document.getElementById('gateway-diagnostics');if(diagnostics)dock.append(diagnostics);
  function select(key,focus=false){
    if(!pages.some(p=>p.key===key))key='display';
    tabs.forEach(tab=>{const active=tab.dataset.page===key;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;if(active&&focus)tab.focus();});
    panels.forEach((panel,i)=>panel.hidden=pages[i].key!==key);
    try{localStorage.setItem('gateway_dock_tab',key);}catch{}
  }
  tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>select(tab.dataset.page));tab.addEventListener('keydown',e=>{
    let next;if(e.key==='ArrowDown'||e.key==='ArrowRight')next=(i+1)%tabs.length;
    if(e.key==='ArrowUp'||e.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;
    if(e.key==='Home')next=0;if(e.key==='End')next=tabs.length-1;
    if(next!==undefined){e.preventDefault();select(tabs[next].dataset.page,true);}
  });});
  let saved='display';try{saved=localStorage.getItem('gateway_dock_tab')||saved;}catch{}
  select(saved);document.getElementById('tools-sub').textContent='TQ';
})();
