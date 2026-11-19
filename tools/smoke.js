// Pre-ship check: run every page script in a fake DOM and render every kid screen against LIVE state.
// usage: node tools/smoke.js   (from the repo root; Chore Time must be running, CHORE_URL=host:port, default localhost:8790)
const fs=require('fs'),vm=require('vm'),cp=require('child_process');
const el=()=>({innerHTML:'',textContent:'',value:'',style:{setProperty(){}},dataset:{},className:'',classList:{add(){},remove(){},toggle(){},contains(){return false}},
  getContext:()=>new Proxy({},{get:()=>function(){}}),setAttribute(){},appendChild(){},insertAdjacentHTML(){},querySelector:()=>el(),querySelectorAll:()=>[]});
function ctx(){const c={document:{querySelectorAll:()=>[],querySelector:()=>el(),getElementById:()=>el(),createElement:()=>el(),addEventListener(){},activeElement:null,
  documentElement:{style:{setProperty(){}}}},addEventListener(){},location:{pathname:'/',search:''},navigator:{userAgent:''},innerWidth:1280,innerHeight:800,
  setInterval(){},setTimeout(){},clearTimeout(){},clearInterval(){},fetch:()=>new Promise(()=>{}),XMLHttpRequest:function(){this.open=()=>{};this.send=()=>{}},
  localStorage:{getItem(){return null},setItem(){}},sessionStorage:{getItem(){return null},setItem(){}},getComputedStyle:()=>({getPropertyValue:()=>''}),
  requestAnimationFrame(){},console,Date,Math,JSON,prompt(){return null},confirm(){return false}};c.window=c;c.parent=c;return vm.createContext(c)}
const st=JSON.parse(cp.execSync('curl -s '+(process.env.CHORE_URL||'localhost:8790')+'/api/state').toString());
// ids come from whatever config is running (the demo family by default), never hard-coded
const K=st.config.kids.map(k=>k.id), live=st.config.chores.filter(c=>!c.hidden), ids=f=>live.filter(f).map(c=>c.id);
const rooms=ids(c=>c.room&&!c.bundle), bundles=ids(c=>c.bundle), shared=ids(c=>c.shared&&c.propose)[0]||rooms[0];
const tv0=Object.keys((st.tv||{}).tvs||{})[0]||'*', k0=K[0], k1=K[1]||K[0], k2=K[2]||K[0], r0=rooms[0], r1=rooms[1]||rooms[0];
const ask=a=>a.map(i=>'askChore("'+i+'");').join('');
for(const f of ['static/kids.html','static/inspect.html','static/tvlock.html','static/now.html','static/music-block.html']){
  const c=ctx();[...fs.readFileSync(f,'utf8').matchAll(/<script>([\s\S]*?)<\/script>/g)].forEach(m=>vm.runInContext(m[1],c));
  if(f.includes('kids')){vm.runInContext('S='+JSON.stringify(st)+';render();'+K.map(k=>'kid="'+k+'";page=0;renderKid();openDetail("points");openDetail("streak");openDetail("news");openDetail("goal");closeDetail();openCalendar();CALWEEK.setDate(CALWEEK.getDate()+7);renderCalendar();closeCalendar();openAwards();'+K.map(x=>'openAwards("'+x+'");').join('')+'showNewAwards();'+ask([r0])+'kid="'+k1+'";'+ask(bundles)+
      'whoWatches(S.config.rewards[0],"'+tv0+'");S.paused=S.paused||{};S.paused["'+k1+'"]={chore:"'+shared+'",ts:S.now-3600,elapsed:60,team:["'+k1+'"]};remindJob("'+k1+'",0);'+
      'S.active=S.active||{};S.active["'+k2+'"]={chore:"'+r1+'",ts:S.now-3600,team:["'+k2+'"]};remindJob("'+k2+'",0);'+
      'var _al='+JSON.stringify(rooms.slice(0,7))+'.map(function(c,i){return {id:"t"+i,chore:c,price:9,base:3}});showAnnounce(_al,function(){});if(AWV.held[0])awardDetail(AWV.held[0],AWV.owner,true);if(AWV.my[0])awardDetail(AWV.my[0],AWV.owner,false);').join(''),c);
    const pack={version:'smoke',title:'Packing test',total_points:3,bag_points:1,required_capacity:1,correct_bag:'pair',bags:[{id:'pair',name:'Two bags',icon:'1f9f3',capacity:1}],missions:[{id:'m',title:'Pack',icon:'1f392',points:2,bag:'backpack',bag_name:'backpack',capacity:1,items:[{id:'i',name:'Item',icon:'1f455',size:1}]}]};
    vm.runInContext('PACK_TEST=true;S.packing_game='+JSON.stringify(pack)+';packingTestState(S);render();openKid("packing_test");packOpen();PACK.bag="pair";PACK.checked.i=true;packRender();packClose();',c);
    // also the "doing now" race bar path with a fake active chore + record
    const st2=JSON.parse(JSON.stringify(st));st2.active={[k1]:{chore:r0,ts:Date.now()/1000-120}};st2.records={[r0]:{kid:k0,sec:300,ts:0}};st2.typical={[r0]:{minutes:8,n:4}};
    st2.awards={[k1]:[{id:'x',icon:'1f947',title:'Fastest ever!',sub:'5:00',ts:Date.now()/1000}]};st2.bedtime={on:true,at:'20:30'};
    vm.runInContext('S='+JSON.stringify(st2)+';kid="'+k1+'";renderKid();openAwards();showNewAwards();recordTry("'+r0+'");kid="'+k0+'";recordTry("'+r0+'");openNews();render();',c)}
  if(f.includes('inspect')){vm.runInContext('S='+JSON.stringify(st)+';render();PM="tv";render();PM="ct";render();TOUR={i:0,changed:[],val:null};PM="tour";render();renderTour();TOUR.i=99;renderTour();var rr=S.history.find(function(h){return h.type==="report"});if(rr){MOSTLY=rr.id;PM="mostly";render()}NOTEFORM={chore:"'+r0+'",mode:"reopen"};PM="noteform";render();if(rr){NOTYET=rr.id;PM="notyet";render()}PM=null;',c)}
  if(f.includes('now')){vm.runInContext('S='+JSON.stringify(st)+';draw();',c)}
  if(f.includes('music-block')){vm.runInContext('render(Object.assign({},'+JSON.stringify(st)+',{music_block:{effective:true,blockers:[{kid:"'+k0+'",kind:"cleanup",name:"Mandatory chores",items:["Put things away"]}]}}));',c)}
  console.log('OK',f)}
