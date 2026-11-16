// CSRF: svaki non-GET fetch automatski dobiva X-CSRF-Token iz csrf_token cookieja
(function(){
  const origFetch = window.fetch;
  function csrfToken(){
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? m[1] : '';
  }
  window.fetch = function(input, init){
    init = init || {};
    const method = (init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const t = csrfToken();
      if (t) {
        if (init.headers instanceof Headers) init.headers.set('X-CSRF-Token', t);
        else init.headers = Object.assign({}, init.headers, {'X-CSRF-Token': t});
      }
    }
    return origFetch.call(this, input, init);
  };
})();

let D=null, OPT=null, CH={};

// ---- NAV ----
function goTo(name,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='usporedba') renderUsporedba();
  if(name==='financije') loadFinancije();
  if(name==='racuni') loadRacuni();
  if(name==='dug') loadDug();
  if(name==='isplativost') loadIsplativost();
  if(name==='optimalno'&&!OPT) loadOpt();
  if(name==='povijest'&&!POV) { brziIzbor('30dana'); }
  if(name==='postavke') { loadPostavke(); loadOmmInfo(); }
  if(name==='pregled') { loadPeakSnaga(); loadPlantInfo(); loadCijene(); loadWeather(); loadEnergyFlow(); loadPowerLimit(); loadRacunDug(); }
}

// ---- HELPERS ----
const f0=v=>v==null?'—':(+v).toFixed(0);
const f1=v=>v==null?'—':(+v).toFixed(1);
const f2=v=>v==null?'—':(+v).toFixed(2);
const eur=v=>v==null?'—':(+v).toFixed(2)+' €';
const sgn=v=>v>=0?'+':'';
// Lokalni datum - ispravlja timezone (browser je u HR, server u UTC)
const localDate=(d=new Date())=>{
  const y=d.getFullYear(), m=String(d.getMonth()+1).padStart(2,'0'), dd=String(d.getDate()).padStart(2,'0');
  return `${y}-${m}-${dd}`;
};
const today=()=>localDate();
// Tjedan od ponedjeljka (ISO tjedan)
const wkS=()=>{const d=new Date(); const day=d.getDay()||7; d.setDate(d.getDate()-day+1); return localDate(d);};
// Zadnjih 7 dana
const last7=()=>{const d=new Date(); d.setDate(d.getDate()-6); return localDate(d);};
const mS=()=>{const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-01`;};
const yS=()=>new Date().getFullYear()+'-01-01';
const sumK=(arr,k)=>arr.reduce((a,b)=>a+(+b[k]||0),0);
const avgK=(arr,k)=>{const v=arr.filter(r=>r[k]!=null).map(r=>+r[k]);return v.length?v.reduce((a,b)=>a+b)/v.length:0;};
const set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
// HTML-escape za sve ne-numeričke vrijednosti koje idu u innerHTML (XSS)
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---- ZAJEDNIČKI HELPERI ZA NOVAC, STANJA SEKCIJA I DOHVAT ----
// novac(v)        iznos u EUR po novoj konvenciji predznaka: račun/trošak je POZITIVAN
//                 ("63,39 €"), otkup/prihod također pozitivan (boju daje pozivatelj, npr.
//                 klasa c-green); negativan iznos znači stvarni pretplatu/odobrenje i
//                 ispisuje se s minusom. null/NaN → "—". Stari eur() ostaje za postojeći kod.
// stanje(cilj,s,tekst)  stanje asinkrone sekcije; cilj = id ili element (kartica, grid, tbody):
//                 'ucitavam' → prigušen red "Učitavam…", sadržaj izblijedi
//                 'greska'   → red "Podaci trenutno nisu dostupni"
//                 'prazno'   → sekcija se sažme u JEDAN prigušeni red (tekst obavezan)
//                 'ok'       → makne red i klase
//                 Ne dira postojeću djecu (id-jevi ostaju); u <tbody> red je <tr><td colspan>.
// dohvati(url,cilj)  fetch JSON-a; cilj = id/element ili niz njih. Pri PRVOM dohvatu pokaže
//                 'ucitavam' (kasniji refresh ne trepće), na uspjeh 'ok', na grešku 'greska'
//                 i vrati null — pozivatelj samo provjeri `if(!d) return;`.
// otvoriTab(ime)  goTo() po imenu taba (tabovi nose data-tab).
const novac=v=>(v==null||isNaN(+v))?'—':(+v).toLocaleString('hr-HR',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
const ST_TEKST={ucitavam:'Učitavam…',greska:'Podaci trenutno nisu dostupni',prazno:'Nema podataka'};
const _stEl=c=>typeof c==='string'?document.getElementById(c):c;
function stanje(cilj,s,tekst){
  const el=_stEl(cilj);
  if(!el) return;
  el.classList.remove('st-ucitavam','st-greska','st-prazno');
  const txt=tekst||ST_TEKST[s]||'';
  if(el.tagName==='TBODY'){
    // tablica: red poruke zamjenjuje retke (pozivatelj ih ionako ponovno iscrta)
    if(s==='ok'){ el.querySelectorAll('tr.st-red').forEach(r=>r.remove()); return; }
    el.innerHTML='<tr class="st-red"><td colspan="99" class="st-linija">'+esc(txt)+'</td></tr>';
    return;
  }
  let l=el.querySelector(':scope > .st-linija');
  if(s==='ok'){ if(l) l.remove(); return; }
  if(!l){
    l=document.createElement('div'); l.className='st-linija';
    const naslov=s==='prazno'?null:el.querySelector(':scope > .card-title, :scope > h3');
    if(naslov) naslov.after(l); else el.prepend(l);
  }
  l.textContent=txt;
  el.classList.add('st-'+s);
}
async function dohvati(url,cilj){
  const ciljevi=(cilj==null?[]:Array.isArray(cilj)?cilj:[cilj]).map(_stEl).filter(Boolean);
  ciljevi.forEach(e=>{ if(!e.dataset.ucitano) stanje(e,'ucitavam'); });
  try{
    const r=await fetch(url,{credentials:'same-origin'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    ciljevi.forEach(e=>{ e.dataset.ucitano='1'; stanje(e,'ok'); });
    return d;
  }catch(e){
    console.error(url,e);
    ciljevi.forEach(e=>stanje(e,'greska'));
    return null;
  }
}
function otvoriTab(ime){
  const t=document.querySelector('.tab[data-tab="'+ime+'"]');
  if(t) goTo(ime,t);
}
// 'YYYY-MM-DD' → '25.11.2026.'
const datumHr=d=>{const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(d||'');return m?`${+m[3]}.${+m[2]}.${m[1]}.`:'—';};

// ---- MULTI-OMM ----
let selectedOMM = localStorage.getItem('omm') || '';
// Dodaj ?omm= na URL ako je odabrano specifično mjerno mjesto
function withOmm(url){
  if(!selectedOMM) return url;
  return url + (url.includes('?') ? '&' : '?') + 'omm=' + encodeURIComponent(selectedOMM);
}
async function loadOmmList(){
  try{
    const r = await fetch('/api/omm', {credentials:'same-origin'});
    const d = await r.json();
    const sel = document.getElementById('ommSelect');
    if(!sel) return;
    const list = d.omm || [];
    // Selektor ima smisla samo uz ≥2 mjerna mjesta
    if(list.length < 2){ sel.style.display='none'; selectedOMM=''; return; }
    // Validiraj spremljeni izbor
    if(selectedOMM && !list.some(o=>o.id===selectedOMM)){ selectedOMM=''; localStorage.removeItem('omm'); }
    sel.innerHTML = list.map(o=>{
      const ime = o.naziv || o.id;
      const adr = o.adresa ? ` · ${o.adresa}` : '';
      return `<option value="${esc(o.id)}">${esc(ime)}${esc(adr)} (${esc(o.id)})</option>`;
    }).join('');
    sel.value = selectedOMM || list[0].id;
    sel.style.display = '';
  }catch(e){console.error('omm list', e);}
}
function changeOmm(){
  const sel = document.getElementById('ommSelect');
  selectedOMM = sel.value || '';
  localStorage.setItem('omm', selectedOMM);
  // Osvježi poglede koji ovise o OMM-u
  load();
  if(window._usporedbaRows!==undefined) renderUsporedba();
  if(typeof POV!=='undefined' && POV) loadPovijest();
}

// ---- LOAD ----
// Sekcije Pregleda koje se pune iz /api/data
const PREGLED_DATA_CILJEVI=['card-danas','pg-kpi','pg-grafovi','card-kumulativ'];
// Sekcije Pregleda s vlastitim endpointom (pune se pri otvaranju taba). Auto-refresh ponovi
// samo one koje su ostale u stanju greške, da jedan neuspjeli zahtjev ne ostane do reloada —
// ali najviše jednom u 5 min po sekciji (vrijeme zove vanjski servis s timeoutom od 8 s).
const PREGLED_SEKCIJE=[['card-peak',loadPeakSnaga],['card-postrojenje',loadPlantInfo],['card-cijene',loadCijene],
  ['card-vrijeme',loadWeather],['card-powlimit',loadPowerLimit]];
const SEKCIJA_RETRY_MS=300000, _sekcijaPokusaj={};
async function load(){
  loadStatus();
  // sažetak računa se rijetko mijenja — osvježi ga uz auto-refresh najviše svakih 10 min
  if(Date.now()-_sazetakTs>600000) loadRacunDug();
  const sad=Date.now();
  PREGLED_SEKCIJE.forEach(([id,fn])=>{
    const e=document.getElementById(id);
    if(!e||!e.classList.contains('st-greska')) return;
    if(sad-(_sekcijaPokusaj[id]||0)<SEKCIJA_RETRY_MS) return;
    _sekcijaPokusaj[id]=sad; fn();
  });
  const d=await dohvati(withOmm('/api/data'),PREGLED_DATA_CILJEVI);
  if(!d || !Array.isArray(d.dnevna)){
    if(d) PREGLED_DATA_CILJEVI.forEach(c=>stanje(c,'greska'));
    set('lastUpdate','nema veze');
    return;
  }
  const bezCijena=!D?.efektivne_cijene;
  D=d;
  try{ renderPregled(); }catch(e){ console.error(e); }
  // Povijest otvorena prije prvog /api/data: KPI "Okvirni trošak" je ostao bez cijena — docrtaj
  if(bezCijena && POV){ try{ renderPovijest(); }catch(e){ console.error(e); } }
  set('lastUpdate', new Date().toLocaleTimeString('hr-HR'));
}

// ---- HEADER: svježina podataka (/api/stats/status) ----
async function loadStatus(){
  const box=document.getElementById('hdr-status');
  if(!box) return;
  const d=await dohvati('/api/stats/status');
  if(!d){
    // ne briši zadnje poznato stanje zbog jednog neuspjelog refresha
    if(!box.children.length) box.innerHTML='<span class="spill" title="Status podataka trenutno nije dostupan"><b>Status</b> nedostupan</span>';
    return;
  }
  const danas=today(), jucer=(()=>{const x=new Date();x.setDate(x.getDate()-1);return localDate(x);})();
  const danTxt=dt=>dt===danas?'danas':dt===jucer?'jučer':`${+dt.slice(8,10)}.${+dt.slice(5,7)}.`;
  const pills=[];
  // HEP: zadnji_uzorak nosi oznaku KRAJA intervala — ponoć znači "cijeli zadnji_dan"
  const h=d.hep||{};
  if(h.zadnji_uzorak){
    const vr=h.zadnji_uzorak.slice(11,16);
    const dan=h.zadnji_dan||h.zadnji_uzorak.slice(0,10);
    const txt=vr==='00:00'?`do ${danTxt(dan)}`:`do ${danTxt(h.zadnji_uzorak.slice(0,10))} ${vr}`;
    pills.push({k:h.ok?'ok':(h.kasni_h>=96?'bad':'warn'),n:'HEP',t:txt,
      title:`Zadnje HEP 15-min očitanje: ${h.zadnji_uzorak.replace('T',' ')} (kasni ${f0(h.kasni_h)} h). HEP podaci inače stižu s oko 1 dan zakašnjenja.`});
  }else pills.push({k:'bad',n:'HEP',t:'nema podataka',title:'U bazi nema HEP 15-min očitanja'});
  const s=d.sma||{};
  if(s.zadnji){
    const m=s.kasni_min;
    const prije=m==null?'?':m<60?`prije ${m} min`:m<2880?`prije ${Math.round(m/60)} h`:`prije ${Math.round(m/1440)} d`;
    pills.push({k:s.ok?'ok':(m>=120?'bad':'warn'),n:'SMA',t:s.ok?'uživo':prije,
      title:`Zadnji SMA uzorak: ${s.zadnji.replace('T',' ')}`});
  }else pills.push({k:'bad',n:'SMA',t:'nema podataka',title:'U bazi nema SMA live uzoraka'});
  const izvor=d.cijene_izvor||'';
  pills.push({k:(izvor.startsWith('račun')?'ok':'')+' spill-cij',n:'Cijene',t:izvor||'—',
    title:'Odakle su aktivne jedinične cijene'+(d.model?` · model: ${d.model}`:'')+'. Detalji: Pregled → Aktivne cijene.'});
  const sy=d.sync||{};
  if(sy.greske_24h>0) pills.push({k:'warn',n:'Sync',t:`${sy.greske_24h} grešaka / 24 h`,
    title:'Neuspjele sinkronizacije u zadnja 24 h'+(sy.zadnji_ok?` · zadnja uspješna: ${sy.zadnji_ok.replace('T',' ')}`:'')});
  box.innerHTML=pills.map(p=>`<span class="spill ${p.k}" title="${esc(p.title)}"><b>${esc(p.n)}</b><span class="spill-t"> · ${esc(p.t)}</span></span>`).join('');
}

// ---- PREGLED: kartica "Račun i dug" (/api/stats/sazetak) ----
// window.SAZETAK drži zadnji odgovor (koristi ga i tab Financije).
let _sazetakTs=0;
async function loadRacunDug(){
  _sazetakTs=Date.now();
  const d=await dohvati('/api/stats/sazetak','card-racun-dug');
  if(!d){ _sazetakTs=0; return; }
  window.SAZETAK=d;
  renderRacunDug(d);
}
function renderRacunDug(S){
  const box=document.getElementById('rd-body');
  if(!box) return;
  const r=S.racun, m=S.mjesec||{};
  // dug + dospijeće
  let dugHtml, dospHtml;
  if(!r){
    dugHtml=`<div class="rd-val c-muted">—</div><div class="rd-sub">Nema uvezenih računa — dodaj ga u tabu Računi</div>`;
    dospHtml=`<div class="rd-val sm c-muted">—</div>`;
  }else{
    const dugKl=r.dug==null?'c-muted':r.dug>0.005?'c-grid':'c-green';
    // dug je stanje NA DAN izdavanja računa i ne sadrži sam taj račun; ako zadnji račun nema
    // podatak o dugu, server ga (i dospijeće) uzima sa zadnjeg računa koji ga ima (dug_period)
    const dugP=r.dug_period||r.period;
    const dugNap=(r.dug!=null&&r.dug<=0.005?'nema starog duga · ':'')
      +(dugP!==r.period?`dug prema računu ${esc(dugP)} · zadnji račun ${esc(r.period)}: ${novac(r.iznos)}`
                       :`po računu ${esc(r.period)} · iznos računa ${novac(r.iznos)}`);
    dugHtml=`<div class="rd-val ${dugKl}">${novac(r.dug)}</div>
      <div class="rd-sub">${dugNap}</div>`;
    const n=r.dana_do_dospijeca;
    let rok='', rokKl='c-muted';
    if(n!=null){
      if(n<0){ rok=`kasni ${-n} ${-n===1?'dan':'dana'}`; rokKl='c-grid'; }
      else if(n===0){ rok='dospijeva danas'; rokKl='c-warn'; }
      else { rok=`za ${n} ${n===1?'dan':'dana'}`; rokKl=n<=3?'c-warn':'c-muted'; }
    }
    // račun dospijeva i kad starog duga nema — odbrojavanje se uvijek prikazuje
    dospHtml=`<div class="rd-val sm">${esc(datumHr(r.datum_dospijeca))}</div>
      <div class="rd-sub ${rokKl}">${esc(rok)}${r.dug_period&&r.dug_period!==r.period?` · račun ${esc(r.dug_period)}`:''}</div>`;
  }
  // procjena tekućeg mjeseca (isti brojevi kao Financije → Procjena tekućeg mjeseca)
  let procHtml;
  if(m.ima_podataka){
    const pct=m.n_dana_mjesec?Math.min(100,Math.round(100*m.dani_s_podacima/m.n_dana_mjesec)):0;
    procHtml=`<div class="rd-val">${novac(m.procjena_eur)}</div>
      <div class="rd-sub">do sada ${novac(m.do_sada_eur)} · ${f0(m.dani_s_podacima)}/${f0(m.n_dana_mjesec)} dana s podacima</div>
      <div class="aut-bar" style="margin-top:6px"><div class="aut-fill" style="width:${pct}%"></div></div>`;
  }else{
    procHtml=`<div class="rd-val c-muted">—</div><div class="rd-sub">još nema HEP podataka za ovaj mjesec</div>`;
  }
  const otk=+m.otkup_procjena_eur||0;
  const otkHtml=otk>0
    ? `<div class="rd-val sm c-green">${novac(otk)}</div><div class="rd-sub">neto za platiti ≈ ${novac(m.neto_procjena_eur)}</div>`
    : `<div class="rd-val sm c-muted">—</div><div class="rd-sub">ovaj mjesec se ne očekuje višak</div>`;
  box.innerHTML=`<div class="rd-grid">
      <div><div class="rd-lbl">Otvoreni dug</div>${dugHtml}</div>
      <div><div class="rd-lbl">Dospijeće</div>${dospHtml}</div>
      <div><div class="rd-lbl">Procjena računa · ${esc(m.mjesec||'tekući mjesec')}</div>${procHtml}</div>
      <div><div class="rd-lbl">Očekivani otkup viška</div>${otkHtml}</div>
    </div>
    <div class="rd-foot">
      <span>${esc(S.model_naziv||'')}${S.cijene_izvor?' · cijene: '+esc(S.cijene_izvor):''}</span>
      <button class="btn-link" type="button" id="rd-fin">Financije →</button>
    </div>`;
  document.getElementById('rd-fin').addEventListener('click',()=>otvoriTab('financije'));
}

const OPT_CILJEVI=['card-opt-sol','card-opt-pot','card-opt-aut','card-opt-prep'];
async function loadOpt(){
  const d=await dohvati('/api/stats/optimalno',OPT_CILJEVI);
  if(!d) return;
  if(!Array.isArray(d.hep_satno)||!Array.isArray(d.sma_satno)){ OPT_CILJEVI.forEach(c=>stanje(c,'greska')); return; }
  OPT=d;
  try{ renderOpt(); }catch(e){ console.error(e); OPT_CILJEVI.forEach(c=>stanje(c,'greska')); }
}

// ---- PREGLED ----
function renderPregled(){
  const hasSma=D.sma_live!=null;
  document.getElementById('noSmaNotice').style.display=hasSma?'none':'block';

  // SMA-trenutno widget je uklonjen — Energy flow ga zamijenio
  if(hasSma) loadEnergyFlow();

  // SMA — danas (live agregirano iz sma_live)
  const sT = D.sma_today || D.sma_dnevna.find(r=>r.datum===today()) || {};
  set('d-sma-p',  sT.pv_generation_kwh != null     ? f1(sT.pv_generation_kwh)     : '—');
  set('d-sma-fee',sT.feed_in_kwh != null           ? f1(sT.feed_in_kwh)           : '—');
  set('d-sma-con',sT.total_consumption_kwh != null ? f1(sT.total_consumption_kwh) : '—');
  set('d-sma-grd',sT.grid_consumption_kwh != null  ? f1(sT.grid_consumption_kwh)  : '—');

  // KPI grupa "Danas" — izvedeno iz istog SMA dnevnog zbroja (PV danas je već u kartici gore)
  const pvD=sT.pv_generation_kwh, feD=sT.feed_in_kwh, grD=sT.grid_consumption_kwh, toD=sT.total_consumption_kwh;
  if(pvD!=null && feD!=null){
    const self=Math.max(0,pvD-feD);
    set('k-d-self',f1(self));
    set('k-d-self-sub',pvD>0?`kWh · ${f0(100*self/pvD)}% proizvodnje`:'kWh');
  }else{ set('k-d-self','—'); set('k-d-self-sub','kWh'); }
  // autarkija = 1 − iz mreže / potrošnja kuće
  set('k-d-aut',(grD!=null && toD>0)?f0(Math.max(0,Math.min(1,1-grD/toD))*100)+'%':'—');
  const netoEl=document.getElementById('k-d-neto');
  if(netoEl){
    if(grD!=null && feD!=null){
      const n=feD-grD;
      netoEl.textContent=(n>=0?'+':'')+f1(n);
      netoEl.className='kpi-val '+(n>=0?'c-feed':'c-grid');
      set('k-d-neto-sub',n>=0?'kWh · više predano nego uzeto':'kWh · više uzeto nego predano');
    }else{ netoEl.textContent='—'; netoEl.className='kpi-val'; set('k-d-neto-sub','kWh'); }
  }

  // HEP — zadnji dan u bazi (može kasniti 24h)
  const danas=today();
  const hepRows = D.dnevna.filter(r => r.datum < danas || (r.kwh_plus||0) > 0 || (r.kwh_minus||0) > 0);
  const dH = hepRows.length ? hepRows[hepRows.length-1] : null;
  // prefer "danas" ako postoji s podacima, inače najnoviji raspoloživi
  const dToday = D.dnevna.find(r=>r.datum===danas && ((r.kwh_plus||0)>0 || (r.kwh_minus||0)>0));
  const hepShow = dToday || dH;
  set('d-hep-p', hepShow?f1(hepShow.kwh_plus):'—');
  set('d-hep-m', hepShow?f1(hepShow.kwh_minus):'—');
  if (hepShow) {
    const n=(hepShow.kwh_minus||0)-(hepShow.kwh_plus||0);
    const el=document.getElementById('d-hep-neto');
    if (el) {
      el.textContent=(n>=0?'+':'')+f1(n);
      el.className='fl-val '+(n>=0?'c-feed':'c-grid');
    }
    const dateEl = document.getElementById('d-hep-datum');
    if (dateEl) dateEl.textContent = hepShow.datum;
    const lbl = document.getElementById('d-hep-lbl');
    if (lbl) {
      lbl.textContent = hepShow.datum === danas ? '(danas, djelomično)' : '(' + hepShow.datum + ' — kasni)';
    }
  }

  const ws=wkS(), l7=last7(), ms=mS();
  const tjH=D.dnevna.filter(r=>r.datum>=ws),   tjS=D.sma_dnevna.filter(r=>r.datum>=ws);
  const d7H=D.dnevna.filter(r=>r.datum>=l7),   d7S=D.sma_dnevna.filter(r=>r.datum>=l7);
  const mjH=D.dnevna.filter(r=>r.datum>=ms),   mjS=D.sma_dnevna.filter(r=>r.datum>=ms);

  // Tjedan (od ponedjeljka)
  // HEP kasni ~1 dan: ponedjeljkom za ovaj tjedan još nema nijednog HEP dana → "—", ne 0
  set('k-tj-h',tjH.length?f1(sumK(tjH,'kwh_plus')):'—');
  set('k-tj-s',f1(sumK(tjS,'pv_generation_kwh')));
  set('k-tj-p',tjH.length?f1(sumK(tjH,'kwh_minus')):'—');
  set('k-tj-a',f0(tjS.length?avgK(tjS,'autarky_rate')*100:0)+'%');

  // Zadnjih 7 dana
  set('k-7d-h',f1(sumK(d7H,'kwh_plus')));
  set('k-7d-s',f1(sumK(d7S,'pv_generation_kwh')));
  set('k-7d-p',f1(sumK(d7H,'kwh_minus')));
  set('k-7d-a',f0(d7S.length?avgK(d7S,'autarky_rate')*100:0)+'%');
  // Mjesec
  set('k-mj-h',f1(sumK(mjH,'kwh_plus')));
  set('k-mj-s',f1(sumK(mjS,'pv_generation_kwh')));
  set('k-mj-p',f1(sumK(mjH,'kwh_minus')));
  set('k-mj-a',f0(mjS.length?avgK(mjS,'autarky_rate')*100:0)+'%');
  const all=D.sma_dnevna.filter(r=>r.pv_generation_kwh>0);
  set('k-pr-s',f1(all.length?sumK(all,'pv_generation_kwh')/all.length:0));

  // SMA sat se spaja s HEP satom po ključu (oba lokalni početak sata), ne po
  // indeksu — HEP kasni ~1 dan pa nizovi ne završavaju u istom satu.
  const smaPoSatu=new Map((D.sma_satna||[]).map(r=>[r.sat,r]));
  // Svih 7 dana (168 sati). Os X: na ponoći oznaka dana ("pon 16."), na podne "12h"; na uskom
  // grafu (mobitel) samo dan u tjednu ("pon") jer se sedam punih oznaka sudara. Ostale oznake
  // i linije mreže se preskaču. Tooltip nosi dan + sat.
  const sat7=D.satna.slice(-168);
  const danSat=ts=>{const d=new Date(ts);return isNaN(d)?ts.slice(5,10):d.toLocaleDateString('hr-HR',{weekday:'short'}).replace('.','')+' '+d.getDate()+'.';};
  mkChart('cSatni','line',sat7.map(r=>danSat(r.ts)+' '+r.ts.slice(11,16)),[
    {label:'HEP potrošnja (kWh)',data:sat7.map(r=>r.kwh_plus||0),borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,.07)',fill:true,tension:.4,pointRadius:0,borderWidth:2},
    {label:'SMA solar (kWh)',data:sat7.map(r=>{const m=smaPoSatu.get(r.ts);return m?(m.pv_w||0)/1000:null;}),spanGaps:false,borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.07)',fill:true,tension:.4,pointRadius:0,borderWidth:2},
  ],'kWh',{xTicks:{autoSkip:false,maxRotation:0,callback:function(v){
    const ts=sat7[v]&&sat7[v].ts; if(!ts) return null;
    const hh=ts.slice(11,13);
    // širina se čita s grafa pri svakom crtanju (prati i promjenu veličine prozora)
    const uski=this.chart.width<600;
    if(hh==='00') return uski?danSat(ts).split(' ')[0]:danSat(ts);
    return hh==='12'&&!uski?'12h':null;
  }}});

  const h30=D.dnevna.slice(-30);
  mkChart('cDnevni','bar',h30.map(r=>r.datum.slice(5)),[
    {label:'Potrošnja',data:h30.map(r=>r.kwh_plus||0),backgroundColor:'rgba(167,139,250,.65)',borderRadius:2},
    {label:'Predaja',data:h30.map(r=>r.kwh_minus||0),backgroundColor:'rgba(56,189,248,.65)',borderRadius:2},
    {label:'Solar',data:h30.map(r=>{const s=D.sma_dnevna.find(x=>x.datum===r.datum);return s?s.pv_generation_kwh||0:0;}),type:'line',borderColor:'#fbbf24',fill:false,tension:.4,pointRadius:2,borderWidth:2},
  ],'kWh');

  renderKumulativ();
}

// Kumulativ kWh za tekući mjesec — running sum po danima (1. → danas)
function renderKumulativ(){
  if(!document.getElementById('cKumulativ')) return;
  const ms=mS(), danas=today();
  const hep=D.dnevna.filter(r=>r.datum>=ms && r.datum<=danas).sort((a,b)=>a.datum<b.datum?-1:1);
  const labels=[], cP=[], cM=[], cS=[];
  let aP=0,aM=0,aS=0;
  for(const r of hep){
    aP+=(+r.kwh_plus||0);
    aM+=(+r.kwh_minus||0);
    const s=D.sma_dnevna.find(x=>x.datum===r.datum);
    aS+=s?(+s.pv_generation_kwh||0):0;
    labels.push(r.datum.slice(8));  // dan u mjesecu
    cP.push(+aP.toFixed(2)); cM.push(+aM.toFixed(2)); cS.push(+aS.toFixed(2));
  }
  mkChart('cKumulativ','line',labels,[
    {label:'Potrošnja iz mreže (kWh)',data:cP,borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,.10)',fill:true,tension:.3,pointRadius:0,borderWidth:2},
    {label:'Predaja (kWh)',data:cM,borderColor:'#38bdf8',backgroundColor:'rgba(56,189,248,.08)',fill:true,tension:.3,pointRadius:0,borderWidth:2},
    {label:'Solar (kWh)',data:cS,borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.08)',fill:true,tension:.3,pointRadius:0,borderWidth:2},
  ],'kWh');
}

// ---- USPOREDBA ----
const USP_CILJEVI=['u-kpi','card-u-graf','card-u-tablica'];
async function renderUsporedba(){
  const rows=await dohvati('/api/stats/usporedba',USP_CILJEVI);
  if(!rows) return;
  if(!Array.isArray(rows)){ USP_CILJEVI.forEach(c=>stanje(c,'greska')); return; }
  window._usporedbaRows = rows;
  try{ renderUsporedbaPeriod(); }catch(e){ console.error(e); USP_CILJEVI.forEach(c=>stanje(c,'greska')); }
}

// Period filter za Usporedba tab
window._usporedbaPeriod = window._usporedbaPeriod || 'month';

function _periodRange(period) {
  const danas = new Date();
  const y = danas.getFullYear(), m = danas.getMonth();
  let od, do_, label;
  if (period === 'day')   { od = today(); do_ = today(); label = 'danas'; }
  else if (period === 'week') {
    const d = new Date(danas);
    const dow = (d.getDay() + 6) % 7;   // ponedjeljak = 0
    d.setDate(d.getDate() - dow);
    od = localDate(d); do_ = today();
    label = 'ovaj tjedan (od pon)';
  }
  else if (period === 'month') {
    od = `${y}-${String(m+1).padStart(2,'0')}-01`;
    do_ = today();
    label = 'ovaj mjesec';
  }
  else if (period === 'year')  { od = `${y}-01-01`; do_ = today(); label = 'ova godina'; }
  else                          { od = '1900-01-01'; do_ = today(); label = 'sve raspoloživo'; }
  return { od, do_, label };
}

function setUsporedbaPeriod(period, btn) {
  window._usporedbaPeriod = period;
  document.querySelectorAll('[data-period]').forEach(b => {
    b.classList.remove('btn-primary');
    b.classList.add('btn-secondary');
  });
  if (btn) { btn.classList.remove('btn-secondary'); btn.classList.add('btn-primary'); }
  renderUsporedbaPeriod();
}

function renderUsporedbaPeriod() {
  const rows = window._usporedbaRows || [];
  const { od, do_, label } = _periodRange(window._usporedbaPeriod || 'month');
  const filtered = rows.filter(r => r.datum >= od && r.datum <= do_);
  // U razlike i zbrojeve ulaze SAMO dani s oba izvora (server: oba_izvora — današnji dan nikad,
  // jer HEP kasni ~1 dan). Nedostajuća vrijednost je null, ne 0.
  const usp = filtered.filter(r => r.oba_izvora);

  document.getElementById('u-period-info').textContent =
    `${od} → ${do_}  ·  ${filtered.length} dana`;
  document.getElementById('u-chart-title').textContent = `Usporedba HEP vs SMA — ${label}`;
  document.getElementById('u-table-title').textContent = `Tablica usporedbe — ${label}`;
  ['u-pred-lbl','u-pot-lbl','u-sol-lbl'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = el.textContent.replace(/·.*$/, '').trim() + ' · ' + label;
  });

  const razl=(a,b)=>usp.reduce((z,r)=>z+((+r[a])-(+r[b])),0);
  if(usp.length){
    const dR=razl('hep_predaja','sma_predaja'), dP=razl('hep_potrosnja','sma_mreza');
    set('u-pred-d',sgn(dR)+f1(dR)+' kWh');
    set('u-pot-d', sgn(dP)+f1(dP)+' kWh');
  }else{
    set('u-pred-d','—'); set('u-pot-d','—');
  }
  const sSol=filtered.filter(r=>r.sma_proizvodnja!=null);
  set('u-sol-t', sSol.length?f1(sumK(sSol,'sma_proizvodnja'))+' kWh':'—');
  const sAut=filtered.filter(r=>r.autarky_rate!=null);
  set('u-aut', sAut.length?f0(avgK(sAut,'autarky_rate')*100)+'%':'—');
  set('u-usp-info', !filtered.length ? ''
    : usp.length
      ? `Uspoređeno ${usp.length} od ${filtered.length} dana — razlike se računaju samo za dane u kojima postoje i HEP i SMA mjerenja mreže (današnji dan i dani bez SMA podataka o mreži su izostavljeni).`
      : `Nijedan od ${filtered.length} dana u razdoblju nema oba izvora (HEP podaci stižu s oko 1 dan zakašnjenja) — razlike se ne računaju.`);

  // Graf: svaka serija crta samo svoje stvarne vrijednosti (null → praznina). Današnji,
  // nedovršeni dan se ne crta uz ostale dane da linija na kraju ne "padne".
  const zaGraf = filtered.length>1 ? filtered.filter(r=>!r._realtime) : filtered;
  const nv=k=>zaGraf.map(r=>r[k]==null?null:+r[k]);
  const pr=zaGraf.length<=31?2:0;
  mkChart('cUsporedba','line',zaGraf.map(r=>r.datum.slice(5)),[
    {label:'HEP potrošnja',data:nv('hep_potrosnja'),borderColor:'#a78bfa',pointRadius:pr,borderWidth:2,tension:.3},
    {label:'SMA mreža',data:nv('sma_mreza'),borderColor:'#f87171',pointRadius:pr,borderWidth:1.5,tension:.3,borderDash:[4,2]},
    {label:'HEP predaja',data:nv('hep_predaja'),borderColor:'#38bdf8',pointRadius:pr,borderWidth:2,tension:.3},
    {label:'SMA solar',data:nv('sma_proizvodnja'),borderColor:'#fbbf24',pointRadius:pr,borderWidth:2,tension:.3},
  ],'kWh');

  const tb=document.getElementById('tUsporedba');
  if(!filtered.length){ stanje(tb,'prazno','Nema podataka za odabrano razdoblje'); return; }
  const dTd=d=>d==null?'<td class="c-muted">—</td>'
    :`<td class="${Math.abs(d)>2?(d>0?'neg':'pos'):'c-muted'}">${sgn(d)+f1(d)}</td>`;
  tb.innerHTML=filtered.slice().reverse().map(row=>{
    const oba=row.oba_izvora;
    const dp=oba?row.hep_potrosnja-row.sma_mreza:null;
    const dr=oba?row.hep_predaja-row.sma_predaja:null;
    return `<tr>
      <td>${esc(row.datum)}${row._realtime?' <span class="c-muted">· u tijeku</span>':''}</td>
      <td class="c-consume">${f1(row.hep_potrosnja)}</td><td>${f1(row.sma_mreza)}</td>${dTd(dp)}
      <td class="c-feed">${f1(row.hep_predaja)}</td><td>${f1(row.sma_predaja)}</td>${dTd(dr)}
      <td class="c-solar">${f1(row.sma_proizvodnja)}</td>
      <td>${row.autarky_rate!=null?f0(row.autarky_rate*100)+'%':'—'}</td>
    </tr>`;
  }).join('');
}

// ---- FINANCIJE ----
// Sve brojke na tabu dolaze gotove sa servera (model obračuna bira server) — ovdje nema
// računanja kWh × cijena.
function loadFinancije(){
  loadFinSazetak(); loadHepStanje(); loadProcjenaTrenutni(); loadVtNtTrosak(); loadFinMjesecni();
}

// Otvori Pregled i skroliraj na karticu "Aktivne cijene" (jedino mjesto s punom razradom cijena)
function otvoriCijene(){
  otvoriTab('pregled');
  const c=document.getElementById('card-cijene');
  if(c) c.scrollIntoView({behavior:'smooth',block:'start'});
}

// Zaglavlje taba + kartica "Godina do danas" iz /api/stats/sazetak (isti odgovor kao "Račun i dug")
async function loadFinSazetak(){
  const ciljevi=['fin-head','card-godina'];
  // svjež odgovor s Pregleda (≤10 min) — ne dohvaćaj ponovno
  if(window.SAZETAK && Date.now()-_sazetakTs<600000){
    ciljevi.forEach(c=>stanje(c,'ok'));
    renderFinSazetak(window.SAZETAK);
    return;
  }
  const d=await dohvati('/api/stats/sazetak',ciljevi);
  if(!d) return;
  window.SAZETAK=d;
  renderFinSazetak(d);
}
function renderFinSazetak(S){
  set('fin-model', S.model_naziv||'—');
  set('fin-izvor', S.cijene_izvor?'Cijene: '+S.cijene_izvor:'');
  const box=document.getElementById('fin-godina');
  if(!box) return;
  const g=S.godina||{}, z=S.zadnjih_12||{}, bs=S.bez_solara, m=S.mjesec||{};
  if(!g.n_mjeseci && !z.n_mjeseci){
    stanje('card-godina','prazno','Godina do danas — još nema računa ni HEP podataka');
    return;
  }
  const kart=(lbl,val,kl,sub)=>`<div class="fin"><div class="lbl">${lbl}</div><div class="val ${kl}">${val}</div><div class="sub">${sub}</div></div>`;
  const mjTxt=n=>`${f0(n||0)} mj.`;
  // bez_solara je null i kad godina još nema završenog mjeseca (cijeli siječanj), ne samo bez SMA
  const zavrsenih=(g.n_mjeseci||0)-(g.ukljucuje_procjenu_tekuceg?1:0);
  const ustedaHtml=bs
    ? kart('Ušteda zbog solara · procjena',novac(bs.usteda_eur),'pos',
        `bez solara ≈ ${novac(bs.godina_eur)}, sa solarom neto ${novac(bs.neto_eur)} · ${mjTxt(bs.n_mjeseci)} završenih`)
    : kart('Ušteda zbog solara','—','c-muted',zavrsenih>0
        ? 'nema podataka o proizvodnji (SMA) za ovu godinu'
        : 'još nema završenog mjeseca ove godine');
  box.innerHTML=`
    <div class="grupa-naslov">${esc(g.godina??'')}. do danas <small>${mjTxt(g.n_mjeseci)}${g.ukljucuje_procjenu_tekuceg?' · uključuje procjenu tekućeg mjeseca':''}</small></div>
    <div class="g-fin">
      ${kart('Računi',novac(g.racuni_eur),'','iznosi HEP računa (za platiti)')}
      ${kart('Otkup viška',novac(g.otkup_eur),'pos','isplata HEP-a za predani višak')}
      ${kart('Neto trošak',novac(g.neto_eur),'neu','računi − otkup')}
      ${ustedaHtml}
    </div>
    <div class="grupa-naslov">Zadnjih 12 završenih mjeseci <small>${mjTxt(z.n_mjeseci)}</small></div>
    <div class="g-fin">
      ${kart('Računi',novac(z.racuni_eur),'','stvarni račun gdje je uvezen, inače procjena')}
      ${kart('Otkup viška',novac(z.otkup_eur),'pos','isplata HEP-a za predani višak')}
      ${kart('Neto trošak',novac(z.neto_eur),'neu','računi − otkup')}
    </div>
    <div class="sitno">
      ${g.ukljucuje_procjenu_tekuceg?`U računima ${esc(g.godina)}. je i procjena za ${esc(m.mjesec||'tekući mjesec')} (${novac(m.procjena_eur)}). `:''}
      ${bs&&bs.napomena?esc(bs.napomena):''}
    </div>`;
}

// Mjesečni podaci (/api/stats/mjesecni) dijele Financije i Računi — jedan dohvat, cache u window.RACUNI
async function dohvatiMjesecne(cilj){
  if(window.RACUNI){ (Array.isArray(cilj)?cilj:[cilj]).forEach(c=>stanje(c,'ok')); return window.RACUNI; }
  const d=await dohvati('/api/stats/mjesecni',cilj);
  if(!d) return null;
  if(!Array.isArray(d.mjeseci)){ (Array.isArray(cilj)?cilj:[cilj]).forEach(c=>stanje(c,'greska')); return null; }
  window.RACUNI=d;
  return d;
}
// Boje serija — iste na grafu Financija i grafu Računa
const BOJA_RACUN_STVARNI='rgba(251,191,36,.85)', BOJA_RACUN_PROCJ='rgba(248,113,113,.6)', BOJA_OTKUP='rgba(52,211,153,.7)';
const MODEL_NAZIV={bijeli:'HEPI bijeli (net-metering)',standardni:'standardni dvotarifni'};

// Graf "Mjesečni računi i otkup" — zadnjih 18 mj.; stup računa je stvarni iznos ako je račun
// uvezen (žuto), inače procjena (crveno); otkup viška kao drugi stup
async function loadFinMjesecni(){
  const R=await dohvatiMjesecne('card-fin-mj');
  if(!R) return;
  const mj=R.mjeseci.slice(0,18).reverse();
  if(!mj.length){ stanje('card-fin-mj','prazno','Mjesečni računi i otkup — još nema podataka'); return; }
  const imaProj=mj.some(r=>r.procj_izvor==='projekcija');
  set('fin-mj-sub',`Zadnjih ${mj.length} mj. · stvarni račun gdje je uvezen, inače procjena`
    +(imaProj?' (tekući mjesec = projekcija do kraja mjeseca)':'')+' · otkup viška je zasebna isplata');
  mkChart('cFinMj','bar',mj.map(r=>r.mjesec),[
    {label:'Račun — stvarni (€)',data:mj.map(r=>r.stvarni_iznos),backgroundColor:BOJA_RACUN_STVARNI,borderRadius:3,stack:'racun'},
    {label:'Račun — procjena (€)',data:mj.map(r=>r.stvarni_iznos!=null?null:r.procj_iznos),backgroundColor:BOJA_RACUN_PROCJ,borderRadius:3,stack:'racun'},
    {label:'Otkup viška (€)',data:mj.map(r=>r.otkup_eur||0),backgroundColor:BOJA_OTKUP,borderRadius:3,stack:'otkup'},
  ],'€');
}

// saveTarifa: maknuto — tarifa se sad uređuje u Postavkama (VT/NT slider + .env)

// ---- RAČUNI ----
// window.RACUNI može već biti napunjen s taba Financije — zato se tab uvijek iscrta (iz cachea
// bez novog zahtjeva), a registri se dohvate samo jednom
let _registriUcitani=false;
async function loadRacuni(){
  if(!_registriUcitani) loadRegistri();
  const R=await dohvatiMjesecne(['r-kpi','tRacuniUnos','card-racuni-graf','tRacuni']);
  if(!R) return;
  try{ renderRacuni(); }catch(e){ console.error(e); }
}
// Nakon spremanja/brisanja računa: mjesečni podaci i sažetak (Pregled, Financije) više ne vrijede
function racuniPromijenjeni(){
  window.RACUNI=null;
  window.SAZETAK=null; _sazetakTs=0;
  loadRacuni();
}

async function loadRegistri(){
  const tb=document.getElementById('tRegistri');
  if(!tb) return;
  try{
    const r=await fetch('/api/stats/registri');
    const d=await r.json();
    const mj=d.mjeseci||[];
    _registriUcitani=true;
    if(!mj.length){
      tb.innerHTML='<tr><td colspan="8" class="c-muted" style="padding:14px;text-align:center">Nema registara — pokreni HEP sync (podaci se pune mjesečno)</td></tr>';
      return;
    }
    tb.innerHTML='';
    mj.forEach(m=>{
      tb.innerHTML+=`<tr>
        <td>${esc(m.mjesec)}</td>
        <td class="c-consume">${f1(m.vt_kwh)}</td>
        <td class="c-consume">${f1(m.nt_kwh)}</td>
        <td><strong>${f1(m.ukupno_kwh)}</strong></td>
        <td>${m.vt_perc!=null?f0(m.vt_perc)+'%':'—'}</td>
        <td class="c-feed">${f1(m.pred_vt_kwh)}</td>
        <td class="c-feed">${f1(m.pred_nt_kwh)}</td>
        <td><strong>${f1(m.predaja_kwh)}</strong></td>
      </tr>`;
    });
  }catch(e){
    console.error(e);
    tb.innerHTML='<tr><td colspan="8" class="c-muted" style="padding:14px;text-align:center">Greška pri učitavanju registara</td></tr>';
  }
}

async function addRacun(){
  const period = document.getElementById('r-period').value.trim();
  const iznos  = document.getElementById('r-iznos').value;
  if(!period||!iznos){ showToast('Unesite barem razdoblje i iznos!'); return; }

  // Konvertiraj MM/GGGG u YYYY-MM
  const parts = period.split('/');
  const periodIso = parts.length===2 ? `${parts[1]}-${parts[0].padStart(2,'0')}` : period;

  const extra = window._pdfExtra || {};
  await fetch('/api/racuni', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      period: periodIso,
      iznos: +iznos,
      kwh_vt: +document.getElementById('r-vt').value||null,
      kwh_nt: +document.getElementById('r-nt').value||null,
      kwh_plus: (+document.getElementById('r-vt').value||0) + (+document.getElementById('r-nt').value||0),
      kwh_minus: +document.getElementById('r-predaja').value||null,
      opskrba: +document.getElementById('r-opskrba').value||null,
      mreza: +document.getElementById('r-mreza').value||null,
      pdv: +document.getElementById('r-pdv-iznos').value||null,
      dug:             extra.dug ?? null,
      pretplata:       extra.pretplata ?? null,
      mjerna_mjernina: extra.mjerna_mjernina ?? null,
      kamata:          extra.kamata ?? null,
      datum_racuna:    extra.datum_racuna ?? null,
      datum_dospijeca: extra.datum_dospijeca ?? null,
      model_tarife:    extra.model_tarife ?? null,
      sifra_kupca:     extra.sifra_kupca ?? null,
      broj_racuna:     extra.broj_racuna ?? null,
    })
  });
  window._pdfExtra = {};
  showToast('Račun spremljen!');
  racuniPromijenjeni();
}

// PDF parser output → spremi proširena polja u skrivene globale za POST
window._pdfExtra = {};

async function uploadPdf(){
  const fileEl = document.getElementById('r-pdf');
  const status = document.getElementById('r-pdf-status');
  if (!fileEl.files.length) { status.textContent='Odaberi PDF prvo'; return; }
  status.textContent='Učitavam i parsiram...';
  const fd = new FormData();
  fd.append('file', fileEl.files[0]);
  try {
    const r = await fetch('/api/racuni/upload-pdf', {method:'POST', body: fd, credentials:'same-origin'});
    const d = await r.json();
    if (!d.ok) { status.textContent='Greška: ' + (d.error||'parser fail'); return; }
    const setIf = (id, v) => { if (v !== null && v !== undefined) document.getElementById(id).value = v; };
    setIf('r-period',   d.period);
    setIf('r-iznos',    d.iznos);
    setIf('r-vt',       d.kwh_vt);
    setIf('r-nt',       d.kwh_nt);
    setIf('r-predaja',  d.kwh_minus);
    setIf('r-opskrba',  d.opskrba);
    setIf('r-mreza',    d.mreza);
    setIf('r-pdv-iznos',d.pdv);
    // Proširena polja — nemaju input, drže se u globalu i šalju kroz addRacun
    window._pdfExtra = {
      dug:             d.dug,
      pretplata:       d.pretplata,
      mjerna_mjernina: d.mjerna_mjernina,
      kamata:          d.kamata,
      datum_racuna:    d.datum_racuna,
      datum_dospijeca: d.datum_dospijeca,
      model_tarife:    d.model_tarife,
      sifra_kupca:     d.sifra_kupca,
      broj_racuna:     d.broj_racuna,
    };
    const allFields = ['period','iznos','kwh_vt','kwh_nt','kwh_minus','opskrba','mreza','pdv',
                       'dug','pretplata','mjerna_mjernina','kamata','datum_racuna','datum_dospijeca','model_tarife'];
    const filled = allFields.filter(k => d[k] !== null && d[k] !== undefined).length;
    const extras = [];
    if (d.dug != null)             extras.push(`dug ${novac(d.dug)}`);
    if (d.datum_dospijeca)         extras.push(`dospijeće ${d.datum_dospijeca}`);
    if (d.model_tarife)            extras.push(d.model_tarife);
    status.innerHTML = `✓ Prepoznato ${filled}/${allFields.length} polja iz "${esc(d.filename)}".`
      + (extras.length ? ` <span style="color:var(--muted)">(${esc(extras.join(' · '))})</span>` : '')
      + ` Klikni "Spremi račun".`;
    if (d.warning) {
      status.innerHTML += `<div style="margin-top:8px;padding:8px 10px;border-radius:6px;`
        + `background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.4);color:#fbbf24;font-size:12px">`
        + `⚠️ ${esc(d.warning)}</div>`;
    }
  } catch(e) {
    status.textContent = 'Network error: ' + e;
  }
}

function setVtUdio(v){
  const el = document.getElementById('cfg-vt-udio');
  if (el) {
    el.value = v;
    document.getElementById('cfg-vt-udio-val').textContent = v + '%';
  }
}

async function loadVtKalibracija(){
  const box = document.getElementById('vt-kalibracija');
  box.style.display = 'block';
  box.innerHTML = 'Računam iz HEP satnih očitanja...';
  try {
    const r = await fetch('/api/stats/vt-kalibracija', {credentials:'same-origin'});
    const d = await r.json();
    const valid = (d.mjeseci || []).filter(m => m.ukupno_kwh > 0);
    if (!valid.length) {
      box.innerHTML = '<div style="color:#f87171">Nema dovoljno HEP podataka.</div>';
      return;
    }
    // Prosjek zadnjih 12 mj.
    const totUk = valid.reduce((s,m) => s + m.ukupno_kwh, 0);
    const totVtS = valid.reduce((s,m) => s + m.vt_simple_kwh, 0);
    const totVtW = valid.reduce((s,m) => s + m.vt_workdays_kwh, 0);
    const avgSimple = Math.round(100 * totVtS / totUk);
    const avgWorkdays = Math.round(100 * totVtW / totUk);

    let html = `<div style="margin-bottom:10px;color:var(--muted)">
      <strong style="color:var(--text)">Stvarni VT udio iz HEP satnih očitanja</strong>
      (sat ${esc(d.vt_od)}-${esc(d.vt_do)}h, zadnjih ${valid.length} mj.):
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
      <div style="border:1px solid var(--border);border-radius:6px;padding:10px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Simple (svi dani)</div>
        <div style="font-size:24px;color:#22d3ee;font-family:var(--mono);margin:6px 0">${avgSimple}%</div>
        <button class="btn btn-secondary btn-sm" onclick="setVtUdio(${avgSimple})">Primijeni</button>
      </div>
      <div style="border:1px solid var(--border);border-radius:6px;padding:10px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Bez vikenda (pon–pet)</div>
        <div style="font-size:24px;color:#22d3ee;font-family:var(--mono);margin:6px 0">${avgWorkdays}%</div>
        <button class="btn btn-secondary btn-sm" onclick="setVtUdio(${avgWorkdays})">Primijeni</button>
      </div>
    </div>
    <table style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr style="color:var(--muted)">
        <th style="text-align:left;padding:4px">Mjesec</th>
        <th style="text-align:right;padding:4px">Ukupno kWh</th>
        <th style="text-align:right;padding:4px">VT simple %</th>
        <th style="text-align:right;padding:4px">VT pon–pet %</th>
      </tr></thead><tbody>`;
    for (const m of valid) {
      html += `<tr>
        <td style="padding:4px;font-family:var(--mono)">${esc(m.mjesec)}</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${esc(m.ukupno_kwh)}</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${esc(m.vt_simple_perc ?? '—')}%</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${esc(m.vt_workdays_perc ?? '—')}%</td>
      </tr>`;
    }
    html += '</tbody></table>';
    box.innerHTML = html;
  } catch(e) {
    box.innerHTML = '<div style="color:#f87171">Greška: ' + esc(e) + '</div>';
  }
}

async function deleteRacun(period){
  if(!confirm('Obrisati račun '+period+'?')) return;
  await fetch('/api/racuni?period='+period, {method:'DELETE'});
  racuniPromijenjeni();
}

// Izvor procjene mjeseca (procj_izvor iz /api/stats/mjesecni)
const IZVOR_PROCJENE={registri:'registri brojila',projekcija:'projekcija mjeseca',satna:'satna očitanja',standardni:'standardna formula'};
// Razlika procjena − stvarni račun: ispod 1 € prigušeno, do 10 € žuto, više crveno
function razlikaHtml(d){
  if(d==null) return '<td class="c-muted">—</td>';
  const a=Math.abs(d), kl=a<1?'c-muted':a<10?'c-warn':'c-grid';
  return `<td class="${kl}">${d>0?'+':d<0?'−':''}${novac(a)}</td>`;
}

function renderRacuni(){
  const R=window.RACUNI;
  if(!R||!R.mjeseci) return;
  const mj=R.mjeseci.filter(r=>r.hep_potrosnja>0);
  const racuni=R.racuni||[];
  const poMjesecu={};
  R.mjeseci.forEach(m=>poMjesecu[m.mjesec]=m);

  // KPI — gotov sažetak zadnjih 12 završenih mjeseci sa servera
  const s12=R.sazetak_12mj||{};
  const n12=s12.n_mjeseci||0;
  set('r-ukupno', n12?novac(s12.racuni_eur):'—');
  set('r-ukupno-sub', n12?`${n12} mj. · stvarni račun, inače procjena`:'još nema završenih mjeseci');
  set('r-prihod', n12?novac(s12.otkup_eur):'—');
  set('r-prihod-sub', n12?`neto trošak ${novac(s12.neto_eur)}`:'isplata HEP-a za predani višak');
  set('r-prosj', n12?novac(s12.racuni_eur/n12):'—');
  set('r-max', s12.najskuplji?novac(s12.najskuplji.iznos):'—');
  set('r-max-sub', s12.najskuplji?s12.najskuplji.mjesec:'');

  // Tablica unesenih računa
  const tb2=document.getElementById('tRacuniUnos');
  if(racuni.length===0){
    stanje(tb2,'prazno','Nema unesenih računa — unesite prvi račun gore');
  } else {
    const BEZ_CIJENA='Račun je spremljen bez jediničnih cijena. Ponovno učitaj PDF tog računa i spremi ga — time se spremaju i cijene po kWh, pa procjene za to razdoblje koriste stvarne cijene.';
    tb2.innerHTML=racuni.map(r=>{
      const m=poMjesecu[r.period];
      const per = r.period.slice(5)+'/'+r.period.slice(0,4);
      const znacka=r.cijene_json
        ? '<span class="znacka ok" title="Jedinične cijene s ovog računa su spremljene">cijene ✓</span>'
        : `<span class="znacka" title="${esc(BEZ_CIJENA)}">bez cijena</span>`;
      return `<tr>
        <td style="font-weight:600">${esc(per)}</td>
        <td class="c-solar">${novac(r.iznos)}</td>
        <td>${m?novac(m.procj_iznos):'—'}</td>
        ${razlikaHtml(m?m.razlika:null)}
        <td>${r.kwh_vt!=null?esc(r.kwh_vt)+' kWh':'—'}</td>
        <td>${r.kwh_nt!=null?esc(r.kwh_nt)+' kWh':'—'}</td>
        <td class="c-feed">${r.kwh_minus!=null?esc(r.kwh_minus)+' kWh':'—'}</td>
        <td>${novac(r.opskrba)}</td>
        <td>${novac(r.mreza)}</td>
        <td>${novac(r.pdv)}</td>
        <td>${znacka}</td>
        <td><button data-racun-period="${esc(r.period)}" title="Obriši račun" style="background:none;border:none;color:var(--grid-out);cursor:pointer;font-size:14px">✕</button></td>
      </tr>`;
    }).join('');
    // Brisanje preko data-racun-period + listenera (ne inline onclick s interpoliranom vrijednošću);
    // ne data-period — taj atribut već koriste gumbi filtera perioda u Usporedbi
    tb2.querySelectorAll('button[data-racun-period]').forEach(b=>b.addEventListener('click',()=>deleteRacun(b.dataset.racunPeriod)));
  }

  // Graf — procjena i stvarni račun usporedno, otkup kao treći stup
  const last24=mj.slice(0,24).reverse();
  set('r-graf-sub',`Model: ${MODEL_NAZIV[R.model]||R.model||'—'} · iznos za platiti (s PDV-om i fiksnim naknadama) · otkup viška je zasebna isplata`);
  if(!last24.length){
    stanje('card-racuni-graf','prazno','Račun po mjesecu — još nema HEP podataka');
  } else {
    mkChart('cRacuni','bar',last24.map(r=>r.mjesec),[
      {label:'Procjena (€)',data:last24.map(r=>r.procj_iznos),backgroundColor:BOJA_RACUN_PROCJ,borderRadius:3},
      {label:'Stvarni račun (€)',data:last24.map(r=>r.stvarni_iznos),backgroundColor:BOJA_RACUN_STVARNI,borderRadius:3},
      {label:'Otkup viška (€)',data:last24.map(r=>r.otkup_eur||0),backgroundColor:BOJA_OTKUP,borderRadius:3},
    ],'€');
  }

  // Tablica svih mjeseci
  const tb=document.getElementById('tRacuni');
  if(!mj.length){ stanje(tb,'prazno','Nema HEP podataka po mjesecima'); return; }
  tb.innerHTML=mj.map(row=>{
    const stv=row.stvarni_iznos, otk=row.otkup_eur||0;
    const racun=stv!=null?stv:row.procj_iznos;
    return `<tr>
      <td style="font-weight:600">${esc(row.mjesec)}</td>
      <td class="c-consume">${f1(row.hep_potrosnja)}</td>
      <td class="c-feed">${f1(row.hep_predaja)}</td>
      <td class="c-solar">${row.sma_pv!=null?f1(row.sma_pv):'—'}</td>
      <td class="c-muted">${row.sma_autarkija!=null?f0(row.sma_autarkija)+'%':'—'}</td>
      <td>${novac(row.procj_iznos)}</td>
      <td class="c-muted">${esc(IZVOR_PROCJENE[row.procj_izvor]||row.procj_izvor||'—')}</td>
      <td class="${stv!=null?'c-solar':'c-muted'}">${novac(stv)}</td>
      ${razlikaHtml(row.razlika)}
      <td class="${otk>0?'c-green':'c-muted'}">${otk>0?novac(otk):'—'}</td>
      <td>${racun!=null?novac(racun-otk):'—'}</td>
    </tr>`;
  }).join('');
}

// ---- OPTIMALNO ----
function renderOpt(){
  if(!OPT)return;
  const hrs=Array.from({length:24},(_,i)=>i);
  const lbl=hrs.map(h=>h.toString().padStart(2,'0')+'h');
  const sH={};OPT.sma_satno.forEach(r=>sH[r.sat]=r);
  const hH={};OPT.hep_satno.forEach(r=>hH[r.sat]=r);
  const sol=hrs.map(h=>sH[h]?.prosj_pv_w||0);
  const pot=hrs.map(h=>hH[h]?.prosj_potrosnja||0);
  // autarkija (0–100 %) dolazi sa servera po satu, iz zajedničkog razdoblja HEP+SMA; sat bez
  // podatka ostaje null → praznina u grafu (ne nula)
  const aut=hrs.map(h=>{const v=hH[h]?.autarkija; return v==null?null:+v;});
  const raz=OPT.autarkija_razdoblje;
  set('opt-aut-sub', aut.some(v=>v!=null)
    ? 'Koliko % potrošnje kuće u tom satu pokriva solar'+(raz?` · ${datumHr(raz.od)} – ${datumHr(raz.do)}`:'')
    : 'Nema razdoblja u kojem postoje i HEP i SMA podaci — autarkija se ne može izračunati');
  const mx=Math.max(...sol,.001);

  mkChart('cOptSol','bar',lbl,[{
    label:'Prosj. solar (kW)',data:sol,
    backgroundColor:hrs.map((_,h)=>`rgba(251,191,36,${.15+sol[h]/mx*.8})`),borderRadius:4,
  }],'kW');

  mkChart('cOptPot','bar',lbl,[{label:'Prosj. potrošnja (kWh)',data:pot,backgroundColor:'rgba(167,139,250,.6)',borderRadius:3}],'kWh');
  mkChart('cOptAut','line',lbl,[{label:'Autarkija (%)',data:aut,borderColor:'#34d399',backgroundColor:'rgba(52,211,153,.1)',fill:true,tension:.4,pointRadius:3,borderWidth:2}],'%',{y:{min:0,max:100}});

  const top3=[...sol].map((v,i)=>({v,i})).sort((a,b)=>b.v-a.v).slice(0,3);
  const highAut=hrs.filter(h=>aut[h]>70);
  const prep=document.getElementById('prep');
  prep.innerHTML='';
  const add=(ik,naz,op,boja)=>{prep.innerHTML+=`<div class="prep" style="border-left:3px solid ${boja}"><div class="ph" style="color:${boja}">${ik} ${naz}</div><div class="pt">${op}</div></div>`;};
  add('☀️',`Vršna solar — ${top3[0].i}h`,`Prosj. ${f1(top3[0].v)} kW · idealno za perilicu, sušilicu, punjač EV.`,'#fbbf24');
  if(top3[1]) add('🌤️',`Dobra solar — ${top3[1].i}h`,`Prosj. ${f1(top3[1].v)} kW · pokrenite veće trošače.`,'#e8a000');
  if(top3[2]) add('⛅',`OK solar — ${top3[2].i}h`,`Prosj. ${f1(top3[2].v)} kW · još uvijek isplativo.`,'#b45309');
  if(highAut.length) add('🔋','Visoka autarkija',`Sati ${highAut.map(h=>h+'h').join(', ')} — prosj. >70% solarnog pokrivanja. Minimalni troškovi kupnje.`,'#34d399');
  const nocni=[0,1,2,3,4,5,22,23];
  const nocPot=nocni.reduce((a,h)=>a+(pot[h]||0),0)/nocni.length;
  add('🌙','Noćna potrošnja',`22h–6h: prosj. ${f2(nocPot)} kWh/sat iz mreže. Izbjegavajte velike trošače (perilica, sušilica).`,'#526070');
  const cheapH=hrs.filter(h=>sol[h]>mx*0.5);
  if(cheapH.length) add('💰','Najjeftiniji sati',`${cheapH.map(h=>h+'h').join(', ')} — solar >50% vršne. Koristite vlastitu energiju umjesto da je šaljete u mrežu.`,'#38bdf8');
}

// ---- POVIJEST ----
let POV = null;

function toggleUsporedba(){
  const p=document.getElementById('usporedba-panel');
  p.style.display=p.style.display==='none'?'block':'none';
}

function brziIzbor(tip){
  const now=new Date();
  let od, doo=localDate();
  switch(tip){
    case 'danas':  od=localDate(); break;
    case 'tjedan': { const d=new Date();const day=d.getDay()||7;d.setDate(d.getDate()-day+1);od=localDate(d); break; }
    case '7dana':  { const d=new Date();d.setDate(d.getDate()-6);od=localDate(d); break; }
    case 'mj':     od=mS(); break;
    case '30dana': { const d=new Date();d.setDate(d.getDate()-29);od=localDate(d); break; }
    case 'god':    od=yS(); break;
    case 'sve':    od='2022-01-01'; break;
    default: od=mS();
  }
  document.getElementById('pov-od').value=od;
  document.getElementById('pov-do').value=doo;

  // Automatski postavi rezoluciju
  const days=(new Date(doo)-new Date(od))/(1000*86400);
  const resEl=document.getElementById('pov-res');
  if(tip==='danas') resEl.value='hour';
  else if(days<=14) resEl.value='day';
  else if(days<=90) resEl.value='day';
  else resEl.value='week';

  loadPovijest();
}

const POV_CILJEVI=['pov-kpi','card-pov1','card-pov2','card-pov-tab'];
async function loadPovijest(){
  const od  = document.getElementById('pov-od').value || localDate(new Date(Date.now()-30*86400000));
  const doo = document.getElementById('pov-do').value || localDate();
  const res = document.getElementById('pov-res').value || 'day';
  if(!od||!doo) return;

  const d=await dohvati(withOmm(`/api/povijest?od=${encodeURIComponent(od)}&do=${encodeURIComponent(doo)}&res=${encodeURIComponent(res)}`),POV_CILJEVI);
  if(!d) return;
  if(!Array.isArray(d.hep)||!Array.isArray(d.sma)){ POV_CILJEVI.forEach(c=>stanje(c,'greska')); return; }
  POV=d;
  try{ renderPovijest(); }catch(e){ console.error(e); POV_CILJEVI.forEach(c=>stanje(c,'greska')); }
}

function renderPovijest(){
  if(!POV) return;
  const {hep,sma,res,od,do:doo}=POV;

  // Merge hep i sma po datumu/tjednu
  const smaMap={};
  sma.forEach(r=>smaMap[r.datum||r.tjedan||r.sat]=r);

  // Labeli
  const keyField = res==='hour'?'ts':res==='week'?'datum_od':'datum';
  const labels = hep.map(r=>{
    const k=r[keyField]||r.tjedan||r.sat;
    if(!k) return '—';
    if(res==='hour') return k.slice(11,16);
    if(res==='week') return k.slice(5);
    return k.slice(5);
  });

  // KPI za period
  const totP=hep.reduce((a,b)=>a+(b.kwh_plus||0),0);
  const totM=hep.reduce((a,b)=>a+(b.kwh_minus||0),0);
  const totSol=sma.reduce((a,b)=>a+(b.pv_kwh||b.pv_generation_kwh||0),0);
  const neto=totM-totP;
  // Okvirna vrijednost energije po efektivnim cijenama sa servera (/api/data) — bez tvrdo
  // upisanih cijena. NIJE iznos računa (nema mjesečnog netiranja ni fiksnih naknada);
  // trošak je pozitivan, a ako predaja vrijedi više od kupnje prikazuje se zeleno.
  const ec=D?.efektivne_cijene;
  const okv=(ec&&ec.kupnja_avg!=null&&ec.prodaja!=null)?totP*ec.kupnja_avg-totM*ec.prodaja:null;

  document.getElementById('pov-kpi').innerHTML=`
    <div class="kpi cons"><div class="kpi-lbl">Potrošnja iz mreže</div><div class="kpi-val c-consume">${f1(totP)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi feed"><div class="kpi-lbl">Predaja u mrežu</div><div class="kpi-val c-feed">${f1(totM)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi solar"><div class="kpi-lbl">Solar (SMA)</div><div class="kpi-val c-solar">${f1(totSol)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi ${neto>=0?'grn':'cons'}"><div class="kpi-lbl">Neto bilanca</div><div class="kpi-val ${neto>=0?'c-green':'c-grid'}">${sgn(neto)+f1(neto)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi ${okv==null?'acc':okv>0?'cons':'grn'}"><div class="kpi-lbl">Okvirni trošak energije</div><div class="kpi-val ${okv==null?'c-muted':okv>0?'':'c-green'}">${novac(okv==null?null:Math.abs(okv))}</div><div class="kpi-sub">${okv==null?'cijene nisu učitane':okv>0?'kupnja − vrijednost predaje · okvirno':'predaja vrijedi više od kupnje · okvirno'}</div></div>
    <div class="kpi acc"><div class="kpi-lbl">Period</div><div class="kpi-val" style="font-size:14px">${hep.length} ${res==='hour'?'sati':res==='week'?'tjedana':'dana'}</div><div class="kpi-sub">${esc(od)} — ${esc(doo)}</div></div>
  `;

  // Graf 1 - HEP
  const unitLabel = res==='hour'?'kWh/h':'kWh';
  mkChart('cPov1', res==='hour'?'line':'bar', labels, [
    {label:'Potrošnja',data:hep.map(r=>r.kwh_plus||0),
     backgroundColor:'rgba(167,139,250,.65)',borderColor:'#a78bfa',
     borderRadius:2,fill:res==='hour',tension:.3,pointRadius:res==='hour'?0:undefined},
    {label:'Predaja',data:hep.map(r=>r.kwh_minus||0),
     backgroundColor:'rgba(56,189,248,.65)',borderColor:'#38bdf8',
     borderRadius:2,fill:res==='hour',tension:.3,pointRadius:res==='hour'?0:undefined},
  ], unitLabel);

  // Graf 2 - SMA solar
  const smaLabels = res==='hour'
    ? hep.map(r=>{ const k=r.ts||r.sat; return k?k.slice(11,16):''; })
    : hep.map(r=>r[keyField]||'').map(k=>k.slice(5));

  const smaPv = hep.map(r=>{
    const k=r[keyField]||r.tjedan||r.sat;
    const s=smaMap[k]||smaMap[k?.slice(0,10)];
    return s?(s.pv_kwh||s.pv_generation_kwh||0):0;
  });
  const smaInv1 = hep.map(r=>{
    const k=r[keyField]||r.tjedan||r.sat;
    const s=smaMap[k]||smaMap[k?.slice(0,10)];
    return s?(s.inv1_kwh||s.pv_kwh_inv1||0):0;
  });
  const smaInv2 = hep.map(r=>{
    const k=r[keyField]||r.tjedan||r.sat;
    const s=smaMap[k]||smaMap[k?.slice(0,10)];
    return s?(s.inv2_kwh||s.pv_kwh_inv2||0):0;
  });

  mkChart('cPov2', res==='hour'?'line':'bar', labels, [
    {label:'Inv1 (10kW)',data:smaInv1,backgroundColor:'rgba(251,191,36,.5)',borderColor:'#f59e0b',borderRadius:2,tension:.3,pointRadius:0},
    {label:'Inv2 (20kW)',data:smaInv2,backgroundColor:'rgba(251,191,36,.8)',borderColor:'#fbbf24',borderRadius:2,tension:.3,pointRadius:0},
    {label:'Ukupno solar',data:smaPv,type:'line',borderColor:'#fbbf24',pointRadius:0,borderWidth:2,tension:.3},
  ], unitLabel);

  // Tablica
  const thead=document.getElementById('pov-thead');
  thead.innerHTML = res==='week'
    ? '<th>Tjedan</th><th>Od</th><th>Potrošnja</th><th>Predaja</th><th>Solar</th><th>Dana</th><th>Neto bilanca</th>'
    : res==='hour'
    ? '<th>Sat</th><th>Potrošnja (kWh)</th><th>Predaja (kWh)</th><th>Solar (kWh)</th>'
    : '<th>Datum</th><th>Potrošnja (kWh)</th><th>Predaja (kWh)</th><th>Solar (kWh)</th><th>Inv1</th><th>Inv2</th><th>Neto bilanca</th>';

  const tb=document.getElementById('tPovijest');
  tb.innerHTML='';
  if(!hep.length){ stanje(tb,'prazno','Nema podataka za odabrano razdoblje'); return; }
  hep.forEach(row=>{
    const k=row[keyField]||row.tjedan||row.sat;
    const s=smaMap[k]||smaMap[k?.slice(0,10)];
    const pv=s?(s.pv_kwh||s.pv_generation_kwh||0):0;
    const inv1=s?(s.inv1_kwh||s.pv_kwh_inv1||0):0;
    const inv2=s?(s.inv2_kwh||s.pv_kwh_inv2||0):0;
    const n=(row.kwh_minus||0)-(row.kwh_plus||0);

    if(res==='week'){
      tb.innerHTML+=`<tr>
        <td style="font-weight:600">${esc(row.tjedan)}</td>
        <td class="c-muted">${esc(row.datum_od||'')}</td>
        <td class="c-consume">${f1(row.kwh_plus)}</td>
        <td class="c-feed">${f1(row.kwh_minus)}</td>
        <td class="c-solar">${pv?f1(pv):'—'}</td>
        <td class="c-muted">${esc(row.n_dana)}</td>
        <td class="${n>=0?'pos':'neg'}">${sgn(n)+f1(n)}</td>
      </tr>`;
    } else if(res==='hour'){
      const ts=row.ts||row.sat;
      tb.innerHTML+=`<tr>
        <td style="font-family:var(--mono)">${esc(ts?ts.slice(0,16):'')}</td>
        <td class="c-consume">${f2(row.kwh_plus)}</td>
        <td class="c-feed">${f2(row.kwh_minus)}</td>
        <td class="c-solar">${pv?f2(pv):'—'}</td>
      </tr>`;
    } else {
      tb.innerHTML+=`<tr>
        <td style="font-weight:600">${esc(row.datum)}</td>
        <td class="c-consume">${f1(row.kwh_plus)}</td>
        <td class="c-feed">${f1(row.kwh_minus)}</td>
        <td class="c-solar">${pv?f1(pv):'—'}</td>
        <td class="c-muted">${inv1?f1(inv1):'—'}</td>
        <td class="c-muted">${inv2?f1(inv2):'—'}</td>
        <td class="${n>=0?'pos':'neg'}">${sgn(n)+f1(n)}</td>
      </tr>`;
    }
  });
}

async function loadUsporedba(){
  const aOd=document.getElementById('cmp-a-od').value;
  const aDo=document.getElementById('cmp-a-do').value;
  const bOd=document.getElementById('cmp-b-od').value;
  const bDo=document.getElementById('cmp-b-do').value;
  if(!aOd||!aDo||!bOd||!bDo) return;

  const u=(o,d)=>withOmm(`/api/povijest?od=${encodeURIComponent(o)}&do=${encodeURIComponent(d)}&res=day`);
  const [rA, rB] = await Promise.all([dohvati(u(aOd,aDo),'cmp-kpi'), dohvati(u(bOd,bDo))]);
  if(!rA||!rB||!Array.isArray(rA.hep)||!Array.isArray(rB.hep)){ stanje('cmp-kpi','greska'); return; }
  stanje('cmp-kpi','ok');

  const sumA={p:0,m:0,sol:0}, sumB={p:0,m:0,sol:0};
  rA.hep.forEach(r=>{sumA.p+=r.kwh_plus||0;sumA.m+=r.kwh_minus||0;});
  (rA.sma||[]).forEach(r=>{sumA.sol+=r.pv_generation_kwh||0;});
  rB.hep.forEach(r=>{sumB.p+=r.kwh_plus||0;sumB.m+=r.kwh_minus||0;});
  (rB.sma||[]).forEach(r=>{sumB.sol+=r.pv_generation_kwh||0;});

  const kpi=document.getElementById('cmp-kpi');
  const diff=(a,b,inv=false)=>{
    const d=a-b; const pct=b>0?d/b*100:0;
    const cls=inv?(d>0?'neg':'pos'):(d>0?'pos':'neg');
    return `<span class="${cls}">${sgn(d)+f1(d)} (${sgn(pct)+f0(pct)}%)</span>`;
  };
  kpi.innerHTML=`
    <div class="fin"><div class="lbl">Potrošnja A</div><div class="val c-consume">${f1(sumA.p)} kWh</div></div>
    <div class="fin"><div class="lbl">Potrošnja B</div><div class="val c-consume">${f1(sumB.p)} kWh</div><div class="sub">vs A: ${diff(sumB.p,sumA.p,true)}</div></div>
    <div class="fin"><div class="lbl">Solar A</div><div class="val c-solar">${f1(sumA.sol)} kWh</div></div>
    <div class="fin"><div class="lbl">Solar B</div><div class="val c-solar">${f1(sumB.sol)} kWh</div><div class="sub">vs A: ${diff(sumB.sol,sumA.sol)}</div></div>
    <div class="fin"><div class="lbl">Predaja A</div><div class="val c-feed">${f1(sumA.m)} kWh</div></div>
    <div class="fin"><div class="lbl">Predaja B</div><div class="val c-feed">${f1(sumB.m)} kWh</div><div class="sub">vs A: ${diff(sumB.m,sumA.m)}</div></div>
  `;

  // Graf usporedbe - normalizirano po danu
  const nA=rA.hep.length||1, nB=rB.hep.length||1;
  mkChart('cCmp','bar',['Potrošnja /dan','Solar /dan','Predaja /dan'],[
    {label:`Period A (${aOd} — ${aDo})`,data:[sumA.p/nA,sumA.sol/nA,sumA.m/nA],backgroundColor:'rgba(56,189,248,.7)',borderRadius:4},
    {label:`Period B (${bOd} — ${bDo})`,data:[sumB.p/nB,sumB.sol/nB,sumB.m/nB],backgroundColor:'rgba(251,191,36,.7)',borderRadius:4},
  ],'kWh/dan');
}

// ---- CHART FACTORY ----
// ---- DUG ----
window.DUG = null;
const DUG_CILJEVI=['dug-kpi','card-dug-proj','card-dug-pov'];
async function loadDug(){
  loadDugCijene();
  const d=await dohvati('/api/stats/dug',DUG_CILJEVI);
  if(!d) return;
  window.DUG = d;
  try{ renderDug(); }catch(e){ console.error(e); DUG_CILJEVI.forEach(c=>stanje(c,'greska')); }
}

// Jedan prigušeni red s cijenama — isti izvor i ista polja kao kartica "Aktivne cijene" na Pregledu
async function loadDugCijene(){
  const c=await dohvati('/api/stats/cijene');
  const ck=v=>(v*100).toLocaleString('hr-HR',{minimumFractionDigits:2,maximumFractionDigits:2});
  set('dug-cijene', (c && c.vt_full_eur_kwh!=null && c.nt_full_eur_kwh!=null)
    ? `Cijene: kupnja VT ${ck(c.vt_full_eur_kwh)} · NT ${ck(c.nt_full_eur_kwh)} c/kWh (s PDV-om)`
      + (c.vt_otkup!=null?` · otkup viška VT ${ck(c.vt_otkup)}`+(c.nt_otkup!=null?` · NT ${ck(c.nt_otkup)}`:'')+' c/kWh':'')
      + ` (izvor: ${c.izvor||'—'})`
    : 'Cijene po kWh');
}

function renderDug(){
  const D2 = window.DUG;
  const tb = document.getElementById('tDug');
  if(!D2 || !D2.trenutni){
    stanje('dug-kpi','prazno','Nema uvezenih računa s podatkom o dugu — dodaj račun (PDF) u tabu Računi');
    stanje('card-dug-proj','prazno','Projekcija isplate duga: nema podataka o dugu');
    stanje(tb,'prazno','Nema računa s podatkom o dugu');
    return;
  }
  const t = D2.trenutni, v = D2.vrh || t;
  set('dug-trenutni', novac(t.dug));
  set('dug-datum', (t.datum_racuna?datumHr(t.datum_racuna):(t.period||'')) + ' · račun ' + novac(t.iznos));
  set('dug-vrh', novac(v.dug));
  set('dug-vrh-mj', v.period);
  // pad od vrha je smanjenje duga — ispisuje se kao pozitivan iznos sa strelicom, ne kao minus
  const pad = v.dug - t.dug;
  set('dug-pad', pad>0.005 ? '↓ '+novac(pad) : '—');
  const avgCharge = D2.godisnji_neto!=null ? D2.godisnji_neto/12 : null;
  set('dug-mjtrosak', novac(avgCharge));

  // breakeven
  set('dug-breakeven', avgCharge!=null ? novac(avgCharge)+'/mj' : '—');

  // tempo (samo post-vrh pad iz stvarnih računa)
  const rac = D2.racuni||[];
  let tempo = null;
  const iVrh = rac.findIndex(x=>x.period===v.period);
  if(iVrh>=0 && iVrh < rac.length-1){
    const a = rac[iVrh], b = rac[rac.length-1];
    const mj = _mjeseciIzmedju(a.period, b.period);
    if(mj>0){ tempo = (b.dug - a.dug)/mj; }
  }
  if(tempo!=null && tempo<0){
    const mjeseci = Math.ceil(t.dug / (-tempo));
    set('dug-tempo', _datumZaMjeseci(t.period, mjeseci));
    set('dug-tempo-sub', `~${(-tempo).toFixed(0)} €/mj (ljetni tempo)`);
  } else {
    set('dug-tempo', '—');
    set('dug-tempo-sub', 'nema pada');
  }

  // postavi default uplatu na razumnu (pokriće + isplata u ~24 mj) — samo prvi put,
  // da ponovno otvaranje taba ne pregazi korisnikov klizač
  const sl = document.getElementById('dug-uplata');
  if(avgCharge!=null && !sl.dataset.postavljeno){
    const sug = Math.ceil((avgCharge + t.dug/24)/10)*10;
    sl.value = Math.min(400, Math.max(0, sug));
    sl.dataset.postavljeno = '1';
  }
  renderDugProj();

  // povijest duga po računima (najnoviji gore); Δ = dug − dug prethodnog računa, zeleno kad pada
  tb.innerHTML = rac.slice().reverse().map(x=>{
    const d = x.delta;
    const dTd = d==null ? '<td class="c-muted">—</td>'
      : `<td class="${d<-0.005?'pos':d>0.005?'neg':'c-muted'}">${d<-0.005?'↓ ':d>0.005?'↑ ':''}${novac(Math.abs(d))}</td>`;
    return `<tr><td>${esc(x.period)}</td><td>${novac(x.iznos)}</td><td>${novac(x.dug)}</td>${dTd}<td>${esc(datumHr(x.datum_dospijeca))}</td></tr>`;
  }).join('');
  if(!rac.length) stanje(tb,'prazno','Nema računa s podatkom o dugu');
}

function _mjeseciIzmedju(p1, p2){
  const [y1,m1]=p1.split('-').map(Number), [y2,m2]=p2.split('-').map(Number);
  return (y2-y1)*12 + (m2-m1);
}
function _datumZaMjeseci(period, n){
  let [y,m]=period.split('-').map(Number);
  m += n;
  y += Math.floor((m-1)/12);
  m = ((m-1)%12)+1;
  const naz=['sij','velj','ožu','tra','svi','lip','srp','kol','ruj','lis','stu','pro'];
  return naz[m-1]+' '+y;
}

// ---- ISPLATIVOST (ROI + godišnji net-metering saldo) ----
async function loadIsplativost(){
  const [R, S] = await Promise.all([
    dohvati('/api/stats/roi','card-roi'), dohvati('/api/stats/godisnji-saldo','card-saldo')
  ]);
  if(R){ try{ renderRoi(R); }catch(e){ console.error(e); stanje('card-roi','greska'); } }
  if(S){ try{ renderSaldo(S); }catch(e){ console.error(e); stanje('card-saldo','greska'); } }
}

function renderRoi(R){
  if(!R) return;
  // prefill (ne gazi ako korisnik trenutno tipka)
  const inv=document.getElementById('roi-invest'), com=document.getElementById('roi-commission');
  if(inv && document.activeElement!==inv && R.investicija_eur!=null) inv.value=R.investicija_eur;
  if(com && document.activeElement!==com && R.commission) com.value=R.commission;

  set('roi-godkorist', novac(R.godisnja_korist_eur));
  set('roi-godkorist-sub', `${R.n_mjeseci||0} mj. mjereno`);
  set('roi-payback', R.payback_godina!=null ? R.payback_godina.toFixed(1)+' god' : '—');
  set('roi-breakeven', R.breakeven_mjesec ? 'do '+R.breakeven_mjesec
       : (R.investicija_eur==null ? 'unesi cijenu sustava' : 'unesi datum puštanja'));
  set('roi-pokriveno', R.pokriveno_perc!=null ? R.pokriveno_perc.toFixed(0)+' %' : '—');
  set('roi-korist-sub', `${novac(R.korist_ukupno_eur)} ukupno${R.commission?' od '+datumHr(R.commission):''}`);
}

function renderSaldo(S){
  const tb=document.getElementById('tSaldo');
  if(!S || !S.total){ stanje(tb,'prazno','Nema registarskih podataka'); return; }
  const t=S.total;
  set('saldo-razdoblje', `${t.od} → ${t.do} · ${t.n_mjeseci} mj.`);
  set('saldo-uzeto', f0(t.uzeto_kwh)+' kWh');
  set('saldo-pred', f0(t.pred_kwh)+' kWh');
  set('saldo-pokrivenost', t.pokrivenost_perc!=null ? `${t.pokrivenost_perc}% pokriveno predajom` : '');
  set('saldo-neto', sgn(t.neto_saldo_kwh)+f0(t.neto_saldo_kwh)+' kWh');
  // neto trošak = računi − otkup (pozitivno = plaćeno); ako je otkup veći, iznos je zelen
  const nt=t.neto_trosak_eur, ntEl=document.getElementById('saldo-trosak');
  ntEl.textContent=novac(nt==null?null:Math.abs(nt));
  ntEl.classList.toggle('c-green', nt!=null && nt<0);
  set('saldo-trosak-sub', nt!=null && nt<0 ? 'neto primljeno · procjena iz registara' : 'procjena iz registara (račun − otkup)');
  tb.innerHTML = (S.mjeseci||[]).map(m=>`<tr>
    <td>${esc(m.mjesec)}</td><td>${f0(m.uzeto_kwh)}</td><td>${f0(m.pred_kwh)}</td>
    <td>${novac(m.racun_eur)}</td><td class="pos">${novac(m.otkup_eur)}</td>
    <td class="pos">${novac(m.korist_eur)}</td></tr>`).join('');
}

async function spremiRoi(){
  const data={
    PV_INVESTMENT_EUR: document.getElementById('roi-invest').value,
    PV_COMMISSION_DATE: document.getElementById('roi-commission').value,
  };
  try{
    const r=await fetch('/api/postavke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!r.ok) throw new Error(r.status);
    set('roi-saved','✓ spremljeno');
    setTimeout(()=>set('roi-saved',''),2500);
    loadIsplativost();
  }catch(e){ set('roi-saved','✗ greška'); console.error(e); }
}

function renderDugProj(){
  const D2 = window.DUG;
  if(!D2 || !D2.trenutni) return;
  const uplata = +document.getElementById('dug-uplata').value;
  set('dug-uplata-val', uplata+' €');
  const sez = D2.sezonski || {};
  const t = D2.trenutni;
  let [y,m] = t.period.split('-').map(Number);

  // simulacija unaprijed (max 60 mj)
  const labels = [], dataProj = [];
  let dug = t.dug, isplata = null;
  const naz=['sij','velj','ožu','tra','svi','lip','srp','kol','ruj','lis','stu','pro'];
  for(let k=1; k<=60; k++){
    m++; if(m>12){ m=1; y++; }
    const charge = sez[m] != null ? sez[m] : (D2.godisnji_neto||0)/12;
    dug = dug + charge - uplata;
    labels.push(naz[m-1]+' '+String(y).slice(2));
    dataProj.push(Math.max(0, +dug.toFixed(2)));
    if(dug<=0 && isplata===null){ isplata = k; break; }
    if(k>=36 && dug > t.dug){ break; }  // raste — nema isplate
  }
  if(isplata!=null){
    set('dug-sezproj', _datumZaMjeseci(t.period, isplata));
  } else {
    set('dug-sezproj', 'ne gasi se');
  }

  // graf: stvarni dug (povijest) + projekcija
  const rac = D2.racuni||[];
  const histLabels = rac.map(x=>{ const [yy,mm]=x.period.split('-'); return naz[+mm-1]+' '+yy.slice(2); });
  const histData = rac.map(x=>x.dug);
  const allLabels = histLabels.concat(labels);
  const histPadded = histData.concat(labels.map(()=>null));
  const projPadded = histData.map(()=>null);
  // poveži zadnji stvarni s prvom projekcijom
  if(histData.length){ projPadded[histData.length-1] = histData[histData.length-1]; }
  const projFull = projPadded.concat(dataProj);

  mkChart('cDug','line',allLabels,[
    {label:'Stvarni dug', data:histPadded.concat(), borderColor:'#f87171', backgroundColor:'rgba(248,113,113,.08)', fill:true, tension:.2, pointRadius:3, borderWidth:2, spanGaps:false},
    {label:'Projekcija', data:projFull, borderColor:'#38bdf8', borderDash:[5,3], fill:false, tension:.2, pointRadius:0, borderWidth:2, spanGaps:true},
  ],'€');
}

// opts (neobavezno): {xTicks:{…}} — dodatne Chart.js opcije za oznake osi X (npr. vlastiti callback);
//                    {y:{…}} — opcije osi Y (npr. {min:0,max:100} za postotke)
function mkChart(id,type,labels,datasets,unit,opts){
  if(CH[id])CH[id].destroy();
  const isDark=document.documentElement.getAttribute('data-theme')!=='light';
  const gridColor=isDark?'rgba(31,45,61,.5)':'rgba(208,218,228,.7)';
  const tickColor=isDark?'#526070':'#7a8fa6';
  CH[id]=new Chart(document.getElementById(id).getContext('2d'),{
    type,data:{labels,datasets},
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{labels:{color:tickColor,font:{family:'IBM Plex Sans',size:11},boxWidth:10,padding:12}},
        tooltip:{
          backgroundColor:isDark?'#0f1623':'#ffffff',
          borderColor:isDark?'#1f2d3d':'#d0dae4',
          borderWidth:1,
          titleColor:isDark?'#d0dde8':'#1a2535',
          bodyColor:isDark?'#526070':'#7a8fa6',
          padding:10,
          filter:i=>i.parsed.y!=null,
          callbacks:{label:c=>` ${c.dataset.label}: ${(+c.parsed.y).toFixed(2)} ${unit}`}
        }
      },
      scales:{
        x:{grid:{color:gridColor},ticks:Object.assign({color:tickColor,font:{family:'IBM Plex Mono',size:11},maxTicksLimit:12},(opts&&opts.xTicks)||{})},
        y:Object.assign({grid:{color:gridColor},ticks:{color:tickColor,font:{family:'IBM Plex Mono',size:11}}},(opts&&opts.y)||{}),
      }
    }
  });
}

// ---- POSTAVKE ----
function toggleOptSec(id){
  const b=document.getElementById(id+'-body');
  const badge=document.getElementById(id+'-badge');
  b.classList.toggle('open');
  badge.textContent=b.classList.contains('open')?'▲':'▼';
}

async function loadPostavke(){
  // Konfiguracija
  const cfg=await dohvati('/api/postavke','app-info');
  if(cfg){
    try{
      document.getElementById('cfg-hep-user').value=cfg.HEP_USERNAME||'';
      document.getElementById('cfg-hep-sifra').value=cfg.HEP_SIFRA||'';
      document.getElementById('cfg-sma-user').value=cfg.SMA_USERNAME||'';
      document.getElementById('cfg-sma-plant').value=cfg.SMA_PLANT_ID||'';
      document.getElementById('cfg-sma-inv1').value=cfg.SMA_INV1_ID||'';
      document.getElementById('cfg-sma-inv2').value=cfg.SMA_INV2_ID||'';
      document.getElementById('cfg-ha-url').value=cfg.HA_URL||'';
      // Postavi placeholdere za lozinke
      if(cfg.HEP_PASSWORD_SET) document.getElementById('cfg-hep-pass').placeholder='(postavljena — ostavi prazno)';
      if(cfg.SMA_PASSWORD_SET) document.getElementById('cfg-sma-pass').placeholder='(postavljena — ostavi prazno)';
      if(cfg.HA_TOKEN_SET) document.getElementById('cfg-ha-token').placeholder='(postavljen — ostavi prazno)';
      if(cfg.DASHBOARD_PASSWORD_SET) document.getElementById('cfg-app-pass').placeholder='(postavljena — ostavi prazno)';

      // VT/NT slider
      const vtUdio = parseInt(cfg.VT_UDIO_PERC || '45', 10);
      const sliderEl = document.getElementById('cfg-vt-udio');
      if (sliderEl) { sliderEl.value = vtUdio; document.getElementById('cfg-vt-udio-val').textContent = vtUdio + '%'; }

      document.getElementById('app-info').innerHTML=`
        <div class="summary-item ok"><div class="label">URL</div><div class="value">${esc(location.hostname)}</div></div>
        <div class="summary-item ok"><div class="label">HEP ODS</div><div class="value">${esc(cfg.HEP_USERNAME||'—')}</div></div>
        <div class="summary-item ${cfg.SMA_USERNAME?'ok':'skip'}"><div class="label">SMA</div><div class="value">${esc(cfg.SMA_USERNAME||'Nije konfigurirano')}</div></div>
        <div class="summary-item ${cfg.HA_URL?'ok':'skip'}"><div class="label">Home Assistant</div><div class="value">${esc(cfg.HA_URL||'Nije konfigurirano')}</div></div>
      `;
    }catch(e){ console.error(e); stanje('app-info','greska'); }
  }

  // Status sustava
  const stat=await dohvati('/api/postavke/status','status-kpi');
  if(stat){
    try{
      document.getElementById('status-kpi').innerHTML=`
        <div class="kpi acc"><div class="kpi-lbl">Verzija</div><div class="kpi-val" style="font-size:16px">${esc(stat.version)}</div><div class="kpi-sub">HEP Energy Monitor</div></div>
        <div class="kpi solar"><div class="kpi-lbl">HEP zadnji sync</div><div class="kpi-val" style="font-size:14px">${esc(stat.hep_last_sync?stat.hep_last_sync.slice(0,16):'—')}</div><div class="kpi-sub">${esc(stat.hep_range?.od||'—')} — ${esc(stat.hep_range?.do||'—')}</div></div>
        <div class="kpi solar"><div class="kpi-lbl">SMA zadnji sync</div><div class="kpi-val" style="font-size:14px">${esc(stat.sma_last_sync?stat.sma_last_sync.slice(0,16):'—')}</div><div class="kpi-sub">${esc(stat.sma_range?.od?.slice(0,10)||'—')} — ${esc(stat.sma_range?.do?.slice(0,10)||'—')}</div></div>
        <div class="kpi grn"><div class="kpi-lbl">Baza podataka</div><div class="kpi-val" style="font-size:16px">${esc(stat.db_size_mb)} MB</div><div class="kpi-sub">HEP: ${(stat.tables?.ocitanja_15min||0).toLocaleString()} zapisa</div></div>
        <div class="kpi cons"><div class="kpi-lbl">SMA 15-min</div><div class="kpi-val" style="font-size:16px">${(stat.tables?.sma_15min||0).toLocaleString()}</div><div class="kpi-sub">zapisa</div></div>
        <div class="kpi feed"><div class="kpi-lbl">SMA live</div><div class="kpi-val" style="font-size:16px">${(stat.tables?.sma_live||0).toLocaleString()}</div><div class="kpi-sub">zapisa</div></div>
      `;
    }catch(e){ console.error(e); stanje('status-kpi','greska'); }
  }

  // Korisnici
  loadKorisnici();
}

async function savePostavke(){
  const data={
    HEP_USERNAME: document.getElementById('cfg-hep-user').value,
    HEP_SIFRA:    document.getElementById('cfg-hep-sifra').value,
    SMA_USERNAME: document.getElementById('cfg-sma-user').value,
    SMA_PLANT_ID: document.getElementById('cfg-sma-plant').value,
    SMA_INV1_ID:  document.getElementById('cfg-sma-inv1').value,
    SMA_INV2_ID:  document.getElementById('cfg-sma-inv2').value,
    HA_URL:       document.getElementById('cfg-ha-url').value,
  };
  // Dodaj lozinke samo ako su unesene
  const hepPass=document.getElementById('cfg-hep-pass').value;
  const smaPass=document.getElementById('cfg-sma-pass').value;
  const haToken=document.getElementById('cfg-ha-token').value;
  const appPass=document.getElementById('cfg-app-pass').value;
  if(hepPass) data.HEP_PASSWORD=hepPass;
  if(smaPass) data.SMA_PASSWORD=smaPass;
  if(haToken) data.HA_TOKEN=haToken;
  if(appPass) data.DASHBOARD_PASSWORD=appPass;

  // VT/NT omjer (uvijek šaljemo)
  const vtSlider = document.getElementById('cfg-vt-udio');
  if (vtSlider) data.VT_UDIO_PERC = vtSlider.value;

  try{
    const r=await fetch('/api/postavke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    const st=document.getElementById('cfg-status');
    if(d.ok && d.warning){
      // Server je spremio, ali uz upozorenje (npr. vrijednost ne dolazi do sync kontejnera) — ostaje vidljivo
      st.textContent='⚠ Spremljeno uz upozorenje: '+d.warning;
      showToast(d.env_restart_required?'Spremljeno — potrebna izmjena .env na serveru!':'Spremljeno uz upozorenje!');
    } else if(d.ok){
      st.textContent='✓ Spremljeno';
      showToast('Konfiguracija spremljena!');
      setTimeout(()=>st.textContent='',3000);
    } else {
      st.textContent='✗ Greška'+(d.error?': '+d.error:'');
    }
  }catch(e){document.getElementById('cfg-status').textContent='✗ Greška';}
}

async function runImport(tip){
  const out=document.getElementById('import-output');
  out.style.display='block';
  out.innerHTML='⏳ Pokretanje...<br>';

  const endpoints={
    'hep-30': '/api/setup/import-hep?dani=30',
    'hep-sve': '/api/setup/import-hep?dani=1500',
    'sma': '/api/setup/import-sma',
    'ha': '/api/setup/sync-ha',
  };

  try{
    out.innerHTML+=`▶ ${esc(tip)} import...<br>`;
    const r=await fetch(endpoints[tip],{method:'POST'});
    const d=await r.json();
    out.innerHTML+=`✓ ${esc(d.info||'Gotovo')}<br>`;
    showToast('Import završen!');
  }catch(e){
    out.innerHTML+=`✗ Greška: ${esc(e.message)}<br>`;
  }
  out.scrollTop=out.scrollHeight;
}

async function loadKorisnici(){
  try{
    const tb=document.getElementById('tKorisnici');
    const korisnici=await dohvati('/api/postavke/korisnici',tb);
    if(!korisnici) return;
    if(!Array.isArray(korisnici)){ stanje(tb,'greska'); return; }
    tb.innerHTML='';
    if(!korisnici.length){
      tb.innerHTML='<tr><td colspan="4" style="padding:12px;color:var(--muted);text-align:center">Nema korisnika — koristi se DASHBOARD_PASSWORD iz .env</td></tr>';
      return;
    }
    korisnici.forEach(k=>{
      tb.innerHTML+=`<tr>
        <td style="padding:9px 12px">${esc(k.username)}</td>
        <td style="padding:9px 12px"><span style="background:${k.uloga==='admin'?'rgba(34,211,153,.15)':'rgba(82,96,112,.15)'};color:${k.uloga==='admin'?'var(--green)':'var(--muted)'};padding:2px 8px;border-radius:4px;font-size:11px">${esc(k.uloga)}</span></td>
        <td style="padding:9px 12px;color:var(--muted)">${esc(k.zadnja_prijava?k.zadnja_prijava.slice(0,16):'Nikad')}</td>
        <td style="padding:9px 12px"><button data-username="${esc(k.username)}" style="background:none;border:none;color:var(--grid-out);cursor:pointer">✕</button></td>
      </tr>`;
    });
    // Brisanje preko data-username + listenera (ne inline onclick s interpoliranom vrijednošću)
    tb.querySelectorAll('button[data-username]').forEach(b=>b.addEventListener('click',()=>deleteKorisnik(b.dataset.username)));
  }catch(e){console.error(e);}
}

async function addKorisnik(){
  const name=document.getElementById('usr-name').value.trim();
  const pass=document.getElementById('usr-pass').value;
  const role=document.getElementById('usr-role').value;
  if(!name||!pass){showToast('Unesite korisnika i lozinku!');return;}
  // Server vraća {ok:false,error} (npr. degradiranje zadnjeg admina) — ne javljaj uspjeh naslijepo
  let d=null;
  try{
    const r=await fetch('/api/postavke/korisnici',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:name,password:pass,uloga:role})});
    d=await r.json();
  }catch(e){console.error(e);}
  if(!d||!d.ok){showToast('✗ '+(d?.error||'Korisnik nije spremljen'));return;}
  document.getElementById('usr-name').value='';
  document.getElementById('usr-pass').value='';
  showToast('Korisnik dodan!');
  loadKorisnici();
}

async function deleteKorisnik(name){
  if(!confirm('Obrisati korisnika '+name+'?'))return;
  let d=null;
  try{
    const r=await fetch('/api/postavke/korisnici?username='+name,{method:'DELETE'});
    d=await r.json();
  }catch(e){console.error(e);}
  if(!d||!d.ok) showToast('✗ '+(d?.error||'Korisnik nije obrisan'));
  loadKorisnici();
}
async function autoBackup(){
  const r=await fetch('/api/postavke/backup/auto',{method:'POST'});
  const d=await r.json();
  const el=document.getElementById('backup-status');
  if(d.ok){el.textContent='✓ Backup: '+d.path;showToast('Backup uspješan!');}
  else el.textContent='✗ Greška: '+d.error;
}
function setupAutoBackup(){showToast('Automatski backup radi svaki dan kroz sync_loop.sh');}

function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3000);}

let syncTimer = null;

function toggleTheme(){
  const html=document.documentElement;
  const isLight=html.getAttribute('data-theme')==='light';
  html.setAttribute('data-theme', isLight?'dark':'light');
  document.getElementById('themeToggle').textContent=isLight?'🌙':'☀️';
  localStorage.setItem('theme', isLight?'dark':'light');
  // Redraw charts
  Object.values(CH).forEach(c=>c&&c.update());
}

// Inicijalizacija teme
(function(){
  const saved=localStorage.getItem('theme')||'dark';
  document.documentElement.setAttribute('data-theme',saved);
  // Postavi toggle emoji kad DOM bude spreman
  document.addEventListener('DOMContentLoaded',()=>{
    const btn=document.getElementById('themeToggle');
    if(btn) btn.textContent=saved==='light'?'☀️':'🌙';
  });
})();

function changeSyncInterval() {
  const sek = +document.getElementById('syncInterval').value;
  if (syncTimer) clearInterval(syncTimer);
  if (sek > 0) syncTimer = setInterval(load, sek * 1000);
}

// ===== HEP ODS: Peak snaga, OMM info, VT/NT trošak =====

async function loadHepStanje() {
  const box = document.getElementById('hep-stanje');
  if (!box) return;
  const d = await dohvati('/api/racuni/stanje', 'card-hep-stanje');
  if (!d) return;
  try {
    if (!d.has_data) {
      box.innerHTML = `<em>Nema unesenog HEP računa — uploadaj PDF u sekciji "Dodaj stvarni HEP račun".</em>
        <div style="margin-top:8px">Mjesečna pretplata po default tarifima: <strong style="color:var(--text)">${novac(d.mjesecna_pretplata)}</strong>
        (opskrbna ${novac(d.pretplata_opskrba)} + mjerna ${novac(d.pretplata_mjerna)}).</div>`;
      return;
    }
    const dugColor = d.dug > 0 ? '#f87171' : '#22c55e';
    const danasISO = localDate();
    const overdueDays = d.datum_dospijeca && d.datum_dospijeca < danasISO
      ? Math.floor((new Date(danasISO) - new Date(d.datum_dospijeca)) / 86400000)
      : null;
    const dospijece_html = d.datum_dospijeca
      ? `<strong style="color:${overdueDays > 0 ? '#f87171' : 'var(--text)'}">${esc(d.datum_dospijeca)}</strong>`
        + (overdueDays > 0 ? ` <span style="color:#f87171">(zakasnilo ${overdueDays} d)</span>` : '')
      : '—';
    box.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:14px">
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Zadnji račun</div>
          <div style="font-size:22px;color:var(--text);font-family:var(--mono);margin-top:4px">${novac(d.iznos||0)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">period ${esc(d.period||'—')} · ${esc(d.datum_racuna||'—')}</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Nepodmireni dug</div>
          <div style="font-size:22px;color:${dugColor};font-family:var(--mono);margin-top:4px">${novac(d.dug ?? 0)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">${d.kamata ? 'uklj. kamata '+novac(d.kamata) : 'na dan računa'}</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Dospijeće</div>
          <div style="font-size:16px;margin-top:4px;font-family:var(--mono)">${dospijece_html}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">datum plaćanja računa</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Mjesečna pretplata</div>
          <div style="font-size:22px;color:#22d3ee;font-family:var(--mono);margin-top:4px">${novac(d.mjesecna_pretplata)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">opskrba ${d.pretplata_opskrba.toFixed(3)} + mjerna ${d.pretplata_mjerna.toFixed(3)}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--muted);font-family:var(--mono)">
        ${d.model_tarife ? '⚡ '+esc(d.model_tarife)+' · ' : ''}
        ${d.broj_racuna ? 'br. '+esc(d.broj_racuna)+' · ' : ''}
        ${d.sifra_kupca ? 'šifra kupca '+esc(d.sifra_kupca) : ''}
      </div>`;
  } catch(e) {
    console.error(e);
    stanje(box.parentElement, 'greska');
  }
}

async function loadProcjenaTrenutni() {
  const box = document.getElementById('hep-procjena');
  if (!box) return;
  const d = await dohvati('/api/stats/procjena-trenutni', 'card-procjena');
  if (!d) return;
  try {
    // ima_podataka=false: mjesec je tek počeo, HEP još nije poslao očitanja (procjena bi bile samo fiksne naknade)
    if (d.ima_podataka === false || (!d.kwh_plus_proteklo && d.kwh_plus_proteklo !== 0)) {
      box.innerHTML = '<em>Nema podataka iz HEP scrapera za tekući mjesec.</em>';
      return;
    }
    // racun_procjena VEĆ uključuje fiksne naknade (opskrbna + mjerna), kao i pravi račun —
    // ne pribrajati ih ponovno

    // HEPI bijeli specific
    let bijeli_html = '';
    if (d.bijeli_procjena) {
      const bp = d.bijeli_procjena;
      const op = d.otkup_procjena;
      const neto = bp.iznos - (op?.ukupno_eur || 0);
      bijeli_html = `
      <div style="background:var(--bg);border:1px solid #22d3ee44;border-radius:8px;padding:14px;margin-top:10px">
        <div style="font-size:13px;color:#22d3ee;font-weight:600;margin-bottom:10px">⚡ HEPI bijeli — net-metering izračun</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;font-size:12px">
          <div><div style="color:var(--muted);font-size:11px">Saldo VT</div><div style="font-family:var(--mono);color:${bp.saldo_vt>=0?'#f87171':'#22c55e'}">${bp.saldo_vt>=0?'+':''}${bp.saldo_vt} kWh</div></div>
          <div><div style="color:var(--muted);font-size:11px">Saldo NT</div><div style="font-family:var(--mono);color:${bp.saldo_nt>=0?'#f87171':'#22c55e'}">${bp.saldo_nt>=0?'+':''}${bp.saldo_nt} kWh</div></div>
          <div><div style="color:var(--muted);font-size:11px">Opskrba</div><div style="font-family:var(--mono)">${novac(bp.opskrba)}</div></div>
          <div><div style="color:var(--muted);font-size:11px">Mreža</div><div style="font-family:var(--mono)">${novac(bp.mreza)}</div></div>
          <div><div style="color:var(--muted);font-size:11px">PDV 13%</div><div style="font-family:var(--mono)">${novac(bp.pdv)}</div></div>
          <div><div style="color:var(--muted);font-size:11px">Račun (procjena)</div><div style="font-family:var(--mono);color:#fbbf24">${novac(bp.iznos)}</div></div>
          ${op ? `
          <div><div style="color:var(--muted);font-size:11px">Otkup VT viška</div><div style="font-family:var(--mono);color:#22c55e">${op.vt_kwh} kWh → ${novac(op.vt_eur)}</div></div>
          <div><div style="color:var(--muted);font-size:11px">Otkup NT viška</div><div style="font-family:var(--mono);color:#22c55e">${op.nt_kwh} kWh → ${novac(op.nt_eur)}</div></div>` : ''}
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:13px">
          <strong>Neto za platiti: <span style="color:${neto>=0?'#f87171':'#22c55e'};font-family:var(--mono)">${novac(neto)}</span></strong>
          <span style="color:var(--muted);font-size:11px"> · račun ${novac(bp.iznos)}${op?.ukupno_eur ? ' − otkup '+novac(op.ukupno_eur) : ''} · u računu je fiksno ${novac(d.fiksne_naknade)} + PDV</span>
        </div>
      </div>`;
    }

    box.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;font-family:var(--mono)">${esc(d.model)}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:10px">
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Proteklo (${d.dani_s_podacima}/${d.n_dana_mjesec} dana)</div>
          <div style="font-size:22px;color:var(--text);font-family:var(--mono);margin-top:4px">${novac(d.racun_proteklo)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">potrošnja ${d.kwh_plus_proteklo} kWh · predaja ${d.kwh_minus_proteklo} kWh</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Procjena cijeli mjesec</div>
          <div style="font-size:22px;color:#fbbf24;font-family:var(--mono);margin-top:4px">${novac(d.racun_procjena)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">uključuje fiksno ${novac(d.fiksne_naknade)} pretplate + PDV</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Projicirano kWh</div>
          <div style="font-size:18px;color:var(--text);font-family:var(--mono);margin-top:4px">${d.kwh_plus_procjena} ↓ / ${d.kwh_minus_procjena} ↑</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">potrošnja / predaja u mrežu</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">VT udio stvarni</div>
          <div style="font-size:22px;color:#22d3ee;font-family:var(--mono);margin-top:4px">${d.vt_udio_stvarni}%</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">VT ${d.kwh_vt_proteklo} / NT ${d.kwh_nt_proteklo} kWh</div>
        </div>
      </div>
      ${bijeli_html}
      <div style="font-size:12px;color:var(--muted);margin-top:10px">
        Procjena vrijedi za <strong>${esc(d.mjesec)}</strong>, ${esc(d.postotak_mjeseca)}% mjeseca odrađeno.
        Tarife: VT ${(d.tarifa.vt_kwh*100).toFixed(2)} c/kWh · NT ${(d.tarifa.nt_kwh*100).toFixed(2)} c/kWh · VT otkup ${(d.tarifa.vt_otkup*100).toFixed(2)} c/kWh · PDV ${(d.tarifa.pdv*100).toFixed(0)}%.
      </div>`;
  } catch(e) {
    console.error(e);
    stanje(box.parentElement, 'greska');
  }
}

async function loadCijene() {
  const d = await dohvati('/api/stats/cijene', 'card-cijene');
  if (!d) return;
  try {
    set('cijene-src', `Izvor cijena: ${d.izvor}` + (d.vt_udio_izvor ? ` · VT udio: ${d.vt_udio_izvor}` : ''));
    set('cij-avg',   (d.avg_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-avg-sub', `s PDV ${(d.pdv*100).toFixed(0)}% · VT udio ${d.vt_udio_perc}%`);
    set('cij-vt',    (d.vt_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-nt',    (d.nt_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-otkup', (d.otkup*100).toFixed(2)+' c/kWh');
    set('cij-otkup-sub', `VT otkup ${(d.vt_otkup*100).toFixed(2)} c/kWh`);
    set('cij-pret',  novac(d.pretplata_ukupno)+'/mj');
    set('cij-pret-sub', `opskrba ${d.pretplata_opskrba.toFixed(3)} + mjerna ${d.pretplata_mjerna.toFixed(3)}`);

    const rows = [
      ['VT opskrba',  d.vt_opskrba],
      ['NT opskrba',  d.nt_opskrba],
      ['VT distribucija', d.vt_distrib],
      ['NT distribucija', d.nt_distrib],
      ['VT prijenos', d.vt_prijenos],
      ['NT prijenos', d.nt_prijenos],
      ['Solidarna naknada', d.solidarna],
      ['OIE naknada', d.oie],
      ['Opskrbna naknada (€/mj)', d.pretplata_opskrba],
      ['Mjerna mjernina (€/mj)',  d.pretplata_mjerna],
      ['PDV', d.pdv*100 + ' %'],
    ];
    document.getElementById('cij-detalji').innerHTML = rows.map(r =>
      `<tr><td style="padding:3px 8px">${r[0]}</td>
       <td style="padding:3px 8px;text-align:right">${typeof r[1]==='string'?r[1]:r[1].toFixed(6)+' €/kWh'}</td></tr>`
    ).join('');
  } catch(e) { console.error('cijene', e); stanje('card-cijene', 'greska'); }
}

const WMO_CODES = {
  0:['☀️','Vedro'], 1:['🌤️','Pretežno vedro'], 2:['⛅','Djelomično oblačno'], 3:['☁️','Oblačno'],
  45:['🌫️','Magla'], 48:['🌫️','Magla s injem'],
  51:['🌦️','Slaba rosulja'], 53:['🌦️','Rosulja'], 55:['🌧️','Jaka rosulja'],
  61:['🌧️','Slaba kiša'], 63:['🌧️','Kiša'], 65:['🌧️','Jaka kiša'],
  71:['🌨️','Slab snijeg'], 73:['🌨️','Snijeg'], 75:['❄️','Jak snijeg'],
  80:['🌦️','Pljuskovi'], 81:['🌦️','Jaki pljuskovi'], 82:['⛈️','Vrlo jaki pljuskovi'],
  95:['⛈️','Grmljavinsko nevrijeme'], 96:['⛈️','Grmljavina s tučom'], 99:['⛈️','Jaka grmljavina'],
};

async function loadWeather() {
  const d = await dohvati('/api/stats/weather', 'card-vrijeme');
  if (!d) { set('weather-desc', '—'); return; }
  try {
    const cur = d.current || {};
    const wmo = WMO_CODES[cur.weather_code] || ['🌡️','—'];
    set('weather-icon', wmo[0]);
    set('weather-temp', (cur.temperature_2m ?? '—') + '°C');
    set('weather-desc', `${wmo[1]} · vjetar ${(cur.wind_speed_10m ?? 0).toFixed(0)} km/h · oblačnost ${cur.cloud_cover ?? 0}%`);
    set('weather-loc', d.timezone ? d.timezone.replace('_',' ') : 'lokacija');

    // 7-dan strip
    const daily = d.daily || {};
    const days = daily.time || [];
    let html = '';
    for (let i = 0; i < days.length; i++) {
      const w = WMO_CODES[daily.weather_code[i]] || ['🌡️',''];
      const dt = new Date(days[i]);
      const isToday = i === 0;
      const dayLbl = isToday ? 'Danas' : dt.toLocaleDateString('hr-HR',{weekday:'short'}).replace('.','');
      const tmax = daily.temperature_2m_max[i];
      const tmin = daily.temperature_2m_min[i];
      const cloud = daily.cloud_cover_mean ? daily.cloud_cover_mean[i] : null;
      const sun_h = daily.sunshine_duration ? Math.round(daily.sunshine_duration[i]/3600) : null;
      const precip = daily.precipitation_sum ? daily.precipitation_sum[i] : 0;
      html += `
        <div style="background:var(--bg);border:1px solid ${isToday?'var(--accent)':'var(--border)'};border-radius:6px;padding:8px 4px;text-align:center" title="${w[1]}">
          <div style="font-size:10px;color:var(--muted);text-transform:uppercase;font-weight:${isToday?700:400}">${dayLbl}</div>
          <div style="font-size:24px;line-height:1;margin:4px 0">${w[0]}</div>
          <div style="font-size:12px;font-family:var(--mono)"><strong>${tmax?.toFixed(0)}°</strong></div>
          <div style="font-size:10px;color:var(--muted);font-family:var(--mono)">${tmin?.toFixed(0)}°</div>
          ${sun_h!=null?`<div style="font-size:10px;color:#fbbf24;margin-top:2px">☀️ ${sun_h}h</div>`:''}
          ${precip>0?`<div style="font-size:10px;color:#22d3ee">💧 ${precip.toFixed(1)}mm</div>`:''}
        </div>`;
    }
    document.getElementById('weather-7day').innerHTML = html;
  } catch(e) { console.error('weather', e); stanje('card-vrijeme', 'greska'); }
}

function _renderEnergyFlow(live) {
  const efStatus = document.getElementById('ef-status');
  if (!live) {
    document.getElementById('energy-flow').innerHTML =
      '<div style="color:var(--muted);font-size:13px;padding:40px 0;text-align:center;width:100%">Nema live podataka iz SMA.</div>';
    if (efStatus) efStatus.textContent = '⚠ nema podataka';
    return;
  }
  let pv     = Math.round(live.pv_generation_w || 0);
  const feed = Math.round(live.feed_in_w || 0);
  const grid = Math.round(live.external_consumption_w || 0);
  const battery = (live.battery_soc != null) ? live.battery_soc : null;

  // Sanity: ako feed > pv (bez baterije), PV je underreported (npr. drugi
  // inverter offline u HA-u). Korigiraj pv da reflectira stvarnu proizvodnju.
  let warning = '';
  if (pv < feed && battery == null) {
    const corrected = feed + grid;
    warning = `⚠ Drugi inverter offline u HA · PV korigiran (${pv} → ${corrected} W)`;
    pv = corrected;
  }

  // cons: prvi direktni sensor, inače computed iz pv-feed+grid
  let cons;
  if (live.total_consumption_w != null && live.total_consumption_w > 0) {
    cons = Math.round(live.total_consumption_w);
  } else {
    cons = Math.max(0, pv - feed + grid);
  }
  const direct = Math.max(0, Math.min(pv - feed, cons));     // kuća iz solara

  // Computed metrics
  const autarky = cons > 0 ? Math.max(0, Math.min(100, Math.round(100 * (1 - grid / cons)))) : (pv > 0 ? 100 : 0);
  const selfCons = pv > 0 ? Math.min(100, Math.round(100 * direct / pv)) : 0;

  const arrow = (active, dir, color = '#22d3ee') => active
    ? `<div style="font-size:24px;color:${color};animation:flowblink 1.5s infinite">${dir}</div>`
    : `<div style="font-size:24px;color:#1f2d3d">${dir}</div>`;

  document.getElementById('energy-flow').innerHTML = `
    <style>@keyframes flowblink{0%,100%{opacity:.4}50%{opacity:1}}</style>
    <div style="text-align:center;flex:1">
      <div style="font-size:42px">${pv > 0 ? '☀️' : '🌙'}</div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-top:4px">PV</div>
      <div style="font-size:22px;color:#fbbf24;font-family:var(--mono);font-weight:700">${pv.toLocaleString()} W</div>
    </div>
    ${arrow(pv > 0, '→', '#fbbf24')}
    <div style="text-align:center;flex:1">
      <div style="font-size:42px">🏠</div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-top:4px">Kuća</div>
      <div style="font-size:22px;color:#a78bfa;font-family:var(--mono);font-weight:700">${cons.toLocaleString()} W</div>
      <div style="font-size:10px;color:var(--muted);margin-top:2px">od toga solar: ${direct} W</div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:center;gap:6px;flex:0 0 auto;padding:0 8px">
      ${arrow(feed > 0, '↑', '#22c55e')}
      ${arrow(grid > 0, '↓', '#f87171')}
    </div>
    <div style="text-align:center;flex:1">
      <div style="font-size:42px">⚡</div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-top:4px">Mreža</div>
      <div style="font-size:14px;font-family:var(--mono);margin-top:2px">
        <div style="color:#22c55e">↑ ${feed.toLocaleString()} W</div>
        <div style="color:#f87171">↓ ${grid.toLocaleString()} W</div>
      </div>
    </div>
    ${battery != null ? `
    <div style="text-align:center;flex:1">
      <div style="font-size:42px">🔋</div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-top:4px">Baterija</div>
      <div style="font-size:22px;color:#22d3ee;font-family:var(--mono);font-weight:700">${Math.round(battery)}%</div>
    </div>` : ''}
    ${warning ? `<div style="position:absolute;bottom:-4px;left:0;right:0;text-align:center;font-size:11px;color:#fbbf24">${warning}</div>` : ''}
  `;

  // innerHTML umjesto textContent — strong tagovi se moraju renderirati
  const autEl = document.getElementById('ef-autarkija');
  const scEl  = document.getElementById('ef-selfcons');
  if (autEl) autEl.innerHTML = `Autarkija: <strong style="color:#22c55e">${autarky}%</strong>`;
  if (scEl)  scEl.innerHTML  = `Self-consumption: <strong style="color:#fbbf24">${selfCons}%</strong>`;

  // Last refresh
  const ts = live.ts ? new Date(live.ts.replace('+00:00','Z').replace(/\.\d+$/,'')) : null;
  const efRefresh = document.getElementById('ef-refresh');
  if (ts && !isNaN(ts)) {
    const ageSec = Math.round((Date.now() - ts.getTime()) / 1000);
    const ageStr = ageSec < 60 ? ageSec+'s' : ageSec < 3600 ? Math.round(ageSec/60)+'min' : Math.round(ageSec/3600)+'h';
    const fresh = ageSec < 360;  // 6 min
    if (efStatus)  efStatus.textContent = fresh ? `🟢 LIVE (${ageStr} ago)` : `🟡 STALE (${ageStr} ago)`;
    if (efRefresh) efRefresh.textContent = `Posljednji refresh: ${ts.toLocaleTimeString('hr-HR')}`;
  } else {
    if (efStatus)  efStatus.textContent = '⏳';
    if (efRefresh) efRefresh.textContent = '—';
  }
}

async function loadEnergyFlow() {
  // Dohvati svjež zapis direktno — ne pouzdaj se na D.sma_live koji može biti par minuta star
  set('ef-status', '⏳ refresh…');
  try {
    const r = await fetch('/api/sma/live', {credentials:'same-origin'});
    const j = await r.json();
    _renderEnergyFlow(j.error ? null : j);
  } catch(e) {
    // fallback na D; bez ikakvih podataka → poruka o grešci umjesto "nema SMA"
    if (D && D.sma_live) _renderEnergyFlow(D.sma_live);
    else {
      document.getElementById('energy-flow').innerHTML =
        '<div class="st-linija" style="text-align:center">'+ST_TEKST.greska+'</div>';
      set('ef-status', '⚠');
    }
  }
}

let _powLimitChart = null;
async function loadPowerLimit() {
  const NASLOV = '⚙️ Ograničenje snage — zadnjih 24h: ';
  const d = await dohvati('/api/stats/power-limit-history', 'card-powlimit');
  if (!d) return;
  try {
    const info = document.getElementById('pl-info');
    // bez podataka: jedan prigušeni red umjesto visoke prazne kartice
    if (d.note || !d.series || !d.series.length) {
      stanje('card-powlimit', 'prazno', NASLOV + 'nema podataka' + (d.note ? ' (' + d.note + ')' : ''));
      return;
    }

    // HA last_changed je UTC (+00:00) — labela iz trenutka u lokalnom vremenu, ne rezanjem stringa
    const labels = d.series.map(s => {
      const t = s.ts ? new Date(s.ts) : new Date(NaN);
      return isNaN(t) ? String(s.ts||'').slice(11,16) : t.toLocaleTimeString('hr-HR',{hour:'2-digit',minute:'2-digit'});
    });
    const data   = d.series.map(s => s.w / 1000);  // W → kW
    if (_powLimitChart) _powLimitChart.destroy();
    _powLimitChart = new Chart(document.getElementById('cPowLimit'), {
      type: 'line',
      data: { labels, datasets: [{
        label: 'Power limit (kW)', data,
        borderColor: '#22d3ee', backgroundColor: 'rgba(34,211,238,.15)',
        borderWidth: 2, pointRadius: 0, tension: .2, fill: true,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { beginAtZero: true, ticks: { callback: v => v + ' kW' } } },
        plugins: { legend: { display: false } },
      },
    });
    const min = Math.min(...data), max = Math.max(...data);
    info.textContent = `Entity: ${d.entity} · raspon: ${min.toFixed(1)}–${max.toFixed(1)} kW (${d.series.length} zapisa)`;
  } catch(e) { console.error('power-limit', e); stanje('card-powlimit', 'greska'); }
}

async function loadPlantInfo() {
  const ciljevi = ['card-postrojenje', 'card-eko', 'kpi-g-ukupno'];
  const d = await dohvati('/api/stats/plant-info', ciljevi);
  if (!d) return;
  try {
    set('pi-nom',  d.nominal_kw ? d.nominal_kw + ' kWp' : '—');
    set('pi-comm', d.commission_date || '—');
    if (d.power_limit_w != null) {
      set('pi-plim', `${(d.power_limit_w/1000).toFixed(1)} kW`
          + (d.power_limit_perc != null ? ` (${d.power_limit_perc}%)` : ''));
    } else {
      set('pi-plim', '—');
    }
    set('pi-suff', d.sufficiency_perc != null ? d.sufficiency_perc + '%' : '—');
    set('pi-cons', d.consumption_perc != null ? d.consumption_perc + '%' : '—');

    set('pi-co2t', d.co2_today_kg + ' kg');
    set('pi-co2u', d.co2_total_kg >= 1000 ? (d.co2_total_kg/1000).toFixed(2)+' t' : d.co2_total_kg+' kg');
    set('pi-rt',   novac(d.reimbursement_today_eur));
    set('pi-ru',   novac(d.reimbursement_total_eur));
    set('pi-rate-info', `CO₂ faktor ${d.co2_factor_g_kwh} g/kWh · otkup ${(d.feed_tariff*100).toFixed(2)} c/kWh`);

    // PV danas / ovaj mjesec su u kartici "Danas" i grupi "Ovaj mjesec" — ovdje samo godina i ukupno
    set('pi-pv-y', f1(d.pv_year_kwh));
    set('pi-pv-t', f1(d.pv_total_kwh));
  } catch(e) { console.error('plant-info', e); ciljevi.forEach(c => stanje(c, 'greska')); }
}

async function loadPeakSnaga() {
  const d = await dohvati('/api/stats/peak-snaga', 'card-peak');
  if (!d) return;
  try {
    const setKpi = (id, idTs, period, key) => {
      const el  = document.getElementById(id);
      const ets = document.getElementById(idTs);
      const v = d[period];
      if (!v) return;
      el.textContent  = (v[key+'_kw'] ?? '—') + ' kW';
      ets.textContent = v[key+'_ts'] ? v[key+'_ts'].replace('T',' ').slice(0,16) : '—';
    };
    setKpi('pk-week',  'pk-week-ts',  'tjedan',  'peak_potrosnja');
    setKpi('pk-month', 'pk-month-ts', 'mjesec',  'peak_potrosnja');
    setKpi('pk-year',  'pk-year-ts',  'godina',  'peak_potrosnja');
    setKpi('pk-pred',  'pk-pred-ts',  'godina',  'peak_predaja');
  } catch(e) { console.error('peak-snaga', e); stanje('card-peak', 'greska'); }
}

async function loadOmmInfo() {
  const box = document.getElementById('omm-info');
  if (!box) return;
  try {
    const r = await fetch('/api/postavke/mjerno-mjesto', {credentials:'same-origin'});
    const rows = await r.json();
    if (!rows.length) { box.innerHTML = '<em>Nema mjernih mjesta — pokreni HEP scraper.</em>'; return; }
    let html = '';
    for (const m of rows) {
      const tipBadge = m.tip === 'Potrosac'
        ? '<span style="color:#22d3ee">Potrošač</span>'
        : '<span style="color:#fbbf24">Proizvođač</span>';
      html += `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px">
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Kupac</div><div style="font-family:var(--mono);color:var(--text)">${esc(m.naziv||'—')}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Šifra OMM</div><div style="font-family:var(--mono);color:var(--text)">${esc(m.id)}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">OIB</div><div style="font-family:var(--mono)">${esc(m.oib||'—')}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Tip</div><div>${tipBadge}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Adresa</div><div style="font-size:12px">${esc(m.adresa||'—')}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Raspon očitanja</div><div style="font-family:var(--mono);font-size:12px">${esc((m.prvi_zapis||'').slice(0,10))} → ${esc((m.zadnji_zapis||'').slice(0,10))}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">15-min zapisa</div><div style="font-family:var(--mono)">${(m.n_15min||0).toLocaleString()}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Ukupna potrošnja</div><div style="font-family:var(--mono);color:#22d3ee">${(m.uk_potrosnja||0).toLocaleString()} kWh</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Ukupna predaja</div><div style="font-family:var(--mono);color:#fbbf24">${(m.uk_predaja||0).toLocaleString()} kWh</div></div>
      </div>`;
    }
    box.innerHTML = html;
  } catch(e) { console.error('omm info', e); box.textContent = ST_TEKST.greska; }
}

let _vtntChart = null;
async function loadVtNtTrosak() {
  const d = await dohvati('/api/stats/vt-nt-trosak', 'card-vtnt');
  if (!d) return;
  try {
    const mj = (d.mjeseci || []).slice().reverse();  // chronological for chart
    document.getElementById('vtnt-info').textContent =
      `VT prozor: ${d.vt_od}-${d.vt_do}h · cijene: VT ${(d.tarife.vt_kwh_eur*100).toFixed(2)} c/kWh · NT ${(d.tarife.nt_kwh_eur*100).toFixed(2)} c/kWh · otkup ${(d.tarife.otkup*100).toFixed(2)} c/kWh`;

    const ctx = document.getElementById('cVtNt');
    if (_vtntChart) _vtntChart.destroy();
    _vtntChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: mj.map(m => m.mjesec),
        datasets: [
          { label: 'VT trošak (€)',   data: mj.map(m => m.vt_eur),      backgroundColor: 'rgba(248,113,113,.7)', stack:'cost' },
          { label: 'NT trošak (€)',   data: mj.map(m => m.nt_eur),      backgroundColor: 'rgba(34,211,238,.7)',  stack:'cost' },
          { label: 'Vrijednost predaje (€)', data: mj.map(m => m.predaja_eur), backgroundColor: 'rgba(34,197,94,.7)', stack:'predaja' },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { x:{stacked:true}, y:{stacked:true, ticks:{callback:v=>v+' €'}} },
      },
    });

    // Tablica (kronoloski desc)
    const tbody = document.getElementById('tVtNt');
    tbody.innerHTML = (d.mjeseci || []).map(m => `
      <tr>
        <td style="font-family:var(--mono)">${esc(m.mjesec)}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.vt_kwh}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.nt_kwh}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.vt_perc ?? '—'}%</td>
        <td style="text-align:right;font-family:var(--mono);color:#f87171">${novac(m.vt_eur)}</td>
        <td style="text-align:right;font-family:var(--mono);color:#22d3ee">${novac(m.nt_eur)}</td>
        <td style="text-align:right;font-family:var(--mono);color:#22c55e">${novac(m.predaja_eur)}</td>
      </tr>`).join('');
  } catch(e) {
    console.error(e);
    stanje('card-vtnt', 'greska');
  }
}

loadOmmList();
load();
syncTimer = setInterval(load, 60000);
// Pregled je default aktivan tab pa se goTo('pregled') NE okida na prvom
// učitavanju — zato ovdje ručno pozovi sve njegove loadere (inače su
// Informacije o postrojenju / Eko & Financije / PV kartice prazne do prvog
// prebacivanja taba).
loadPeakSnaga();
loadPlantInfo();
loadCijene();
loadWeather();
loadPowerLimit();
// Auto-refresh Energy flow svakih 10 sekundi (samo kad je Pregled aktivan)
setInterval(() => {
  if (document.getElementById('page-pregled')?.classList.contains('active')) {
    loadEnergyFlow();
  }
}, 10000);
