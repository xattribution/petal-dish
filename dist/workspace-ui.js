import {connectionCatalog,CONNECTION_METHODS} from './connections.js';
const $=id=>document.getElementById(id);
const state={connections:{},print:{feed:true,mount:true,clip:true,lever:true}};
export const workspaceSettings=()=>({...(Object.keys(state.connections).length?{connections:structuredClone(state.connections)}:{}),printSelection:{...state.print}});
// Does any seam (default or override) use clips? Drives the clip-fit controls.
export const overridesUseClips=()=>[...Object.values(state.connections.families||{}),...Object.values(state.connections.joints||{})].some(c=>c.seamJoint===1||c.seamJoint===2);
// Seam method diagrams: flange walls in line color, hardware in accent / amber. Colors are CSS variables so the
// selected (solid accent) card can redraw them in its ink color.
const seamIcon=method=>`<svg viewBox="0 0 120 64" aria-hidden="true"><path d="M12 43h96M38 43V20h18v23m8 0V20h18v23" stroke="var(--icon-line)" stroke-width="4" fill="none"/>${[
 '<path d="M24 30h72m-65-8v16m58-16v16" stroke="var(--icon-hw)" stroke-width="6"/>',
 '<path d="M30 25V12h60v13" stroke="var(--icon-hw2)" stroke-width="8" fill="none"/>',
 '<path d="M24 30h72M30 20V9h60v11" stroke="var(--icon-hw)" stroke-width="6" fill="none"/>',
 '<path d="M24 30h66l17-20M22 23v14" stroke="var(--icon-hw2)" stroke-width="7" fill="none"/>'][method]}</svg>`;
const hubIcon=method=>`<svg viewBox="0 0 120 64" aria-hidden="true"><path d="M14 22h92M14 40h92" stroke="var(--icon-line)" stroke-width="4" fill="none"/>${method?'<path d="M60 10v44M50 10h20M50 54h20" stroke="var(--icon-hw)" stroke-width="6" fill="none"/>':'<path d="M60 40V24" stroke="var(--icon-hw)" stroke-width="10"/><path d="M60 56V36" stroke="var(--icon-hw2)" stroke-width="4"/>'}</svg>`;
const SEAM_TITLES=['M3 or M4 bolts clamp flat seats on both flanges, with a loose nut.','Printed clips snap over both flanges. No hardware.','Every station takes a bolt or a clip.','Quick-release cam levers through the seam holes. No tools; the springs print in TPU.'];
function card(label,icon,checked,title,onclick){const b=document.createElement('button');b.type='button';b.className='connection-card';b.setAttribute('role','radio');b.setAttribute('aria-checked',String(checked));if(title)b.title=title;b.innerHTML=icon+'<span>'+label+'</span>';b.onclick=onclick;return b;}
// Roving focus for tab lists and radio groups: the selected item is the group's one stop in the tab order; arrow keys,
// Home and End move to another item and choose it (tabs answer only to left and right).
const items=group=>[...group.querySelectorAll('[role=tab],[role=radio]')].filter(b=>!b.hidden&&!b.disabled);
const rove=group=>{const list=items(group),on=list.find(b=>b.getAttribute('aria-checked')==='true'||b.getAttribute('aria-selected')==='true')||list[0];for(const b of list)b.tabIndex=b===on?0:-1;};
function arrowKeys(group,vertical=true){group.addEventListener('keydown',e=>{const list=items(group),i=list.indexOf(document.activeElement);if(i<0)return;const next={ArrowRight:i+1,ArrowLeft:i-1,Home:0,End:list.length-1,...(vertical?{ArrowDown:i+1,ArrowUp:i-1}:{})}[e.key];if(next===undefined)return;e.preventDefault();const b=list[(next+list.length)%list.length];b.focus();if(b.getAttribute('aria-checked')!=='true'&&b.getAttribute('aria-selected')!=='true')b.click();});}
export function setupWorkspace({changed,getModel,showJoint}){
 // Category tabs
 const tabs=[...document.querySelectorAll('[data-category]')],panels=[...document.querySelectorAll('[data-category-panel]')];
 const show=name=>{for(const t of tabs)t.setAttribute('aria-selected',String(t.dataset.category===name));for(const p of panels)p.hidden=p.dataset.categoryPanel!==name;rove(tabs[0].parentElement);};
 for(const t of tabs)t.onclick=()=>show(t.dataset.category);show('Shape');arrowKeys(tabs[0].parentElement,false);
 // Info tips: one floating bubble on the page, so panel scrolling never clips it. Hover, focus or tap shows it.
 // A tip inside a field's label describes that field instead of joining its name: the control points at it with
 // aria-describedby, the tip leaves the tab order, and keyboard focus on the control shows the bubble.
 let tipCount=0;
 for(const t of document.querySelectorAll('.tip')){t.setAttribute('aria-label',t.dataset.tip);const host=t.closest('label'),control=host&&(host.control||host.querySelector('input,select'));
  if(control){t.id||=`tip-${++tipCount}`;control.setAttribute('aria-describedby',[control.getAttribute('aria-describedby'),t.id].filter(Boolean).join(' '));t.setAttribute('aria-hidden','true');t.removeAttribute('tabindex');}}
 const tipFor=el=>{const id=el instanceof Element&&el.getAttribute('aria-describedby')?.split(' ').find(x=>x.startsWith('tip-'));return id?document.getElementById(id):null;};
 const bubble=document.createElement('div');bubble.className='tip-bubble';bubble.setAttribute('role','tooltip');bubble.hidden=true;document.body.append(bubble);
 const showTip=(t,above=false)=>{bubble.textContent=t.dataset.tip;bubble.hidden=false;const r=t.getBoundingClientRect(),b=bubble.getBoundingClientRect();let y=above&&r.top-b.height-8>=8?r.top-b.height-8:r.bottom+8;if(y+b.height>innerHeight-8)y=r.top-b.height-8;bubble.style.left=Math.min(innerWidth-b.width-8,Math.max(8,r.left+r.width/2-b.width/2))+'px';bubble.style.top=Math.max(8,y)+'px';};
 const tipOf=e=>e.target instanceof Element?e.target.closest('.tip'):null;
 document.addEventListener('pointerover',e=>{const t=tipOf(e);if(t)showTip(t);});document.addEventListener('pointerout',e=>{if(tipOf(e)&&document.activeElement!==tipOf(e))bubble.hidden=true;});
 // Tabbing onto a described field shows its tip above the label, clear of the field itself.
 let keyboard=false;document.addEventListener('keydown',e=>{if(e.key==='Tab')keyboard=true;},true);document.addEventListener('pointerdown',()=>{keyboard=false;},true);
 document.addEventListener('focusin',e=>{const own=tipOf(e),t=own||(keyboard&&tipFor(e.target));if(t)showTip(t,!own);else bubble.hidden=true;});document.addEventListener('focusout',e=>{if(tipOf(e)||tipFor(e.target))bubble.hidden=true;});
 // A tap on a tip inside a label or summary shows the tip instead of focusing the field or toggling the section.
 document.addEventListener('click',e=>{const t=tipOf(e);if(t){e.preventDefault();t.focus();showTip(t);}});
 document.addEventListener('scroll',()=>{bubble.hidden=true;},true);
 // Default seam method: cards drive the hidden #seamJoint select through its normal input event.
 // The cards are made once and then only re-checked, so keyboard focus stays on the card just chosen.
 const seam=$('seamJoint'),seamCards=$('seam-cards');
 const drawSeamCards=()=>{if(!seamCards.children.length)seamCards.replaceChildren(...CONNECTION_METHODS.map((name,i)=>card(name,seamIcon(i),false,SEAM_TITLES[i],()=>{if(Number(seam.value)===i)return;seam.value=String(i);seam.dispatchEvent(new Event('input',{bubbles:true}));})));
  [...seamCards.children].forEach((b,i)=>b.setAttribute('aria-checked',String(Number(seam.value)===i)));rove(seamCards);seamNotice();};
 arrowKeys(seamCards);
 // Seam overrides beat the default cards; say so under them, with a way to edit or clear them.
 function seamNotice(){const c=state.connections,m=getModel(),catalog=m?connectionCatalog(m).filter(j=>j.family==='radial'||j.family==='ring'):[],fam=Object.keys(c.families||{}),own=Object.keys(c.joints||{}).length;
  const n=catalog.length?catalog.filter(j=>c.families?.[j.family]||c.joints?.[j.id]).length:fam.length+own;$('seam-overrides').hidden=!n;
  if(n)$('seam-overrides-text').textContent=`${n} ${n===1?'seam is':'seams are'} set individually${fam.length?` (${fam.map(f=>f==='ring'?'every ring → ring seam':'every petal → petal seam').join(', ')})`:''} and ${n===1?'ignores':'ignore'} this default.`;}
 $('seam-overrides-edit').onclick=()=>{const fam=Object.keys(state.connections.families||{});$('connection-scope').value=fam.includes('ring')&&!fam.includes('radial')?'ring':fam.length?'radial':'specific';refresh();dialog.showModal();};
 $('seam-overrides-clear').onclick=()=>{if(!confirm('Clear the individual seam choices? Every seam then uses the default above.'))return;delete state.connections.families;delete state.connections.joints;changed();refresh();drawSeamCards();};
 // Print inclusion toggles sit with their accessory.
 for(const input of document.querySelectorAll('[data-accessory]'))input.onchange=e=>{state.print[e.target.dataset.accessory]=e.target.checked;changed();};
 // Individual joints editor
 const dialog=$('connections-editor');$('open-overrides').onclick=()=>{refresh();dialog.showModal();};$('overrides-close').onclick=()=>dialog.close();
 function current(){const scope=$('connection-scope').value,catalog=getModel()?connectionCatalog(getModel()):[],j=catalog.find(j=>j.id===$('connection-joint').value),family=scope==='specific'?j?.family:scope;return{scope,j,family,catalog};}
 const hubFamily=f=>f==='root'||f==='mount';
 function choice(){const {scope,j,family}=current();if(scope==='specific')return Number(j?.method);if(hubFamily(family))return Number($(family==='root'?'rootThrough':'mountThrough').value);return state.connections.families?.[family]?.seamJoint??Number(seam.value);}
 // A family choice that is the default, or the petal seam family of a one-ring dish (which is every seam), sets the
 // default itself instead of an override that would hide the default cards.
 function commit(method,size){const {scope,j,family}=current(),c=state.connections,m=getModel();
  if(hubFamily(family)){if(scope==='specific')(c[family==='root'?'roots':'mounts']??={})[j.index]=method;else{$(family==='root'?'rootThrough':'mountThrough').value=method;delete c[family==='root'?'roots':'mounts'];}}
  else if(scope==='specific')(c.joints??={})[j.id]={seamJoint:method,seamBolt:size};
  else if(family==='radial'&&m&&m.layout.rows<2){seam.value=String(method);$('seamBolt').value=String(size);delete c.families?.radial;}
  else if(method===Number(seam.value)&&size===Number($('seamBolt').value))delete c.families?.[family];
  else(c.families??={})[family]={seamJoint:method,seamBolt:size};
  if(c.families&&!Object.keys(c.families).length)delete c.families;
  changed();refresh();drawSeamCards();
 }
 const hasOverride=({scope,j,family})=>scope==='specific'?j&&(hubFamily(family)?state.connections[family==='root'?'roots':'mounts']?.[j.index]!==undefined:Boolean(state.connections.joints?.[j.id])):hubFamily(family)?false:Boolean(state.connections.families?.[family]);
 let cardSet='';
 function refresh(){const now=current(),{scope,j,family,catalog}=now,hub=hubFamily(family),active=choice(),cards=$('connection-cards'),set=hub?family+$(family==='root'?'rootBolt':'mountBolt').value:'seam';
  $('specific-choice').hidden=scope!=='specific';$('connection-preview').hidden=scope!=='specific';$('connection-size-label').hidden=hub;
  // a whole hub family has no default above it: its choice is the default
  $('connection-inherit').hidden=hub&&scope!=='specific';$('connection-inherit').disabled=!hasOverride(now);
  if(set!==cardSet||!cards.children.length){cardSet=set;cards.replaceChildren(...(hub?['Heat-set insert','Through bolt'].map(x=>`${x} · M${$(family==='root'?'rootBolt':'mountBolt').value}`):CONNECTION_METHODS).map((name,i)=>card(name,hub?hubIcon(i):seamIcon(i),false,hub?'':SEAM_TITLES[i],()=>commit(i,Number($('connection-size').value)))));}
  [...cards.children].forEach((b,i)=>b.setAttribute('aria-checked',String(i===active)));rove(cards);
  if(!hub)$('connection-size').value=scope==='specific'?j?.size??3:state.connections.families?.[family]?.seamBolt??$('seamBolt').value;
  const summary=$('connection-summary');summary.classList.remove('connection-summary-error');
  summary.textContent=hub?`M${$(family==='root'?'rootBolt':'mountBolt').value} interface. Through bolts take a nut and washers.`:scope==='specific'?'Changes both sides of this joint.':family==='radial'&&getModel()?.layout.rows<2?'With one ring this is every seam, so it sets the default seam method.':'A joint set on its own keeps its own choice.';
  const overrides=Object.values(state.connections).reduce((n,v)=>n+Object.keys(v).length,0);$('connections-overview').textContent=`${catalog.length} joints · ${overrides?overrides+(overrides===1?' override':' overrides'):'all use the defaults above'}`;
 }
 arrowKeys($('connection-cards'));
 $('connection-preview').onclick=()=>{const {j,scope}=current();if(scope==='specific'&&j){showJoint?.(j);dialog.close();}};$('connection-scope').onchange=refresh;$('connection-joint').onchange=refresh;$('connection-size').onchange=()=>commit(choice(),Number($('connection-size').value));
 $('connection-inherit').onclick=()=>{const {scope,j,family}=current();if(scope==='specific')delete state.connections[family==='root'?'roots':family==='mount'?'mounts':'joints']?.[hubFamily(family)?j.index:j.id];else if(hubFamily(family))delete state.connections[family==='root'?'roots':'mounts'];else delete state.connections.families?.[family];changed();refresh();drawSeamCards();};
 $('connection-clear').onclick=()=>{if(Object.keys(state.connections).length&&!confirm('Clear every individual joint choice? Every joint then uses the defaults.'))return;state.connections={};changed();refresh();drawSeamCards();};
 drawSeamCards();
 return {
  seamChanged:drawSeamCards,
  // After each build: drop single-joint and petal-root choices whose joint the new dish does not have (a change of petal
  // or ring count moves the joints; f/D does not), and return how many went.
  update(){const m=getModel(),select=$('connection-joint'),old=select.value,catalog=connectionCatalog(m),ids=new Set(catalog.map(j=>j.id)),c=state.connections;let dropped=0;
   for(const [group,key]of [['joints',id=>id],['roots',i=>'root:'+i]])for(const id of Object.keys(c[group]||{}))if(!ids.has(key(id))){delete c[group][id];dropped++;}
   for(const group of ['joints','roots'])if(c[group]&&!Object.keys(c[group]).length)delete c[group];
   select.replaceChildren(...catalog.map(j=>new Option(j.label,j.id)));if([...select.options].some(o=>o.value===old))select.value=old;
   const ring=$('connection-scope').querySelector('[value=ring]');ring.hidden=m.layout.rows<2;if(ring.hidden&&$('connection-scope').value==='ring')$('connection-scope').value='radial';
   // Show a print toggle only for accessories this design has.
   for(const el of document.querySelectorAll('.accessory-toggle')){const kind=el.dataset.for;el.hidden=!m.parts.some(p=>p.kind===kind);}
   drawSeamCards();refresh();return dropped;},
  // A build that failed after a change made in the joints dialog says why there, not only behind the dialog.
  failed(text){if(!dialog.open)return;const summary=$('connection-summary');summary.textContent=text;summary.classList.add('connection-summary-error');},
  // Bring a field into view: its tab, any closed section around it, then focus.
  reveal(el){const panel=el?.closest('[data-category-panel]');if(!panel)return;show(panel.dataset.categoryPanel);for(let d=el.closest('details');d;d=d.parentElement?.closest('details'))d.open=true;el.focus();el.scrollIntoView?.({block:'center'});},
  reset(){state.connections={};state.print={feed:true,mount:true,clip:true,lever:true};for(const i of document.querySelectorAll('[data-accessory]'))i.checked=true;},
  // A loaded configuration's joint overrides and print selection.
  restore({connections,printSelection}){state.connections=structuredClone(connections||{});state.print={feed:true,mount:true,clip:true,lever:true,...printSelection};for(const i of document.querySelectorAll('[data-accessory]'))i.checked=state.print[i.dataset.accessory]!==false;}
 };
}
