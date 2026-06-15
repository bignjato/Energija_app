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
  if(name==='financije') { renderFin(); loadVtNtTrosak(); loadHepStanje(); loadProcjenaTrenutni(); }
  if(name==='racuni'&&!window.RACUNI) loadRacuni();
  if(name==='dug') loadDug();
  if(name==='optimalno'&&!OPT) loadOpt();
  if(name==='povijest'&&!POV) { brziIzbor('30dana'); }
  if(name==='postavke') { loadPostavke(); loadOmmInfo(); }
  if(name==='pregled') { loadPeakSnaga(); loadPlantInfo(); loadCijene(); loadWeather(); loadEnergyFlow(); loadPowerLimit(); }
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
      return `<option value="${o.id}">${ime}${adr} (${o.id})</option>`;
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
async function load(){
  try{
    const r=await fetch(withOmm('/api/data'));
    D=await r.json();
    renderPregled();
    set('lastUpdate', new Date().toLocaleTimeString('hr-HR'));
  }catch(e){console.error(e);}
}

async function loadOpt(){
  try{
    const r=await fetch('/api/stats/optimalno');
    OPT=await r.json();
    renderOpt();
  }catch(e){console.error(e);}
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
  set('k-tj-h',f1(sumK(tjH,'kwh_plus')));
  set('k-tj-s',f1(sumK(tjS,'pv_generation_kwh')));
  set('k-tj-p',f1(sumK(tjH,'kwh_minus')));
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
  const all=D.sma_dnevna.filter(r=>r.pv_generation_kwh>0);
  set('k-pr-s',f1(all.length?sumK(all,'pv_generation_kwh')/all.length:0));

  mkChart('cSatni','line',D.satna.slice(-96).map(r=>r.ts.slice(11,16)),[
    {label:'HEP potrošnja (kWh)',data:D.satna.slice(-96).map(r=>r.kwh_plus||0),borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,.07)',fill:true,tension:.4,pointRadius:0,borderWidth:2},
    {label:'SMA solar (kWh)',data:D.sma_satna.slice(-96).map(r=>(r.pv_w||0)/1000),borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.07)',fill:true,tension:.4,pointRadius:0,borderWidth:2},
  ],'kWh');

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
async function renderUsporedba(){
  try{
    const r=await fetch('/api/stats/usporedba');
    const rows=await r.json();
    if(!Array.isArray(rows)||!rows.length)return;
    window._usporedbaRows = rows;
    renderUsporedbaPeriod();
  }catch(e){console.error(e);}
}

// Period filter za Usporedba tab
window._usporedbaPeriod = window._usporedbaPeriod || 'month';

function _periodRange(period) {
  const danas = new Date(today());
  const y = danas.getFullYear(), m = danas.getMonth();
  let od, do_, label;
  if (period === 'day')   { od = today(); do_ = today(); label = 'danas'; }
  else if (period === 'week') {
    const d = new Date(danas);
    const dow = (d.getDay() + 6) % 7;   // ponedjeljak = 0
    d.setDate(d.getDate() - dow);
    od = d.toISOString().slice(0,10); do_ = today();
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
  if (!rows.length) return;
  const { od, do_, label } = _periodRange(window._usporedbaPeriod || 'month');
  const filtered = rows.filter(r => r.datum >= od && r.datum <= do_);

  document.getElementById('u-period-info').textContent =
    `${od} → ${do_}  ·  ${filtered.length} dana`;
  document.getElementById('u-chart-title').textContent = `Usporedba HEP vs SMA — ${label}`;
  document.getElementById('u-table-title').textContent = `Tablica usporedbe — ${label}`;
  ['u-pred-lbl','u-pot-lbl','u-sol-lbl'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = el.textContent.replace(/·.*$/, '').trim() + ' · ' + label;
  });

  const tHP=sumK(filtered,'hep_predaja'),tSP=filtered.reduce((a,b)=>a+(b.sma_predaja||0),0);
  const tHK=sumK(filtered,'hep_potrosnja'),tSK=filtered.reduce((a,b)=>a+(b.sma_mreza||0),0);
  const tSol=filtered.reduce((a,b)=>a+(b.sma_proizvodnja||0),0);
  const aAut=avgK(filtered.filter(r=>r.autarky_rate),'autarky_rate')*100;
  set('u-pred-d',sgn(tHP-tSP)+f1(tHP-tSP)+' kWh');
  set('u-pot-d', sgn(tHK-tSK)+f1(tHK-tSK)+' kWh');
  set('u-sol-t', f1(tSol)+' kWh');
  set('u-aut',   f0(aAut)+'%');

  mkChart('cUsporedba','line',filtered.map(r=>r.datum.slice(5)),[
    {label:'HEP potrošnja',data:filtered.map(r=>r.hep_potrosnja||0),borderColor:'#a78bfa',pointRadius:0,borderWidth:2,tension:.3},
    {label:'SMA mreža',data:filtered.map(r=>r.sma_mreza||0),borderColor:'#f87171',pointRadius:0,borderWidth:1.5,tension:.3,borderDash:[4,2]},
    {label:'HEP predaja',data:filtered.map(r=>r.hep_predaja||0),borderColor:'#38bdf8',pointRadius:0,borderWidth:2,tension:.3},
    {label:'SMA solar',data:filtered.map(r=>r.sma_proizvodnja||0),borderColor:'#fbbf24',pointRadius:0,borderWidth:2,tension:.3},
  ],'kWh');

  const tb=document.getElementById('tUsporedba');
  tb.innerHTML='';
  filtered.slice().reverse().forEach(row=>{
    const dp=(row.hep_potrosnja||0)-(row.sma_mreza||0);
    const dr=(row.hep_predaja||0)-(row.sma_predaja||0);
    tb.innerHTML+=`<tr>
      <td>${row.datum}</td>
      <td class="c-consume">${f1(row.hep_potrosnja)}</td><td>${f1(row.sma_mreza)}</td>
      <td class="${Math.abs(dp)>2?(dp>0?'neg':'pos'):'c-muted'}">${sgn(dp)+f1(dp)}</td>
      <td class="c-feed">${f1(row.hep_predaja)}</td><td>${f1(row.sma_predaja)}</td>
      <td class="${Math.abs(dr)>2?(dr>0?'neg':'pos'):'c-muted'}">${sgn(dr)+f1(dr)}</td>
      <td class="c-solar">${f1(row.sma_proizvodnja)}</td>
      <td>${row.autarky_rate?f0(row.autarky_rate*100)+'%':'—'}</td>
    </tr>`;
  });
}

// ---- FINANCIJE ----
function renderFin(){
  if(!D)return;
  // Cijene info kartice
  const ec = D.efektivne_cijene || {kupnja_avg:0.17, prodaja:0.065, vt_udio:45, model:'standardni'};
  set('ci-kup',   (ec.kupnja_avg*100).toFixed(2)+' c/kWh');
  set('ci-pro',   (ec.prodaja*100).toFixed(2)+' c/kWh');
  set('ci-vt',    ec.vt_udio+'%');
  set('ci-model', ec.model === 'bijeli' ? 'HEPI bijeli' : 'Standardni');
  calcFin();
}

function calcFin(){
  if(!D)return;
  const ec = D.efektivne_cijene || {kupnja_avg:0.17, prodaja:0.065};
  const cp = ec.kupnja_avg;
  const cs = ec.prodaja;
  const ms=mS(),ys=yS();
  const mjH=D.dnevna.filter(r=>r.datum>=ms),godH=D.dnevna.filter(r=>r.datum>=ys);
  const mjS=D.sma_dnevna.filter(r=>r.datum>=ms);
  const mK=sumK(mjH,'kwh_plus'),mP=sumK(mjH,'kwh_minus');
  const gK=sumK(godH,'kwh_plus'),gP=sumK(godH,'kwh_minus');
  const mSol=sumK(mjS,'pv_generation_kwh');
  set('fn-t-mj',eur(-mK*cp)); set('fn-t-mj-k',f1(mK)+' kWh');
  set('fn-p-mj',eur(mP*cs));  set('fn-p-mj-k',f1(mP)+' kWh');
  set('fn-u-mj',eur(mP*cs-mK*cp));
  set('fn-bez',eur(-(mK+mSol-mP)*cp));
  set('fn-t-god',eur(-gK*cp));
  set('fn-p-god',eur(gP*cs));
  set('fn-u-god',eur(gP*cs-gK*cp));
  const l30=D.dnevna.slice(-30);
  set('fn-prosj',eur(-sumK(l30,'kwh_plus')/30*cp));

  // --- PROJEKCIJA ---
  const now = new Date();
  const danasU  = now.getDate();                                          // dan u mjesecu (1-31)
  const ukupDana = new Date(now.getFullYear(), now.getMonth()+1, 0).getDate(); // ukupno dana u mj.
  const preostaoDana = ukupDana - danasU;
  const protekloDana = mjH.length || 1;

  // Prosječni dan u ovom mjesecu
  const prosj_k  = mK  / protekloDana;  // prosj. kupnja/dan
  const prosj_p  = mP  / protekloDana;  // prosj. predaja/dan
  const prosj_sol= mSol / protekloDana; // prosj. solar/dan

  // Projekcija do kraja (trenutno + ostatak)
  const proj_k   = mK   + prosj_k   * preostaoDana;
  const proj_p   = mP   + prosj_p   * preostaoDana;
  const proj_sol = mSol + prosj_sol  * preostaoDana;
  const proj_t   = proj_k * cp;
  const proj_pr  = proj_p * cs;
  const proj_net = proj_pr - proj_t;

  set('fn-proj-trosak',   eur(-proj_t));
  set('fn-proj-prihod',   eur(proj_pr));
  set('fn-proj-neto',     eur(proj_net));
  set('fn-proj-solar',    f1(proj_sol)+' kWh');
  set('fn-proj-potrosnja', f1(proj_k)+' kWh');
  set('fn-proj-info',     `Dan ${danasU}/${ukupDana} · još ${preostaoDana} dana`);

  // Prošlogodišnji isti mjesec
  const laniMj = `${now.getFullYear()-1}-${String(now.getMonth()+1).padStart(2,'0')}`;
  const laniH = D.dnevna.filter(r=>r.datum.startsWith(laniMj));
  if(laniH.length){
    const lK=sumK(laniH,'kwh_plus'), lP=sumK(laniH,'kwh_minus');
    set('fn-proj-lani', eur(lP*cs - lK*cp));
    set('fn-proj-lani-sub', `${laniMj} · ${f1(lK)} kWh / ${f1(lP)} kWh`);
  }

  // Progress bar
  const pct = Math.round(danasU / ukupDana * 100);
  set('fn-proj-pct', pct+'%');
  const bar = document.getElementById('fn-proj-bar');
  if(bar) bar.style.width = pct+'%';
  set('fn-proj-od', '1.'+String(now.getMonth()+1).padStart(2,'0')+'.'+now.getFullYear());
  set('fn-proj-do', ukupDana+'.'+String(now.getMonth()+1).padStart(2,'0')+'.'+now.getFullYear());

  const bm={};
  D.dnevna.forEach(r=>{const m=r.datum.slice(0,7);if(!bm[m])bm[m]={p:0,mi:0};bm[m].p+=r.kwh_plus||0;bm[m].mi+=r.kwh_minus||0;});
  const mos=Object.keys(bm).sort().slice(-18);
  mkChart('cFin','bar',mos,[
    {label:'Trošak (€)',data:mos.map(m=>-(bm[m].p*cp)),backgroundColor:'rgba(248,113,113,.7)',borderRadius:3},
    {label:'Prihod (€)',data:mos.map(m=>bm[m].mi*cs),backgroundColor:'rgba(52,211,153,.7)',borderRadius:3},
    {label:'Neto (€)',data:mos.map(m=>bm[m].mi*cs-bm[m].p*cp),type:'line',borderColor:'#22d3ee',pointRadius:3,borderWidth:2,tension:.3},
  ],'€');
}

// saveTarifa: maknuto — tarifa se sad uređuje u Postavkama (VT/NT slider + .env)

// ---- RAČUNI ----
async function loadRacuni(){
  try{
    const r=await fetch('/api/stats/mjesecni');
    window.RACUNI=await r.json();
    renderRacuni();
  }catch(e){console.error(e);}
  loadRegistri();
}

async function loadRegistri(){
  const tb=document.getElementById('tRegistri');
  if(!tb) return;
  try{
    const r=await fetch('/api/stats/registri');
    const d=await r.json();
    const mj=d.mjeseci||[];
    if(!mj.length){
      tb.innerHTML='<tr><td colspan="8" class="c-muted" style="padding:14px;text-align:center">Nema registara — pokreni HEP sync (podaci se pune mjesečno)</td></tr>';
      return;
    }
    tb.innerHTML='';
    mj.forEach(m=>{
      tb.innerHTML+=`<tr>
        <td>${m.mjesec}</td>
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
  window.RACUNI = null;
  loadRacuni();
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
    if (d.dug != null)             extras.push(`dug ${d.dug.toFixed(2)} €`);
    if (d.datum_dospijeca)         extras.push(`dospijeće ${d.datum_dospijeca}`);
    if (d.model_tarife)            extras.push(d.model_tarife);
    status.innerHTML = `✓ Prepoznato ${filled}/${allFields.length} polja iz "${d.filename}".`
      + (extras.length ? ` <span style="color:var(--muted)">(${extras.join(' · ')})</span>` : '')
      + ` Klikni "Spremi račun".`;
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
      (sat ${d.vt_od}-${d.vt_do}h, zadnjih ${valid.length} mj.):
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
        <td style="padding:4px;font-family:var(--mono)">${m.mjesec}</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${m.ukupno_kwh}</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${m.vt_simple_perc ?? '—'}%</td>
        <td style="padding:4px;text-align:right;font-family:var(--mono)">${m.vt_workdays_perc ?? '—'}%</td>
      </tr>`;
    }
    html += '</tbody></table>';
    box.innerHTML = html;
  } catch(e) {
    box.innerHTML = '<div style="color:#f87171">Greška: ' + e + '</div>';
  }
}

async function deleteRacun(period){
  if(!confirm('Obrisati račun '+period+'?')) return;
  await fetch('/api/racuni?period='+period, {method:'DELETE'});
  window.RACUNI = null;
  loadRacuni();
}

function renderRacuni(){
  const R=window.RACUNI;
  if(!R||!R.mjeseci) return;
  const mj=R.mjeseci.filter(r=>r.hep_potrosnja>0);
  const racuni=R.racuni||[];

  // KPI iz procjene
  const ukupno=mj.reduce((a,b)=>a+(b.procj_racun||0),0);
  const prihod=mj.reduce((a,b)=>a+(b.hep_predaja||0)*0.064379,0);
  const zadnjih12=mj.slice(0,12);
  const prosj=zadnjih12.reduce((a,b)=>a+(b.procj_racun||0),0)/Math.max(zadnjih12.length,1);
  const maxR=mj.reduce((a,b)=>b.procj_racun>a.val?{val:b.procj_racun,mj:b.mjesec}:a,{val:0,mj:''});
  set('r-ukupno', eur(-ukupno));
  set('r-prihod', eur(prihod));
  set('r-prosj', eur(-prosj));
  set('r-max', eur(-maxR.val));
  set('r-max-sub', maxR.mj);

  // Tablica unesenih računa
  const racuniMap = {};
  racuni.forEach(r => racuniMap[r.period] = r);

  const tb2=document.getElementById('tRacuniUnos');
  tb2.innerHTML='';
  if(racuni.length===0){
    tb2.innerHTML='<tr><td colspan="11" class="c-muted" style="padding:20px;text-align:center">Nema unesenih računa — unesite prvi račun gore</td></tr>';
  } else {
    racuni.forEach(r=>{
      const mjRow = mj.find(m=>m.mjesec===r.period);
      const procj = mjRow?.procj_racun||0;
      const diff = procj - r.iznos;
      const per = r.period.slice(5)+'/'+r.period.slice(0,4);
      tb2.innerHTML+=`<tr>
        <td style="font-weight:600">${per}</td>
        <td class="c-solar">${eur(-r.iznos)}</td>
        <td>${procj?eur(-procj):'—'}</td>
        <td class="${Math.abs(diff)<30?'c-green':Math.abs(diff)<80?'c-solar':'neg'}">${procj?sgn(diff)+f2(diff)+' €':'—'}</td>
        <td>${r.kwh_vt!=null?r.kwh_vt+' kWh':'—'}</td>
        <td>${r.kwh_nt!=null?r.kwh_nt+' kWh':'—'}</td>
        <td class="c-feed">${r.kwh_minus!=null?r.kwh_minus+' kWh':'—'}</td>
        <td>${r.opskrba!=null?eur(-r.opskrba):'—'}</td>
        <td>${r.mreza!=null?eur(-r.mreza):'—'}</td>
        <td>${r.pdv!=null?eur(-r.pdv):'—'}</td>
        <td><button onclick="deleteRacun('${r.period}')" style="background:none;border:none;color:var(--grid-out);cursor:pointer;font-size:14px">✕</button></td>
      </tr>`;
    });
  }

  // Graf
  const last24=mj.slice(0,24).reverse();
  mkChart('cRacuni','bar',last24.map(r=>r.mjesec),[
    {label:'Procj. račun (€)',data:last24.map(r=>-(r.procj_racun||0)),backgroundColor:'rgba(248,113,113,.7)',borderRadius:3},
    {label:'Stvarni račun (€)',data:last24.map(r=>{const rc=racuniMap[r.mjesec];return rc?-rc.iznos:null;}),backgroundColor:'rgba(251,191,36,.8)',borderRadius:3},
    {label:'Prihod predaja (€)',data:last24.map(r=>(r.hep_predaja||0)*0.064379),backgroundColor:'rgba(52,211,153,.7)',borderRadius:3},
  ],'€');

  // Tablica svih mjeseci
  const tb=document.getElementById('tRacuni');
  tb.innerHTML='';
  mj.forEach(row=>{
    const rc=racuniMap[row.mjesec];
    const diff=rc ? row.procj_racun-rc.iznos : null;
    tb.innerHTML+=`<tr>
      <td style="font-weight:600">${row.mjesec}</td>
      <td class="c-consume">${f1(row.hep_potrosnja)}</td>
      <td class="c-feed">${f1(row.hep_predaja)}</td>
      <td class="c-solar">${row.sma_pv!=null?f1(row.sma_pv):'—'}</td>
      <td class="c-muted">${row.sma_autarkija!=null?f0(row.sma_autarkija)+'%':'—'}</td>
      <td class="neg">${eur(-row.procj_racun)}</td>
      <td class="${rc?'c-solar':'c-muted'}">${rc?eur(-rc.iznos):'—'}</td>
      <td class="${diff!=null?(Math.abs(diff)<30?'c-green':Math.abs(diff)<80?'c-solar':'neg'):'c-muted'}">${diff!=null?sgn(diff)+f2(diff)+' €':'—'}</td>
    </tr>`;
  });
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
  const aut=hrs.map(h=>(sH[h]?.prosj_autarkija||0)*100);
  const mx=Math.max(...sol,.001);

  mkChart('cOptSol','bar',lbl,[{
    label:'Prosj. solar (kW)',data:sol,
    backgroundColor:hrs.map((_,h)=>`rgba(251,191,36,${.15+sol[h]/mx*.8})`),borderRadius:4,
  }],'kW');

  mkChart('cOptPot','bar',lbl,[{label:'Prosj. potrošnja (kWh)',data:pot,backgroundColor:'rgba(167,139,250,.6)',borderRadius:3}],'kWh');
  mkChart('cOptAut','line',lbl,[{label:'Autarkija (%)',data:aut,borderColor:'#34d399',backgroundColor:'rgba(52,211,153,.1)',fill:true,tension:.4,pointRadius:3,borderWidth:2}],'%');

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

async function loadPovijest(){
  const od  = document.getElementById('pov-od').value || localDate(new Date(Date.now()-30*86400000));
  const doo = document.getElementById('pov-do').value || localDate();
  const res = document.getElementById('pov-res').value || 'day';
  if(!od||!doo) return;

  try{
    const r=await fetch(withOmm(`/api/povijest?od=${od}&do=${doo}&res=${res}`));
    POV=await r.json();
    renderPovijest();
  }catch(e){console.error(e);}
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
  const t=D?.tarifa;
  const cp=t?.cijena_kupnja||0.12, cs=t?.cijena_prodaja||0.065;
  const finNeto=totM*cs-totP*cp;

  document.getElementById('pov-kpi').innerHTML=`
    <div class="kpi cons"><div class="kpi-lbl">Potrošnja iz mreže</div><div class="kpi-val c-consume">${f1(totP)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi feed"><div class="kpi-lbl">Predaja u mrežu</div><div class="kpi-val c-feed">${f1(totM)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi solar"><div class="kpi-lbl">Solar (SMA)</div><div class="kpi-val c-solar">${f1(totSol)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi ${neto>=0?'grn':'cons'}"><div class="kpi-lbl">Neto bilanca</div><div class="kpi-val ${neto>=0?'c-green':'c-grid'}">${sgn(neto)+f1(neto)}</div><div class="kpi-sub">kWh</div></div>
    <div class="kpi ${finNeto>=0?'grn':'cons'}"><div class="kpi-lbl">Financijski neto</div><div class="kpi-val ${finNeto>=0?'pos':'neg'}">${eur(finNeto)}</div><div class="kpi-sub">prihod − trošak</div></div>
    <div class="kpi acc"><div class="kpi-lbl">Period</div><div class="kpi-val" style="font-size:14px">${hep.length} ${res==='hour'?'sati':res==='week'?'tjedana':'dana'}</div><div class="kpi-sub">${od} — ${doo}</div></div>
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
  hep.forEach(row=>{
    const k=row[keyField]||row.tjedan||row.sat;
    const s=smaMap[k]||smaMap[k?.slice(0,10)];
    const pv=s?(s.pv_kwh||s.pv_generation_kwh||0):0;
    const inv1=s?(s.inv1_kwh||s.pv_kwh_inv1||0):0;
    const inv2=s?(s.inv2_kwh||s.pv_kwh_inv2||0):0;
    const n=(row.kwh_minus||0)-(row.kwh_plus||0);

    if(res==='week'){
      tb.innerHTML+=`<tr>
        <td style="font-weight:600">${row.tjedan}</td>
        <td class="c-muted">${row.datum_od||''}</td>
        <td class="c-consume">${f1(row.kwh_plus)}</td>
        <td class="c-feed">${f1(row.kwh_minus)}</td>
        <td class="c-solar">${pv?f1(pv):'—'}</td>
        <td class="c-muted">${row.n_dana}</td>
        <td class="${n>=0?'pos':'neg'}">${sgn(n)+f1(n)}</td>
      </tr>`;
    } else if(res==='hour'){
      const ts=row.ts||row.sat;
      tb.innerHTML+=`<tr>
        <td style="font-family:var(--mono)">${ts?ts.slice(0,16):''}</td>
        <td class="c-consume">${f2(row.kwh_plus)}</td>
        <td class="c-feed">${f2(row.kwh_minus)}</td>
        <td class="c-solar">${pv?f2(pv):'—'}</td>
      </tr>`;
    } else {
      tb.innerHTML+=`<tr>
        <td style="font-weight:600">${row.datum}</td>
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

  const [rA, rB] = await Promise.all([
    fetch(withOmm(`/api/povijest?od=${aOd}&do=${aDo}&res=day`)).then(r=>r.json()),
    fetch(withOmm(`/api/povijest?od=${bOd}&do=${bDo}&res=day`)).then(r=>r.json()),
  ]);

  const sumA={p:0,m:0,sol:0}, sumB={p:0,m:0,sol:0};
  rA.hep.forEach(r=>{sumA.p+=r.kwh_plus||0;sumA.m+=r.kwh_minus||0;});
  rA.sma.forEach(r=>{sumA.sol+=r.pv_generation_kwh||0;});
  rB.hep.forEach(r=>{sumB.p+=r.kwh_plus||0;sumB.m+=r.kwh_minus||0;});
  rB.sma.forEach(r=>{sumB.sol+=r.pv_generation_kwh||0;});

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
async function loadDug(){
  try{
    const r = await fetch('/api/stats/dug');
    window.DUG = await r.json();
    renderDug();
  }catch(e){ console.error(e); }
}

function renderDug(){
  const D2 = window.DUG;
  if(!D2 || !D2.trenutni){ return; }
  const t = D2.trenutni, v = D2.vrh, c = D2.cijene;
  set('dug-trenutni', eur(t.dug));
  set('dug-datum', (t.datum_racuna||t.period||'') + ' · račun ' + eur(t.iznos));
  set('dug-vrh', eur(v.dug));
  set('dug-vrh-mj', v.period);
  set('dug-pad', eur(t.dug - v.dug));
  const avgCharge = D2.godisnji_neto!=null ? D2.godisnji_neto/12 : null;
  set('dug-mjtrosak', avgCharge!=null ? eur(avgCharge) : '—');

  // cijene
  set('dug-c-vt-buy',  (c.vt_kupnja*100).toFixed(2)+' c');
  set('dug-c-nt-buy',  (c.nt_kupnja*100).toFixed(2)+' c');
  set('dug-c-avg-buy', (c.avg_kupnja*100).toFixed(2)+' c');
  set('dug-c-vt-sell', (c.vt_otkup*100).toFixed(2)+' c');
  set('dug-c-nt-sell', (c.nt_otkup*100).toFixed(2)+' c');
  set('dug-c-pretplata', eur(c.pretplata)+'/mj');
  const ratio = c.vt_otkup>0 ? (c.vt_kupnja/c.vt_otkup) : 0;
  set('dug-asimetrija',
    `Kupnja VT je ${ratio.toFixed(1)}× skuplja od otkupa VT viška — svaki kWh potrošen iz mreže vrijedi puno više nego predan. Isplati se trošiti dok sunce proizvodi.`);

  // breakeven
  set('dug-breakeven', avgCharge!=null ? eur(avgCharge)+'/mj' : '—');

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

  // postavi default uplatu na razumnu (pokriće + isplata u ~24 mj)
  if(avgCharge!=null){
    const sug = Math.ceil((avgCharge + t.dug/24)/10)*10;
    const sl = document.getElementById('dug-uplata');
    sl.value = Math.min(400, Math.max(0, sug));
  }
  renderDugProj();
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

function mkChart(id,type,labels,datasets,unit){
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
          callbacks:{label:c=>` ${c.dataset.label}: ${(+c.parsed.y).toFixed(2)} ${unit}`}
        }
      },
      scales:{
        x:{grid:{color:gridColor},ticks:{color:tickColor,font:{family:'IBM Plex Mono',size:11},maxTicksLimit:12}},
        y:{grid:{color:gridColor},ticks:{color:tickColor,font:{family:'IBM Plex Mono',size:11}}},
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
  try{
    // Konfiguracija
    const r=await fetch('/api/postavke');
    const cfg=await r.json();
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

    // Status sustava
    const s=await fetch('/api/postavke/status');
    const stat=await s.json();
    document.getElementById('status-kpi').innerHTML=`
      <div class="kpi acc"><div class="kpi-lbl">Verzija</div><div class="kpi-val" style="font-size:16px">${stat.version}</div><div class="kpi-sub">HEP Energy Monitor</div></div>
      <div class="kpi solar"><div class="kpi-lbl">HEP zadnji sync</div><div class="kpi-val" style="font-size:14px">${stat.hep_last_sync?stat.hep_last_sync.slice(0,16):'—'}</div><div class="kpi-sub">${stat.hep_range?.od||'—'} — ${stat.hep_range?.do||'—'}</div></div>
      <div class="kpi solar"><div class="kpi-lbl">SMA zadnji sync</div><div class="kpi-val" style="font-size:14px">${stat.sma_last_sync?stat.sma_last_sync.slice(0,16):'—'}</div><div class="kpi-sub">${stat.sma_range?.od?.slice(0,10)||'—'} — ${stat.sma_range?.do?.slice(0,10)||'—'}</div></div>
      <div class="kpi grn"><div class="kpi-lbl">Baza podataka</div><div class="kpi-val" style="font-size:16px">${stat.db_size_mb} MB</div><div class="kpi-sub">HEP: ${(stat.tables?.ocitanja_15min||0).toLocaleString()} zapisa</div></div>
      <div class="kpi cons"><div class="kpi-lbl">SMA 15-min</div><div class="kpi-val" style="font-size:16px">${(stat.tables?.sma_15min||0).toLocaleString()}</div><div class="kpi-sub">zapisa</div></div>
      <div class="kpi feed"><div class="kpi-lbl">SMA live</div><div class="kpi-val" style="font-size:16px">${(stat.tables?.sma_live||0).toLocaleString()}</div><div class="kpi-sub">zapisa</div></div>
    `;

    document.getElementById('app-info').innerHTML=`
      <div class="summary-item ok"><div class="label">URL</div><div class="value">${location.hostname}</div></div>
      <div class="summary-item ok"><div class="label">HEP ODS</div><div class="value">${cfg.HEP_USERNAME||'—'}</div></div>
      <div class="summary-item ${cfg.SMA_USERNAME?'ok':'skip'}"><div class="label">SMA</div><div class="value">${cfg.SMA_USERNAME||'Nije konfigurirano'}</div></div>
      <div class="summary-item ${cfg.HA_URL?'ok':'skip'}"><div class="label">Home Assistant</div><div class="value">${cfg.HA_URL||'Nije konfigurirano'}</div></div>
    `;

    // Korisnici
    loadKorisnici();
  }catch(e){console.error(e);}
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
    if(d.ok){
      document.getElementById('cfg-status').textContent='✓ Spremljeno';
      showToast('Konfiguracija spremljena!');
      setTimeout(()=>document.getElementById('cfg-status').textContent='',3000);
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
    out.innerHTML+=`▶ ${tip} import...<br>`;
    const r=await fetch(endpoints[tip],{method:'POST'});
    const d=await r.json();
    out.innerHTML+=`✓ ${d.info||'Gotovo'}<br>`;
    showToast('Import završen!');
  }catch(e){
    out.innerHTML+=`✗ Greška: ${e.message}<br>`;
  }
  out.scrollTop=out.scrollHeight;
}

async function loadKorisnici(){
  try{
    const r=await fetch('/api/postavke/korisnici');
    const korisnici=await r.json();
    const tb=document.getElementById('tKorisnici');
    tb.innerHTML='';
    if(!korisnici.length){
      tb.innerHTML='<tr><td colspan="4" style="padding:12px;color:var(--muted);text-align:center">Nema korisnika — koristi se DASHBOARD_PASSWORD iz .env</td></tr>';
      return;
    }
    korisnici.forEach(k=>{
      tb.innerHTML+=`<tr>
        <td style="padding:9px 12px">${k.username}</td>
        <td style="padding:9px 12px"><span style="background:${k.uloga==='admin'?'rgba(34,211,153,.15)':'rgba(82,96,112,.15)'};color:${k.uloga==='admin'?'var(--green)':'var(--muted)'};padding:2px 8px;border-radius:4px;font-size:11px">${k.uloga}</span></td>
        <td style="padding:9px 12px;color:var(--muted)">${k.zadnja_prijava?k.zadnja_prijava.slice(0,16):'Nikad'}</td>
        <td style="padding:9px 12px"><button onclick="deleteKorisnik('${k.username}')" style="background:none;border:none;color:var(--grid-out);cursor:pointer">✕</button></td>
      </tr>`;
    });
  }catch(e){console.error(e);}
}

async function addKorisnik(){
  const name=document.getElementById('usr-name').value.trim();
  const pass=document.getElementById('usr-pass').value;
  const role=document.getElementById('usr-role').value;
  if(!name||!pass){showToast('Unesite korisnika i lozinku!');return;}
  await fetch('/api/postavke/korisnici',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:name,password:pass,uloga:role})});
  document.getElementById('usr-name').value='';
  document.getElementById('usr-pass').value='';
  showToast('Korisnik dodan!');
  loadKorisnici();
}

async function deleteKorisnik(name){
  if(!confirm('Obrisati korisnika '+name+'?'))return;
  await fetch('/api/postavke/korisnici?username='+name,{method:'DELETE'});
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
  try {
    const r = await fetch('/api/racuni/stanje', {credentials:'same-origin'});
    const d = await r.json();
    if (!d.has_data) {
      box.innerHTML = `<em>Nema unesenog HEP računa — uploadaj PDF u sekciji "Dodaj stvarni HEP račun".</em>
        <div style="margin-top:8px">Mjesečna pretplata po default tarifima: <strong style="color:var(--text)">${d.mjesecna_pretplata} €</strong>
        (opskrbna ${d.pretplata_opskrba} € + mjerna ${d.pretplata_mjerna} €).</div>`;
      return;
    }
    const dugColor = d.dug > 0 ? '#f87171' : '#22c55e';
    const danasISO = new Date().toISOString().slice(0,10);
    const overdueDays = d.datum_dospijeca && d.datum_dospijeca < danasISO
      ? Math.floor((new Date(danasISO) - new Date(d.datum_dospijeca)) / 86400000)
      : null;
    const dospijece_html = d.datum_dospijeca
      ? `<strong style="color:${overdueDays > 0 ? '#f87171' : 'var(--text)'}">${d.datum_dospijeca}</strong>`
        + (overdueDays > 0 ? ` <span style="color:#f87171">(zakasnilo ${overdueDays} d)</span>` : '')
      : '—';
    box.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:14px">
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Zadnji račun</div>
          <div style="font-size:22px;color:var(--text);font-family:var(--mono);margin-top:4px">${(d.iznos||0).toFixed(2)} €</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">period ${d.period||'—'} · ${d.datum_racuna||'—'}</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Nepodmireni dug</div>
          <div style="font-size:22px;color:${dugColor};font-family:var(--mono);margin-top:4px">${(d.dug ?? 0).toFixed(2)} €</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">${d.kamata ? 'uklj. kamata '+d.kamata.toFixed(2)+' €' : 'na dan računa'}</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Dospijeće</div>
          <div style="font-size:16px;margin-top:4px;font-family:var(--mono)">${dospijece_html}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">datum plaćanja računa</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Mjesečna pretplata</div>
          <div style="font-size:22px;color:#22d3ee;font-family:var(--mono);margin-top:4px">${d.mjesecna_pretplata.toFixed(2)} €</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">opskrba ${d.pretplata_opskrba.toFixed(3)} + mjerna ${d.pretplata_mjerna.toFixed(3)}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--muted);font-family:var(--mono)">
        ${d.model_tarife ? '⚡ '+d.model_tarife+' · ' : ''}
        ${d.broj_racuna ? 'br. '+d.broj_racuna+' · ' : ''}
        ${d.sifra_kupca ? 'šifra kupca '+d.sifra_kupca : ''}
      </div>`;
  } catch(e) {
    box.innerHTML = '<em style="color:#f87171">Greška: '+e+'</em>';
  }
}

async function loadProcjenaTrenutni() {
  const box = document.getElementById('hep-procjena');
  if (!box) return;
  try {
    const r = await fetch('/api/stats/procjena-trenutni', {credentials:'same-origin'});
    const d = await r.json();
    if (!d.kwh_plus_proteklo && d.kwh_plus_proteklo !== 0) {
      box.innerHTML = '<em>Nema podataka iz HEP scrapera za tekući mjesec.</em>';
      return;
    }
    const ukupno_proj = (d.racun_procjena + d.fiksne_naknade).toFixed(2);

    // HEPI bijeli specific
    let bijeli_html = '';
    if (d.bijeli_procjena) {
      const bp = d.bijeli_procjena;
      const op = d.otkup_procjena;
      const neto = (bp.iznos + d.fiksne_naknade - (op?.ukupno_eur || 0)).toFixed(2);
      bijeli_html = `
      <div style="background:var(--bg);border:1px solid #22d3ee44;border-radius:8px;padding:14px;margin-top:10px">
        <div style="font-size:13px;color:#22d3ee;font-weight:600;margin-bottom:10px">⚡ HEPI bijeli — net-metering izračun</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;font-size:12px">
          <div><div style="color:var(--muted);font-size:11px">Saldo VT</div><div style="font-family:var(--mono);color:${bp.saldo_vt>=0?'#f87171':'#22c55e'}">${bp.saldo_vt>=0?'+':''}${bp.saldo_vt} kWh</div></div>
          <div><div style="color:var(--muted);font-size:11px">Saldo NT</div><div style="font-family:var(--mono);color:${bp.saldo_nt>=0?'#f87171':'#22c55e'}">${bp.saldo_nt>=0?'+':''}${bp.saldo_nt} kWh</div></div>
          <div><div style="color:var(--muted);font-size:11px">Opskrba</div><div style="font-family:var(--mono)">${bp.opskrba} €</div></div>
          <div><div style="color:var(--muted);font-size:11px">Mreža</div><div style="font-family:var(--mono)">${bp.mreza} €</div></div>
          <div><div style="color:var(--muted);font-size:11px">PDV 13%</div><div style="font-family:var(--mono)">${bp.pdv} €</div></div>
          <div><div style="color:var(--muted);font-size:11px">Račun (procjena)</div><div style="font-family:var(--mono);color:#fbbf24">${bp.iznos} €</div></div>
          ${op ? `
          <div><div style="color:var(--muted);font-size:11px">Otkup VT viška</div><div style="font-family:var(--mono);color:#22c55e">${op.vt_kwh} kWh → ${op.vt_eur} €</div></div>
          <div><div style="color:var(--muted);font-size:11px">Otkup NT viška</div><div style="font-family:var(--mono);color:#22c55e">${op.nt_kwh} kWh → ${op.nt_eur} €</div></div>` : ''}
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:13px">
          <strong>Neto za platiti: <span style="color:${neto>=0?'#f87171':'#22c55e'};font-family:var(--mono)">${neto} €</span></strong>
          <span style="color:var(--muted);font-size:11px"> · račun ${bp.iznos} € + fiksno ${d.fiksne_naknade} €${op?.ukupno_eur ? ' − otkup '+op.ukupno_eur+' €' : ''}</span>
        </div>
      </div>`;
    }

    box.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;font-family:var(--mono)">${d.model}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:10px">
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Proteklo (${d.dani_s_podacima}/${d.n_dana_mjesec} dana)</div>
          <div style="font-size:22px;color:var(--text);font-family:var(--mono);margin-top:4px">${d.racun_proteklo.toFixed(2)} €</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">potrošnja ${d.kwh_plus_proteklo} kWh · predaja ${d.kwh_minus_proteklo} kWh</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Procjena cijeli mjesec</div>
          <div style="font-size:22px;color:#fbbf24;font-family:var(--mono);margin-top:4px">${ukupno_proj} €</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">+ fiksno ${d.fiksne_naknade.toFixed(2)} € pretplate</div>
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
        Procjena vrijedi za <strong>${d.mjesec}</strong>, ${d.postotak_mjeseca}% mjeseca odrađeno.
        Tarife: VT ${(d.tarifa.vt_kwh*100).toFixed(2)} c/kWh · NT ${(d.tarifa.nt_kwh*100).toFixed(2)} c/kWh · VT otkup ${(d.tarifa.vt_otkup*100).toFixed(2)} c/kWh · PDV ${(d.tarifa.pdv*100).toFixed(0)}%.
      </div>`;
  } catch(e) {
    box.innerHTML = '<em style="color:#f87171">Greška: '+e+'</em>';
  }
}

async function loadCijene() {
  try {
    const r = await fetch('/api/stats/cijene', {credentials:'same-origin'});
    const d = await r.json();
    set('cijene-src', `Izvor: ${d.izvor}`);
    set('cij-avg',   (d.avg_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-avg-sub', `s PDV ${(d.pdv*100).toFixed(0)}% · VT udio ${d.vt_udio_perc}%`);
    set('cij-vt',    (d.vt_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-nt',    (d.nt_full_eur_kwh*100).toFixed(2)+' c/kWh');
    set('cij-otkup', (d.otkup*100).toFixed(2)+' c/kWh');
    set('cij-otkup-sub', `VT otkup ${(d.vt_otkup*100).toFixed(2)} c/kWh`);
    set('cij-pret',  d.pretplata_ukupno.toFixed(2)+' €/mj');
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
  } catch(e) { console.error('cijene', e); }
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
  try {
    const r = await fetch('/api/stats/weather', {credentials:'same-origin'});
    const d = await r.json();
    if (d.error) { set('weather-desc', 'API: '+d.error); return; }
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
  } catch(e) { set('weather-desc', 'Greška: '+e); }
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
    // fallback na D
    _renderEnergyFlow(D && D.sma_live);
  }
}

let _powLimitChart = null;
async function loadPowerLimit() {
  try {
    const r = await fetch('/api/stats/power-limit-history', {credentials:'same-origin'});
    const d = await r.json();
    const info = document.getElementById('pl-info');
    if (d.note) { info.textContent = d.note; return; }
    if (!d.series || !d.series.length) { info.textContent = 'Nema podataka za 24h.'; return; }

    const labels = d.series.map(s => s.ts.slice(11,16));
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
  } catch(e) { document.getElementById('pl-info').textContent = 'Greška: '+e; }
}

async function loadPlantInfo() {
  try {
    const r = await fetch('/api/stats/plant-info', {credentials:'same-origin'});
    const d = await r.json();
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
    set('pi-rt',   d.reimbursement_today_eur.toFixed(2) + ' €');
    set('pi-ru',   d.reimbursement_total_eur.toFixed(2) + ' €');
    set('pi-rate-info', `CO₂ faktor ${d.co2_factor_g_kwh} g/kWh · otkup ${(d.feed_tariff*100).toFixed(2)} c/kWh`);

    set('pi-pv-d', f1(d.pv_today_kwh));
    set('pi-pv-m', f1(d.pv_month_kwh));
    set('pi-pv-y', f1(d.pv_year_kwh));
    set('pi-pv-t', f1(d.pv_total_kwh));
  } catch(e) { console.error('plant-info', e); }
}

async function loadPeakSnaga() {
  try {
    const r = await fetch('/api/stats/peak-snaga', {credentials:'same-origin'});
    const d = await r.json();
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
  } catch(e) { console.error('peak-snaga', e); }
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
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Kupac</div><div style="font-family:var(--mono);color:var(--text)">${m.naziv||'—'}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Šifra OMM</div><div style="font-family:var(--mono);color:var(--text)">${m.id}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">OIB</div><div style="font-family:var(--mono)">${m.oib||'—'}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Tip</div><div>${tipBadge}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Adresa</div><div style="font-size:12px">${m.adresa||'—'}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Raspon očitanja</div><div style="font-family:var(--mono);font-size:12px">${(m.prvi_zapis||'').slice(0,10)} → ${(m.zadnji_zapis||'').slice(0,10)}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">15-min zapisa</div><div style="font-family:var(--mono)">${(m.n_15min||0).toLocaleString()}</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Ukupna potrošnja</div><div style="font-family:var(--mono);color:#22d3ee">${(m.uk_potrosnja||0).toLocaleString()} kWh</div></div>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px">Ukupna predaja</div><div style="font-family:var(--mono);color:#fbbf24">${(m.uk_predaja||0).toLocaleString()} kWh</div></div>
      </div>`;
    }
    box.innerHTML = html;
  } catch(e) { box.innerHTML = '<em style="color:#f87171">Greška: '+e+'</em>'; }
}

let _vtntChart = null;
async function loadVtNtTrosak() {
  try {
    const r = await fetch('/api/stats/vt-nt-trosak', {credentials:'same-origin'});
    const d = await r.json();
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
          { label: 'Predaja (€)',     data: mj.map(m => -m.predaja_eur),backgroundColor: 'rgba(34,197,94,.7)',   stack:'cost' },
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
        <td style="font-family:var(--mono)">${m.mjesec}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.vt_kwh}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.nt_kwh}</td>
        <td style="text-align:right;font-family:var(--mono)">${m.vt_perc ?? '—'}%</td>
        <td style="text-align:right;font-family:var(--mono);color:#f87171">${m.vt_eur}</td>
        <td style="text-align:right;font-family:var(--mono);color:#22d3ee">${m.nt_eur}</td>
        <td style="text-align:right;font-family:var(--mono);color:#22c55e">-${m.predaja_eur}</td>
        <td style="text-align:right;font-family:var(--mono);font-weight:700">${m.neto_trosak}</td>
      </tr>`).join('');
  } catch(e) {
    document.getElementById('vtnt-info').textContent = 'Greška: ' + e;
  }
}

loadOmmList();
load();
syncTimer = setInterval(load, 60000);
// Učitaj peak snagu odmah (Pregled je default tab)
loadPeakSnaga();
// Auto-refresh Energy flow svakih 10 sekundi (samo kad je Pregled aktivan)
setInterval(() => {
  if (document.getElementById('page-pregled')?.classList.contains('active')) {
    loadEnergyFlow();
  }
}, 10000);
