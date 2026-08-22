/* ═══════════════════════════════════════════════════════════════════════════
   Books — client application.

   THE MONEY RULE
   Amounts arrive from the API as JSON *strings* and stay strings. A JS number
   is an IEEE-754 double: it cannot hold 0.1 exactly and loses precision past
   2^53. Formatting a string is safe; arithmetic on it is not. Every total
   displayed here was computed by the server. The two exceptions are labelled
   as estimates: the invoice line preview, and the count-up animation (which
   animates a *display* value and always lands on the exact server string).
   ═══════════════════════════════════════════════════════════════════════════ */
const S={api:'',access:'',refresh:'',tenant:null,user:null,perms:[],ref:null,reauth:null};

/* ── theme ─────────────────────────────────────────────────────────────────
   Three states, not two: 'light', 'dark', and *unset* — which means "follow
   the OS" and is the default. Storing only 'light'/'dark' would freeze a new
   user into whatever their OS happened to say the first time they loaded the
   page, and they would never see the system-following behaviour again.

   localStorage, not sessionStorage: the rest of the session (tokens) is
   deliberately session-scoped so closing the tab ends the session, but a
   theme preference is not a credential and re-picking it every morning is
   an irritation, not a security control.                                    */
const THEME_KEY='books.theme';
function applyTheme(t){
  const root=document.documentElement;
  if(t==='light'||t==='dark')root.setAttribute('data-theme',t);
  else root.removeAttribute('data-theme');
  // Reflect the *effective* theme, which for the unset state means asking
  // the media query rather than the attribute.
  const dark=t==='dark'||(!t&&matchMedia('(prefers-color-scheme:dark)').matches);
  document.querySelectorAll('.js-thm').forEach(b=>{
    b.textContent=dark?'☀':'☾';
    b.title=dark?'Switch to light theme':'Switch to dark theme';});
  // Charts are SVG written into innerHTML with literal colours resolved at
  // render time, so they do not re-theme on their own. Re-render the current
  // view if one is mounted.
  if(typeof S!=='undefined'&&S.tenant&&window.__view)go(window.__view);
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme');
  const dark=cur?cur==='dark'
    :matchMedia('(prefers-color-scheme:dark)').matches;
  const next=dark?'light':'dark';
  localStorage.setItem(THEME_KEY,next);
  applyTheme(next);
}
/* Read a design token. Charts build SVG strings, so they need the resolved
   value rather than `var(--c1)` — an SVG `stroke` attribute does not accept
   a custom property, only a `style` does, and half of these are attributes. */
const tok=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
/* Applied before first paint (see the inline call at the bottom of this file)
   so there is no white flash on a dark-themed reload. */
applyTheme(localStorage.getItem(THEME_KEY));
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',()=>{
  if(!localStorage.getItem(THEME_KEY))applyTheme(null);});

/* ── formatting ────────────────────────────────────────────────────────── */
const money=(v,c)=>{if(v==null||v==='')return '—';
  const s=String(v),n=s.startsWith('-');const[i,f='']=(n?s.slice(1):s).split('.');
  return (c?c+' ':'')+(n?'-':'')+i.replace(/\B(?=(\d{3})+(?!\d))/g,',')+'.'+(f+'00').slice(0,2);};
const short=v=>{const n=Math.abs(parseFloat(v)||0);
  if(n>=1e9)return (n/1e9).toFixed(1)+'B'; if(n>=1e6)return (n/1e6).toFixed(1)+'M';
  if(n>=1e3)return (n/1e3).toFixed(1)+'K'; return String(Math.round(n));};
const dt=d=>d?new Date(d).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}):'—';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const initials=s=>String(s||'?').trim().split(/\s+/).slice(0,2).map(x=>x[0]).join('').toUpperCase();

/* Count-up. Animates the *rendering* only: the final frame writes the exact
   formatted server string, so no rounding introduced here can ever survive. */
function countUp(el){
  const raw=el.dataset.val, cur=el.dataset.cur||'', target=parseFloat(raw);

  // Write the exact server value FIRST, before deciding whether to animate.
  //
  // The animation used to begin by painting 0.00 and rely on rAF to climb to
  // the real figure. requestAnimationFrame does not run in a background tab —
  // browsers suspend it entirely — so a dashboard loaded in a tab the user
  // was not looking at froze on its first frame. They switched to it and saw
  // "EGP 0.00" for revenue, cash flow, receivables and payroll, with correct
  // values sitting in the data-val attributes underneath. Zero is not a
  // neutral placeholder on a financial dashboard: it is a number the reader
  // will believe.
  //
  // So the exact value is the default state and the count-up is decoration
  // layered on top. If rAF never runs, the figure is simply correct and
  // static — which is the right failure.
  el.textContent=money(raw,cur);
  if(!isFinite(target))return;
  if(matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  // Hidden tab: leave the exact value in place and let the visibilitychange
  // handler below animate it when the user actually arrives.
  if(document.hidden)return;

  el.dataset.counted='1';
  const dur=750, t0=performance.now();
  (function step(t){
    const p=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-p,3);
    if(p<1){ el.textContent=money((target*e).toFixed(2),cur); requestAnimationFrame(step); }
    else el.textContent=money(raw,cur);   // exact server value
  })(t0);
}
const runCounts=()=>document.querySelectorAll('[data-val]').forEach(countUp);

// Run the count-up for figures that were mounted while the tab was hidden, so
// the effect plays once when the user first sees them rather than never. The
// `counted` flag keeps it to once: re-animating every alt-tab would turn a
// welcome flourish into a twitch.
document.addEventListener('visibilitychange',()=>{
  if(document.hidden)return;
  document.querySelectorAll('[data-val]:not([data-counted])').forEach(countUp);
});

function toast(m,kind){const t=document.createElement('div');
  t.className='toast'+(kind==='bad'?' bad':kind==='ok'?' ok':'');
  t.textContent=m;document.body.appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';
    setTimeout(()=>t.remove(),300);},3800);}

/* ── API ───────────────────────────────────────────────────────────────── */
async function api(p,o={}){
  const h={'Content-Type':'application/json'};
  if(S.access)h['Authorization']='Bearer '+S.access;
  if(o.idem)h['Idempotency-Key']=crypto.randomUUID();
  if(o.reauth&&S.reauth)h['X-Reauth-Token']=S.reauth;
  const r=await fetch(S.api+p,{...o,headers:{...h,...(o.headers||{})}});
  if(r.status===401&&S.refresh&&!o._retry){
    const rr=await fetch(S.api+'/api/v1/auth/refresh/',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh:S.refresh})});
    if(rr.ok){S.access=(await rr.json()).access;save();return api(p,{...o,_retry:1});}
    logout();throw new Error('Session expired');}
  const txt=await r.text(); let d=null; try{d=txt?JSON.parse(txt):null}catch{}
  if(!r.ok){const e=d&&d.error; let m=(e&&e.detail)||('Error '+r.status);
    if(e&&e.fields)m+=' — '+Object.entries(e.fields)
      .map(([k,v])=>k.replace(/_/g,' ')+': '+[].concat(v).join(', ')).join(' | ');
    const x=new Error(m);x.status=r.status;x.code=e&&e.code;throw x;}
  return d;}
const list=async(p,q='')=>{try{const d=await api(`/api/v1/${p}/${q}`);return d.results||[];}
  catch(e){if(e.status===403)return null;throw e;}};
const safe=p=>api(p).catch(()=>null);

/* Sensitive actions demand a fresh password proof. Prompting at the moment of
   the action (rather than caching a long-lived elevation) is the point: it
   ties the confirmation to the specific thing being authorised. */
async function ensureReauth(){
  if(S.reauth)return true;
  const pw=await passwordDlg('Confirm your password to continue.');
  if(!pw)return false;
  try{const d=await api('/api/v1/auth/reauth/',{method:'POST',
      body:JSON.stringify({password:pw})});
    S.reauth=d.token||d.reauth_token||d.access; return true;}
  catch(e){toast(e.message,'bad');return false;}}

/* ── session ───────────────────────────────────────────────────────────── */
const save=()=>sessionStorage.setItem('achr',JSON.stringify(
  {api:S.api,a:S.access,r:S.refresh,t:S.tenant,u:S.user}));
function logout(){
  sessionStorage.removeItem('achr');
  sessionStorage.removeItem('achr.next');
  // Clear the route too. Reloading with /payslips still in the address bar
  // would send the next person who signs in on this machine to the last
  // screen the previous one was looking at.
  history.replaceState({},'','/');
  location.reload();
}

function authTab(k){
  document.getElementById('tabIn').classList.toggle('on',k==='in');
  document.getElementById('tabUp').classList.toggle('on',k==='up');
  document.getElementById('fIn').classList.toggle('hidden',k!=='in');
  document.getElementById('fUp').classList.toggle('hidden',k==='in');
  if(k==='up')loadRef();}

async function loadRef(){
  if(S.ref)return;
  try{
    S.ref=await (await fetch(S.api+'/api/v1/auth/reference/')).json();
    const led=new Set(S.ref.ledger_currencies||[]);
    document.getElementById('s_country').innerHTML=(S.ref.countries||[])
      .map(c=>`<option value="${c.code}" data-cur="${c.default_currency}"
        data-tz="${c.default_timezone||'UTC'}">${esc(c.name)}</option>`).join('');
    document.getElementById('s_cur').innerHTML=(S.ref.currencies||[])
      .filter(c=>!led.size||led.has(c.code))
      .map(c=>`<option value="${c.code}">${c.code} — ${esc(c.name)}</option>`).join('');
    document.getElementById('s_tz').innerHTML=(S.ref.timezones||['UTC'])
      .map(t=>`<option>${t}</option>`).join('');
    onCountry();
  }catch(e){/* the form still works with server defaults */}}

function onCountry(){
  const o=document.getElementById('s_country').selectedOptions[0]; if(!o)return;
  const cur=document.getElementById('s_cur'), tz=document.getElementById('s_tz');
  const want=o.dataset.cur;
  /* Not every country's currency is a supported ledger currency yet. Rather
     than silently pick a different one, leave the user's choice visible. */
  if([...cur.options].some(x=>x.value===want))cur.value=want;
  if([...tz.options].some(x=>x.value===o.dataset.tz))tz.value=o.dataset.tz;}

async function doLogin(ev){ev.preventDefault();
  const b=document.getElementById('loginBtn'),x=document.getElementById('loginErr');
  b.disabled=true;b.innerHTML='<span class="spin"></span> Signing in…';x.classList.add('hidden');
  S.api=document.getElementById('api').value.replace(/\/+$/,'');
  try{const r=await fetch(S.api+'/api/v1/auth/login/',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('email').value,
                           password:document.getElementById('pass').value})});
    const d=await r.json();
    if(!r.ok)throw new Error((d.error&&d.error.detail)||'Invalid credentials');
    S.access=d.access;S.refresh=d.refresh;S.tenant=d.tenant;S.user=d.user;save();await boot();
  }catch(e){x.textContent=e.message.includes('Failed to fetch')
      ?'Cannot reach the server. Is runserver running?':e.message;x.classList.remove('hidden');}
  finally{b.disabled=false;b.textContent='Sign in';}}

async function doSignup(ev){ev.preventDefault();
  const b=document.getElementById('upBtn'),x=document.getElementById('upErr');
  b.disabled=true;b.innerHTML='<span class="spin"></span> Creating…';x.classList.add('hidden');
  S.api=document.getElementById('api').value.replace(/\/+$/,'');
  const body={company_name:document.getElementById('s_co').value,
    country:document.getElementById('s_country').value,
    base_currency:document.getElementById('s_cur').value,
    timezone:document.getElementById('s_tz').value,
    full_name:document.getElementById('s_name').value,
    email:document.getElementById('s_email').value,
    password:document.getElementById('s_pass').value};
  try{const r=await fetch(S.api+'/api/v1/auth/signup/',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){const e=d.error||{};let m=e.detail||'Could not create the organisation';
      if(e.fields)m+=' — '+Object.entries(e.fields).map(([k,v])=>
        k.replace(/_/g,' ')+': '+[].concat(v).join(', ')).join(' | ');
      throw new Error(m);}
    S.access=d.access;S.refresh=d.refresh;S.tenant=d.tenant;S.user=d.user;save();
    await boot();toast('Organisation created — your chart of accounts is ready','ok');
  }catch(e){x.textContent=e.message;x.classList.remove('hidden');}
  finally{b.disabled=false;b.textContent='Create organisation';}}

/* ── navigation ────────────────────────────────────────────────────────── */
const MENU=[
 {k:'dash',ic:'⌂',l:'Home'},
 {ic:'▤',l:'Items',ch:[['items','Items'],['stock','Stock on Hand']]},
 {ic:'🛒',l:'Sales',ch:[['customers','Customers'],['invoices','Invoices'],
   ['payments','Payments Received'],['creditnotes','Credit Notes']]},
 {ic:'🛍',l:'Purchases',ch:[['vendors','Vendors'],['expenses','Expenses'],
   ['recurringexpenses','Recurring Expenses'],['bills','Bills'],
   ['recurringbills','Recurring Bills'],['billpayments','Payments Made'],
   ['vendorcredits','Vendor Credits']]},
 {ic:'◷',l:'Time Tracking',ch:[['projects','Projects'],['timesheets','Timesheets']]},
 {ic:'🏦',l:'Banking',ch:[['banking','Overview'],['banksetup','Bank Setup'],
   ['banktx','Transactions']]},
 {ic:'👤',l:'Accountant',ch:[['journal','Manual Journals'],['accounts','Chart of Accounts'],
   ['taxrates','Tax Rates'],['periods','Fiscal Periods']]},
 {ic:'👥',l:'Payroll & HR',ch:[['employees','Employees'],['departments','Departments'],
   ['shifts','Shifts'],['shiftassign','Shift Assignments'],
   ['leavetypes','Leave Types'],['leaves','Leave Requests'],
   ['overtime','Overtime'],['ottypes','Overtime Types'],
   ['structures','Salary Structures'],['structureassign','Structure Assignments'],
   ['payroll','Pay Runs'],['payslips','Payslips']]},
 {ic:'▥',l:'Reports',ch:[['reports','Financial Statements'],
   ['gl','General Ledger'],['journalregister','Journal Register'],
   ['partystmt','Party Statement'],['ratios','Financial Ratios']]},
 {ic:'⚙',l:'Settings',ch:[['org','Organisation'],['branding','Document Branding'],
   ['team','Users & Roles'],['invites','Invitations'],['audit','Audit Log']]},
];
const FLAT=()=>MENU.flatMap(m=>m.ch?m.ch:[[m.k,m.l]]);

async function boot(){
  document.getElementById('auth').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  document.getElementById('orgName').textContent=(S.tenant.name||'').toUpperCase();
  document.getElementById('whoName').textContent=S.user.full_name||S.user.email;
  document.getElementById('baseCur').textContent=
    S.tenant.base_currency+(S.tenant.country?' · '+S.tenant.country:'');
  try{const me=await api('/api/v1/auth/me/');S.perms=me.permissions||[];
    document.getElementById('whoRole').textContent=(me.roles||[]).map(r=>r.name||r).join(', ');}catch{}

  /* The login response carries a *slim* tenant — id, name, slug, currency,
     country — and no `settings`. Document branding lives in
     `settings.branding`, so without this every printed invoice, bill and
     voucher came out unbranded until the user happened to open Settings ->
     Document Branding, which is the one screen that re-fetched the full
     record. Merged rather than assigned: the slim projection is authoritative
     for the fields it does carry, and `/tenancy/current/` answers an envelope
     whose `.tenant` is the object we actually want. */
  try{const cur=await api('/api/v1/tenancy/current/');
    const full=cur&&(cur.tenant||cur);
    if(full&&full.base_currency){S.tenant={...S.tenant,...full};save();}}catch{}
  document.getElementById('nav').innerHTML=MENU.map((m,i)=>{
    if(!m.ch)return `<div class="top" data-p="${m.k}" onclick="go('${m.k}')">
      <span class="ic">${m.ic}</span><span class="tx">${m.l}</span></div>`;
    return `<div class="top" data-g="${i}" onclick="tog(${i})">
        <span class="ic">${m.ic}</span><span class="tx">${m.l}</span><span class="cv">▶</span></div>
      <div class="sub2" id="sub${i}">${m.ch.map(([k,l])=>
        `<a href="#" data-p="${k}" onclick="go('${k}');return false">${l}</a>`).join('')}</div>`;
  }).join('');
  // Land where the URL says, not always on the dashboard. `replace` because
  // the hash is already correct — rewriting it would push a duplicate entry.
  // Where to land: the URL if it names a screen, else the destination the
  // user was asking for when login interrupted them, else the dashboard.
  let wanted=routeOf();
  if(!wanted){
    const saved=sessionStorage.getItem('achr.next');
    sessionStorage.removeItem('achr.next');
    if(saved&&VIEWS[saved]&&FLAT().some(i=>i[0]===saved))wanted=saved;
  }
  go(wanted||'dash',{replace:false});}

/* One group open at a time — keeps the whole menu reachable without scrolling. */
function tog(i){const sub=document.getElementById('sub'+i);
  const head=document.querySelector(`.top[data-g="${i}"]`);
  const was=sub.classList.contains('open');
  document.querySelectorAll('.sub2').forEach(x=>x.classList.remove('open'));
  document.querySelectorAll('.top[data-g]').forEach(x=>x.classList.remove('open'));
  if(!was){sub.classList.add('open');head.classList.add('open');}}

/* Off-canvas navigation, below 900px. A class on <body> rather than on the
   sidebar so the scrim (body::after) and the drawer are driven by one state. */
function toggleNav(force){
  const open=force===undefined?!document.body.classList.contains('nav-open'):force;
  document.body.classList.toggle('nav-open',open);
}
// Tapping the scrim closes. The scrim is a ::after pseudo-element and cannot
// take its own listener, so the click is caught on <body> and filtered to
// events that landed on the body itself rather than on the drawer.
document.addEventListener('click',e=>{
  if(document.body.classList.contains('nav-open')&&e.target===document.body)toggleNav(false);
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')toggleNav(false);
});

const VIEWS={};

/* The screen named in the URL, or '' when there is none. Sanitised against
   the menu: a hand-typed /../../etc or /nonsense must not be looked up in
   VIEWS, and must not end up in innerHTML anywhere. */
function routeOf(){
  const raw=decodeURIComponent(location.pathname.replace(/^\/+/,'').replace(/\/+$/,'')).trim();
  if(!raw)return '';
  return (VIEWS[raw]&&FLAT().some(i=>i[0]===raw))?raw:'';
}

/* Back/forward, and any hand-edited address. Guarded on the current view so
   that go()'s own pushState does not bounce straight back through here. An
   empty path (the site root) routes to the dashboard. */
window.addEventListener('popstate',()=>{
  if(!S.access)return;                       // not signed in: nothing to route
  const r=routeOf();
  if(r!==window.__view)go(r||'dash',{replace:true});
});
/* Navigate to a screen, and put it in the URL.

   History-API routing with clean paths (`/journal`, and `/` for the home
   dashboard): the Django catch-all serves index.html for every non-API path,
   so a reload or a bookmarked deep link resolves on the server and this SPA
   renders it from `location.pathname`. (It used to be hash routing —
   `#/journal` — because the server served the app only at a single `/app/`
   route; the catch-all is what makes real paths safe now, and drops the
   unprofessional `/app/#` from the address bar.)

   `replace` is passed when the URL is already right — restoring on boot, or
   responding to the user pressing Back — so that navigating does not push a
   duplicate entry and trap them in their own history. */
function go(p,opts){
  const o=opts||{};
  // Remembered so applyTheme() can re-render the current screen: the SVG
  // charts bake resolved colours into markup and cannot re-theme in place.
  window.__view=p;
  if(routeOf()!==p){const u=p==='dash'?'/':'/'+p;
    o.replace?history.replaceState({},'',u):history.pushState({},'',u);}
  document.querySelectorAll('#nav a,.top[data-p]').forEach(a=>
    a.classList.toggle('on',a.dataset.p===p));
  MENU.forEach((m,i)=>{if(m.ch&&m.ch.some(c=>c[0]===p)){
    document.getElementById('sub'+i).classList.add('open');
    document.querySelector(`.top[data-g="${i}"]`).classList.add('open');}});
  refreshPal();
  toggleNav(false);
  const l=FLAT().find(i=>i[0]===p);
  document.getElementById('pageTitle').textContent=l?l[1]:'';
  document.getElementById('pageActions').innerHTML='';
  V(skeleton());
  (VIEWS[p]||VIEWS.dash)();}
/* Mount a view. Removing the class, forcing a reflow, then re-adding it is
   what restarts a CSS animation — assigning innerHTML alone does not, so
   every screen after the first would mount without the transition.
   `void v.offsetWidth` is the reflow: reading a layout property flushes the
   pending style change so the browser sees the class genuinely leave and
   re-enter, rather than coalescing remove+add into no change at all.
   Deliberately synchronous rather than requestAnimationFrame — rAF is
   throttled to a stop in a background tab, which would leave the class
   unapplied on any view rendered while the user is looking elsewhere. */
const V=h=>{const v=document.getElementById('view');
  v.classList.remove('swap');v.innerHTML=h;
  void v.offsetWidth;
  v.classList.add('swap');
  runCounts();};
const A=h=>document.getElementById('pageActions').innerHTML=h;
const skeleton=()=>`<div class="panel"><div class="pb">
  <div class="sk" style="width:32%"></div><div class="sk" style="width:70%"></div>
  <div class="sk" style="width:52%"></div><div class="sk" style="width:61%"></div></div></div>`;

/* ── shared bits ───────────────────────────────────────────────────────── */
const ST={draft:['Draft','t-mut'],sent:['Sent','t-info'],partially_paid:['Partially Paid','t-warn'],
 paid:['Paid','t-ok'],overdue:['Overdue','t-dang'],voided:['Void','t-mut'],
 written_off:['Written Off','t-dang'],posted:['Posted','t-ok'],reversed:['Reversed','t-mut'],
 calculated:['Calculated','t-info'],pending_approval:['Pending','t-warn'],approved:['Approved','t-ok'],
 cancelled:['Cancelled','t-mut'],submitted:['Submitted','t-info'],rejected:['Rejected','t-dang'],
 active:['Active','t-ok'],unpaid:['Unpaid','t-warn'],pending:['Pending','t-warn'],
 captured:['Captured','t-ok'],reimbursed:['Reimbursed','t-ok'],accepted:['Accepted','t-ok'],
 revoked:['Revoked','t-mut'],expired:['Expired','t-dang'],trial:['Trial','t-info'],
 open:['Open','t-ok'],closed:['Closed','t-mut'],soft_closed:['Soft closed','t-warn'],
 pending_manager:['With Manager','t-warn'],pending_hr:['With HR','t-warn'],
 awaiting_approval:['Awaiting Approval','t-warn'],on_leave:['On Leave','t-info'],
 suspended:['Suspended','t-dang'],terminated:['Terminated','t-mut'],
 resigned:['Resigned','t-mut'],half_day:['Half Day','t-warn'],
 present:['Present','t-ok'],absent:['Absent','t-dang'],late:['Late','t-warn']};
const tag=s=>{const x=ST[s]||[String(s||'').replace(/_/g,' '),'t-mut'];
  return `<span class="tag ${x[1]}">${x[0]}</span>`;};
/* Quantities come back as numeric(N,6) strings — "5.000000" days. Trailing
   zeros past the point a human would write are noise, but the value is still
   only ever *displayed*: like money(), this formats a string and never does
   arithmetic on it. */
const qty=v=>{if(v==null||v==='')return '—';
  const s=String(v);return s.includes('.')?s.replace(/0+$/,'').replace(/\.$/,''):s;};
const denied=()=>`<div class="panel anim"><div class="empty"><h4>No access</h4>
  <p>Your role does not include permission for this screen.</p></div></div>`;
const tbl=(h,r,t,sub,btn)=>r.length
  ?`<div class="panel anim"><table><thead><tr>${h.map(x=>
      `<th${x.startsWith('~')?' class="num"':''}>${x.replace('~','')}</th>`).join('')}</tr></thead>
    <tbody>${r.join('')}</tbody></table></div>`
  :`<div class="panel anim"><div class="empty"><h4>${t||'Nothing here yet'}</h4>
    <p>${sub||''}</p>${btn||''}</div></div>`;

/* ── SVG charts ────────────────────────────────────────────────────────
   Hand-rolled rather than a chart library: three shapes is less code than the
   bundle, and it keeps the page a single dependency-free file that works
   offline. `--len` + the `dash` keyframe animate the line drawing itself.  */
/* `let`, not `const`: the series colours are design tokens now, and a
   theme switch has to be able to replace them. Every call site indexes
   PAL[n] as before, so refreshing the binding re-themes all three charts
   without touching their code. */
let PAL=[];
const refreshPal=()=>{PAL=['--c1','--c2','--c3','--c4','--c5','--c6'].map(tok);};
// Populate once at load as well as per-navigation. go() refreshes it before
// every view, but a chart rendered from anywhere else (a modal, a report run
// that does not route) would otherwise draw with an empty palette and emit
// `fill="undefined"`, which paints black in every browser.
refreshPal();

function lineChart(points,w,h,cur){
  if(!points.length)return '<div class="note">No data.</div>';
  const vals=points.map(p=>p.v), mn=Math.min(0,...vals), mx=Math.max(1,...vals);
  const pad=26, iw=w-pad*2, ih=h-30;
  const X=i=>pad+(points.length<2?iw/2:i*iw/(points.length-1));
  const Y=v=>10+ih-((v-mn)/((mx-mn)||1))*ih;
  const d=points.map((p,i)=>(i?'L':'M')+X(i).toFixed(1)+','+Y(p.v).toFixed(1)).join(' ');
  const area=d+` L${X(points.length-1).toFixed(1)},${(10+ih).toFixed(1)} L${X(0).toFixed(1)},${(10+ih).toFixed(1)} Z`;
  const zero=Y(0);
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px">
    <defs><linearGradient id="lg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${PAL[0]}" stop-opacity=".22"/>
      <stop offset="1" stop-color="${PAL[0]}" stop-opacity="0"/></linearGradient></defs>
    <line x1="${pad}" y1="${zero}" x2="${w-pad}" y2="${zero}" stroke="${tok('--line')}"/>
    <path d="${area}" fill="url(#lg)" style="animation:fadeIn .8s .2s both"/>
    <path d="${d}" fill="none" stroke="${PAL[0]}" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round"
      style="--len:2000;stroke-dasharray:2000;animation:dash 1.1s cubic-bezier(.4,0,.2,1) both"/>
    ${points.map((p,i)=>`<circle cx="${X(i).toFixed(1)}" cy="${Y(p.v).toFixed(1)}" r="3"
      fill="${tok('--panel')}" stroke="${PAL[0]}" stroke-width="2"
      style="animation:pop .3s ${(0.5+i*0.04).toFixed(2)}s both"><title>${esc(p.l)}: ${money(p.v,cur)}</title></circle>`).join('')}
    ${points.map((p,i)=>`<text x="${X(i).toFixed(1)}" y="${h-6}" font-size="9"
      fill="${tok('--mut')}" text-anchor="middle">${esc(p.l)}</text>`).join('')}
  </svg>`;}

function barPairs(rows,h,cur){
  if(!rows.length)return '<div class="note">No data.</div>';
  const mx=Math.max(1,...rows.flatMap(r=>[r.a,r.b]));
  return `<div style="display:flex;gap:6px;align-items:flex-end;height:${h}px;padding-top:10px">
    ${rows.map((r,i)=>`<div style="flex:1;display:flex;gap:2px;align-items:flex-end;height:100%">
      <div title="Income ${money(r.a,cur)}" style="flex:1;background:${PAL[2]};border-radius:3px 3px 0 0;
        height:${Math.max(2,r.a/mx*100)}%;transform-origin:bottom;
        animation:grow .6s ${(i*0.05).toFixed(2)}s cubic-bezier(.22,.8,.3,1) both"></div>
      <div title="Expense ${money(r.b,cur)}" style="flex:1;background:${PAL[4]};border-radius:3px 3px 0 0;
        height:${Math.max(2,r.b/mx*100)}%;transform-origin:bottom;
        animation:grow .6s ${(i*0.05+0.06).toFixed(2)}s cubic-bezier(.22,.8,.3,1) both"></div>
    </div>`).join('')}</div>
  <div style="display:flex;gap:6px;font-size:9.5px;color:var(--mut);margin-top:5px">
    ${rows.map(r=>`<span style="flex:1;text-align:center">${esc(r.l)}</span>`).join('')}</div>`;}

function donut(items,cur){
  if(!items.length)return '<div class="note">No data.</div>';
  const tot=items.reduce((s,x)=>s+x.v,0)||1; let ang=0;
  const seg=items.map((x,i)=>{const a0=ang,a1=ang+x.v/tot*359.9;ang=a1;
    const r=46,cx=54,cy=54,rad=d=>(d-90)*Math.PI/180;
    const x0=cx+r*Math.cos(rad(a0)),y0=cy+r*Math.sin(rad(a0));
    const x1=cx+r*Math.cos(rad(a1)),y1=cy+r*Math.sin(rad(a1));
    return `<path d="M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${(a1-a0)>180?1:0},1 ${x1},${y1} Z"
      fill="${PAL[i%6]}" style="animation:fadeIn .5s ${(i*0.07).toFixed(2)}s both"><title>${esc(x.l)}</title></path>`;}).join('');
  return `<div style="display:flex;align-items:center;gap:18px">
    <svg width="108" height="108" viewBox="0 0 108 108" style="flex-shrink:0">${seg}
      <circle cx="54" cy="54" r="27" fill="${tok('--panel')}"/></svg>
    <div style="flex:1;font-size:12px">${items.map((x,i)=>
      `<div style="display:flex;align-items:center;gap:8px;padding:3px 0">
        <i style="width:9px;height:9px;border-radius:2px;background:${PAL[i%6]};flex-shrink:0"></i>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.l)}</span>
        <b style="margin-left:auto;font-variant-numeric:tabular-nums">${money(x.v,cur)}</b>
      </div>`).join('')}</div></div>`;}

/* ── DASHBOARD ─────────────────────────────────────────────────────────────
   Date range, display currency and the KPI block are *state*, not arguments:
   changing the range must not re-enter go() (which would reset the nav and
   replay the page transition), so the filter bar mutates S.range and calls
   the renderer directly.                                                    */
const RANGES = {
  mtd: ['MTD', () => { const n = new Date();
        return [new Date(n.getFullYear(), n.getMonth(), 1), n]; }],
  qtd: ['QTD', () => { const n = new Date();
        return [new Date(n.getFullYear(), Math.floor(n.getMonth() / 3) * 3, 1), n]; }],
  ytd: ['YTD', () => { const n = new Date();
        return [new Date(n.getFullYear(), 0, 1), n]; }],
};
const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 6e4)
  .toISOString().slice(0, 10);

/* Default: year to date. Financial dashboards are read against the fiscal
   year far more often than against any other window. */
S.range = { key: 'ytd', from: null, to: null };
S.dispCur = null;   // null = the tenant's base currency
S.fx = null;        // { CODE: rate-to-base }, loaded once per session

function rangeDates() {
  if (S.range.key === 'custom' && S.range.from && S.range.to)
    return [S.range.from, S.range.to];
  const [, fn] = RANGES[S.range.key] || RANGES.ytd;
  const [a, b] = fn();
  return [iso(a), iso(b)];
}

function setRange(k) {
  S.range.key = k;
  if (k === 'custom') {
    const [f, t] = rangeDates();
    S.range.from = S.range.from || f; S.range.to = S.range.to || t;
  }
  VIEWS.dash();
}
function setCustom() {
  S.range.key = 'custom';
  S.range.from = document.getElementById('r_from').value;
  S.range.to = document.getElementById('r_to').value;
  if (S.range.from && S.range.to) VIEWS.dash();
}

/* Display currency. The ledger is stored in the tenant's base currency, so
   this converts *for display only* at the latest rate on file and says so.
   It deliberately does not re-fetch anything in another currency: there is no
   such thing as "the balance sheet in USD" without a stated translation
   policy (IAS 21 uses closing rate for assets, historical for equity), and
   inventing one silently would be worse than not offering the toggle. */
async function loadFx() {
  if (S.fx) return S.fx;
  const rows = await list('exchange-rates');
  const base = S.tenant.base_currency, m = {};
  (rows || []).forEach(r => {
    if (r.to_currency === base) m[r.from_currency] = parseFloat(r.rate);
    else if (r.from_currency === base && parseFloat(r.rate) > 0)
      m[r.to_currency] = 1 / parseFloat(r.rate);
  });
  S.fx = m; return m;
}
const dispCur = () => S.dispCur || S.tenant.base_currency;
/* Returns a *string*, keeping the money rule: the conversion is the one
   place the client does arithmetic, it is labelled as indicative, and the
   underlying base figure is never overwritten. */
function conv(v) {
  const n = parseFloat(v); if (!isFinite(n)) return '0';
  if (!S.dispCur || S.dispCur === S.tenant.base_currency) return String(v ?? 0);
  const r = (S.fx || {})[S.dispCur];
  return r && r > 0 ? (n / r).toFixed(2) : String(v ?? 0);
}
async function setCur(c) {
  S.dispCur = c || null;
  await loadFx();
  VIEWS.dash();
}

/* Grouped bars — used for aging buckets and revenue-vs-expense. Separate from
   barPairs() because that one hard-codes a two-series income/expense legend. */
function bars(rows, h, cur, colour) {
  if (!rows.length) return '<div class="note">No data.</div>';
  const mx = Math.max(1, ...rows.map(r => Math.abs(r.v)));
  return `<div style="display:flex;gap:8px;align-items:flex-end;height:${h}px;padding-top:8px">
    ${rows.map((r, i) => `<div style="flex:1;display:flex;flex-direction:column;
        justify-content:flex-end;height:100%">
      <div title="${esc(r.l)}: ${money(r.v, cur)}" style="background:${colour || PAL[i % 6]};
        border-radius:4px 4px 0 0;height:${Math.max(2, Math.abs(r.v) / mx * 100)}%;
        transform-origin:bottom;
        animation:grow .55s ${(i * .06).toFixed(2)}s cubic-bezier(.22,.8,.3,1) both"></div>
    </div>`).join('')}</div>
  <div style="display:flex;gap:8px;font-size:10px;color:var(--mut);margin-top:6px">
    ${rows.map(r => `<span style="flex:1;text-align:center">${esc(r.l)}</span>`).join('')}</div>
  <div style="display:flex;gap:8px;font-size:11px;margin-top:2px;font-variant-numeric:tabular-nums">
    ${rows.map(r => `<span style="flex:1;text-align:center"><b>${short(r.v)}</b></span>`).join('')}</div>`;
}

/* A ratio tile. `good` decides the accent: a quick ratio of 0.4 and a
   debt-to-equity of 0.4 are opposite news, so the direction is passed in
   rather than inferred from the number. */
function ratioTile(label, value, hint, opts) {
  const o = opts || {};
  if (value == null)
    return `<div class="kpi anim ${o.d || ''}"><div class="lbl">${label}</div>
      <b class="mut" style="color:var(--mut)">—</b>
      <i>${esc(o.undef || 'not computable')}</i></div>`;
  const n = parseFloat(value);
  const ok = o.higherIsBetter === false ? n <= o.threshold : n >= o.threshold;
  return `<div class="kpi anim ${o.d || ''}"><div class="lbl">${label}</div>
    <b class="${ok ? 'pos' : 'neg'}">${o.pct ? (n * 100).toFixed(1) + '%' : n.toFixed(2)}</b>
    <i>${esc(hint)}</i></div>`;
}

VIEWS.dash = async function () {
  const [from, to] = rangeDates();
  const q = `?date_from=${from}&date_to=${to}`;
  const base = S.tenant.base_currency, c = dispCur();
  await loadFx();

  const [kpi, pl, aging, apAg, cf, invs, hr] = await Promise.all([
    safe('/api/v1/reporting/kpis/' + q),
    safe('/api/v1/reporting/profit-loss/' + q),
    safe('/api/v1/reporting/ar-aging/' + q),
    safe('/api/v1/reporting/ap-aging/' + q),
    safe('/api/v1/reporting/cash-flow/' + q),
    list('invoices'),
    // `safe`, not `list`: this endpoint is guarded by hr.employee.read, and a
    // finance-only role legitimately holds none of it. A null here hides the
    // operations block rather than failing the whole dashboard.
    safe('/api/v1/reporting/hr-metrics/' + q),
  ]);

  if (!kpi) {
    return V(`<div class="panel anim"><div class="empty"><h4>Analytics unavailable</h4>
      <p>Your role does not include <code>reporting.balance_sheet.read</code>,
         which the ratio block is computed from.</p></div></div>`);
  }

  const m = kpi.metrics, cm = kpi.components;
  const T = (o, ...k) => { if (!o) return null; const t = o.totals || {};
    for (const x of k) if (t[x] != null) return t[x]; return null; };

  const revenue = cm.revenue, expenses = cm.expenses;

  // AR / AP aging buckets, 30 / 60 / 90+.
  const buckets = o => [
    ['Current', T(o, 'bucket_current')], ['1–30', T(o, 'bucket_1_30')],
    ['31–60', T(o, 'bucket_31_60')], ['61–90', T(o, 'bucket_61_90')],
    ['90+', T(o, 'bucket_90_plus')],
  ].map(([l, v]) => ({ l, v: parseFloat(conv(v || 0)) || 0 }));
  const arB = buckets(aging), apB = buckets(apAg);
  const arTot = conv(T(aging, 'total_outstanding') || '0');
  const apTot = conv(T(apAg, 'total_outstanding') || '0');

  // Expense breakdown by category — the P&L's expense section, largest first.
  const expSec = (pl && (pl.sections || []).filter(x => /expense/i.test(x.title || ''))[0]) || {};
  const byCat = (expSec.lines || [])
    .map(l => ({ l: l.label || l.account_name || '', v: Math.abs(parseFloat(conv(l.amount)) || 0) }))
    .filter(x => x.v > 0).sort((a, b) => b.v - a.v).slice(0, 6);

  // Cash-flow trend.
  const cfPts = (() => { const s = cf && (cf.sections || [])[0];
    if (!s || !(s.lines || []).length) return [];
    return (s.lines || []).slice(0, 12)
      .map(l => ({ l: (l.label || '').slice(0, 3), v: parseFloat(conv(l.amount)) || 0 })); })();

  const open = (invs || []).filter(i => ['sent', 'partially_paid', 'overdue'].includes(i.status));
  const overdue = (invs || []).filter(i => i.status === 'overdue');
  const curOpts = [base, ...Object.keys(S.fx || {})];

  A(`<div class="tools" style="margin:0">
    ${Object.entries(RANGES).map(([k, v]) =>
      `<button class="btn sm ${S.range.key === k ? '' : 'sec'}" onclick="setRange('${k}')">${v[0]}</button>`).join('')}
    <button class="btn sm ${S.range.key === 'custom' ? '' : 'sec'}" onclick="setRange('custom')">Custom</button>
    ${S.range.key === 'custom' ? `<input type="date" id="r_from" value="${from}" onchange="setCustom()">
      <input type="date" id="r_to" value="${to}" onchange="setCustom()">` : ''}
    <select style="min-width:96px" onchange="setCur(this.value)">
      ${curOpts.map(x => `<option value="${x}" ${x === c ? 'selected' : ''}>${x}</option>`).join('')}
    </select>
  </div>`);

  V(`
  ${c !== base ? `<div class="note" style="margin:0 0 12px">Figures converted from
    <b>${base}</b> to <b>${c}</b> at the latest rate on file, for display only.
    The ledger, and every report you can file, remain in ${base}.</div>` : ''}

  <div class="kpis">
    <div class="kpi anim"><div class="lbl">Working Capital</div>
      <b class="${parseFloat(cm.current_assets) - parseFloat(cm.current_liabilities) < 0 ? 'neg' : 'pos'}"
         data-val="${conv(m.working_capital)}" data-cur="${c}">—</b>
      <i>current assets − liabilities</i></div>
    <div class="kpi anim d1"><div class="lbl">EBITDA</div>
      <b data-val="${conv(m.ebitda)}" data-cur="${c}">—</b>
      <i>${kpi.flags.ebitda_is_exact ? 'incl. add-backs' : '= net profit (no D&amp;A)'}</i></div>
    ${ratioTile('Quick Ratio', m.quick_ratio, 'liquid assets ÷ current liabilities',
      { threshold: 1, d: 'd2', undef: 'no current liabilities' })}
    ${ratioTile('Debt to Equity', m.debt_to_equity, 'liabilities ÷ equity',
      { threshold: 2, higherIsBetter: false, d: 'd3', undef: 'no equity recorded' })}
    ${ratioTile('Net Profit Margin', m.net_profit_margin, 'net profit ÷ revenue',
      { threshold: 0, pct: 1, d: 'd4', undef: 'no revenue in window' })}
    <div class="kpi anim d5"><div class="lbl">Cash Burn / month</div>
      <b class="${parseFloat(m.cash_burn_rate) > 0 ? 'neg' : 'pos'}"
         data-val="${conv(m.cash_burn_rate)}" data-cur="${c}">—</b>
      <i>over ${esc(kpi.window_months)} months</i></div>
  </div>

  ${kpi.assumptions.length ? `<div class="panel anim d2"><div class="pb">
    ${kpi.assumptions.map(a => `<div class="note" style="margin:0 0 6px">⚠ ${esc(a)}</div>`).join('')}
  </div></div>` : ''}

  <div class="g3c">
    <div class="panel anim d2"><div class="ph"><h3>Cash Flow Trend</h3>
      <span style="color:var(--mut);font-size:11.5px">${from} → ${to}</span></div>
      <div class="pb">${cfPts.length ? lineChart(cfPts, 760, 190, c)
        : `<div class="mrow"><span>Opening cash</span><b>${money(conv(cm.cash_opening), c)}</b></div>
           <div class="mrow"><span>Closing cash</span><b>${money(conv(cm.cash_closing), c)}</b></div>
           <div class="mrow tot"><span>Movement</span>
             <b class="${parseFloat(cm.cash_change) < 0 ? 'neg' : 'pos'}">${money(conv(cm.cash_change), c)}</b></div>
           <div class="note">No periodic cash-flow sections for this window.</div>`}</div></div>
    <div class="panel anim d3"><div class="ph"><h3>Revenue vs Expenses</h3></div><div class="pb">
      ${bars([{ l: 'Revenue', v: parseFloat(conv(revenue)) || 0 },
              { l: 'Expenses', v: parseFloat(conv(expenses)) || 0 },
              { l: 'Net', v: parseFloat(conv(cm.net_profit)) || 0 }], 150, c)}
    </div></div>
  </div>

  <div class="g2c">
    <div class="panel anim d3"><div class="ph"><h3>Expenses by Category</h3></div>
      <div class="pb">${byCat.length ? donut(byCat, c)
        : '<div class="note">No expense activity in this window.</div>'}</div></div>
    <div class="panel anim d4"><div class="ph"><h3>Receivables Aging</h3>
      <span style="color:var(--mut);font-size:11.5px">${money(arTot, c)} outstanding</span></div>
      <div class="pb">${bars(arB, 130, c, PAL[0])}</div></div>
  </div>

  <div class="g2c">
    <div class="panel anim d5"><div class="ph"><h3>Payables Aging</h3>
      <span style="color:var(--mut);font-size:11.5px">${money(apTot, c)} outstanding</span></div>
      <div class="pb">${bars(apB, 130, c, PAL[4])}</div></div>
    <div class="panel anim d6"><div class="ph"><h3>Position</h3></div><div class="pb">
      <div class="mrow"><span>Total assets</span><b>${money(conv(cm.total_assets), c)}</b></div>
      <div class="mrow"><span>Total liabilities</span><b>${money(conv(cm.total_liabilities), c)}</b></div>
      <div class="mrow"><span>Inventory</span><b>${money(conv(cm.inventory), c)}</b></div>
      <div class="mrow tot"><span>Equity</span><b>${money(conv(cm.total_equity), c)}</b></div>
      <div class="legend" style="margin-top:12px">
        <span>Open invoices <b>${open.length}</b></span>
        <span class="${overdue.length ? 'neg' : ''}">Overdue <b>${overdue.length}</b></span></div>
    </div></div>
  </div>

  ${hrBlock(hr, c)}`);
};

/* ── PAYROLL DISBURSEMENT ──────────────────────────────────────────────────
   The last step of the run: `calculate -> submit -> approve -> post -> pay`.

   Posting created the liability (Dr salary expense / Cr salaries payable).
   This discharges it (Dr salaries payable / Cr bank) and is deliberately a
   *second* entry: the money leaves on a different date from the accrual, is
   authorised by a different person, and reconciles against a bank statement
   rather than a payroll register. Between the two the balance sheet correctly
   shows money owed to staff.

   Two inputs, both of which the previous button silently defaulted:

   `bank_account_system_key`
       Which account the cash leaves. The API takes a *system key*, not an
       account id, because the posting service resolves by role — account
       codes differ per national chart. So the picker offers only accounts
       that carry one; a bank coded in Bank Setup whose ledger account has no
       system_key cannot be the source, and saying that is better than
       silently paying from the default.

   `payment_date`
       A file sent on Friday may settle on Monday, and the cash entry must
       carry the date the money actually left, not the date somebody clicked.  */
async function disburseRun(runId){
  let run,accounts;
  try{
    [run,accounts]=await Promise.all([
      api(`/api/v1/payroll-runs/${runId}/`),
      list('accounts'),
    ]);
  }catch(e){ return toast(e.message,'bad'); }

  if(run.status!=='posted'){
    return toast(
      `${run.name} is ${String(run.status).replace(/_/g,' ')}. Only a run that `
      +`has been posted to the ledger can be disbursed — the payment has to `
      +`discharge a liability that exists.`,'bad');
  }

  // Only accounts the posting service can resolve by role.
  // Assets only. A substring match on "bank" also caught `bank_fees` — the
  // *expense* account for charges — and offered it as somewhere salaries
  // could be paid from. Crediting an expense account would balance, post
  // cleanly, and leave both the cash and the P&L wrong.
  const sources=(accounts||[]).filter(a=>
    a.system_key&&a.type==='asset'&&a.is_postable&&a.is_active
    &&(a.is_reconcilable||/^(bank|cash|petty)/i.test(a.system_key)));
  if(!sources.length){
    return toast('No bank or cash account carries a system key — '
      +'the disbursement has no source it can resolve','bad');
  }
  const preferred=sources.find(a=>a.system_key==='bank_main')||sources[0];
  const c=run.currency||C();
  const today=new Date().toISOString().slice(0,10);

  modal(`Execute Disbursement — ${esc(run.name)}`,`
    <div class="mrow"><span>Employees</span><b>${run.employee_count??0}</b></div>
    <div class="mrow"><span>Gross</span><b>${money(run.total_gross,c)}</b></div>
    <div class="mrow"><span>Deductions</span><b>-${money(run.total_deductions,c)}</b></div>
    <div class="mrow tot"><span>Net to disburse</span>
      <b>${money(run.total_net,c)}</b></div>

    <div class="row" style="margin-top:16px">
      <div><label class="req">Pay From</label>
        <select id="d_acct">${sources.map(a=>
          `<option value="${esc(a.system_key)}" ${a.id===preferred.id?'selected':''}>${
            esc(a.code)} — ${esc(a.name)}</option>`).join('')}</select></div>
      <div><label class="req">Payment Date</label>
        <input id="d_date" type="date" value="${today}">
        <div class="note">The date the money actually left, which may not be
          today — a file sent Friday can settle Monday.</div></div>
    </div>

    <div class="note" style="margin-top:14px">This posts a second journal
      entry: <b>Dr Salaries payable ${money(run.total_net,c)}</b> /
      <b>Cr the account above</b>. It discharges the liability raised when the
      run was posted. Entries are never deleted — a mistake is corrected by a
      reversing entry.</div>`,
    'Disburse Now', async()=>{
      const key=document.getElementById('d_acct').value;
      const when=document.getElementById('d_date').value;
      if(!when)return toast('A payment date is required','bad');
      if(!await ensureReauth())return;
      try{
        const entry=await api(`/api/v1/payroll-runs/${runId}/mark-paid/`,{
          method:'POST',idem:1,reauth:1,
          body:JSON.stringify({bank_account_system_key:key,payment_date:when})});
        closeModal();
        toast('Salaries disbursed','ok');
        // Show what was posted rather than just asserting it happened.
        if(entry&&entry.id)openEntry(entry.id);
        else go('payroll');
      }catch(e){
        if(e.code==='reauth_required'){S.reauth=null;
          toast('That confirmation expired — try again','bad');}
        else toast(e.message,'bad');
      }});
}

/* ── OPERATIONS (HR) ───────────────────────────────────────────────────────
   Rendered from `/reporting/hr-metrics/`, which is permissioned separately
   from the financial block — headcount and attendance are HR's data, and a
   finance-only role holds none of it. A null response hides this section
   entirely rather than showing empty tiles that look like real zeros.

   Payroll cost is *employer* cost: gross plus employer contributions. Net pay
   is what lands in people's accounts; the company also pays social insurance
   on top, and reporting net as "payroll cost" understates the budget line by
   roughly a fifth at the default Egyptian rates.                            */
function hrBlock(hr, c) {
  if (!hr) return '';
  const h = hr.headcount, p = hr.payroll, a = hr.attendance,
        lv = hr.leave, cov = hr.coverage;
  const rate = a.rate == null ? null : parseFloat(a.rate);

  return `
  <div class="pagehead" style="padding:22px 0 0">
    <h2 style="font-size:16px">Operations</h2>
    <span class="note" style="margin:0">${dt(hr.date_from)} – ${dt(hr.date_to)}</span>
  </div>

  <div class="kpis" style="margin-top:12px">
    <div class="kpi anim"><div class="lbl">Monthly Payroll Cost</div>
      <b data-val="${conv(p.cost)}" data-cur="${c}">—</b>
      <i>${p.runs_counted ? `${p.runs_counted} approved run${p.runs_counted > 1 ? 's' : ''}`
         : 'no approved run'} · incl. employer</i></div>

    <div class="kpi anim d1"><div class="lbl">Active Employees</div>
      <b>${h.active}</b>
      <i>${h.on_leave} on leave${h.joined ? ` · +${h.joined} joined` : ''}${
        h.left ? ` · -${h.left} left` : ''}</i></div>

    <div class="kpi anim d2"><div class="lbl">Attendance</div>
      ${rate == null
        ? `<b style="color:var(--mut)">—</b><i>nothing captured</i>`
        : `<b class="${rate >= 0.95 ? 'pos' : rate >= 0.85 ? '' : 'neg'}">${
            (rate * 100).toFixed(1)}%</b>
           <i>${a.attended_days}/${a.expected_days} expected days</i>`}</div>

    <div class="kpi anim d3"><div class="lbl">Pending Leave</div>
      <b class="${lv.pending_count ? 'neg' : 'pos'}">${lv.pending_count}</b>
      <i>${lv.pending_count ? 'awaiting a decision' : 'nothing waiting'}</i></div>

    <div class="kpi anim d4"><div class="lbl">Shift Coverage</div>
      <b class="${cov.employees_uncovered ? 'neg' : 'pos'}">${cov.employees_covered}/${
        cov.employees_covered + cov.employees_uncovered}</b>
      <i>next ${cov.horizon_days} days</i></div>
  </div>

  ${hr.assumptions.length ? `<div class="panel anim d2"><div class="pb">
    ${hr.assumptions.map(x => `<div class="note" style="margin:0 0 6px">⚠ ${esc(x)}</div>`).join('')}
  </div></div>` : ''}

  <div class="g2c">
    <div class="panel anim d3"><div class="ph"><h3>Leave Awaiting Approval</h3>
      ${lv.pending_count ? `<button class="btn sm sec" onclick="go('leaves')">Review</button>` : ''}
    </div>
    ${lv.pending.length ? `<table><thead><tr>
        <th>Employee</th><th>Type</th><th>From</th><th class="num">Days</th><th>Status</th>
      </tr></thead><tbody>${lv.pending.map(r => `<tr>
        <td>${esc(r.employee)}</td><td>${esc(r.leave_type || '—')}</td>
        <td>${dt(r.start_date)}
          ${r.starts_in_days != null && r.starts_in_days < 0
            ? '<span class="tag t-dang" style="margin-left:6px">Started</span>'
            : r.starts_in_days != null && r.starts_in_days <= 3
            ? '<span class="tag t-warn" style="margin-left:6px">Soon</span>' : ''}</td>
        <td class="num">${qty(r.total_days)}</td><td>${tag(r.status)}</td>
      </tr>`).join('')}</tbody></table>
      ${lv.pending_count > lv.pending.length
        ? `<div class="note" style="padding:10px 16px">Showing ${lv.pending.length}
           of ${lv.pending_count}.</div>` : ''}`
      : `<div class="empty"><h4>Nothing waiting</h4>
         <p>Every leave request has been decided.</p></div>`}
    </div>

    <div class="panel anim d4"><div class="ph"><h3>Upcoming Shift Coverage</h3>
      <span class="note" style="margin:0">next ${cov.horizon_days} days</span></div>
    ${cov.shifts.length ? `<table><thead><tr>
        <th>Shift</th><th>Hours</th><th class="num">Staff</th><th class="num">Expiring</th>
      </tr></thead><tbody>${cov.shifts.map(sh => `<tr>
        <td>${esc(sh.shift)}</td>
        <td class="mono">${esc(sh.start_time)}–${esc(sh.end_time)}${
          sh.crosses_midnight ? ' <span class="note">+1d</span>' : ''}</td>
        <td class="num">${sh.employees}</td>
        <td class="num ${sh.expiring ? 'neg' : ''}">${sh.expiring || '—'}</td>
      </tr>`).join('')}</tbody></table>
      ${cov.employees_uncovered ? `<div class="note" style="padding:10px 16px">
        ${cov.employees_uncovered} employee(s) have no shift assignment covering
        this period — overtime for them cannot be priced against a pattern.
        <a href="#" onclick="go('shiftassign');return false">Assign shifts</a>.</div>` : ''}`
      : `<div class="empty"><h4>No shifts assigned</h4>
         <p>Assign shifts so attendance and overtime price against a pattern.</p>
         <button class="btn" onclick="go('shiftassign')">Assign a shift</button></div>`}
    </div>
  </div>`;
}

/* ── QUICK CREATE + FORM ENGINE ─────────────────────────────────────────
   One declarative table drives every data-entry screen. Adding a form is a
   dozen lines of config rather than a new modal, which is what keeps
   validation, the money rule and error handling identical across screens
   instead of drifting apart one copy-paste at a time.                     */
const QC=[
 ['CODING',[['account','Chart of Accounts'],['costcenter','Cost Center'],
   ['taxrate','Tax Rate'],['bankaccount','Bank Account'],['item','Item / SKU'],
   ['department','Department'],['leavetype','Leave Type'],['shift','Shift']]],
 ['ENTRY',[['journal','Journal Entry']]],
 ['SALES',[['customer','Customer'],['invoice','Invoice'],['payment','Customer Payment']]],
 ['PURCHASES',[['vendor','Vendor'],['expense','Expense']]],
 ['PAYROLL & HR',[['employee','Employee'],['leave','Leave Request'],
   ['overtimeslip','Overtime Claim'],['salassign','Salary Assignment'],
   ['payrun','Pay Run']]],
 ['TEAM',[['invite','Invite User']]],
];
function quickCreate(ev){ev.stopPropagation();
  if(document.getElementById('qc'))return closeQC();
  const d=document.createElement('div');d.className='qc';d.id='qc';
  d.innerHTML=QC.map(([h,it])=>`<div class="col"><h5>${h}</h5>`+it.map(([k,l])=>
    `<a href="#" onclick="closeQC();openForm('${k}');return false">${l}</a>`).join('')+`</div>`).join('');
  document.querySelector('header').appendChild(d);
  setTimeout(()=>document.addEventListener('click',closeQC,{once:true}),0);}
function closeQC(){const q=document.getElementById('qc');if(q)q.remove();}

const FORMS={
 customer:{t:'New Customer',ep:'customers',after:'customers',f:[
   ['code','Customer Code','text',1],['name','Customer Name','text',1],
   ['display_name','Display Name','text'],['email','Email','email'],['phone','Phone','text'],
   ['receivable_account','Receivable Account','opt',1,'ar'],
   ['payment_terms_days','Payment Terms (days)','int',0,null,'30'],
   ['credit_limit','Credit Limit','money',0,null,'0.00'],['currency','Currency','cur']]},
 vendor:{t:'New Vendor',ep:'vendors',after:'vendors',f:[
   ['code','Vendor Code','text',1],['name','Vendor Name','text',1],
   ['display_name','Display Name','text'],['email','Email','email'],['phone','Phone','text'],
   ['payable_account','Payable Account','opt',1,'ap'],['currency','Currency','cur']]},
 item:{t:'New Item',ep:'items',after:'items',f:[
   ['sku','SKU','text',1],['name','Item Name','text',1],['uom','Unit','opt',1,'uom'],
   ['currency','Currency','cur'],
   ['type','Type','sel',1,[['inventory','Inventory'],['service','Service'],
      ['non_inventory','Non-inventory']]],
   ['sales_price','Selling Price','money',0,null,'0.00'],
   ['purchase_price','Cost Price','money',0,null,'0.00'],
   ['reorder_point','Reorder Point','money',0,null,'0.00'],
   ['income_account','Income Account','opt',0,'income'],
   ['expense_account','COGS Account','opt',0,'expense']]},
 /* Accounts are created from the Chart of Accounts tree (addAccount), not a
    flat form: the code is server-allocated from the parent, so a form that let
    a user type one would post a number the server ignores. */
 department:{t:'New Department',ep:'departments',after:'departments',f:[
   ['code','Code','text',1],['name','Department Name','text',1],
   ['parent','Parent Department','opt',0,'dept']]},
 employee:{t:'New Employee',ep:'employees',after:'employees',f:[
   ['employee_code','Employee Code','text',1],['first_name','First Name','text',1],
   ['last_name','Last Name','text',1],['work_email','Work Email','email'],
   ['phone','Phone','text'],['department','Department','opt',1,'dept'],
   ['hire_date','Hire Date','date',1],
   ['employment_type','Employment Type','sel',1,[['full_time','Full time'],
      ['part_time','Part time'],['contract','Contract'],['intern','Intern']]],
   ['base_salary','Base Salary','money',1,null,'0.00'],
   ['salary_currency','Salary Currency','cur']]},
 expense:{t:'New Expense',ep:'expenses',after:'expenses',f:[
   ['expense_date','Date','date',1],['vendor','Vendor','opt',0,'vendor'],
   ['category','Category','opt',1,'expcat'],
   ['paid_from_account','Paid Through','opt',1,'cash'],
   ['payment_method','Payment Mode','sel',1,[['bank_transfer','Bank transfer'],
      ['cash','Cash'],['card','Card'],['cheque','Cheque']]],
   ['amount','Amount','money',1,null,'0.00'],
   ['tax_amount','Tax Amount','money',0,null,'0.00'],
   ['currency','Currency','cur'],['notes','Notes','text']]},
 payment:{t:'Record Customer Payment',ep:'payments',idem:1,reauth:1,after:'payments',f:[
   ['customer','Customer','opt',1,'cust'],['payment_date','Payment Date','date',1],
   ['amount','Amount','money',1,null,'0.00'],['currency','Currency','cur'],
   ['method','Payment Mode','sel',1,[['bank_transfer','Bank transfer'],['cash','Cash'],
      ['cheque','Cheque'],['card','Card']]],
   ['reference','Reference #','text']]},
 payrun:{t:'New Pay Run',ep:'payroll-runs',after:'payroll',f:[
   ['name','Pay Run Name','text',1],['period_start','Period From','date',1],
   ['period_end','Period To','date',1],['pay_date','Pay Date','date',1],
   ['frequency','Frequency','sel',1,[['monthly','Monthly'],['biweekly','Bi-weekly'],
      ['weekly','Weekly']]],['currency','Currency','cur']]},
 leave:{t:'New Leave Request',ep:'leave-requests',after:'leaves',f:[
   ['employee','Employee','opt',1,'emp'],['leave_type','Leave Type','opt',1,'lt'],
   ['start_date','From','date',1],['end_date','To','date',1],
   ['total_days','Total Days','money',1,null,'1'],['reason','Reason','text']]},
 recurringbill:{t:'New Recurring Bill',ep:'recurring-bills',after:'recurringbills',f:[
   ['name','Schedule Name','text',1],['vendor','Vendor','opt',1,'vendor'],
   ['frequency','Frequency','sel',1,[['monthly','Monthly'],['quarterly','Quarterly'],
      ['weekly','Weekly'],['biweekly','Bi-weekly'],['semiannual','Semi-annual'],
      ['annual','Annual']]],
   ['interval','Every N periods','int',0,null,'1'],
   ['start_date','Starts','date',1],['end_date','Ends (blank = open)','date'],
   ['max_occurrences','Max occurrences (blank = unlimited)','int'],
   ['payment_terms_days','Payment Terms (days)','int',0,null,'30'],
   ['currency','Currency','cur']],
   note:'Generated bills land in Draft. They are never auto-approved — approval '+
        'is the control that stops a compromised schedule paying a fake vendor.'},
 recurringexpense:{t:'New Recurring Expense',ep:'recurring-expenses',
   after:'recurringexpenses',f:[
   ['name','Schedule Name','text',1],['vendor','Vendor','opt',1,'vendor'],
   ['category','Category','opt',1,'expcat'],
   ['paid_from_account','Paid From','opt',1,'cash'],
   ['payment_method','Payment Mode','sel',1,[['company_card','Company card'],
      ['bank_transfer','Bank transfer'],['cash','Cash'],['petty_cash','Petty cash']]],
   ['amount','Amount','money',1,null,'0.00'],
   ['tax_amount','Tax','money',0,null,'0.00'],
   ['frequency','Frequency','sel',1,[['monthly','Monthly'],['quarterly','Quarterly'],
      ['annual','Annual'],['weekly','Weekly']]],
   ['interval','Every N periods','int',0,null,'1'],
   ['start_date','Starts','date',1],['end_date','Ends (blank = open)','date'],
   ['description','Description','text'],['currency','Currency','cur']],
   note:'No bill and no approval to pay — the money leaves the account directly, '+
        'so there is no payable in between.'},
 leavetype:{t:'New Leave Type',ep:'leave-types',after:'leavetypes',f:[
   ['code','Code','text',1],['name','Name','text',1],
   ['accrual_method','Accrual','sel',1,[['monthly','Monthly'],['annual','Annual'],
      ['none','None']]],
   ['accrual_rate_days','Accrual per period (days)','money',0,null,'1.75'],
   ['max_balance_days','Maximum balance (days)','money',0,null,'42'],
   ['carry_over_limit_days','Carry-over limit (days)','money',0,null,'0'],
   ['min_notice_days','Minimum notice (days)','int',0,null,'0'],
   ['requires_attachment_after_days','Evidence required after (days)','int',0,null,'0']],
   note:'Unpaid types must set affects_payroll so the days prorate the salary; '+
        'edit that on the record once created.'},
 shift:{t:'New Shift',ep:'shifts',after:'shifts',f:[
   ['code','Code','text',1],['name','Name','text',1],
   ['start_time','Start (HH:MM)','text',1,null,'09:00'],
   ['end_time','End (HH:MM)','text',1,null,'17:00'],
   ['break_minutes','Break (minutes)','int',0,null,'60'],
   ['expected_hours_per_day','Expected hours/day','money',0,null,'8'],
   ['overtime_after_hours','Overtime after (hours)','money',0,null,'8'],
   ['late_grace_minutes','Late grace (minutes)','int',0,null,'15']],
   note:'A shift ending earlier than it starts crosses midnight — set that flag '+
        'on the record, or attendance will read a night shift as negative hours.'},
 overtimetype:{t:'New Overtime Type',ep:'overtime-types',after:'ottypes',f:[
   ['code','Code','text',1],['name','Name','text',1],
   ['multiplier','Multiplier (1.5 = time and a half)','money',1,null,'1.500000'],
   ['component','Payroll Component','opt',1,'paycomp']],
   note:'The multiplier prices the hours; the component decides which expense '+
        'account the money lands in. Several types can share one component, '+
        'which is why they are separate.'},
 overtimeslip:{t:'New Overtime Claim',ep:'overtime-slips',after:'overtime',f:[
   ['employee','Employee','opt',1,'emp'],
   ['overtime_type','Overtime Type','opt',1,'ottype'],
   ['work_date','Date Worked','date',1],
   ['hours','Hours','money',1,null,'1.00'],
   ['currency','Currency','cur'],['notes','Notes','text']],
   note:'The amount is computed by the server when the claim is approved, '+
        'against the salary in force on the day worked — not the current one.'},
 shiftassign:{t:'Assign a Shift',ep:'shift-assignments',after:'shiftassign',f:[
   ['employee','Employee','opt',1,'emp'],['shift','Shift','opt',1,'shift'],
   ['start_date','From','date',1],['end_date','To (blank = open-ended)','date'],
   ['location','Location','text'],['notes','Notes','text']],
   note:'Overlapping assignments are legal — a two-week cover over a standing '+
        'rotation. The one that started most recently wins.'},
 salstructure:{t:'New Salary Structure',ep:'salary-structures',after:'structures',f:[
   ['code','Code','text',1],['name','Package Name','text',1],
   ['description','Description','text'],['currency','Currency','cur']],
   note:'A template, not a grant. Assign it to employees afterwards — each '+
        'assignment carries its own base salary.'},
 salassign:{t:'Assign a Salary Structure',ep:'salary-structure-assignments',
   after:'structureassign',f:[
   ['employee','Employee','opt',1,'emp'],
   ['structure','Structure','opt',1,'structure'],
   ['from_date','Effective From','date',1],
   ['to_date','Until (blank = current)','date'],
   ['base_salary','Base Salary','money',1,null,'0.00'],
   ['currency','Currency','cur']],
   note:'A promotion is a new assignment, not an edit to the old one — past '+
        'payslips must stay explainable by the package in force then.'},
 bankaccount:{t:'New Bank Account',ep:'bank-accounts',after:'banksetup',f:[
   ['name','Account Label','text',1],['bank_name','Bank Name','text',1],
   ['account_number_last4','Account Number (last 4)','text'],
   ['iban','IBAN','text'],['swift','SWIFT / BIC','text'],['branch','Branch','text'],
   ['currency','Currency','cur'],
   ['ledger_account','Ledger Account','opt',1,'cash'],
   ['opening_balance','Opening Balance','money',0,null,'0.00'],
   ['opening_date','Opening Date','date']],
   note:'Only the last four digits of the account number are stored — a full '+
        'number in an application database is a liability with no upside, and '+
        'reconciliation only ever needs the last four. The ledger account is '+
        'the chart node this bank posts to; every payment made from this '+
        'account credits it.'},
 taxrate:{t:'New Tax Rate',ep:'tax-rates',after:'taxrates',f:[
   ['code','Tax Code','text',1],['name','Tax Name','text',1],
   ['rate','Rate (fraction, e.g. 0.14 = 14%)','money',1,null,'0.140000'],
   ['collected_account','Output / Collected Account','opt',1,'liab'],
   ['paid_account','Input / Paid Account','opt',1,'asset'],
   ['effective_from','Effective From','date',1]],
   note:'The rate is a fraction, not a percentage — 0.14 is 14%. The server '+
        'enforces 0 ≤ rate ≤ 1, so entering 14 is rejected rather than '+
        'silently posting a 1400% tax.'},
 costcenter:{t:'New Cost Center',ep:'departments',after:'departments',f:[
   ['code','Cost Center Code','text',1],['name','Cost Center Name','text',1],
   ['parent','Parent','opt',0,'dept'],
   ['cost_center_account','Cost Center Account','opt',0,'expense']],
   note:'Cost centers are departments carrying a cost-center account. Payroll '+
        'and expense postings use it to attribute cost to the right unit.'},
 invite:{t:'Invite a User',ep:'invitations',reauth:1,after:'invites',f:[
   ['email','Work Email','email',1],['role','Role','opt',1,'role'],
   ['department','Department (optional)','opt',0,'dept']],
   note:'They receive a link to set their own password. You can only grant roles '+
        'below your own authority.'},
};
const LOADERS={
 income:async()=>(await list('accounts')||[]).filter(a=>a.type==='income'&&a.is_postable),
 expense:async()=>(await list('accounts')||[]).filter(a=>a.type==='expense'&&a.is_postable),
 all:async()=>(await list('accounts')||[]),
 ar:async()=>{const a=await list('accounts')||[];const m=a.filter(x=>x.system_key==='ar_control');
   return m.length?m:a.filter(x=>x.type==='asset'&&x.is_postable);},
 ap:async()=>{const a=await list('accounts')||[];const m=a.filter(x=>x.system_key==='ap_control');
   return m.length?m:a.filter(x=>x.type==='liability'&&x.is_postable);},
 cash:async()=>{const a=await list('accounts')||[];
   const m=a.filter(x=>x.is_reconcilable||/cash|bank/i.test(x.system_key||''));
   return m.length?m:a.filter(x=>x.type==='asset'&&x.is_postable);},
 paycomp:async()=>(await list('payroll-components')||[]),
 ottype:async()=>(await list('overtime-types')||[]),
 shift:async()=>(await list('shifts')||[]),
 structure:async()=>(await list('salary-structures')||[]),
 liab:async()=>(await list('accounts')||[]).filter(a=>a.type==='liability'&&a.is_postable),
 asset:async()=>(await list('accounts')||[]).filter(a=>a.type==='asset'&&a.is_postable),
 dept:async()=>(await list('departments')||[]),vendor:async()=>(await list('vendors')||[]),
 cust:async()=>(await list('customers')||[]),emp:async()=>(await list('employees')||[]),
 lt:async()=>(await list('leave-types')||[]),expcat:async()=>(await list('expense-categories')||[]),
 uom:async()=>(await list('units-of-measure')||[]),role:async()=>(await list('roles')||[]),
};
const lbl=o=>o.display_name||o.name||o.full_name||
  ((o.first_name||'')+' '+(o.last_name||'')).trim()||o.code||o.id;

async function openForm(kind){
  if(kind==='invoice')return newInvoice();
  if(kind==='journal')return newJournal();
  const F=FORMS[kind];if(!F)return;
  if(F.reauth&&!await ensureReauth())return;
  const opts={};
  for(const fd of F.f)if(fd[2]==='opt'&&fd[4]&&!opts[fd[4]])opts[fd[4]]=await LOADERS[fd[4]]();
  const html='<div class="row">'+F.f.map(fd=>{
    const k=fd[0],l=fd[1],t=fd[2],req=fd[3],extra=fd[4],def=fd[5];
    const L='<label class="'+(req?'req':'')+'">'+l+'</label>';
    if(t==='sel')return '<div>'+L+'<select id="q_'+k+'">'+
      extra.map(o=>'<option value="'+o[0]+'">'+o[1]+'</option>').join('')+'</select></div>';
    if(t==='opt'){const rows=opts[extra]||[];
      return '<div>'+L+'<select id="q_'+k+'">'+(req?'':'<option value="">— none —</option>')+
        rows.map(o=>'<option value="'+o.id+'">'+esc(lbl(o))+'</option>').join('')+'</select></div>';}
    if(t==='cur')return '<div>'+L+'<input id="q_'+k+'" value="'+S.tenant.base_currency+'"></div>';
    if(t==='date')return '<div>'+L+'<input id="q_'+k+'" type="date" value="'+
      new Date().toISOString().slice(0,10)+'"></div>';
    return '<div>'+L+'<input id="q_'+k+'"'+(t==='email'?' type="email"':'')+' value="'+
      (def||'')+'"'+(t==='money'?' class="num"':'')+'></div>';}).join('')+'</div>'+
    (F.note?'<div class="note">'+F.note+'</div>':'')+
    (F.f.some(x=>x[2]==='money')?'<div class="note">Amounts are sent as text and validated by '+
      'the server — the browser never does the arithmetic.</div>':'');
  modal(F.t,html,'Save',async()=>{
    const body={};
    F.f.forEach(fd=>{const el=document.getElementById('q_'+fd[0]);let v=el?el.value:'';
      if(v==='')return;body[fd[0]]=(fd[2]==='int')?parseInt(v,10):String(v);});
    for(const fd of F.f)if(fd[3]&&!body[fd[0]])return toast(fd[1]+' is required','bad');
    try{const r=await api('/api/v1/'+F.ep+'/',{method:'POST',idem:!!F.idem,
        reauth:!!F.reauth,body:JSON.stringify(body)});
      closeModal();
      if(kind==='invite'&&r&&r.invite_url){
        modal('Invitation sent',`<p style="margin:0 0 12px">Share this link with
          <b>${esc(body.email)}</b> if they do not receive the email:</p>
          <input readonly value="${esc(r.invite_url)}" onclick="this.select()">
          <div class="note">The link expires in 7 days. It sets a password only for a
          brand-new account — an existing user is asked to sign in instead, so an invite
          can never reset someone else's password.</div>`,'Done',closeModal);
      } else toast('Saved','ok');
      go(F.after);}
    catch(e){if(e.code==='reauth_required'){S.reauth=null;toast('Please confirm your password again','bad');}
      else toast(e.message,'bad');}});}

/* ── INVOICE FORM ──────────────────────────────────────────────────────────
   Full-document entry: header, line grid, summary, footer, two save actions.

   THE MONEY RULE still holds. Every figure this screen computes is an
   *estimate shown to the person typing*, computed in float, and labelled as
   such. Nothing it computes is submitted: the server receives quantity, unit
   price, discount rate and tax rate as strings and recomputes every total in
   Decimal. That is why the line grid can afford to be responsive — being
   approximately right in the browser costs nothing when the authority is
   elsewhere.

   Two save actions, because they are two different acts:
     Save as Draft — an editable document, no ledger effect, no number.
     Save & Issue  — allocates the gapless number, posts the revenue entry,
                     releases stock. Reversible only by a reversing entry,
                     which is why it asks first.                             */
const IV = { customers: [], items: [], taxes: [], accounts: [], staff: [],
             lines: 0, editing: null };

//: Payment terms, as (label, days). Due date is derived, and stays derived
//: until the user edits it by hand — at which point the terms selector stops
//: overwriting it, because an explicitly typed date is not a suggestion.
const TERMS = [
  ['Due on Receipt', 0], ['Net 15', 15], ['Net 30', 30],
  ['Net 45', 45], ['Net 60', 60], ['Custom', null],
];

async function newInvoice(existingId) {
  const [cu, it, tx, ac, emp] = await Promise.all([
    list('customers'), list('items'), list('tax-rates'), list('accounts'),
    list('employees'),
  ]);
  if (!cu || !cu.length) {
    return toast('Add a customer first — an invoice needs someone to owe it', 'bad');
  }

  // Editing an existing document. Fetched fresh rather than reused from the
  // list: the list is a projection and may be minutes old, and re-submitting
  // a stale copy would silently revert whatever somebody else changed in
  // between.
  let doc = null;
  if (existingId) {
    try {
      doc = await api(`/api/v1/invoices/${existingId}/`);
    } catch (e) {
      return toast(`Could not load that invoice — ${e.message}`, 'bad');
    }
    // The server refuses to update anything past DRAFT (see
    // InvoiceSerializer.validate) because an issued invoice has been sent to
    // a customer and posted to the ledger. Saying so here means the user
    // finds out before retyping the document, not after pressing Save.
    if (doc.status !== 'draft') {
      return toast(
        `${doc.number || 'That invoice'} is ${String(doc.status).replace(/_/g, ' ')} `
        + `and cannot be edited — issue a credit note instead`, 'bad');
    }
  }
  IV.editing = doc;
  // Per-form state: a queue left from a cancelled invoice would upload onto
  // the next one.
  ATTACH.queued = []; ATTACH.saved = [];
  IV.customers = cu; IV.items = it || []; IV.taxes = tx || [];
  IV.accounts = (ac || []).filter(a => a.type === 'income' && a.is_postable);
  IV.staff = emp || [];
  IV.lines = 0;

  const today = new Date().toISOString().slice(0, 10);
  const c = S.tenant.base_currency;
  const d = doc || {};
  //: Value for a prefilled input: the stored one when editing, the given
  //: default when creating.
  const val = (field, fallback = '') => (d[field] != null && d[field] !== '')
    ? String(d[field]) : fallback;
  const sel = (field, id) => (d[field] === id ? ' selected' : '');

  modal(doc ? `Edit Invoice — ${esc(doc.number || 'Draft')}` : 'New Invoice', `
    <div class="row">
      <div><label class="req">Customer</label>
        <div style="display:flex;gap:6px">
          <select id="i_cust" onchange="ivCustomer()">${IV.customers.map(x =>
            `<option value="${x.id}"${sel('customer', x.id)}>${esc(lbl(x))}</option>`
            ).join('')}</select>
          <button class="btn sec sm" title="New customer"
            onclick="ivQuickCustomer()">+</button>
        </div></div>
      <div><label>Invoice #</label>
        <input id="i_number" value="${esc(val('number'))}"
               placeholder="Allocated when issued" disabled>
        <div class="note">Numbers are gapless and assigned at issue, so a
          draft that is never issued cannot leave a hole in the sequence.</div></div>
      <div><label>Order Number</label>
        <input id="i_order" value="${esc(val('order_number'))}"
               placeholder="Customer's PO reference"></div>
      <div><label class="req">Invoice Date</label>
        <input id="i_issue" type="date" value="${esc(val('issue_date', today))}"
               onchange="ivTerms()"></div>
      <div><label>Terms</label>
        <select id="i_terms" onchange="ivTerms()">${TERMS.map(([l, days], i) => {
          // Editing: the stored due date is a decision already made, so the
          // selector opens on Custom and leaves it alone. Creating: Net 30.
          const isDefault = doc ? (l === 'Custom') : (i === 2);
          return `<option value="${days === null ? '' : days}"${isDefault ? ' selected' : ''}>${l}</option>`;
        }).join('')}</select></div>
      <div><label class="req">Due Date</label>
        <input id="i_due" type="date" value="${esc(val('due_date'))}"
               onchange="ivDueEdited()"></div>
      <div><label>Salesperson</label>
        <select id="i_sales"><option value="">— none —</option>${IV.staff.map(e =>
          `<option value="${e.id}"${sel('salesperson', e.id)}>${esc(lbl(e))}</option>`
          ).join('')}</select></div>
      <div><label>Currency</label><input value="${esc(c)}" disabled></div>
    </div>

    <label style="margin-top:14px">Subject</label>
    <input id="i_subject" value="${esc(val('subject'))}"
           placeholder="What this invoice is for">

    <label style="margin-top:18px">Item Table</label>
    <table class="lines"><thead><tr>
      <th style="width:26%">Item / Description</th>
      <th style="width:9%" class="num">Qty</th>
      <th style="width:13%" class="num">Rate</th>
      <th style="width:13%" class="num">Discount</th>
      <th style="width:16%">Tax</th>
      <th style="width:15%" class="num">Amount</th>
      <th style="width:4%"></th>
    </tr></thead><tbody id="i_lines"></tbody></table>
    <datalist id="i_items">${IV.items.map(x =>
      `<option value="${esc(x.sku)} — ${esc(x.name)}">`).join('')}</datalist>

    <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center">
      <button class="btn sec sm" onclick="ivRow()">+ Add New Row</button>
      <button class="btn sec sm" onclick="ivBulk()">Add Items in Bulk</button>
      <span class="note" style="margin:0">Enter adds a row · Ctrl+Enter saves a draft</span>
    </div>

    <div class="totbox"><div>
      <div class="totrow"><span>Sub Total</span><b id="i_sub">0.00</b></div>
      <div class="totrow"><span>Line Discounts</span><b id="i_disc">0.00</b></div>
      <div class="totrow"><span>Invoice Discount</span>
        <input id="i_hdisc" class="num" value="${esc(val('discount_amount', '0.00'))}"
               oninput="ivCalc()" style="width:110px;padding:3px 7px"></div>
      <div class="totrow"><span>Tax</span><b id="i_tax">0.00</b></div>
      <div class="totrow g"><span>Total (${esc(c)})</span><b id="i_total">0.00</b></div>
      <div class="note">Estimate. The server recomputes every figure in
        fixed-precision decimal and its answer is the one that is filed.</div>
    </div></div>

    <div class="row" style="margin-top:16px">
      <div><label>Customer Notes</label>
        <input id="i_notes" value="${esc(val('notes'))}"
               placeholder="Thanks for your business."></div>
      <div><label>Terms &amp; Conditions</label>
        <input id="i_tc" value="${esc(val('terms'))}"
               placeholder="Payment terms, late-fee policy"></div>
    </div>

    <label style="margin-top:14px">Attachments</label>
    <div id="i_drop" class="drop"
         ondragover="ivDragOver(event)" ondragleave="ivDragLeave(event)"
         ondrop="ivDrop(event)" onclick="document.getElementById('i_file').click()">
      <input type="file" id="i_file" multiple style="display:none"
             onchange="ivPick(this.files)">
      <b>Drop files here</b> or click to browse
      <div class="note" style="margin-top:4px">PDF, images, Office documents,
        CSV or ZIP · up to 10 MB each${doc ? ''
        : ' · uploaded once the invoice is saved'}</div>
    </div>
    <div id="i_attach"></div>`,
    doc ? 'Save Changes' : 'Save as Draft', () => ivSave(false));

  // Footer gets the second action. Issuing is not "saving harder": it posts to
  // the ledger, so it is a separate button that says so.
  const foot = document.querySelector('#ov .mf');
  if (foot) {
    foot.innerHTML = `
      <button class="btn" onclick="ivSave(false)">${doc ? 'Save Changes' : 'Save as Draft'}</button>
      <button class="btn" style="background:var(--ok)" onclick="ivSave(true)">Save &amp; Issue</button>
      <button class="btn sec" onclick="closeModal()">Cancel</button>`;
  }

  // Reset per form: this is module state, and leaving it set from a previous
  // edit would stop the terms selector working on the next invoice.
  ivDueManual = !!doc;
  ivTerms();
  if (doc && (doc.lines || []).length) {
    doc.lines.forEach(l => ivPrefillRow(l));
  } else {
    ivRow();
  }
  ivCalc();
  ivRenderAttachments();
  if (doc) ivLoadAttachments(doc.id);
  document.getElementById('ov').addEventListener('keydown', ivKeys);
  setTimeout(() => { const f = document.getElementById('i_cust'); if (f) f.focus(); }, 30);
}

/* Due date follows the terms until someone types one. */
let ivDueManual = false;
function ivDueEdited() { ivDueManual = true; }
function ivTerms() {
  if (ivDueManual) return;
  const days = document.getElementById('i_terms').value;
  if (days === '') return;                       // Custom: leave it alone
  const issue = document.getElementById('i_issue').value;
  if (!issue) return;
  const d = new Date(issue + 'T00:00:00');
  d.setDate(d.getDate() + parseInt(days, 10));
  document.getElementById('i_due').value = d.toISOString().slice(0, 10);
}

/* Default the line's income account from the customer where it is set. */
function ivCustomer() { ivCalc(); }

async function ivQuickCustomer() {
  // Inline creation without leaving the invoice: closing this modal to create
  // a customer loses everything typed so far, which is why Zoho puts the
  // button here rather than a link to the customers screen.
  closeModal();
  await openForm('customer');
  toast('Create the customer, then start the invoice again', 'ok');
}

function ivRow(prefill) {
  const tb = document.getElementById('i_lines'); if (!tb) return;
  IV.lines += 1;
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input list="i_items" class="i_item" placeholder="Type or select an item"
          value="${prefill ? esc(prefill.label) : ''}"></td>
    <td><input class="i_qty num" inputmode="decimal" value="${prefill ? prefill.qty : '1'}"></td>
    <td><input class="i_rate num" inputmode="decimal" value="${prefill ? prefill.rate : '0.00'}"></td>
    <td><div style="display:flex;gap:2px">
      <input class="i_disc num" inputmode="decimal" value="0" style="min-width:0">
      <select class="i_dmode" style="width:46px;padding:4px">
        <option value="pct">%</option><option value="amt">${esc(S.tenant.base_currency)}</option>
      </select></div></td>
    <td><select class="i_tax"><option value="">— none —</option>${IV.taxes.map(t =>
      `<option value="${t.id}" data-rate="${t.rate}">${esc(t.code)} ${
        (parseFloat(t.rate) * 100).toFixed(0)}%</option>`).join('')}</select></td>
    <td class="num i_amt">0.00</td>
    <td><button class="x" title="Remove row"
        onclick="this.closest('tr').remove();ivCalc()">&times;</button></td>`;
  tb.appendChild(tr);
  ['.i_item', '.i_qty', '.i_rate', '.i_disc', '.i_dmode', '.i_tax'].forEach(sel =>
    tr.querySelector(sel).addEventListener('input', () => ivLineChanged(tr)));
  tr.querySelector('.i_tax').addEventListener('change', ivCalc);
  tr.querySelector('.i_dmode').addEventListener('change', ivCalc);
  ivCalc();
  return tr;
}

/* ── ATTACHMENTS ───────────────────────────────────────────────────────────
   A file only exists in the context of an invoice, so uploads go to
   `POST /invoices/{id}/attachments/` and there is no invoice id to send until
   the document has been saved once.

   Rather than disable the control on a new invoice — which teaches people the
   feature is broken — files chosen before the first save are *queued* in
   memory and uploaded immediately after the invoice is created. On an
   existing draft they upload straight away, because there is somewhere to put
   them.

   Multipart, one request per file. Not base64 in the JSON body: a 10 MB PDF
   becomes ~13 MB of string held in memory on both sides and logged by every
   proxy in between.                                                         */
const ATTACH = { queued: [], saved: [] };

//: Mirrors InvoiceAttachment.MAX_BYTES. Checked here so a 40 MB file is
//: refused before it is uploaded rather than after; the server enforces it
//: again, because a client-side limit is a courtesy, not a control.
const ATTACH_MAX = 10 * 1024 * 1024;
//: Mirrors InvoiceAttachmentUploadSerializer.DANGEROUS_SUFFIXES.
const ATTACH_BLOCKED = ['.html', '.htm', '.svg', '.xhtml', '.xml', '.js', '.mjs',
  '.exe', '.dll', '.bat', '.cmd', '.sh', '.ps1', '.jar', '.msi'];

function ivDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('over'); }
function ivDragLeave(e) { e.currentTarget.classList.remove('over'); }
function ivDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('over');
  ivPick(e.dataTransfer.files);
}

function ivPick(fileList) {
  const files = [...(fileList || [])];
  for (const f of files) {
    const dot = f.name.lastIndexOf('.');
    const suffix = dot >= 0 ? f.name.slice(dot).toLowerCase() : '';
    if (ATTACH_BLOCKED.includes(suffix)) {
      toast(`${suffix} files are not accepted as attachments`, 'bad');
      continue;
    }
    if (f.size === 0) { toast(`${f.name} is empty`, 'bad'); continue; }
    if (f.size > ATTACH_MAX) {
      toast(`${f.name} is ${Math.round(f.size / 1024)} KB; the limit is 10 MB`, 'bad');
      continue;
    }
    if (IV.editing) ivUpload(IV.editing.id, f);
    else { ATTACH.queued.push(f); ivRenderAttachments(); }
  }
  const input = document.getElementById('i_file');
  if (input) input.value = '';        // so the same file can be re-picked
}

async function ivLoadAttachments(invoiceId) {
  try {
    const rows = await api(`/api/v1/invoices/${invoiceId}/attachments/`);
    ATTACH.saved = rows.results || rows || [];
    ivRenderAttachments();
  } catch { /* an invoice with no attachments is not an error */ }
}

async function ivUpload(invoiceId, file, description) {
  const form = new FormData();
  form.append('file', file);
  if (description) form.append('description', description);
  try {
    // Not `api()`: that sets Content-Type: application/json, and a multipart
    // body needs the browser to set it *with the boundary* it generated.
    const r = await fetch(`${S.api}/api/v1/invoices/${invoiceId}/attachments/`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + S.access },
      body: form,
    });
    const text = await r.text();
    let d = null; try { d = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
    if (!r.ok) {
      const e = d && d.error;
      let msg = (e && e.detail) || `Upload failed (${r.status})`;
      if (e && e.fields) {
        msg = Object.values(e.fields).flat().join(' ') || msg;
      }
      throw new Error(msg);
    }
    ATTACH.saved.push(d);
    ivRenderAttachments();
    toast(`${file.name} attached`, 'ok');
    return d;
  } catch (e) {
    toast(`${file.name}: ${e.message}`, 'bad');
    return null;
  }
}

async function ivRemoveAttachment(id) {
  if (!await confirmDlg('Remove this attachment? The file is deleted, not archived.',
      { title: 'Remove attachment', confirmLabel: 'Remove', danger: true })) return;
  try {
    await api(`/api/v1/invoice-attachments/${id}/`, { method: 'DELETE' });
    ATTACH.saved = ATTACH.saved.filter(a => a.id !== id);
    ivRenderAttachments();
    toast('Attachment removed', 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}

function ivUnqueue(index) {
  ATTACH.queued.splice(index, 1);
  ivRenderAttachments();
}

const fileSize = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';

function ivRenderAttachments() {
  const box = document.getElementById('i_attach');
  if (!box) return;
  const rows = [
    ...ATTACH.saved.map(a => `<div class="att">
      <span class="att-n">${esc(a.original_filename)}</span>
      <span class="note" style="margin:0">${fileSize(a.size_bytes)}</span>
      ${a.file_url ? `<a href="${esc(a.file_url)}" target="_blank"
        rel="noopener" class="btn sec sm">Open</a>` : ''}
      <button class="btn sec sm" onclick="ivRemoveAttachment('${a.id}')">Remove</button>
    </div>`),
    // Queued files are visibly not yet stored — showing them identically to
    // saved ones would imply an upload that has not happened.
    ...ATTACH.queued.map((f, i) => `<div class="att pending">
      <span class="att-n">${esc(f.name)}</span>
      <span class="note" style="margin:0">${fileSize(f.size)} · uploads on save</span>
      <button class="btn sec sm" onclick="ivUnqueue(${i})">Remove</button>
    </div>`),
  ];
  box.innerHTML = rows.length
    ? `<div style="margin-top:8px">${rows.join('')}</div>` : '';
}

/* Rebuild one stored line into the grid.

   The discount round-trips as a *rate* because that is what the API stores.
   A line entered as "200 off" comes back as 0.200000 against a 1 000 gross,
   so it re-renders as 20% rather than as the amount originally typed. The
   money is identical; only the phrasing changes. Reconstructing the original
   phrasing would need a second stored field, and a discount that displays
   differently from how it was entered is a smaller surprise than a schema
   that carries two representations of one number and lets them disagree. */
function ivPrefillRow(line) {
  const tr = ivRow();
  if (!tr) return;
  const item = (line.item && IV.items.find(i => i.id === line.item)) || null;
  tr.querySelector('.i_item').value = item
    ? `${item.sku} — ${item.name}` : (line.description || '');
  // Marked filled so ivLineChanged does not overwrite the stored price with
  // the item's current one — a draft raised at last month's rate keeps it.
  if (item) tr.dataset.filled = '1';
  tr.querySelector('.i_qty').value = qty(line.quantity);
  tr.querySelector('.i_rate').value = qty(line.unit_price);
  const rate = parseFloat(line.discount_rate) || 0;
  if (rate > 0) {
    tr.querySelector('.i_dmode').value = 'pct';
    tr.querySelector('.i_disc').value = (rate * 100).toFixed(2).replace(/\.00$/, '');
  }
  if (line.tax_rate) {
    const s2 = tr.querySelector('.i_tax');
    if ([...s2.options].some(o => o.value === line.tax_rate)) s2.value = line.tax_rate;
  }
  return tr;
}

/* Picking a known item fills its price; typing free text leaves it alone. */
function ivLineChanged(tr) {
  const text = tr.querySelector('.i_item').value.trim();
  const item = ivItem(text);
  if (item && !tr.dataset.filled) {
    tr.dataset.filled = '1';
    const rate = item.sales_price ?? item.unit_price;
    if (rate != null && parseFloat(rate)) tr.querySelector('.i_rate').value = qty(rate);
    if (item.tax_rate) {
      const sel = tr.querySelector('.i_tax');
      if ([...sel.options].some(o => o.value === item.tax_rate)) sel.value = item.tax_rate;
    }
  }
  if (!item) delete tr.dataset.filled;
  ivCalc();
}

function ivItem(text) {
  const v = String(text || '').trim().toLowerCase(); if (!v) return null;
  const sku = v.split('—')[0].trim();
  return IV.items.find(i => String(i.sku).toLowerCase() === sku)
      || IV.items.find(i => (i.sku + ' — ' + i.name).toLowerCase() === v)
      || IV.items.find(i => String(i.name || '').toLowerCase() === v)
      || null;
}

/* Display-only arithmetic — see the module note. Never submitted. */
function ivCalc() {
  const rows = [...document.querySelectorAll('#i_lines tr')];
  let sub = 0, disc = 0, tax = 0;
  rows.forEach(tr => {
    const q = parseFloat(tr.querySelector('.i_qty').value) || 0;
    const r = parseFloat(tr.querySelector('.i_rate').value) || 0;
    const d = parseFloat(tr.querySelector('.i_disc').value) || 0;
    const mode = tr.querySelector('.i_dmode').value;
    const gross = q * r;
    // Percent is capped at 100: a 150% discount is a typo, and letting it
    // produce a negative line hides the mistake inside a plausible total.
    const lineDisc = mode === 'pct' ? gross * Math.min(d, 100) / 100 : Math.min(d, gross);
    const net = Math.max(gross - lineDisc, 0);
    const sel = tr.querySelector('.i_tax');
    const rate = parseFloat(sel.selectedOptions[0]?.dataset.rate || 0) || 0;
    sub += gross; disc += lineDisc; tax += net * rate;
    tr.querySelector('.i_amt').textContent = net.toFixed(2);
  });
  const header = parseFloat((document.getElementById('i_hdisc') || {}).value) || 0;
  const $ = id => document.getElementById(id);
  if (!$('i_sub')) return;
  $('i_sub').textContent = sub.toFixed(2);
  $('i_disc').textContent = disc.toFixed(2);
  $('i_tax').textContent = tax.toFixed(2);
  $('i_total').textContent = Math.max(sub - disc - header + tax, 0).toFixed(2);
}

/* Paste a block of SKUs — the "Add Items in Bulk" path. */
function ivBulk() {
  const picked = [];
  const body = `<p style="margin:0 0 10px;font-size:13px">Tick the items to add.
    Quantities default to 1 and prices to the item's own rate.</p>
    <div style="max-height:300px;overflow:auto;border:1px solid var(--line);
        border-radius:var(--r)">
      ${IV.items.length ? `<table><thead><tr><th style="width:36px"></th>
        <th>SKU</th><th>Name</th><th class="num">Rate</th></tr></thead><tbody>
        ${IV.items.map(i => `<tr><td><input type="checkbox" class="bk"
            data-id="${i.id}" style="width:auto"></td>
          <td class="mono">${esc(i.sku)}</td><td>${esc(i.name)}</td>
          <td class="num">${money(i.sales_price ?? 0, S.tenant.base_currency)}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty"><h4>No items coded</h4></div>'}
    </div>`;
  // Nested modal: modal() replaces rather than stacks, so the invoice would be
  // destroyed. Render into a lightweight overlay of its own instead.
  const ov = document.createElement('div');
  ov.className = 'ov'; ov.id = 'bulkov';
  ov.innerHTML = `<div class="modal" style="max-width:640px">
    <div class="mh"><h3>Add Items in Bulk</h3>
      <button class="x" onclick="document.getElementById('bulkov').remove()">&times;</button></div>
    <div class="mb">${body}</div>
    <div class="mf"><button class="btn" id="bulkAdd">Add selected</button>
      <button class="btn sec" onclick="document.getElementById('bulkov').remove()">Cancel</button></div>
  </div>`;
  document.body.appendChild(ov);
  ov.querySelector('#bulkAdd').onclick = () => {
    ov.querySelectorAll('.bk:checked').forEach(cb => {
      const item = IV.items.find(i => i.id === cb.dataset.id);
      if (item) ivRow({ label: `${item.sku} — ${item.name}`, qty: '1',
                        rate: item.sales_price ?? '0.00' });
    });
    ov.remove(); ivCalc();
  };
}

function ivKeys(e) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); return ivSave(false); }
  if (e.key === 'Enter' && e.target.closest && e.target.closest('#i_lines')) {
    e.preventDefault();
    const tr = ivRow();
    if (tr) tr.querySelector('.i_item').focus();
  }
}

async function ivSave(issue) {
  const rows = [...document.querySelectorAll('#i_lines tr')];
  const lines = [];
  for (const tr of rows) {
    const text = tr.querySelector('.i_item').value.trim();
    const qty = tr.querySelector('.i_qty').value.trim();
    const rate = tr.querySelector('.i_rate').value.trim();
    if (!text && !parseFloat(rate)) continue;                 // untouched row
    if (!text) return toast('Every line needs a description', 'bad');
    const item = ivItem(text);

    // Discount goes to the server as a *rate*, which is what the API models
    // (`discount_rate`, 0..1). A fixed amount is converted here because the
    // conversion needs the line's gross, and doing it server-side would mean
    // adding a second discount representation to the model.
    const d = parseFloat(tr.querySelector('.i_disc').value) || 0;
    const mode = tr.querySelector('.i_dmode').value;
    const gross = (parseFloat(qty) || 0) * (parseFloat(rate) || 0);
    let discountRate = 0;
    if (d > 0) {
      discountRate = mode === 'pct' ? Math.min(d, 100) / 100
                                    : (gross > 0 ? Math.min(d, gross) / gross : 0);
    }

    const line = {
      description: item ? (item.name || text) : text,
      quantity: qty || '1',
      unit_price: rate || '0',
      discount_rate: discountRate.toFixed(6),
      income_account: (item && item.income_account) || (IV.accounts[0] || {}).id,
    };
    if (item) line.item = item.id;
    const tax = tr.querySelector('.i_tax').value;
    if (tax) line.tax_rate = tax;
    if (!line.income_account) {
      return toast('No postable income account in the chart', 'bad');
    }
    lines.push(line);
  }
  if (!lines.length) return toast('An invoice needs at least one line', 'bad');

  const v = id => (document.getElementById(id) || {}).value || '';
  const body = {
    customer: v('i_cust'),
    issue_date: v('i_issue'),
    due_date: v('i_due') || v('i_issue'),
    currency: S.tenant.base_currency,
    order_number: v('i_order').trim(),
    subject: v('i_subject').trim(),
    notes: v('i_notes').trim(),
    terms: v('i_tc').trim(),
    discount_amount: (parseFloat(v('i_hdisc')) || 0).toFixed(2),
    lines,
  };
  const sp = v('i_sales'); if (sp) body.salesperson = sp;

  if (issue && !await confirmDlg(
        'Issue this invoice now? It allocates a permanent number, posts the '
        + 'revenue entry to the ledger and releases any stock on its lines.',
        { title: 'Issue invoice', confirmLabel: 'Save & Issue',
          detail: 'Reversible only by a reversing entry — the ledger keeps '
                  + 'both, which is what makes the trail auditable.' })) return;

  try {
    // PATCH when editing. The serializer replaces the lines wholesale rather
    // than diffing them (see InvoiceSerializer.update) — line identity means
    // nothing on a draft, and a partial diff is how a line the user deleted
    // survives and quietly inflates the total.
    const editing = IV.editing;
    const draft = editing
      ? await api(`/api/v1/invoices/${editing.id}/`, {
          method: 'PATCH', body: JSON.stringify(body),
        })
      : await api('/api/v1/invoices/', {
          method: 'POST', body: JSON.stringify(body),
        });
    if (issue) {
      if (!await ensureReauth()) {
        closeModal(); go('invoices');
        return toast('Saved as draft — issuing needs your password', 'bad');
      }
      await api(`/api/v1/invoices/${draft.id}/issue/`, {
        method: 'POST', idem: 1, reauth: 1, body: '{}',
      });
      toast('Invoice issued and posted', 'ok');
    } else {
      toast(editing ? 'Changes saved' : 'Saved as draft', 'ok');
    }

    // Files chosen before the invoice existed. Uploaded now, sequentially:
    // ten parallel multipart POSTs from a browser is how an upload queue
    // becomes a timeout, and the order they appear in should match the order
    // they were dropped.
    if (ATTACH.queued.length) {
      const pending = [...ATTACH.queued];
      ATTACH.queued = [];
      for (const f of pending) await ivUpload(draft.id, f);
    }

    IV.editing = null;
    closeModal();
    go('invoices');
  } catch (e) {
    if (e.code === 'reauth_required') {
      S.reauth = null;
      toast('That confirmation expired — the draft was saved, try issuing again', 'bad');
      closeModal(); go('invoices');
    } else toast(e.message, 'bad');
  }
}

function modal(t,b,l,h){closeModal();
  onSave=h;const d=document.createElement('div');d.className='ov';d.id='ov';
  d.innerHTML=`<div class="modal"><div class="mh"><h3>${t}</h3>
    <button class="x" onclick="closeModal()">&times;</button></div><div class="mb">${b}</div>
    <div class="mf"><button class="btn" onclick="onSave()">${l}</button>
    <button class="btn sec" onclick="closeModal()">Cancel</button></div></div>`;
  document.body.appendChild(d);}
let onSave=null;
const closeModal=()=>{const o=document.getElementById('ov');if(o)o.remove();};
/* Every table action button goes through here: Issue, Approve, Submit,
   Calculate, Post, Void.

   Two things were missing and both produced the same dead end. The handler
   never called `ensureReauth()` and never set `reauth:1`, so every action
   whose permission is marked `is_sensitive` — which is most of the ones that
   move money — came back 403 `reauth_required`, surfaced as the toast
   "This action requires re-authentication" and then did nothing. There was
   no way for the user to supply that proof from a table row, so the buttons
   were simply inert.

   `reauth` defaults to true: the sensitive set is the larger one here, and
   an unnecessary prompt is a nuisance while a missing one is a broken
   button. Callers pass `{reauth:false}` for the handful that are not. */
async function act(res,id,a,after,confirmMsg,opts){
  const o=opts||{};
  if(confirmMsg&&!await confirmDlg(confirmMsg,{
      title:o.title||'Confirm action',
      confirmLabel:o.confirmLabel||'Confirm',
      danger:!!o.danger}))return;
  if(o.reauth!==false&&!await ensureReauth())return;
  try{
    await api(`/api/v1/${res}/${id}/${a}/`,
      {method:'POST',idem:true,reauth:o.reauth!==false,body:'{}'});
    toast(o.done||'Done','ok');
    if(after)after();
  }catch(e){
    // A stale elevation must not look like a permission problem: drop it and
    // say what to do, rather than repeating the same refusal on every retry.
    if(e.code==='reauth_required'){S.reauth=null;
      toast('That confirmation expired — try again','bad');}
    else toast(e.message,'bad');
  }}

/* ── LIST SCREENS ──────────────────────────────────────────────────────── */
const C=()=>S.tenant.base_currency;

VIEWS.invoices=async()=>{const r=await list('invoices');if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('invoice')">+ New</button>
     <button class="btn sec" onclick="refreshOverdue()">Refresh Overdue</button>`);
  V(tbl(['Date','Invoice#','Customer','Status','Due Date','~Amount','~Balance Due',''],
    r.map(i=>`<tr><td>${dt(i.issue_date)}</td>
      <td class="mono"><a href="#" onclick="openDoc('invoice','${i.id}');return false"
        title="Open document">${esc(i.number||'Draft')}</a></td>
      <td>${esc(i.customer_name||'—')}</td><td>${tag(i.status)}</td><td>${dt(i.due_date)}</td>
      <td class="num">${money(i.total_amount,C())}</td>
      <td class="num">${money(i.amount_due,C())}</td>
            <td class="num">${i.status==='draft'
        ?`<button class="btn sm sec" onclick="newInvoice('${i.id}')">Edit</button>
          <button class="btn sm" onclick="act('invoices','${i.id}','issue',()=>go('invoices'),
          'Issue this invoice? A journal entry will be posted — reversible only by a reversing entry.')">Issue</button>`
        :''}</td></tr>`),
    'No invoices','Create your first invoice to get paid.',
    `<button class="btn" onclick="openForm('invoice')">+ New Invoice</button>`));};
async function refreshOverdue(){try{await api('/api/v1/sales/invoices/refresh-overdue/',{method:'POST'});
  toast('Overdue statuses refreshed','ok');go('invoices');}catch(e){toast(e.message,'bad');}}

/* ── STATUS VIEWS ──────────────────────────────────────────────────────────
   The named filters every purchase list carries. Rendered from one table so
   the vocabulary cannot drift between screens, and resolved *server-side* —
   `?view=open` is not a status, it is approved-but-unsettled, and letting the
   client assemble that from a status list means every screen re-derives the
   definition.

   `my_approvals` in particular has to be server-side: it means "documents I
   am entitled to approve", which depends on permissions and ABAC scope the
   browser cannot evaluate. A client-side version would either show rows the
   user cannot act on, or require sending them rows they should not see. */
const VIEW_SETS = {
  bill: ['all','draft','pending_approval','my_approvals','open','overdue',
         'unpaid','partially_paid','paid','voided'],
  expense: ['all','draft','pending_approval','my_approvals','open','unpaid',
            'paid','rejected'],
  credit: ['all','draft','open','unpaid','partially_paid','paid','voided'],
};
const VIEW_LABEL = {
  all:'All', draft:'Draft', pending_approval:'Pending Approval',
  my_approvals:'My Approvals', open:'Open', overdue:'Overdue',
  unpaid:'Unpaid', partially_paid:'Partially Paid', paid:'Paid',
  voided:'Voided', rejected:'Rejected',
};
//: Per screen, so switching away and back keeps the filter you chose.
S.views = {};

function viewBar(screen, kind) {
  const active = S.views[screen] || 'all';
  return `<div class="tools anim" style="margin-bottom:10px">
    ${(VIEW_SETS[kind] || VIEW_SETS.bill).map(v =>
      `<button class="btn sm ${v === active ? '' : 'sec'}"
        onclick="setView('${screen}','${v}')">${VIEW_LABEL[v] || v}</button>`).join('')}
  </div>`;
}
function setView(screen, v) { S.views[screen] = v; go(screen); }
const viewQuery = screen => {
  const v = S.views[screen];
  return v && v !== 'all' ? `?view=${encodeURIComponent(v)}` : '';
};

/* A list screen with the status bar above it. */
const filtered=(res,kind,screen,viewKind,head,row,empty,sub)=>async()=>{
  const r=await list(res, viewQuery(screen));
  if(r===null)return V(denied());
  if(kind)A(`<button class="btn" onclick="openForm('${kind}')">+ New</button>`);
  V(viewBar(screen, viewKind)+tbl(head,r.map(row),empty,sub));};

const simple=(res,kind,head,row,empty,sub)=>async()=>{
  const r=await list(res);if(r===null)return V(denied());
  if(kind)A(`<button class="btn" onclick="openForm('${kind}')">+ New</button>`);
  V(tbl(head,r.map(row),empty,sub));};

VIEWS.customers=simple('customers','customer',
  ['Name','Code','Email','Phone','~Credit Limit','Status'],
  x=>`<tr><td class="uc"><span class="avat">${initials(lbl(x))}</span>${esc(lbl(x))}</td>
    <td class="mono">${esc(x.code||'—')}</td><td>${esc(x.email||'—')}</td>
    <td>${esc(x.phone||'—')}</td><td class="num">${money(x.credit_limit,C())}</td>
    <td>${tag(x.is_active?'active':'cancelled')}</td></tr>`,'No customers');
VIEWS.vendors=simple('vendors','vendor',['Name','Code','Email','Phone','Status'],
  x=>`<tr><td class="uc"><span class="avat">${initials(lbl(x))}</span>${esc(lbl(x))}</td>
    <td class="mono">${esc(x.code||'—')}</td><td>${esc(x.email||'—')}</td>
    <td>${esc(x.phone||'—')}</td><td>${tag(x.is_active?'active':'cancelled')}</td></tr>`,'No vendors');
VIEWS.payments=simple('payments','payment',
  ['Date','Payment#','Customer','Mode','~Amount','~Unused','Status'],
  p=>`<tr><td>${dt(p.payment_date)}</td><td class="mono">${esc(p.number||'—')}</td>
    <td>${esc(p.customer_name||'—')}</td><td>${esc((p.method||'—').replace(/_/g,' '))}</td>
    <td class="num">${money(p.amount,C())}</td><td class="num">${money(p.unapplied_amount,C())}</td>
    <td>${tag(p.status)}</td></tr>`,'No payments received');
VIEWS.creditnotes=simple('credit-notes',null,['Date','Credit Note#','Customer','Status','~Amount'],
  x=>`<tr><td>${dt(x.issue_date)}</td><td class="mono">${esc(x.number||'—')}</td>
    <td>${esc(x.customer_name||'—')}</td><td>${tag(x.status)}</td>
    <td class="num">${money(x.total_amount,C())}</td></tr>`,'No credit notes',
    'Handle returns without modifying the original invoice.');
VIEWS.items=simple('items','item',['SKU','Name','Type','~Rate','~Cost','~Reorder At'],
  i=>`<tr><td class="mono">${esc(i.sku)}</td><td>${esc(i.name)}</td><td>${esc(i.type)}</td>
    <td class="num">${money(i.sales_price,C())}</td><td class="num">${money(i.purchase_price,C())}</td>
    <td class="num">${i.reorder_point??'—'}</td></tr>`,'No items');
VIEWS.stock=simple('stock-levels',null,['Item','Warehouse','~On Hand','~Reserved','~Available','~Avg Cost'],
  s=>`<tr><td>${esc(s.item_name||'—')}</td><td>${esc(s.warehouse_name||'—')}</td>
    <td class="num">${s.quantity_on_hand??'—'}</td><td class="num">${s.quantity_reserved??'—'}</td>
    <td class="num">${s.quantity_available??'—'}</td>
    <td class="num">${money(s.average_cost,C())}</td></tr>`,'No stock records');
/* Bills. The serial opens the document; the action column exposes the two
   transitions that actually move the ledger, because a bill sitting in
   AWAITING_APPROVAL is an obligation the books do not yet know about. */
VIEWS.bills=filtered('bills',null,'bills','bill',
  ['Date','Bill#','Vendor','Status','~Amount','~Balance',''],
  b=>`<tr><td>${dt(b.bill_date||b.issue_date)}</td>
    <td class="mono"><a href="#" onclick="openDoc('bill','${b.id}');return false"
      title="Open document">${esc(b.number||'—')}</a></td>
    <td>${esc(b.vendor_name||'—')}</td><td>${tag(b.status)}</td>
    <td class="num">${money(b.total_amount,C())}</td>
    <td class="num">${money(b.amount_due,C())}</td>
    <td class="num">${b.status==='awaiting_approval'
      ?`<button class="btn sm" onclick="act('bills','${b.id}','approve',()=>go('bills'),
        'Approve this bill? It posts to accounts payable immediately.')">Approve</button>`
      :(['approved','partially_paid','overdue'].includes(b.status)
        ?`<button class="btn sm sec" onclick="payBill('${b.id}','${b.amount_due}')">Pay</button>`
        :'')}</td></tr>`,'No bills');

/* Record a payment against a posted bill. The amount and the account are the
   only things the caller supplies — the service recomputes amount_paid and
   the status under a row lock, so two people paying at once cannot both see
   a zero balance and overpay the vendor. */
async function payBill(id,due){
  const accts=(await list('accounts')||[]).filter(a=>
    a.is_reconcilable||/cash|bank/i.test(a.system_key||''));
  if(!accts.length)return toast('No bank or cash account to pay from','bad');
  modal('Record Bill Payment',`<div class="row">
      <div><label class="req">Amount</label>
        <input id="bp_amt" class="num" value="${esc(due||'0.00')}"></div>
      <div><label class="req">Paid From</label><select id="bp_acc">${accts.map(a=>
        `<option value="${a.id}">${esc(a.code)} — ${esc(a.name)}</option>`).join('')}</select></div>
      <div><label>Payment Date</label><input id="bp_date" type="date"
        value="${new Date().toISOString().slice(0,10)}"></div>
      <div><label>Reference</label><input id="bp_ref" placeholder="Cheque / transfer no."></div>
    </div>
    <div class="note">Amount is sent as text; the server validates it in decimal
      and refuses anything above the outstanding balance.</div>`,'Record Payment',
    async()=>{
      try{
        await api(`/api/v1/bills/${id}/pay/`,{method:'POST',idem:1,body:JSON.stringify({
          amount:document.getElementById('bp_amt').value.trim(),
          paid_from_account:document.getElementById('bp_acc').value,
          payment_date:document.getElementById('bp_date').value,
          reference:document.getElementById('bp_ref').value.trim()})});
        closeModal();toast('Payment recorded','ok');go('bills');
      }catch(e){toast(e.message,'bad');}});
}
VIEWS.vendorcredits=filtered('vendor-credits','vendorcredit','vendorcredits','credit',
  ['Date','Credit#','Vendor','Reason','Status','~Total','~Remaining'],
  c=>`<tr><td>${dt(c.credit_date)}</td>
    <td class="mono">${esc(c.number||'Draft')}</td>
    <td>${esc(c.vendor_name||'—')}</td><td>${esc(c.reason||'—')}</td>
    <td>${tag(c.status)}</td>
    <td class="num">${money(c.total_amount,c.currency)}</td>
    <td class="num">${money(c.amount_remaining,c.currency)}</td></tr>`,
  'No vendor credits',
  'A supplier overcharge or return is corrected by a credit, never by editing a posted bill.');

VIEWS.recurringbills=simple('recurring-bills','recurringbill',
  ['Schedule','Vendor','Every','Next Run','Generated','Status'],
  r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.vendor_name||'—')}</td>
    <td>${r.interval>1?r.interval+' × ':''}${esc(r.frequency)}</td>
    <td>${dt(r.next_run_date)}</td>
    <td class="num">${r.occurrences_generated??0}${
      r.max_occurrences?' / '+r.max_occurrences:''}</td>
    <td>${r.is_exhausted?tag('closed'):(r.is_active?tag('open'):tag('t-mut'))}
      ${r.last_error?'<span class="tag t-dang" style="margin-left:6px">Error</span>':''}</td></tr>`,
  'No recurring bills','Rent, support contracts, leases — anything billed on a schedule.');

VIEWS.recurringexpenses=simple('recurring-expenses','recurringexpense',
  ['Schedule','Vendor','Category','Every','Next Run','~Amount','Status'],
  r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.vendor_name||'—')}</td>
    <td>${esc(r.category_name||'—')}</td>
    <td>${r.interval>1?r.interval+' × ':''}${esc(r.frequency)}</td>
    <td>${dt(r.next_run_date)}</td>
    <td class="num">${money(r.amount,r.currency)}</td>
    <td>${r.is_exhausted?tag('closed'):(r.is_active?tag('open'):tag('t-mut'))}</td></tr>`,
  'No recurring expenses','Subscriptions on the company card — no bill, no approval to pay.');

/* Payments Made. Rows are written by `pay_bill`, which until now posted the
   journal entry without recording the payment — so this screen was structurally
   empty however many bills you paid. */
VIEWS.billpayments=simple('bill-payments',null,
  ['Date','Payment#','Vendor','Bill','Mode','Status','~Amount'],
  x=>`<tr><td>${dt(x.payment_date)}</td><td class="mono">${esc(x.number||'—')}</td>
    <td>${esc(x.vendor_name||'—')}</td>
    <td class="mono">${esc(x.bill_number||'—')}</td>
    <td>${esc((x.payment_method||x.method||'—').replace(/_/g,' '))}</td>
    <td>${tag(x.status)}</td>
    <td class="num">${money(x.amount,x.currency||C())}</td></tr>`,
  'No payments made','Recorded automatically when you pay a bill.');
/* ── BANKING HUB ───────────────────────────────────────────────────────────
   Two figures per account and they are deliberately different: what the bank
   says and what the books say. A single "balance" hides the only number
   anyone opens this screen to find — the gap between them, which is the
   uncleared items plus whatever has not been reconciled yet.               */
VIEWS.banking = async () => {
  const accts = await list('bank-accounts');
  if (accts === null) return V(denied());
  const c = C();

  const cash = accts.filter(a => /cash|petty/i.test(a.name || '') || a.account_type === 'cash');
  const banks = accts.filter(a => !cash.includes(a));
  const sum = rows => rows.reduce((s, a) =>
    s + (parseFloat(a.current_balance ?? a.book_balance ?? 0) || 0), 0);

  A(`<button class="btn sec" onclick="go('banktx')">All Transactions</button>`);

  V(`<div class="kpis">
    <div class="kpi anim"><div class="lbl">Cash in Hand</div>
      <b data-val="${sum(cash)}" data-cur="${c}">—</b>
      <i>${cash.length} account${cash.length === 1 ? '' : 's'}</i></div>
    <div class="kpi anim d1"><div class="lbl">Bank Balance</div>
      <b class="${sum(banks) < 0 ? 'neg' : 'pos'}" data-val="${sum(banks)}" data-cur="${c}">—</b>
      <i>${banks.length} account${banks.length === 1 ? '' : 's'}</i></div>
    <div class="kpi anim d2"><div class="lbl">Accounts</div>
      <b>${accts.length}</b><i>active</i></div>
  </div>

  <div class="panel anim d2"><div class="ph"><h3>Active Accounts</h3></div>
  ${accts.length ? `<table><thead><tr>
      <th>Account</th><th>Bank</th><th class="mono">Number</th><th>Currency</th>
      <th class="num">In Bank</th><th class="num">In Books</th><th class="num">Difference</th><th></th>
    </tr></thead><tbody>${accts.map(a => {
      const bank = parseFloat(a.statement_balance ?? 0) || 0;
      const book = parseFloat(a.current_balance ?? a.book_balance ?? 0) || 0;
      const diff = bank - book;
      return `<tr>
        <td><a href="#" data-acc="${a.id}"
          onclick="bankFeed(this.dataset.acc,this.textContent.trim());return false">
          ${esc(a.name || '—')}</a></td>
        <td>${esc(a.bank_name || '—')}</td>
        <td class="mono">${esc(a.account_number || a.masked_account_number || '—')}</td>
        <td>${esc(a.currency || c)}</td>
        <td class="num">${money(bank, a.currency || c)}</td>
        <td class="num">${money(book, a.currency || c)}</td>
        <td class="num ${Math.abs(diff) > 0.005 ? 'neg' : 'pos'}">${money(diff, a.currency || c)}</td>
        <td class="num"><button class="btn sm sec" data-acc="${a.id}"
          data-nm="${esc(a.name || '')}"
          onclick="bankFeed(this.dataset.acc,this.dataset.nm)">Transactions</button></td>
      </tr>`;}).join('')}</tbody></table>`
    : `<div class="empty"><h4>No bank accounts yet</h4>
       <p>Code your first account in Bank Setup to start reconciling.</p>
       <button class="btn" onclick="go('banksetup')">Go to Bank Setup</button></div>`}
  </div>

  <div class="note">"In Bank" is the last imported statement balance; "In Books"
    is the ledger. A non-zero difference is uncleared items or unreconciled
    transactions — it is the number this screen exists to show, so it is a
    column rather than something you compute yourself.</div>`);
};

/* One account's transaction feed with a running balance. */
async function bankFeed(accountId, label) {
  modal(`Transactions — ${label}`, '<div class="sk"></div><div class="sk"></div>',
        'Close', closeModal);
  try {
    const rows = await list('bank-transactions', `?bank_account=${accountId}`);
    const body = document.querySelector('#ov .mb');
    if (!body) return;
    if (!rows || !rows.length) {
      body.innerHTML = `<div class="empty"><h4>No transactions</h4>
        <p>Import a statement or record a payment against this account.</p></div>`;
      return;
    }
    const c = C();
    // Oldest first so the running balance accumulates in the direction a
    // reader expects; the API returns newest first.
    const asc = [...rows].reverse();
    let running = 0;
    const withBalance = asc.map(t => {
      const dep = parseFloat(t.deposit_amount ?? (parseFloat(t.amount) > 0 ? t.amount : 0)) || 0;
      const wd = parseFloat(t.withdrawal_amount ?? (parseFloat(t.amount) < 0 ? -t.amount : 0)) || 0;
      running += dep - wd;
      return { t, dep, wd, running };
    }).reverse();

    body.innerHTML = `<table><thead><tr>
      <th>Date</th><th>Reference</th><th>Type</th><th>Status</th>
      <th class="num">Deposits</th><th class="num">Withdrawals</th><th class="num">Balance</th>
    </tr></thead><tbody>
    ${withBalance.map(({ t, dep, wd, running }) => `<tr>
      <td>${dt(t.transaction_date || t.date)}</td>
      <td class="mono">${esc(t.reference || t.reference_number || '—')}</td>
      <td>${esc((t.transaction_type || t.type || '').replace(/_/g, ' '))}</td>
      <td>${tag(t.status || 'open')}</td>
      <td class="num">${dep ? money(dep, c) : ''}</td>
      <td class="num">${wd ? money(wd, c) : ''}</td>
      <td class="num">${money(running, c)}</td>
    </tr>`).join('')}
    </tbody></table>`;
  } catch (e) {
    const body = document.querySelector('#ov .mb');
    if (body) body.innerHTML = `<div class="empty"><h4 style="color:var(--dang)">
      Could not load transactions</h4><p>${esc(e.message)}</p></div>`;
  }
}

/* Bank Setup — the master coding screen for banking.
   Separate from the Overview: the overview answers "where does the money
   stand", this answers "which accounts exist and what do they post to". They
   are different jobs and mixing them makes the setup fields compete with the
   balances for the same row. */
VIEWS.banksetup = simple('bank-accounts', 'bankaccount',
  ['Label', 'Bank', 'A/C', 'IBAN', 'SWIFT', 'Currency', 'Ledger Account', '~Opening', 'Status'],
  a => `<tr><td>${esc(a.name || '—')}</td><td>${esc(a.bank_name || '—')}</td>
    <td class="mono">${a.account_number_last4 ? '••••' + esc(a.account_number_last4) : '—'}</td>
    <td class="mono">${esc(a.iban || '—')}</td>
    <td class="mono">${esc(a.swift || '—')}</td>
    <td>${esc(a.currency || '')}</td>
    <td class="mono">${esc(a.ledger_account_code || '—')}</td>
    <td class="num">${money(a.opening_balance, a.currency || C())}</td>
    <td>${a.is_active ? tag('open') : tag('closed')}</td></tr>`,
  'No bank accounts coded yet',
  'Code each bank and cash account here, then import statements against them.');

/* Every bank transaction across all accounts — the reconciliation worklist. */
VIEWS.banktx = simple('bank-transactions', null,
  ['Date', 'Account', 'Reference', 'Type', 'Status', '~Amount'],
  t => `<tr><td>${dt(t.transaction_date || t.date)}</td>
    <td>${esc(t.bank_account_name || '—')}</td>
    <td class="mono">${esc(t.reference || t.reference_number || '—')}</td>
    <td>${esc((t.transaction_type || t.type || '').replace(/_/g, ' '))}</td>
    <td>${tag(t.status || 'open')}</td>
    <td class="num">${money(t.amount, C())}</td></tr>`,
  'No bank transactions',
  'Import a statement, or record payments — vendor payments post here automatically.');

VIEWS.projects=simple('projects',null,['Code','Project','Customer','Billing','~Budget','Status'],
  x=>`<tr><td class="mono">${esc(x.code||'—')}</td><td>${esc(x.name)}</td>
    <td>${esc(x.customer_name||'—')}</td><td>${esc((x.billing_type||'—').replace(/_/g,' '))}</td>
    <td class="num">${money(x.budget_amount,C())}</td><td>${tag(x.status)}</td></tr>`,'No projects');
VIEWS.timesheets=simple('timesheets',null,['Date','Employee','Project','~Hours','Billable','Status'],
  t=>`<tr><td>${dt(t.work_date)}</td><td>${esc(t.employee_name||'—')}</td>
    <td>${esc(t.project_name||'—')}</td><td class="num">${t.hours??'—'}</td>
    <td>${t.is_billable?'Yes':'No'}</td><td>${tag(t.status)}</td></tr>`,'No time entries');
/* ── CHART OF ACCOUNTS (G4) ────────────────────────────────────────────────
   The chart is a positional five-level tree: the code *is* the hierarchy, and
   a flat table throws that away. This screen renders the tree the server
   returns from /accounts/tree/ (one unpaginated request — a tree cut at an
   arbitrary row is not a tree), with expand/collapse, a search that prunes to
   matching branches, a per-level count bar from /accounts/stats/, and a
   top-down add flow: pick a summary node, name a child and choose its side,
   and the server allocates the next free code. The client never invents a
   number, so two accountants adding at once cannot collide. */
let CHART = { roots: [], flat: [], q: '', sel: null, open: new Set() };

const errPanel = m => `<div class="panel anim"><div class="empty">
  <h4 style="color:var(--dang)">Could not load</h4><p>${esc(m)}</p></div></div>`;

/* All accounts as a flat list, from the tree endpoint. Used wherever a full
   set is needed (the account pickers below): the plain /accounts/ list is
   paginated at 100, so a large chart would silently drop leaves from a picker;
   the tree is unpaginated by contract. */
async function flattenAccounts() {
  const d = await api('/api/v1/accounts/tree/');
  const out = [];
  (function walk(ns) { (ns || []).forEach(n => { out.push(n); walk(n.children); }); })(
    (d && d.results) || []);
  return out;
}

const sideOf = t => ({ asset: 'debit', expense: 'debit' }[t] || 'credit');
const sectionLabel = t => ({ asset: 'Assets', liability: 'Liabilities', equity: 'Equity',
  income: 'Income', expense: 'Expenses' }[t] || t);

VIEWS.accounts = async () => {
  A(`<button class="btn" onclick="addAccountPrompt()">+ Add account</button>`);
  await loadChart(false);
};

async function loadChart(keep) {
  let data;
  try { data = await api('/api/v1/accounts/tree/'); }
  catch (e) { return V(e.status === 403 ? denied() : errPanel(e.message)); }
  const roots = (data && data.results) || [];
  CHART.roots = roots;
  CHART.flat = [];
  (function walk(ns) { ns.forEach(n => { CHART.flat.push(n); if (n.children) walk(n.children); }); })(roots);
  if (!keep) { CHART.q = ''; CHART.sel = null; CHART.open = new Set(roots.map(r => r.id)); }
  else { CHART.open = new Set([...CHART.open].filter(id => CHART.flat.some(a => a.id === id))); }
  let stats = null;
  try { stats = await api('/api/v1/accounts/stats/'); } catch { /* the count bar is optional */ }
  paintChart(stats);
}

function paintChart(stats) {
  if (!CHART.roots.length) {
    return V(`<div class="panel anim"><div class="empty">
      <h4>Chart of accounts is empty</h4>
      <p>No accounts have been seeded for this organisation yet.</p></div></div>`);
  }
  const lv = (stats && stats.levels) || {};
  const lvbar = `<div class="lvbar">` +
    [1, 2, 3, 4, 5].map(n => `<span class="lvchip">L${n} <b>${lv[n] || 0}</b></span>`).join('') +
    `<span class="lvchip">Total <b>${CHART.flat.length}</b></span></div>`;
  const tools = `<div class="tools">
    <input id="chartQ" placeholder="Search code or name…" value="${esc(CHART.q)}"
      oninput="chartSearch(this.value)" style="min-width:240px">
    <span class="note" id="chartSel" style="margin:0">${selHint()}</span></div>`;
  V(lvbar + tools + `<div class="panel anim"><div id="chartTree">${renderTree()}</div></div>`);
}

function selHint() {
  if (CHART.sel) {
    const a = CHART.flat.find(x => x.id === CHART.sel);
    if (a) return `Selected <b class="mono">${esc(a.code)}</b> — ${esc(a.name)}` +
      (a.is_postable ? ' (a postable leaf — pick a summary to add beneath)'
                     : ' · “Add account” adds beneath it');
  }
  return 'Click an account to select it; use the ⋯ menu for actions';
}

/* The tree as flat, indented rows. Rendered from a string rather than nested
   DOM so search (which prunes whole branches) and toggle are a single rebuild
   of #chartTree, never a walk of live nodes. Returns an empty-state when a
   search matches nothing. */
function renderTree() {
  const q = CHART.q.trim().toLowerCase();
  const hit = a => !q || String(a.code).toLowerCase().includes(q) ||
    String(a.name).toLowerCase().includes(q);
  function node(a, depth) {
    const kids = a.children || [];
    const kidHtml = kids.map(k => node(k, depth + 1)).filter(Boolean).join('');
    if (!hit(a) && !kidHtml) return '';
    const hasKids = kids.length > 0;
    const open = q ? true : CHART.open.has(a.id);
    const nb = a.normal_balance === 'debit' ? 'debit' : 'credit';
    const tags =
      (a.is_postable ? `<span class="tag ${nb === 'debit' ? 't-info' : 't-mut'}">${nb === 'debit' ? 'Debit' : 'Credit'}</span> ` : '') +
      (a.requires_party ? `<span class="tag t-warn">Party</span> ` : '') +
      (a.is_active === false ? `<span class="tag t-mut">Archived</span> ` : '') +
      (a.system_key ? `<span class="tag t-info" title="Wired into automated postings">System</span> ` : '');
    const row = `<div class="tnode${CHART.sel === a.id ? ' sel' : ''}" data-id="${a.id}"
        style="padding-left:${12 + depth * 20}px" onclick="chartSelect('${a.id}')">
      <button class="tchev ${hasKids ? (open ? 'open' : '') : 'leaf'}"
        onclick="chartToggle(event,'${a.id}')">${hasKids ? '▶' : ''}</button>
      <span class="tbadge">L${a.level}</span>
      <span class="tcode mono">${esc(a.code)}</span>
      <span class="tname ${a.is_postable ? '' : 'sum'}">${esc(a.name)}</span>
      <span>${tags}</span>
      <span class="tbal">${a.is_postable ? money(a.cached_balance, C()) : ''}</span>
      <span class="tact"><button onclick="chartMenu(event,'${a.id}')" title="Actions">⋯</button></span>
    </div>`;
    return row + (hasKids ? `<div class="tkids"${open ? '' : ' style="display:none"'}>${kidHtml}</div>` : '');
  }
  const body = CHART.roots.map(r => node(r, 0)).filter(Boolean).join('');
  if (!body) return `<div class="empty"><h4>No accounts match “${esc(CHART.q)}”</h4>
    <p>Clear the search to see the whole chart.</p></div>`;
  return `<div class="tree">${body}</div>`;
}

function refreshTree() {
  const el = document.getElementById('chartTree');
  if (el) el.innerHTML = renderTree();
}
function chartSearch(v) { CHART.q = v; refreshTree(); }
function chartToggle(ev, id) {
  ev.stopPropagation();
  if (CHART.open.has(id)) CHART.open.delete(id); else CHART.open.add(id);
  refreshTree();
}
function chartSelect(id) {
  CHART.sel = id;
  refreshTree();
  const h = document.getElementById('chartSel');
  if (h) h.innerHTML = selHint();
}

/* One popover at a time, appended to <body> and positioned under the trigger.
   On the body rather than the row so the tree's overflow cannot clip it, and
   so it survives the #chartTree rebuilds that select/toggle perform. */
function closeRMenu() { const m = document.getElementById('rmenu'); if (m) m.remove(); }
function chartMenu(ev, id) {
  ev.stopPropagation();
  closeRMenu();
  const a = CHART.flat.find(x => x.id === id);
  if (!a) return;
  const m = document.createElement('div');
  m.className = 'rmenu'; m.id = 'rmenu';
  const add = (label, fn, cls) => {
    const b = document.createElement('button');
    if (cls) b.className = cls;
    b.textContent = label;
    b.onclick = () => { closeRMenu(); fn(); };
    m.appendChild(b);
  };
  if (!a.is_postable) add('Add child account', () => addAccount(a.id));
  add('Edit name', () => renameAccount(a.id));
  if (a.is_postable) add('View ledger', () => drillAccount(a.id, a.code + ' — ' + a.name));
  if (!a.system_key && a.is_active !== false) add('Archive', () => archiveAccount(a.id), 'dang');
  document.body.appendChild(m);
  const r = ev.currentTarget.getBoundingClientRect();
  m.style.top = (r.bottom + 4) + 'px';
  let left = r.right - 170;
  if (left < 8) left = 8;
  m.style.left = left + 'px';
}
document.addEventListener('click', e => {
  if (!document.getElementById('rmenu')) return;
  if (e.target.closest && (e.target.closest('.rmenu') || e.target.closest('.tact'))) return;
  closeRMenu();
});

function addAccountPrompt() {
  const a = CHART.sel && CHART.flat.find(x => x.id === CHART.sel);
  if (!a) return toast('Select a summary account first, then add beneath it', 'bad');
  if (a.is_postable) return toast(`${a.code} is a postable leaf — pick a summary (non-postable) account`, 'bad');
  addAccount(a.id);
}

/* Add a child under a summary node. The client sends only the parent, a name
   and (optionally) the side; the server allocates the code from the account's
   place in the tree and answers with it. */
function addAccount(parentId) {
  const parent = CHART.flat.find(a => a.id === parentId);
  if (!parent) return;
  if (parent.is_postable) return toast('A postable leaf cannot have children', 'bad');
  const childLevel = parent.level + 1;
  const inherited = sideOf(parent.type);
  modal(`Add account under ${esc(parent.code)} — ${esc(parent.name)}`, `
    <div class="row">
      <div style="grid-column:1/3"><label class="req">Account name</label>
        <input id="ac_name" placeholder="e.g. Petty cash — head office"></div>
      <div><label class="req">Normal balance</label>
        <select id="ac_nb">
          <option value="">Inherit — ${sectionLabel(parent.type)} (${inherited})</option>
          <option value="debit">Debit</option>
          <option value="credit">Credit</option>
        </select></div>
      <div><label>Options</label>
        <label style="display:block;font-weight:400;font-size:12.5px;margin-top:4px">
          <input type="checkbox" id="ac_party" style="width:auto;margin-right:6px">Requires a party</label>
        <label style="display:block;font-weight:400;font-size:12.5px;margin-top:6px">
          <input type="checkbox" id="ac_rec" style="width:auto;margin-right:6px">Reconcilable (bank/cash)</label>
      </div>
    </div>
    <div class="note">The code is allocated by the server — the next free child of
      ${esc(parent.code)}, at level ${childLevel}. ${childLevel === 5
        ? 'Level 5 is a postable leaf: postings land here.'
        : 'This will be a summary account; add postable leaves beneath it.'}</div>`,
    'Add account', async () => {
      const name = document.getElementById('ac_name').value.trim();
      if (!name) return toast('Account name is required', 'bad');
      const body = { parent: parent.id, name };
      const nb = document.getElementById('ac_nb').value;
      if (nb) body.normal_balance_override = nb;
      if (document.getElementById('ac_party').checked) body.requires_party = true;
      if (document.getElementById('ac_rec').checked) body.is_reconcilable = true;
      try {
        const r = await api('/api/v1/accounts/', { method: 'POST', body: JSON.stringify(body) });
        closeModal();
        toast(`Created ${r.code} — ${r.name}`, 'ok');
        CHART.open.add(parent.id);
        CHART.sel = r.id;
        await loadChart(true);
      } catch (e) { toast(e.message, 'bad'); }
    });
}

/* Rename only. Re-parenting is refused server-side (the code encodes the
   position), so the code and place in the tree are shown as fixed. */
function renameAccount(id) {
  const a = CHART.flat.find(x => x.id === id);
  if (!a) return;
  modal(`Edit ${esc(a.code)}`, `
    <div><label class="req">Account name</label><input id="rn_name" value="${esc(a.name)}"></div>
    <div style="margin-top:12px"><label>Description</label>
      <input id="rn_desc" value="${esc(a.description || '')}"></div>
    <div class="note">The code (${esc(a.code)}) and the account's place in the tree
      do not change — only its name.</div>`,
    'Save', async () => {
      const name = document.getElementById('rn_name').value.trim();
      if (!name) return toast('Account name is required', 'bad');
      try {
        await api('/api/v1/accounts/' + id + '/', {
          method: 'PATCH',
          body: JSON.stringify({ name, description: document.getElementById('rn_desc').value.trim() }),
        });
        closeModal();
        toast('Saved', 'ok');
        await loadChart(true);
      } catch (e) { toast(e.message, 'bad'); }
    });
}

/* Archive goes through act(): it confirms, supplies the re-auth proof the
   sensitive action needs, and is idempotent. The server refuses to archive a
   system account, one that still carries a balance, or one with active
   children — the toast carries that message straight through. */
function archiveAccount(id) {
  const a = CHART.flat.find(x => x.id === id);
  if (!a) return;
  act('accounts', id, 'archive', () => loadChart(true),
    `Archive ${a.code} — ${a.name}? Nothing further can be posted to it. This is `
    + `refused if it still carries a balance or has active children.`,
    { title: 'Archive account', confirmLabel: 'Archive', danger: true });
}
VIEWS.taxrates=simple('tax-rates','taxrate',['Code','Tax Name','~Rate','From','Status'],
  t=>`<tr><td class="mono">${esc(t.code)}</td><td>${esc(t.name)}</td>
    <td class="num">${(parseFloat(t.rate)*100).toFixed(2)}%</td>
    <td>${dt(t.effective_from)}</td><td>${t.is_active?tag('open'):tag('closed')}</td></tr>`,
  'No tax rates yet','Rates are fractions in the database and shown as percentages here.');

/* Manual journals. Superseded drafts are hidden by default.

   Posting a draft does not mutate it: `JournalEntryViewSet.post` rebuilds an
   inert draft, hands it to `post_entry` (the only thing allowed to write the
   ledger) and then voids the stored row with a pointer to the entry that
   replaced it. That is the right call for the audit trail, but it means every
   entry posted through this screen leaves a numberless voided twin, and a
   list showing both reads as though each entry were filed twice and cancelled.

   The filter is narrow on purpose: only VOIDED rows that never received a
   number. Those are drafts that never reached the ledger — either superseded
   by a posting or abandoned — so hiding them conceals nothing that moved
   money. A voided entry *with* a number is a real cancellation of something
   that was really filed, and hiding that would be hiding the books. */
VIEWS.journal=async()=>{
  const r=await list('journal-entries');if(r===null)return V(denied());
  const superseded=r.filter(j=>j.status==='voided'&&!j.number).length;
  const rows=r.filter(j=>!(j.status==='voided'&&!j.number));
  A(`<button class="btn" onclick="openForm('journal')">+ New Entry</button>`);
  V(tbl(['Date','Journal#','Notes','Status','~Debit','~Credit'],
    rows.map(j=>`<tr><td>${dt(j.entry_date)}</td><td class="mono">${esc(j.number||'—')}</td>
      <td>${esc(j.memo||'—')}</td><td>${tag(j.status)}</td>
      <td class="num">${money(j.total_debit,j.currency)}</td>
      <td class="num">${money(j.total_credit,j.currency)}</td></tr>`),
    'No journal entries')
    +(superseded?`<div class="note">${superseded} unposted draft${superseded>1?'s':''}
      hidden — superseded or abandoned, never numbered, no ledger effect. Kept
      in the database for the audit trail.</div>`:''));};
VIEWS.employees=simple('employees','employee',['Code','Name','Department','Designation','Hired','Status'],
  e=>`<tr><td class="mono">${esc(e.employee_code)}</td>
    <td class="uc"><span class="avat">${initials(e.full_name||e.first_name)}</span>
      ${esc(e.full_name||((e.first_name||'')+' '+(e.last_name||'')))}</td>
    <td>${esc(e.department_name||'—')}</td><td>${esc(e.job_title_name||'—')}</td>
    <td>${dt(e.hire_date)}</td><td>${tag(e.status)}</td></tr>`,'No employees');
VIEWS.departments=simple('departments','department',['Code','Department','Path'],
  d=>`<tr><td class="mono">${esc(d.code)}</td><td>${esc(d.name)}</td>
    <td class="mono" style="color:var(--mut)">${esc(d.path||'—')}</td></tr>`,'No departments');
/* Overtime claims. The amount column stays blank until approval — the figure
   is computed then, against the salary in force on the day worked, and showing
   a provisional number the server has not agreed to invites disputes. */
VIEWS.overtime=async()=>{const r=await list('overtime-slips');if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('overtimeslip')">+ New Claim</button>`);
  V(tbl(['Date','Employee','Type','~Hours','~Rate','~Amount','Status',''],
    r.map(o=>`<tr><td>${dt(o.work_date)}</td><td>${esc(o.employee_name||'—')}</td>
      <td class="mono">${esc(o.overtime_type_code||'—')}</td>
      <td class="num">${qty(o.hours)}</td>
      <td class="num">${parseFloat(o.hourly_rate)?money(o.hourly_rate,o.currency):'—'}</td>
      <td class="num">${parseFloat(o.amount)?money(o.amount,o.currency):'—'}</td>
      <td>${tag(o.status)}</td>
      <td class="num">${o.status==='draft'
        ?`<button class="btn sm sec" onclick="act('overtime-slips','${o.id}','submit',()=>go('overtime'))">Submit</button>`
        :o.status==='submitted'
        ?`<button class="btn sm" onclick="act('overtime-slips','${o.id}','approve',()=>go('overtime'),
           'Approve these hours? The amount is priced now and cannot be re-priced later.')">Approve</button>`
        :''}</td></tr>`),
    'No overtime claims','Claims are priced at approval and paid by the next payroll run.',
    `<button class="btn" onclick="openForm('overtimeslip')">+ New Claim</button>`));};

/* Leave types. Master data the Leave Request form depends on — an empty list
   here is why that form could not be submitted at all. */
VIEWS.leavetypes=simple('leave-types','leavetype',
  ['Code','Name','Paid','Accrual','~Rate','~Max','Payroll','Status'],
  t=>`<tr><td class="mono">${esc(t.code)}</td><td>${esc(t.name)}</td>
    <td>${t.is_paid?tag('open'):tag('t-mut')}</td>
    <td>${esc(t.accrual_method||'—')}</td>
    <td class="num">${qty(t.accrual_rate_days)}</td>
    <td class="num">${qty(t.max_balance_days)}</td>
    <td>${t.affects_payroll?'<span class="tag t-warn">Prorates</span>':'—'}</td>
    <td>${t.is_active?tag('open'):tag('closed')}</td></tr>`,
  'No leave types',
  'Code Annual, Sick and Unpaid before anyone can request leave.');

VIEWS.shifts=simple('shifts','shift',
  ['Code','Name','Start','End','~Break','~Hours/day','~OT after','Status'],
  x=>`<tr><td class="mono">${esc(x.code)}</td><td>${esc(x.name)}</td>
    <td>${esc(x.start_time||'—')}</td><td>${esc(x.end_time||'—')}
      ${x.crosses_midnight?'<span class="note">+1d</span>':''}</td>
    <td class="num">${x.break_minutes??0}m</td>
    <td class="num">${qty(x.expected_hours_per_day)}</td>
    <td class="num">${qty(x.overtime_after_hours)}</td>
    <td>${x.is_active?tag('open'):tag('closed')}</td></tr>`,
  'No shifts',
  'Define shifts before assigning them — overtime is priced against the pattern.');

VIEWS.ottypes=simple('overtime-types','overtimetype',
  ['Code','Name','~Multiplier','Component','Status'],
  t=>`<tr><td class="mono">${esc(t.code)}</td><td>${esc(t.name)}</td>
    <td class="num">${parseFloat(t.multiplier).toFixed(2)}×</td>
    <td class="mono">${esc(t.component_code||'—')}</td>
    <td>${t.is_active?tag('open'):tag('closed')}</td></tr>`,
  'No overtime types','Code the rates first — weekday, weekend, public holiday.');

VIEWS.shiftassign=simple('shift-assignments','shiftassign',
  ['Employee','Shift','From','To','Location'],
  a=>`<tr><td>${esc(a.employee_name||'—')}</td>
    <td class="mono">${esc(a.shift_code||'—')}</td>
    <td>${dt(a.start_date)}</td><td>${a.end_date?dt(a.end_date):'open'}</td>
    <td>${esc(a.location||'—')}</td></tr>`,
  'No shift assignments','Assign shifts so overtime is priced against the right pattern.');

VIEWS.structures=async()=>{const r=await list('salary-structures');
  if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('salstructure')">+ New Structure</button>`);
  V(tbl(['Code','Package','Currency','~Lines','~On','Status'],
    r.map(x=>`<tr><td class="mono">${esc(x.code)}</td><td>${esc(x.name)}</td>
      <td>${esc(x.currency)}</td><td class="num">${(x.lines||[]).length}</td>
      <td class="num">${x.assignment_count??0}</td>
      <td>${x.is_active?tag('open'):tag('closed')}</td></tr>`),
    'No salary structures',
    'A structure is a package held once and assigned to many people.',
    `<button class="btn" onclick="openForm('salstructure')">+ New Structure</button>`));};

VIEWS.structureassign=simple('salary-structure-assignments','salassign',
  ['Employee','Structure','From','Until','~Base Salary'],
  a=>`<tr><td>${esc(a.employee_name||'—')}</td>
    <td class="mono">${esc(a.structure_code||'—')}</td>
    <td>${dt(a.from_date)}</td><td>${a.to_date?dt(a.to_date):'current'}</td>
    <td class="num">${money(a.base_salary,a.currency)}</td></tr>`,
  'No structure assignments','Assign a package to an employee with their base salary.');

VIEWS.payslips=simple('payslips',null,
  ['Employee','~Paid Days','~Gross','~Income Tax','~Insurance','~Net Pay','Status'],
  p=>`<tr><td>${esc((p.employee_snapshot&&p.employee_snapshot.name)||p.employee_name||'—')}</td>
    <td class="num">${p.paid_days??'—'}</td><td class="num">${money(p.gross_amount,C())}</td>
    <td class="num">${money(p.income_tax_amount,C())}</td>
    <td class="num">${money(p.social_insurance_employee,C())}</td>
    <td class="num"><b>${money(p.net_amount,C())}</b></td>
    <td>${tag(p.payment_status)}</td></tr>`,'No payslips');

VIEWS.leaves=async()=>{const r=await list('leave-requests');if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('leave')">+ New</button>`);
  V(tbl(['Employee','Type','From','To','~Days','Status',''],
    r.map(l=>`<tr><td>${esc(l.employee_name||'—')}</td><td>${esc(l.leave_type_name||'—')}</td>
      <td>${dt(l.start_date)}</td><td>${dt(l.end_date)}</td><td class="num">${l.total_days??'—'}</td>
      <td>${tag(l.status)}</td><td class="num">${
        ['submitted','pending_manager','pending_hr'].includes(l.status)
        ?`<button class="btn sm" onclick="act('leave-requests','${l.id}','approve',()=>go('leaves'))">Approve</button>`:''
      }</td></tr>`),'No leave requests'));};
VIEWS.expenses=async()=>{const r=await list('expenses',viewQuery('expenses'));
  if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('expense')">+ New</button>`);
  V(viewBar('expenses','expense')+tbl(['Date','Expense#','Vendor','Status','~Amount',''],
    r.map(e=>`<tr><td>${dt(e.expense_date)}</td><td class="mono">${esc(e.number||'—')}</td>
      <td>${esc(e.vendor_name||'—')}</td><td>${tag(e.status)}</td>
      <td class="num">${money(e.total_amount,C())}</td>
      <td class="num">${e.status==='submitted'
        ?`<button class="btn sm" onclick="act('expenses','${e.id}','approve',()=>go('expenses'))">Approve</button>`
        :e.status==='draft'?`<button class="btn sm sec" onclick="act('expenses','${e.id}','submit',()=>go('expenses'))">Submit</button>`:''}
      </td></tr>`),'No expenses'));};
VIEWS.payroll=async()=>{const r=await list('payroll-runs');if(r===null)return V(denied());
  A(`<button class="btn" onclick="openForm('payrun')">+ New</button>`);
  const btn=p=>{const b=(a,l,w)=>`<button class="btn sm" onclick="act('payroll-runs','${p.id}','${a}',
      ()=>go('payroll')${w?",'"+w+"'":''})">${l}</button>`;
    return {draft:b('calculate','Calculate'),calculated:b('submit-for-approval','Submit'),
      pending_approval:b('approve','Approve','Approve? Whoever calculated it cannot approve it.'),
      approved:b('post','Post to Ledger','Post this pay run to the general ledger?'),
      // Disbursement is not a status flip — it moves money and needs to know
      // which account it leaves from and on what date, so it gets a form
      // rather than the generic transition button.
      posted:`<button class="btn sm" onclick="disburseRun('${p.id}')">Execute Disbursement</button>`,
      paid:'<span class="note" style="margin:0">Disbursed</span>'}[p.status]||'';};
  V(tbl(['Pay Run','Period','~Staff','~Gross','~Deductions','~Net','Status',''],
    r.map(p=>`<tr><td>${esc(p.name)}</td><td>${dt(p.period_start)} – ${dt(p.period_end)}</td>
      <td class="num">${p.employee_count??0}</td><td class="num">${money(p.total_gross,C())}</td>
      <td class="num">${money(p.total_deductions,C())}</td>
      <td class="num">${money(p.total_net,C())}</td>
      <td>${tag(p.status)}${p.journal_entry
        ?` <a href="#" class="note" style="margin:0"
             onclick="openEntry('${p.journal_entry}');return false">voucher</a>`:''}</td>
      <td class="num">${btn(p)}</td></tr>`),'No pay runs visible',
    'A pay run is hidden from whoever prepared it — sign in as the accountant to approve.'));};
VIEWS.periods=async()=>{const r=await list('fiscal-periods');if(r===null)return V(denied());
  V(tbl(['Period','From','To','Status',''],
    r.map(x=>`<tr><td>${esc(x.name)}</td><td>${dt(x.start_date)}</td><td>${dt(x.end_date)}</td>
      <td>${tag(x.status)}</td><td class="num">${x.status==='open'
        ?`<button class="btn sm sec" onclick="act('fiscal-periods','${x.id}','close',()=>go('periods'),
          'Close this period? Nothing can be posted into it afterwards.')">Close</button>`:''}
      </td></tr>`),'No fiscal periods'));};

/* ── SETTINGS ──────────────────────────────────────────────────────────── */
VIEWS.org=async()=>{
  const t=await safe('/api/v1/tenancy/current/')||S.tenant;
  await loadRef();
  const cs=(S.ref&&S.ref.countries)||[], tz=(S.ref&&S.ref.timezones)||['UTC'];
  A(`<button class="btn" onclick="saveOrg()">Save changes</button>`);
  V(`<div class="panel anim"><div class="ph"><h3>Organisation Profile</h3></div><div class="pb">
    <div class="row">
      <div><label class="req">Organisation name</label><input id="o_name" value="${esc(t.name||'')}"></div>
      <div><label>Legal name</label><input id="o_legal" value="${esc(t.legal_name||'')}"></div>
      <div><label>Country</label><select id="o_country">${cs.map(x=>
        `<option value="${x.code}"${x.code===t.country?' selected':''}>${esc(x.name)}</option>`).join('')
        ||`<option>${esc(t.country||'')}</option>`}</select></div>
      <div><label>Time zone</label><select id="o_tz">${tz.map(x=>
        `<option${x===t.timezone?' selected':''}>${x}</option>`).join('')}</select></div>
      <div><label>Tax registration number</label>
        <input id="o_trn" value="${esc(t.tax_registration_number||'')}"></div>
      <div><label>Fiscal year starts</label><select id="o_fy">${
        ['January','February','March','April','May','June','July','August','September',
         'October','November','December'].map((m,i)=>
        `<option value="${i+1}"${(t.fiscal_year_start_month||1)===i+1?' selected':''}>${m}</option>`).join('')}
      </select></div>
    </div>
    <div class="row" style="margin-top:14px">
      <div><label>Base currency</label><input value="${esc(t.base_currency||'')}" disabled></div>
      <div><label>Plan</label><input value="${esc(t.status||'')}" disabled></div>
    </div>
    <div class="note">The base currency is locked once any journal entry is posted —
      changing it would invalidate every historical report and every stored
      base-currency amount on the ledger.</div>
  </div></div>`);};
async function saveOrg(){
  try{await api('/api/v1/tenancy/current/',{method:'PATCH',reauth:!!S.reauth,body:JSON.stringify({
      name:document.getElementById('o_name').value,
      legal_name:document.getElementById('o_legal').value,
      country:document.getElementById('o_country').value,
      timezone:document.getElementById('o_tz').value,
      tax_registration_number:document.getElementById('o_trn').value,
      fiscal_year_start_month:parseInt(document.getElementById('o_fy').value,10)})});
    toast('Organisation updated','ok');}
  catch(e){if(e.code==='reauth_required'&&await ensureReauth())return saveOrg();
    toast(e.message,'bad');}}

VIEWS.team=async()=>{
  let rows=await safe('/api/v1/team/members/');
  rows=(rows&&(rows.results||rows))||null;
  if(!rows)return V(denied());
  A(`<button class="btn" onclick="openForm('invite')">+ Invite User</button>`);
  V(tbl(['User','Email','Roles','Status',''],
    (rows||[]).map(m=>{
      const nm=m.full_name||m.user_full_name||m.user_email||m.email||'—';
      const roles=(m.roles||[]).map(r=>r.name||r.role_name||r).join(', ')||'—';
      const act=m.is_active!==false;
      return `<tr><td class="uc"><span class="avat">${initials(nm)}</span>${esc(nm)}
        ${m.is_owner?'<span class="tag t-info" style="margin-left:8px">Owner</span>':''}</td>
        <td>${esc(m.user_email||m.email||'—')}</td><td>${esc(roles)}</td>
        <td>${tag(act?'active':'cancelled')}</td>
        <td class="num">${m.is_owner?'':
          `<button class="btn sm sec" onclick="memberAct('${m.id}','${act?'deactivate':'activate'}')">
            ${act?'Deactivate':'Activate'}</button>`}</td></tr>`;}),
    'No team members yet','Invite colleagues by email and give each one a role.',
    `<button class="btn" onclick="openForm('invite')">+ Invite User</button>`));};
async function memberAct(id,a){
  if(!await ensureReauth())return;
  try{await api(`/api/v1/team/members/${id}/${a}/`,{method:'POST',reauth:1,body:'{}'});
    toast('Done','ok');go('team');}
  catch(e){if(e.code==='reauth_required'){S.reauth=null;toast('Confirm your password again','bad');}
    else toast(e.message,'bad');}}

VIEWS.invites=async()=>{
  let rows=await safe('/api/v1/invitations/');
  rows=(rows&&(rows.results||rows))||null;
  if(!rows)return V(denied());
  A(`<button class="btn" onclick="openForm('invite')">+ Invite User</button>`);
  V(tbl(['Email','Role','Status','Expires','Invited by',''],
    (rows||[]).map(i=>`<tr><td>${esc(i.email)}</td><td>${esc(i.role_name||i.role_code||'—')}</td>
      <td>${tag(i.status)}</td><td>${dt(i.expires_at)}</td>
      <td>${esc(i.invited_by_email||'—')}</td>
      <td class="num">${i.status==='pending'?
        `<button class="btn sm sec" onclick="inviteAct('${i.id}','resend')">Resend</button>
         <button class="btn sm sec" onclick="inviteAct('${i.id}','revoke')">Revoke</button>`:''}
      </td></tr>`),'No pending invitations',
    'Invitations you send appear here until they are accepted or revoked.'));};
async function inviteAct(id,a){
  if(!await ensureReauth())return;
  try{const r=await api(`/api/v1/invitations/${id}/${a}/`,{method:'POST',reauth:1,body:'{}'});
    if(a==='resend'&&r&&r.invite_url)
      modal('New invitation link',`<input readonly value="${esc(r.invite_url)}"
        onclick="this.select()"><div class="note">The previous link stopped working the moment
        this one was issued.</div>`,'Done',closeModal);
    else toast('Done','ok');
    go('invites');}
  catch(e){toast(e.message,'bad');}}

VIEWS.audit=async()=>{const r=await list('audit-logs');if(r===null)return V(denied());
  V(tbl(['When','Action','Actor','Object','IP'],
    r.map(a=>`<tr><td>${dt(a.occurred_at)}</td><td>${tag(a.action)}</td>
      <td>${esc(a.actor_email||'—')}</td><td class="mono">${esc(a.object_type||'—')}</td>
      <td class="mono">${esc(a.ip_address||'—')}</td></tr>`),'No audit entries yet',
      'Logins, role grants, period closes and payroll approvals are recorded here.'));};

/* ── REPORTS ───────────────────────────────────────────────────────────── */
VIEWS.reports=async()=>{const y=new Date().getFullYear();
  V(`<div class="tools anim"><label style="margin:0">From</label>
      <input id="r_from" type="date" value="${y}-01-01">
      <label style="margin:0">To</label>
      <input id="r_to" type="date" value="${new Date().toISOString().slice(0,10)}">
      ${[['trial-balance','Trial Balance'],['profit-loss','Profit &amp; Loss'],
         ['balance-sheet','Balance Sheet'],['cash-flow','Cash Flow'],
         ['ar-aging','A/R Ageing'],['ap-aging','A/P Ageing']].map((r,i)=>
        `<button class="btn${i?' sec':''}" onclick="run('${r[0]}','${r[1]}')">${r[1]}</button>`).join('')}
     </div><div id="r_out"><div class="panel anim"><div class="empty"><h4>Pick a report</h4>
     <p>Select a date range and choose a statement above. The General Ledger,
     Journal Register, Party Statement and Financial Ratios have their own
     entries in the sidebar.</p></div></div></div>`);};
async function run(k,t){const o=document.getElementById('r_out');
  // The user can navigate away while a report is still generating. Without
  // this the async continuation below writes into a node that no longer
  // exists and throws, leaving the console noisy and the next report silent.
  if(!o)return;
  o.innerHTML=skeleton();
  const f=document.getElementById('r_from').value,e=document.getElementById('r_to').value;
  try{const d=await api(`/api/v1/reporting/${k}/?date_from=${f}&date_to=${e}`);
    const c=C();
    let h=`<div class="panel anim"><div class="ph"><h3>${t}</h3>
      <span style="color:var(--mut);font-size:11.5px">${dt(f)} – ${dt(e)}</span></div>`;
    const secs=d.sections||[];
    if(!secs.length)h+=`<div class="empty"><h4>No data for this period</h4></div>`;
    else{h+=`<div class="rscroll"><table class="rtbl"><thead><tr><th>Account</th><th class="num">Amount</th></tr></thead><tbody>`;
      secs.forEach(s=>{h+=`<tr style="background:var(--panel-2)"><td colspan="2"><b>${esc(s.title||'')}</b></td></tr>`;
        (s.lines||[]).forEach(l=>{
          const lbl=esc(l.label||l.account_name||'');
          const id=l.account_id||l.account;
          // Only linked when the line names an account. Subtotal and
          // computed rows have nothing to drill into, and a dead link on
          // one row teaches the reader not to trust the live ones.
          // Only the id goes into the attribute. Passing the label through
          // JSON.stringify put double quotes inside a double-quoted onclick=""
          // and shredded the tag; drillAccount reads the name off the link
          // instead, so no amount of punctuation in an account name can break
          // the markup.
          const cell=id?`<a href="#" data-acc="${id}"
              onclick="drillAccount(this.dataset.acc,this.textContent.trim());return false"
              title="Open account ledger">${lbl}</a>`:lbl;
          h+=`<tr><td style="padding-left:32px">${cell}</td>
          <td class="num">${money(l.amount??l.value,c)}</td></tr>`;});
        if(s.total!=null)h+=`<tr><td><b>Total ${esc(s.title||'')}</b></td>
          <td class="num"><b>${money(s.total,c)}</b></td></tr>`;});
      h+=`</tbody></table></div>`;}
    const T=d.totals||{};
    if(Object.keys(T).length)h+=`<div style="padding:12px 16px;border-top:1px solid var(--line);
      background:var(--panel-2);font-size:12.5px;display:flex;flex-wrap:wrap;gap:8px 26px">`+
      Object.entries(T).map(([k2,v])=>`<span>${esc(k2.replace(/_/g,' '))}:
        <b>${money(v,c)}</b></span>`).join('')+`</div>`;
    if(document.getElementById('r_out'))o.innerHTML=h+`</div>`;}
  catch(x){if(!document.getElementById('r_out'))return;
    o.innerHTML=`<div class="panel anim"><div class="empty">
    <h4 style="color:var(--dang)">Could not generate</h4><p>${esc(x.message)}</p></div></div>`;}}

/* ── accept-invite deep link + session restore ─────────────────────────── */
(function(){
  document.getElementById('api').value=location.origin;
  S.api=location.origin;
  const u=new URL(location.href);
  const tok=u.searchParams.get('token');
  if(location.pathname.includes('accept-invite')||tok){
    if(tok)return showAccept(tok);
  }
  const raw=sessionStorage.getItem('achr');
  if(!raw){
    // No session. If the URL names a screen, remember it so the user lands
    // there after signing in rather than on the dashboard — a bookmarked
    // deep link should survive the login it triggers.
    const wanted=location.pathname.replace(/^\/+/,'').replace(/\/+$/,'');
    if(wanted)sessionStorage.setItem('achr.next',wanted);
    return;
  }
  // Hide the sign-in screen *synchronously*, before boot()'s first await.
  // It is visible in the markup by default, so restoring asynchronously made
  // every refresh flash the login form for as long as /auth/me/ took — which
  // reads as "it logged me out and then logged me back in".
  document.getElementById('auth').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  try{const s=JSON.parse(raw);S.api=s.api||location.origin;S.access=s.a;S.refresh=s.r;
    S.tenant=s.t;S.user=s.u;boot();}
  catch{
    sessionStorage.removeItem('achr');
    document.getElementById('auth').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
  }
})();

function showAccept(token){
  document.getElementById('fIn').classList.add('hidden');
  document.getElementById('fUp').classList.add('hidden');
  document.querySelector('.tabs').classList.add('hidden');
  const c=document.querySelector('.card');
  const d=document.createElement('form');
  d.innerHTML=`<h1>Join the team</h1><p class="sub">Set your name and password to accept the invitation</p>
    <label class="req">Full name</label><input id="a_name" required>
    <label class="req">Password</label><input id="a_pass" type="password" required minlength="12">
    <div class="note">At least 12 characters.</div>
    <button class="btn w" type="submit" id="aBtn">Accept invitation</button>
    <div id="aErr" class="err hidden"></div>`;
  d.onsubmit=async ev=>{ev.preventDefault();
    const b=document.getElementById('aBtn'),x=document.getElementById('aErr');
    b.disabled=true;b.innerHTML='<span class="spin"></span> Joining…';x.classList.add('hidden');
    try{const r=await fetch(S.api+'/api/v1/auth/accept-invite/',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({token,
          full_name:document.getElementById('a_name').value,
          password:document.getElementById('a_pass').value})});
      const j=await r.json();
      if(!r.ok)throw new Error((j.error&&j.error.detail)||'This invitation is no longer valid');
      if(j.requires_login){x.textContent='This email already has an account — sign in instead.';
        x.classList.remove('hidden');setTimeout(()=>location.href='/',2500);return;}
      S.access=j.access;S.refresh=j.refresh;S.tenant=j.tenant;S.user=j.user;save();
      history.replaceState({},'','/');await boot();toast('Welcome aboard','ok');}
    catch(e){x.textContent=e.message;x.classList.remove('hidden');}
    finally{b.disabled=false;b.textContent='Accept invitation';}};
  c.appendChild(d);}

/* ── JOURNAL ENTRY GRID ────────────────────────────────────────────────────
   A keyboard-first bulk entry screen. Three things make it fast, and each of
   them is also what keeps it correct:

   * **One side per row.** Typing in Debit clears Credit and vice versa —
     `LineDraft.__post_init__` refuses a line carrying both, so allowing it
     here would only defer the error to the save.
   * **Auto-balance.** The running difference is shown live and F2 drops it on
     the opposite side of the focused row. It is an aid to typing, not a
     validation: the browser's arithmetic is float, so the figure it fills is
     a *suggestion the user can see and correct*, and post_entry re-checks the
     balance in Decimal against the strings actually submitted.
   * **Enter adds a row, Ctrl+Enter saves.** A grid that needs the mouse to
     add its fifth line is not a bulk entry grid.

   Amounts stay strings on the way out. The only arithmetic here is the
   running total, which is display-only and never sent.                      */
let JE = { accounts: [], journals: [], parties: [] };

async function newJournal() {
  // Accounts come from the tree (all postable leaves, unpaginated) rather than
  // the 100-row /accounts/ page, so a large chart cannot hide a leaf from the
  // picker. Parties are loaded so a line on an account that requires one can
  // name it inline.
  let ac;
  try { ac = await flattenAccounts(); }
  catch (e) { return toast(e.message, 'bad'); }
  const posts = ac.filter(a => a.is_postable && a.is_active !== false);
  if (!posts.length) return toast('No postable accounts in the chart', 'bad');
  const [jr, cu, ve, em] = await Promise.all([
    list('journals'), list('customers'), list('vendors'), list('employees'),
  ]);
  JE.accounts = posts;
  JE.journals = jr || [];
  JE.parties = [
    ...(cu || []).map(p => ({ id: p.id, type: 'customer', label: lbl(p) })),
    ...(ve || []).map(p => ({ id: p.id, type: 'vendor', label: lbl(p) })),
    ...(em || []).map(p => ({ id: p.id, type: 'employee', label: lbl(p) })),
  ];
  if (!JE.journals.length) return toast('No journals configured', 'bad');
  const t = new Date().toISOString().slice(0, 10);

  modal('New Journal Entry', `
    <div class="row">
      <div><label class="req">Journal</label><select id="j_journal">
        ${JE.journals.map(j => `<option value="${esc(j.code)}">${esc(j.code)} — ${esc(j.name)}</option>`).join('')}
      </select></div>
      <div><label class="req">Entry Date</label><input id="j_date" type="date" value="${t}"></div>
      <div><label>Currency</label><input id="j_cur" value="${S.tenant.base_currency}"></div>
      <div><label>Rate</label><input id="j_rate" class="num" value="1" inputmode="decimal"
        title="Exchange rate to the base currency — leave at 1 for base-currency entries"></div>
      <div style="grid-column:1/3"><label>Memo</label>
        <input id="j_memo" placeholder="What this entry records"></div>
    </div>
    <label style="margin-top:16px">Lines</label>
    <table class="lines"><thead><tr>
      <th style="width:26%">Account</th><th style="width:18%">Party</th>
      <th style="width:20%">Description</th>
      <th style="width:15%" class="num">Debit</th><th style="width:15%" class="num">Credit</th><th></th>
    </tr></thead><tbody id="j_lines"></tbody></table>
    <datalist id="j_accs">${JE.accounts.map(a =>
      `<option value="${esc(a.code)} — ${esc(a.name)}">`).join('')}</datalist>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap">
      <button class="btn sec sm" onclick="jRow()">+ Add Row (Enter)</button>
      <button class="btn sec sm" onclick="jBalance()">Auto-balance (F2)</button>
      <span class="note" style="margin:0">Ctrl+Enter posts</span>
    </div>
    <div class="totbox"><div>
      <div class="totrow"><span>Total debits</span><b id="j_dr">0.00</b></div>
      <div class="totrow"><span>Total credits</span><b id="j_cr">0.00</b></div>
      <div class="totrow g"><span>Difference</span><b id="j_diff">0.00</b></div>
      <div class="note" id="j_hint">Running total only. The server recomputes it in
        fixed-precision decimal and refuses anything that does not balance.</div>
    </div></div>`, 'Post Entry', saveJournal);

  jRow(); jRow();
  document.getElementById('ov').addEventListener('keydown', jKeys);
  setTimeout(() => { const f = document.querySelector('#j_lines input'); if (f) f.focus(); }, 30);
}

function jRow() {
  const tb = document.getElementById('j_lines'); if (!tb) return;
  const tr = document.createElement('tr');
  const partyOpts = '<option value="">—</option>' + JE.parties.map(p =>
    `<option value="${p.type}:${p.id}">${esc(p.label)} (${p.type[0].toUpperCase()})</option>`).join('');
  tr.innerHTML = `
    <td><input list="j_accs" class="j_acc" placeholder="Code or name"></td>
    <td><select class="j_party"
        title="Optional; expected on control accounts (A/R, A/P)">${partyOpts}</select></td>
    <td><input class="j_desc"></td>
    <td><input class="j_dr num" inputmode="decimal" placeholder="0.00"></td>
    <td><input class="j_cr num" inputmode="decimal" placeholder="0.00"></td>
    <td><button class="x" title="Remove row"
        onclick="this.closest('tr').remove();jCalc()">&times;</button></td>`;
  tb.appendChild(tr);
  // One side per row: entering a debit blanks the credit, and vice versa.
  tr.querySelector('.j_dr').addEventListener('input', e => {
    if (e.target.value) tr.querySelector('.j_cr').value = ''; jCalc(); });
  tr.querySelector('.j_cr').addEventListener('input', e => {
    if (e.target.value) tr.querySelector('.j_dr').value = ''; jCalc(); });
  // The party field only means something once the account is known: an account
  // that requires a party enables it, any other disables and clears it.
  tr.querySelector('.j_acc').addEventListener('input', () => { jSyncParty(tr); jCalc(); });
  tr.querySelector('.j_party').addEventListener('change', jCalc);
  jCalc();
}

/* A line's party picker is usable on *every* line — a party can be attached to
   a revenue or expense line too, which is exactly what the party statement and
   the ageing reports read. An account that *requires* a party (A/R, A/P
   control) while none is chosen is flagged, not blocked: requires_party is
   guidance, and the server does not gate posting on it. (Previously the picker
   was disabled unless the account required a party, so on an ordinary line the
   field was greyed out and a party could not be set at all.) */
function jSyncParty(tr) {
  const acc = jAccount(tr.querySelector('.j_acc').value);
  const sel = tr.querySelector('.j_party');
  const need = !!(acc && acc.requires_party);
  sel.style.borderColor = (need && !sel.value) ? 'var(--warn)' : '';
}

/* Display-only arithmetic. Deliberately not used for anything that is sent. */
function jCalc() {
  const rows = [...document.querySelectorAll('#j_lines tr')];
  let dr = 0, cr = 0, bad = 0;
  rows.forEach(r => {
    const a = r.querySelector('.j_acc').value.trim();
    const d = parseFloat(r.querySelector('.j_dr').value) || 0;
    const c = parseFloat(r.querySelector('.j_cr').value) || 0;
    dr += d; cr += c;
    const filled = d > 0 || c > 0 || !!a;
    const unknown = filled && !jAccount(a);
    if (unknown) bad++;
    r.querySelector('.j_acc').style.borderColor = unknown ? 'var(--dang)' : '';
  });
  const diff = dr - cr;
  const $ = id => document.getElementById(id);
  if (!$('j_dr')) return;
  $('j_dr').textContent = dr.toFixed(2);
  $('j_cr').textContent = cr.toFixed(2);
  $('j_diff').textContent = diff.toFixed(2);
  $('j_diff').className = Math.abs(diff) < 0.005 ? 'pos' : 'neg';
  const filled = rows.filter(r => {
    const a = r.querySelector('.j_acc').value.trim();
    return a || parseFloat(r.querySelector('.j_dr').value) || parseFloat(r.querySelector('.j_cr').value);
  }).length;
  const balanced = Math.abs(diff) < 0.005 && dr > 0;
  $('j_hint').textContent = bad
    ? `${bad} row${bad > 1 ? 's have' : ' has'} an unrecognised account.`
    : balanced
      ? 'Balanced. The server re-checks in decimal before posting.'
      : (dr > 0 || cr > 0)
        ? `Out of balance by ${Math.abs(diff).toFixed(2)} — ${diff > 0 ? 'credits' : 'debits'} are short.`
        : 'Enter at least two lines that balance.';
  // Post stays disabled until the entry is postable: two or more lines, every
  // named account recognised, and debits equal to a non-zero credit total.
  const postBtn = document.querySelector('#ov .mf .btn');
  if (postBtn) postBtn.disabled = !(balanced && !bad && filled >= 2);
}

/* Resolve "1110 — Main bank account", "1110" or "Main bank account". */
function jAccount(text) {
  const v = String(text || '').trim().toLowerCase(); if (!v) return null;
  const code = v.split('—')[0].trim();
  return JE.accounts.find(a => String(a.code).toLowerCase() === code)
      || JE.accounts.find(a => (a.code + ' — ' + a.name).toLowerCase() === v)
      || JE.accounts.find(a => String(a.name || '').toLowerCase() === v)
      || null;
}

/* Drop the outstanding difference onto the focused row's opposite side. */
function jBalance() {
  const rows = [...document.querySelectorAll('#j_lines tr')];
  if (!rows.length) return;
  const dr = rows.reduce((s, r) => s + (parseFloat(r.querySelector('.j_dr').value) || 0), 0);
  const cr = rows.reduce((s, r) => s + (parseFloat(r.querySelector('.j_cr').value) || 0), 0);
  const diff = dr - cr;
  if (Math.abs(diff) < 0.005) return toast('Already balanced', 'ok');
  const active = document.activeElement;
  const target = (active && active.closest && active.closest('#j_lines tr'))
    || rows[rows.length - 1];
  target.querySelector(diff > 0 ? '.j_cr' : '.j_dr').value = Math.abs(diff).toFixed(2);
  target.querySelector(diff > 0 ? '.j_dr' : '.j_cr').value = '';
  jCalc();
}

function jKeys(e) {
  if (e.key === 'F2') { e.preventDefault(); return jBalance(); }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault(); if (onSave) onSave(); return;
  }
  if (e.key === 'Enter' && e.target.closest && e.target.closest('#j_lines')) {
    e.preventDefault(); jRow();
    const inputs = document.querySelectorAll('#j_lines tr:last-child .j_acc');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }
}

async function saveJournal() {
  const rows = [...document.querySelectorAll('#j_lines tr')];
  const lines = [];
  // Collected below, but the re-auth prompt is raised *before* the draft is
  // written — see the call site at the end of this function for why.
  for (const r of rows) {
    const accText = r.querySelector('.j_acc').value.trim();
    // Strings, never parsed: whatever the user typed is what the server
    // validates. The emptiness checks below are on the raw text.
    const d = r.querySelector('.j_dr').value.trim();
    const c = r.querySelector('.j_cr').value.trim();
    if (!accText && !d && !c) continue;                 // untouched row
    const acc = jAccount(accText);
    if (!acc) return toast(`Unknown account: "${accText}"`, 'bad');
    if (d && c) return toast('A line carries one side only, not both', 'bad');
    if (!d && !c) return toast(`Line for ${acc.code} has no amount`, 'bad');
    const line = {
      account: acc.id,
      description: r.querySelector('.j_desc').value.trim(),
      debit: d || '0',
      credit: c || '0',
    };
    // Party, whenever the row named one (optional on any line).
    const partyEl = r.querySelector('.j_party');
    if (partyEl && partyEl.value) {
      const [pt, pid] = partyEl.value.split(':');
      line.partner_type = pt;
      line.partner_id = pid;
    }
    lines.push(line);
  }
  if (lines.length < 2) return toast('An entry needs at least two lines', 'bad');

  const journal = JE.journals.find(
    j => j.code === document.getElementById('j_journal').value);
  const rate = (document.getElementById('j_rate').value || '').trim();
  const body = {
    journal: journal ? journal.id : null,
    entry_date: document.getElementById('j_date').value,
    currency: document.getElementById('j_cur').value.trim().toUpperCase(),
    exchange_rate: rate || '1',
    memo: document.getElementById('j_memo').value.trim(),
    lines,
  };

  // `accounting.journal_entry.post` is is_sensitive, so the server demands a
  // fresh password proof. Ask for it *here*, before the draft is written —
  // prompting between the two calls means a cancelled prompt leaves an
  // orphan draft behind, and journal entries cannot be hard-deleted, so that
  // orphan is permanent clutter in the manual-journals list.
  if (!await ensureReauth()) return;

  try {
    // Two calls, because they are two acts: the draft is an editable
    // document; posting it takes the period lock, allocates the gapless
    // number and writes the balances. Idempotency-Key on the post so a
    // retried click cannot double-post.
    const draft = await api('/api/v1/journal-entries/', {
      method: 'POST', body: JSON.stringify(body),
    });
    await api(`/api/v1/journal-entries/${draft.id}/post/`, {
      method: 'POST', idem: 1, reauth: 1, body: JSON.stringify({}),
    });
    closeModal();
    toast('Entry posted', 'ok');
    go('journal');
  } catch (e) {
    if (e.code === 'reauth_required') {
      S.reauth = null;
      toast('Please confirm your password again', 'bad');
    } else toast(e.message, 'bad');
  }
}

/* ── DRILL-DOWN + DOCUMENT PREVIEW ─────────────────────────────────────────
   A statement figure that cannot be opened is a number the reader has to take
   on trust. Every amount below is reachable in two clicks: report line ->
   account ledger -> the voucher that moved it.

   Printing goes through a dedicated stylesheet rather than window.print() on
   the whole SPA: printing the app prints the sidebar, the nav and the toast
   stack. `#print-root` is the only thing @media print keeps.               */

function drillAccount(id, label) {
  const f = (document.getElementById('r_from') || {}).value || '';
  const t = (document.getElementById('r_to') || {}).value || '';
  modal(`Ledger — ${label}`, '<div class="sk"></div><div class="sk"></div>', 'Close', closeModal);
  (async () => {
    try {
      const q = f && t ? `?date_from=${f}&date_to=${t}` : '';
      const d = await api(`/api/v1/accounts/${id}/ledger/${q}`);
      const rows = d.results || d || [];
      const c = C();
      const body = document.querySelector('#ov .mb');
      if (!body) return;
      if (!rows.length) {
        body.innerHTML = '<div class="empty"><h4>No posted movement</h4>'
          + '<p>Nothing hit this account in the selected range.</p></div>';
        return;
      }
      body.innerHTML = `
        ${d.opening_balance != null ? `<div class="mrow"><span>Opening balance</span>
          <b>${money(d.opening_balance, c)}</b></div>` : ''}
        <table style="margin-top:8px"><thead><tr>
          <th>Date</th><th>Voucher</th><th>Journal</th><th>Memo</th>
          <th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th>
        </tr></thead><tbody>
        ${rows.map(r => `<tr>
          <td>${dt(r.entry_date)}</td>
          <td><a href="#" class="mono" onclick="openEntry('${r.entry}');return false"
                 title="Open voucher">${esc(r.entry_number || '—')}</a></td>
          <td class="mono">${esc(r.journal_code || '')}</td>
          <td>${esc(r.entry_memo || r.description || '')}</td>
          <td class="num">${r.debit > 0 ? money(r.debit, c) : ''}</td>
          <td class="num">${r.credit > 0 ? money(r.credit, c) : ''}</td>
          <td class="num">${money(r.running_balance, c)}</td>
        </tr>`).join('')}
        </tbody></table>
        ${d.closing_balance != null ? `<div class="mrow tot"><span>Closing balance</span>
          <b>${money(d.closing_balance, c)}</b></div>` : ''}`;
    } catch (e) {
      const body = document.querySelector('#ov .mb');
      if (body) body.innerHTML = `<div class="empty"><h4 style="color:var(--dang)">
        Could not load ledger</h4><p>${esc(e.message)}</p></div>`;
    }
  })();
}

/* ══════════════════════════════════════════════════════════════════════════
   GL detail reports — General Ledger · Journal Register · Party Statement ·
   Financial Ratios. Each is its own sidebar entry (under Reports) and its own
   VIEWS screen; they share the small renderers below and read the
   /reporting/* endpoints, printing in ACHR's theme.
   ══════════════════════════════════════════════════════════════════════════ */
const grVal = id => (document.getElementById(id) || {}).value || '';
const grEmpty = () => `<div class="panel anim"><div class="empty"><h4>Pick a range</h4>
  <p>Choose a date range and press Run.</p></div></div>`;
const grNoData = () => `<div class="empty"><h4>No data for this period</h4></div>`;

function grDateInputs() {
  const y = new Date().getFullYear();
  return `<label style="margin:0">From</label>
    <input id="gr_from" type="date" value="${y}-01-01">
    <label style="margin:0">To</label>
    <input id="gr_to" type="date" value="${new Date().toISOString().slice(0, 10)}">`;
}
function grHead(title, f, e) {
  return `<div class="ph"><h3>${esc(title)}</h3>
    <span style="color:var(--mut);font-size:11.5px">${dt(f)} – ${dt(e)}</span></div>`;
}

/* One place that runs a report and swaps the panel. `render(d, currency)`
   returns the table markup; navigating away mid-request must not throw. */
async function grReport(ep, title, render, extraQ) {
  const o = document.getElementById('gr_out'); if (!o) return;
  o.innerHTML = skeleton();
  const f = grVal('gr_from'), e = grVal('gr_to');
  try {
    const d = await api(`/api/v1/reporting/${ep}/?date_from=${f}&date_to=${e}${extraQ || ''}`);
    if (!document.getElementById('gr_out')) return;
    o.innerHTML = `<div class="panel anim">${grHead(title, f, e)}${render(d, C(), f, e)}</div>`;
  } catch (x) {
    if (document.getElementById('gr_out')) o.innerHTML = errPanel(x.message);
  }
}

/* Transactional table: `cols` is [{h, num?, cell(line)}]; multi-section reports
   (the general ledger) get a heading row per account. */
function grTxn(cols, sections, c) {
  let h = `<div class="rscroll"><table class="rtbl"><thead><tr>` +
    cols.map(col => `<th${col.num ? ' class="num"' : ''}>${col.h}</th>`).join('') +
    `</tr></thead><tbody>`;
  const many = sections.length > 1;
  sections.forEach(s => {
    if (many) h += `<tr style="background:var(--panel-2)">
      <td colspan="${cols.length}"><b>${esc(s.title || '')}</b></td></tr>`;
    (s.lines || []).forEach(l => {
      const bold = l.is_bold ? ' style="font-weight:600"' : '';
      h += `<tr${bold}>` +
        cols.map(col => `<td${col.num ? ' class="num"' : ''}>${col.cell(l)}</td>`).join('') +
        `</tr>`;
    });
  });
  return h + `</tbody></table></div>`;
}

/* Statement-level totals strip, matching the classic reports screen. */
function grTotals(d, c) {
  const T = d.totals || {}; if (!Object.keys(T).length) return '';
  return `<div style="padding:12px 16px;border-top:1px solid var(--line);
    background:var(--panel-2);font-size:12.5px;display:flex;flex-wrap:wrap;gap:8px 26px">` +
    Object.entries(T).map(([k, v]) =>
      `<span>${esc(k.replace(/_/g, ' '))}: <b>${money(v, c)}</b></span>`).join('') + `</div>`;
}

const grAmt = (v, c) => Number(v) ? money(v, c) : '';

/* ── General Ledger ─ per-account: opening → movements → closing balance.
   The account picker defaults to the whole chart; choosing one account scopes
   the report to that account and its descendants (the endpoint's `account`). */
VIEWS.gl = async () => {
  V(skeleton());
  let accs = [];
  try { accs = (await flattenAccounts()).filter(a => a.is_postable && a.is_active !== false); }
  catch (e) { return V(errPanel(e.message)); }
  V(`<div class="tools anim">
     <label style="margin:0">Account</label>
     <select id="gr_acct" style="min-width:260px"><option value="">All accounts</option>
       ${accs.map(a => `<option value="${a.id}">${esc(a.code)} — ${esc(a.name)}</option>`).join('')}
     </select>
     ${grDateInputs()}
     <button class="btn" onclick="grGeneralLedger()">Run</button></div>
     <div id="gr_out">${grEmpty()}</div>`);
};
function grGeneralLedger() {
  const acc = grVal('gr_acct');
  grReport('general-ledger', 'General Ledger', (d, c) =>
    (d.sections || []).length
      ? grTxn([
          { h: 'Date', cell: l => l.meta && l.meta.kind === 'movement' ? dt(l.meta.date) : '' },
          { h: 'Ref', cell: l => esc((l.meta && l.meta.number) || '') },
          { h: 'Description', cell: l => esc(l.label) },
          { h: 'Debit', num: 1, cell: l => grAmt(l.debit, c) },
          { h: 'Credit', num: 1, cell: l => grAmt(l.credit, c) },
          { h: 'Balance', num: 1, cell: l => money(l.amount, c) },
        ], d.sections, c) + grTotals(d, c)
      : grNoData(), acc ? `&account=${acc}` : '');
}

/* ── Journal Register ─ every posting in book order. */
VIEWS.journalregister = () => {
  V(`<div class="tools anim">${grDateInputs()}
     <button class="btn" onclick="grJournalRegister()">Run</button></div>
     <div id="gr_out">${grEmpty()}</div>`);
};
function grJournalRegister() {
  grReport('journal-register', 'Journal Register', (d, c) =>
    (d.sections && d.sections[0] && d.sections[0].lines.length)
      ? grTxn([
          { h: 'Date', cell: l => dt(l.meta.date) },
          { h: 'Entry', cell: l => esc(l.meta.number || '') },
          { h: 'Account', cell: l => esc(l.label) },
          { h: 'Description', cell: l => esc(l.meta.description || l.meta.memo || '') },
          { h: 'Party', cell: l => esc(l.meta.partner || '') },
          { h: 'Debit', num: 1, cell: l => grAmt(l.debit, c) },
          { h: 'Credit', num: 1, cell: l => grAmt(l.credit, c) },
        ], d.sections, c) + grTotals(d, c)
      : grNoData());
}

/* ── Party Statement ─ one customer's / supplier's control-account ledger. */
const PARTY = { cust: [], vend: [] };
VIEWS.partystmt = async () => {
  V(skeleton());
  const [cu, ve] = await Promise.all([list('customers'), list('vendors')]);
  PARTY.cust = cu || []; PARTY.vend = ve || [];
  V(`<div class="tools anim">
     <label style="margin:0">Type</label>
     <select id="gr_ptype" onchange="grPartyType()">
       <option value="customer">Customer</option><option value="vendor">Vendor</option></select>
     <label style="margin:0">Party</label>
     <select id="gr_pid" style="min-width:180px"></select>
     ${grDateInputs()}
     <button class="btn" onclick="grPartyStatement()">Run</button></div>
     <div id="gr_out">${grEmpty()}</div>`);
  grPartyType();
};
function grPartyType() {
  const arr = grVal('gr_ptype') === 'vendor' ? PARTY.vend : PARTY.cust;
  const sel = document.getElementById('gr_pid'); if (!sel) return;
  sel.innerHTML = arr.length
    ? arr.map(x => `<option value="${x.id}">${esc(x.name || x.display_name || x.code || x.id)}</option>`).join('')
    : `<option value="">— none —</option>`;
}
function grPartyStatement() {
  const t = grVal('gr_ptype'), pid = grVal('gr_pid');
  if (!pid) { toast('Pick a party first', 'warn'); return; }
  grReport('party-statement', 'Party Statement', (d, c) => {
    const p = (d.metadata && d.metadata.partner) || {};
    return `<div style="padding:0 16px 10px;color:var(--mut);font-size:12px">
        ${esc(p.name || '(unnamed)')} · ${esc(p.type || '')}</div>` +
      grTxn([
        { h: 'Date', cell: l => l.meta && l.meta.kind === 'movement' ? dt(l.meta.date) : '' },
        { h: 'Ref', cell: l => esc((l.meta && l.meta.number) || '') },
        { h: 'Account', cell: l => esc((l.meta && l.meta.account_code) || '') },
        { h: 'Description', cell: l => esc(l.label) },
        { h: 'Debit', num: 1, cell: l => grAmt(l.debit, c) },
        { h: 'Credit', num: 1, cell: l => grAmt(l.credit, c) },
        { h: 'Balance', num: 1, cell: l => money(l.amount, c) },
      ], d.sections, c) + grTotals(d, c);
  }, `&partner_type=${t}&partner_id=${pid}`);
}

/* ── Financial Ratios ─ grouped indicators + the figures behind them. */
VIEWS.ratios = () => {
  V(`<div class="tools anim">${grDateInputs()}
     <button class="btn" onclick="grRatiosRun()">Run</button></div>
     <div id="gr_out">${grEmpty()}</div>`);
};
function grRatiosRun() {
  grReport('financial-ratios', 'Financial Ratios', (d, c) => {
    let h = (d.warnings || []).map(w =>
      `<div style="padding:8px 16px;color:var(--warn);font-size:11.5px">${esc(w)}</div>`).join('');
    (d.sections || []).forEach(s => {
      const fig = s.key === 'figures';
      h += `<div class="rscroll"><table class="rtbl"><thead><tr>
        <th>${esc(s.title)}</th><th class="num">${fig ? 'Amount' : 'Value'}</th></tr></thead><tbody>`;
      (s.lines || []).forEach(l => {
        const v = fig ? money(l.amount, c)
          : (l.meta && l.meta.na ? '<span style="color:var(--mut)">n/a</span>' : esc(String(l.amount)));
        const note = (!fig && l.note && !(l.meta && l.meta.na))
          ? `<span style="color:var(--mut);font-size:11px"> · ${esc(l.note)}</span>` : '';
        h += `<tr><td>${esc(l.label)}${note}</td><td class="num">${v}</td></tr>`;
      });
      h += `</tbody></table></div>`;
    });
    return h;
  });
}

/* The journal voucher, as a printable document. */
async function openEntry(id) {
  if (!id) return;
  modal('Journal Voucher', '<div class="sk"></div><div class="sk"></div>', 'Close', closeModal);
  try {
    const e = await api(`/api/v1/journal-entries/${id}/`);
    const c = e.currency || C();
    const lines = e.lines || [];
    const body = document.querySelector('#ov .mb');
    if (!body) return;
    body.innerHTML = `<div id="print-root" style="position:relative">
      ${docHeader(`Journal Voucher`, e.number || '(unposted)', [
        ['Date', dt(e.entry_date)], ['Journal', esc(e.journal_code || e.journal || '')],
        ['Status', (e.status || '').toUpperCase()], ['Currency', esc(c)],
      ])}
      ${e.memo ? `<p style="margin:14px 0 0;color:var(--mut)">${esc(e.memo)}</p>` : ''}
      <table style="margin-top:14px"><thead><tr>
        <th>Account</th><th>Description</th>
        <th class="num">Debit</th><th class="num">Credit</th>
      </tr></thead><tbody>
      ${lines.map(l => `<tr>
        <td class="mono">${esc(l.account_code || '')} ${esc(l.account_name || '')}</td>
        <td>${esc(l.description || '')}</td>
        <td class="num">${parseFloat(l.debit) > 0 ? money(l.debit, c) : ''}</td>
        <td class="num">${parseFloat(l.credit) > 0 ? money(l.credit, c) : ''}</td>
      </tr>`).join('')}
      </tbody></table>
      <div class="totbox"><div>
        <div class="totrow"><span>Total debits</span><b>${money(e.total_debit, c)}</b></div>
        <div class="totrow g"><span>Total credits</span><b>${money(e.total_credit, c)}</b></div>
      </div></div>
    </div>
    <button class="btn sec sm" style="margin-top:12px" onclick="printDoc()">Print / PDF</button>`;
  } catch (x) {
    const body = document.querySelector('#ov .mb');
    if (body) body.innerHTML = `<div class="empty"><h4 style="color:var(--dang)">
      Could not load voucher</h4><p>${esc(x.message)}</p></div>`;
  }
}

/* Invoice or bill, as the document a customer or auditor would be handed. */
async function openDoc(kind, id) {
  const ep = kind === 'invoice' ? 'invoices' : 'bills';
  modal(kind === 'invoice' ? 'Invoice' : 'Bill',
        '<div class="sk"></div><div class="sk"></div>', 'Close', closeModal);
  try {
    const d = await api(`/api/v1/${ep}/${id}/`);
    const c = d.currency || C();
    const lines = d.lines || [];
    const party = kind === 'invoice'
      ? (d.customer_name || d.customer || '') : (d.vendor_name || d.vendor || '');
    const body = document.querySelector('#ov .mb');
    if (!body) return;
    body.innerHTML = `<div id="print-root" style="position:relative">
      ${docHeader(kind === 'invoice' ? 'Invoice' : 'Bill',
        d.number || '(draft)', [
        [kind === 'invoice' ? 'Invoice date' : 'Bill date',
         dt(d.issue_date || d.bill_date)],
        ['Due date', dt(d.due_date)],
        [kind === 'invoice' ? 'Bill to' : 'Bill from', esc(party)],
        ['Status', (d.status || '').replace(/_/g, ' ').toUpperCase()],
      ])}
      <table style="margin-top:16px"><thead><tr>
        <th>#</th><th>Description</th><th class="num">Qty</th>
        <th class="num">Rate</th><th class="num">Amount</th>
      </tr></thead><tbody>
      ${lines.length ? lines.map((l, i) => `<tr>
        <td>${i + 1}</td><td>${esc(l.description || l.item_name || '')}</td>
        <td class="num">${l.quantity ?? ''}</td>
        <td class="num">${l.unit_price != null ? money(l.unit_price, c) : ''}</td>
        <td class="num">${money(l.line_total ?? l.line_subtotal, c)}</td>
      </tr>`).join('') : '<tr><td colspan="5" class="note">No lines.</td></tr>'}
      </tbody></table>
      <div class="totbox"><div>
        <div class="totrow"><span>Sub total</span><b>${money(d.subtotal_amount, c)}</b></div>
        ${parseFloat(d.tax_amount) ? `<div class="totrow"><span>Tax</span>
          <b>${money(d.tax_amount, c)}</b></div>` : ''}
        ${parseFloat(d.withholding_amount) ? `<div class="totrow"><span>Withheld</span>
          <b>-${money(d.withholding_amount, c)}</b></div>` : ''}
        <div class="totrow g"><span>Total (${esc(c)})</span><b>${money(d.total_amount, c)}</b></div>
        <div class="totrow"><span>Balance due</span><b>${money(d.amount_due, c)}</b></div>
      </div></div>
      ${docFooter()}
    </div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn sec sm" onclick="printDoc()">Print / PDF</button>
      ${d.journal_entry ? `<button class="btn sec sm"
        onclick="openEntry('${d.journal_entry}')">View journal entry</button>` : ''}
    </div>`;
  } catch (x) {
    const body = document.querySelector('#ov .mb');
    if (body) body.innerHTML = `<div class="empty"><h4 style="color:var(--dang)">
      Could not load document</h4><p>${esc(x.message)}</p></div>`;
  }
}

/* Shared masthead: the tenant's identity on every printed document, because
   a voucher with no company name on it is not evidence of anything. */
function docHeader(kind, serial, rows) {
  // Everything configurable comes from Settings -> Document Branding. The
  // fallbacks keep an unconfigured tenant printing something sane rather than
  // a document with a blank masthead.
  const b = (S.tenant.settings && S.tenant.settings.branding) || {};
  const c = esc(b.colour || '#4f46e5');
  return `${b.watermark ? `<div style="position:absolute;inset:0;display:flex;
      align-items:center;justify-content:center;font-size:76px;font-weight:800;
      opacity:.07;transform:rotate(-24deg);pointer-events:none;letter-spacing:.1em;
      z-index:0">${esc(b.watermark)}</div>` : ''}
  <div style="display:flex;justify-content:space-between;align-items:flex-start;
      border-bottom:2px solid ${c};padding-bottom:12px;position:relative;z-index:1">
    <div style="display:flex;gap:14px;align-items:center">
      ${b.logo ? `<img src="${esc(b.logo)}" alt="" style="max-height:52px;max-width:170px">` : ''}
      <div>
        <div style="font-size:18px;font-weight:650">${esc(S.tenant.name || '')}</div>
        <div class="note" style="margin:2px 0 0">${b.header
          ? esc(b.header)
          : esc(S.tenant.country || '') + ' · ' + esc(S.tenant.base_currency || '')}</div>
        ${b.tax_id || b.commercial_register ? `<div class="note" style="margin:2px 0 0">
          ${b.tax_id ? 'Tax ID ' + esc(b.tax_id) : ''}
          ${b.tax_id && b.commercial_register ? ' · ' : ''}
          ${b.commercial_register ? 'CR ' + esc(b.commercial_register) : ''}</div>` : ''}
      </div>
    </div>
    <div style="text-align:right">
      <div style="font-size:20px;font-weight:650;letter-spacing:-.02em;color:${c}">${esc(kind)}</div>
      <div class="mono" style="font-size:13px">${esc(serial)}</div>
    </div>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:6px 30px;margin-top:12px;font-size:12.5px;
      position:relative;z-index:1">
    ${rows.map(([k, v]) => `<span style="color:var(--mut)">${esc(k)}:
      <b style="color:var(--fg)">${v}</b></span>`).join('')}
  </div>`;
}

/* Bank details, terms and the footer line, printed under the totals. */
function docFooter() {
  const b = (S.tenant.settings && S.tenant.settings.branding) || {};
  if (!b.bank_details && !b.terms && !b.footer) return '';
  return `<div class="note" style="margin-top:20px;border-top:1px solid var(--line);
      padding-top:10px;position:relative;z-index:1">
    ${b.bank_details ? `<div><b>Payment:</b> ${esc(b.bank_details)}</div>` : ''}
    ${b.terms ? `<div style="margin-top:4px">${esc(b.terms)}</div>` : ''}
    ${b.footer ? `<div style="margin-top:6px">${esc(b.footer)}</div>` : ''}
  </div>`;
}

/* Print only the document. Without the @media print rule in index.html this
   would print the sidebar, the nav and whatever toast happened to be open. */
function printDoc() {
  document.body.classList.add('printing');
  window.print();
  setTimeout(() => document.body.classList.remove('printing'), 300);
}

/* ── DIALOGS ───────────────────────────────────────────────────────────────
   `confirm()` and `prompt()` are gone. They were not merely ugly:

   * They render the *origin* ("127.0.0.1:8000 says…") above your text, which
     on a finance app reads like a phishing warning at the exact moment the
     user is being asked to authorise a posting.
   * They block the whole renderer thread, so the count-up animations freeze
     mid-number and any in-flight request cannot resolve behind them.
   * `prompt()` for a password is the worst of the three: it is not a password
     field, so the characters are visible, and browsers do not offer it to a
     password manager.

   These return Promises so callers stay linear: `if (!await confirmDlg(...)) return;`

   Deliberately built on the same `.ov`/`.modal` markup as every other modal,
   rather than a second widget: one set of styles, one focus treatment, one
   Escape handler. `modal()` closes whatever is open first, so these nest
   correctly with the drill-down chain.                                     */

function confirmDlg(message, opts) {
  const o = opts || {};
  return new Promise(resolve => {
    let settled = false;
    const done = v => { if (settled) return; settled = true; closeModal(); resolve(v); };
    window.__dlgResolve = done;

    modal(o.title || 'Please confirm',
      `<p style="margin:0;font-size:14px;line-height:1.65">${esc(message)}</p>
       ${o.detail ? `<div class="note">${esc(o.detail)}</div>` : ''}`,
      o.confirmLabel || 'Confirm', () => done(true));

    // Re-style the footer: the shared modal() gives a neutral Save/Cancel,
    // and a destructive confirmation must not look like an ordinary save.
    const foot = document.querySelector('#ov .mf');
    if (foot) {
      foot.innerHTML =
        `<button class="btn${o.danger ? ' dang' : ''}" id="dlgYes">${esc(o.confirmLabel || 'Confirm')}</button>
         <button class="btn sec" id="dlgNo">${esc(o.cancelLabel || 'Cancel')}</button>`;
      foot.querySelector('#dlgYes').onclick = () => done(true);
      foot.querySelector('#dlgNo').onclick = () => done(false);
      setTimeout(() => foot.querySelector('#dlgYes').focus(), 30);
    }
    // The X and the backdrop mean "no". A dialog whose dismiss path is
    // ambiguous gets clicked through.
    const x = document.querySelector('#ov .mh .x');
    if (x) x.onclick = () => done(false);
    const ov = document.getElementById('ov');
    if (ov) ov.addEventListener('mousedown', e => { if (e.target === ov) done(false); });
    dlgKeys(done);
  });
}

/* Password prompt, as a real password field. */
function passwordDlg(message) {
  return new Promise(resolve => {
    let settled = false;
    const done = v => { if (settled) return; settled = true; closeModal(); resolve(v); };
    const submit = () => {
      const el = document.getElementById('dlgPw');
      done(el && el.value ? el.value : null);
    };

    modal('Confirm your identity',
      `<p style="margin:0 0 12px;font-size:14px">${esc(message)}</p>
       <label class="req">Password</label>
       <input id="dlgPw" type="password" autocomplete="current-password">
       <div class="note">This action is marked sensitive, so it needs a fresh
         password proof even though you are already signed in. The proof is
         tied to this action and is not stored.</div>`,
      'Confirm', submit);

    const foot = document.querySelector('#ov .mf');
    if (foot) {
      foot.innerHTML = `<button class="btn" id="dlgYes">Confirm</button>
        <button class="btn sec" id="dlgNo">Cancel</button>`;
      foot.querySelector('#dlgYes').onclick = submit;
      foot.querySelector('#dlgNo').onclick = () => done(null);
    }
    const x = document.querySelector('#ov .mh .x');
    if (x) x.onclick = () => done(null);
    const input = document.getElementById('dlgPw');
    if (input) {
      setTimeout(() => input.focus(), 30);
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
      });
    }
    dlgKeys(done, null);
  });
}

/* Escape closes with the "no" value. Registered on document rather than the
   overlay because focus may be inside an input that stops propagation. */
function dlgKeys(done, cancelValue) {
  const handler = e => {
    if (e.key === 'Escape') {
      document.removeEventListener('keydown', handler);
      done(cancelValue === undefined ? false : cancelValue);
    }
  };
  document.addEventListener('keydown', handler);
}

/* ── DOCUMENT BRANDING ─────────────────────────────────────────────────────
   What appears on a printed invoice, bill or voucher. Stored in
   `Tenant.settings.branding`, which the API already accepts as a writable key
   (WRITABLE_SETTING_KEYS in apps/tenancy/serializers.py).

   The logo is held as a data URI in that JSON rather than uploaded to object
   storage. That is a deliberate trade for this build: there is no file
   endpoint, and a small mark inline means the print path has no second
   network fetch to fail at exactly the wrong moment. It is also why the size
   is capped — settings JSON is read on every tenant load, so a 2 MB logo
   would tax every request in the product to decorate a page printed twice a
   month. Anything larger belongs behind a media endpoint.                   */
const BRAND_MAX_BYTES = 256 * 1024;

const brand = () => ((S.tenant && S.tenant.settings && S.tenant.settings.branding) || {});

VIEWS.branding = async () => {
  // Re-read rather than trusting the cached tenant: another admin may have
  // changed this since sign-in, and silently overwriting their footer with a
  // stale copy of yours is the kind of edit nobody can explain afterwards.
  try {
    // `/tenancy/current/` answers an *envelope* — {tenant, settings,
    // subscription, membership, ...} — not the tenant. Assigning the envelope
    // straight to S.tenant drops base_currency, country and name, and every
    // screen that reads them starts sending blanks: the first symptom is
    // "currency: This field is required" from an unrelated form.
    const fresh = await api('/api/v1/tenancy/current/');
    const t = fresh && (fresh.tenant || fresh);
    if (t && t.base_currency) { S.tenant = t; save(); }
  } catch { /* fall back to the cached tenant */ }

  const b = brand();
  V(`<div class="g2c">
    <div class="panel anim"><div class="ph"><h3>Identity</h3></div><div class="pb">
      <label>Company logo</label>
      <input type="file" id="b_logo" accept="image/png,image/jpeg,image/svg+xml"
             onchange="brandLogo(this)">
      <div class="note">PNG, JPEG or SVG, up to 256&nbsp;KB. Stored inline with
        your settings so printing never depends on a second request.</div>
      <div id="b_logo_prev" style="margin-top:10px">${b.logo
        ? `<img src="${esc(b.logo)}" alt="Logo" style="max-height:64px;max-width:220px">
           <button class="btn sec sm" style="margin-left:10px"
             onclick="brandClearLogo()">Remove</button>`
        : '<span class="note">No logo set.</span>'}</div>

      <label style="margin-top:18px">Watermark text</label>
      <input id="b_watermark" value="${esc(b.watermark || '')}"
             placeholder="e.g. DRAFT, COPY, PAID">
      <div class="note">Printed diagonally behind the document body. Leave blank
        for none.</div>

      <label style="margin-top:18px">Accent colour</label>
      <input id="b_colour" type="color" value="${esc(b.colour || '#4f46e5')}"
             style="height:40px;padding:4px">
      <div class="note">Used for the rule under the masthead and the document
        title. It does not change the app theme.</div>
    </div></div>

    <div class="panel anim d1"><div class="ph"><h3>Header &amp; footer</h3></div><div class="pb">
      <label>Header line</label>
      <input id="b_header" value="${esc(b.header || '')}"
             placeholder="Trading name, address, phone">
      <label style="margin-top:14px">Tax ID</label>
      <input id="b_tax" value="${esc(b.tax_id || '')}" placeholder="Tax registration number">
      <label style="margin-top:14px">Commercial register</label>
      <input id="b_cr" value="${esc(b.commercial_register || '')}" placeholder="CR number">
      <label style="margin-top:14px">Bank account details</label>
      <input id="b_bank" value="${esc(b.bank_details || '')}"
             placeholder="Bank · IBAN · SWIFT — shown on invoices so customers can pay">
      <label style="margin-top:14px">Terms &amp; conditions</label>
      <input id="b_terms" value="${esc(b.terms || '')}"
             placeholder="Payment terms, late-fee policy, jurisdiction">
      <label style="margin-top:14px">Footer line</label>
      <input id="b_footer" value="${esc(b.footer || '')}"
             placeholder="Thank you for your business">
    </div></div>
  </div>

  <div class="panel anim d2"><div class="ph"><h3>Preview</h3>
    <button class="btn" onclick="saveBranding()">Save branding</button></div>
    <div class="pb" id="b_prev">${brandPreview()}</div></div>

  <div class="note">These fields appear on printed invoices, bills and journal
    vouchers. Nothing here affects the ledger — it is presentation only, which
    is why it is editable after documents have been issued.</div>`);

  // Live preview. Cheap enough to re-render whole on every keystroke, and a
  // preview that lags behind the field it previews is worse than none.
  ['b_header','b_tax','b_cr','b_bank','b_terms','b_footer','b_watermark','b_colour']
    .forEach(id => { const el = document.getElementById(id);
      if (el) el.addEventListener('input', () => {
        const p = document.getElementById('b_prev');
        if (p) p.innerHTML = brandPreview(collectBranding()); }); });
};

function collectBranding() {
  const v = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  return {
    ...brand(),                     // keep the logo, which is not an input
    header: v('b_header'), tax_id: v('b_tax'),
    commercial_register: v('b_cr'), bank_details: v('b_bank'),
    terms: v('b_terms'), footer: v('b_footer'),
    watermark: v('b_watermark'), colour: v('b_colour') || '#4f46e5',
  };
}

function brandPreview(b) {
  b = b || brand();
  const c = esc(b.colour || '#4f46e5');
  return `<div style="border:1px solid var(--line);border-radius:var(--r);
      padding:18px;position:relative;overflow:hidden;background:var(--panel)">
    ${b.watermark ? `<div style="position:absolute;inset:0;display:flex;align-items:center;
      justify-content:center;font-size:52px;font-weight:800;opacity:.06;
      transform:rotate(-24deg);pointer-events:none;letter-spacing:.1em">
      ${esc(b.watermark)}</div>` : ''}
    <div style="display:flex;justify-content:space-between;align-items:flex-start;
        border-bottom:2px solid ${c};padding-bottom:10px;position:relative">
      <div style="display:flex;gap:12px;align-items:center">
        ${b.logo ? `<img src="${esc(b.logo)}" style="max-height:44px;max-width:150px">` : ''}
        <div>
          <div style="font-size:16px;font-weight:650">${esc(S.tenant.name || '')}</div>
          ${b.header ? `<div class="note" style="margin:2px 0 0">${esc(b.header)}</div>` : ''}
        </div>
      </div>
      <div style="text-align:right">
        <div style="font-size:19px;font-weight:650;color:${c}">Invoice</div>
        <div class="mono" style="font-size:12.5px">INV-2026-000123</div>
      </div>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:4px 24px;margin-top:10px;font-size:12px;
        color:var(--mut);position:relative">
      ${b.tax_id ? `<span>Tax ID: <b style="color:var(--fg)">${esc(b.tax_id)}</b></span>` : ''}
      ${b.commercial_register ? `<span>CR: <b style="color:var(--fg)">${esc(b.commercial_register)}</b></span>` : ''}
    </div>
    <div class="note" style="margin-top:22px;border-top:1px solid var(--line);padding-top:10px">
      ${b.bank_details ? `<div>${esc(b.bank_details)}</div>` : ''}
      ${b.terms ? `<div style="margin-top:4px">${esc(b.terms)}</div>` : ''}
      ${b.footer ? `<div style="margin-top:4px">${esc(b.footer)}</div>` : ''}
    </div>
  </div>`;
}

function brandLogo(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > BRAND_MAX_BYTES) {
    input.value = '';
    return toast(`Logo is ${Math.round(file.size / 1024)} KB; the limit is 256 KB`, 'bad');
  }
  const reader = new FileReader();
  reader.onload = () => {
    S.tenant.settings = S.tenant.settings || {};
    S.tenant.settings.branding = { ...brand(), logo: reader.result };
    document.getElementById('b_logo_prev').innerHTML =
      `<img src="${reader.result}" alt="Logo" style="max-height:64px;max-width:220px">
       <button class="btn sec sm" style="margin-left:10px" onclick="brandClearLogo()">Remove</button>`;
    document.getElementById('b_prev').innerHTML = brandPreview(collectBranding());
    toast('Logo loaded — press Save branding to keep it', 'ok');
  };
  reader.readAsDataURL(file);
}

function brandClearLogo() {
  S.tenant.settings = S.tenant.settings || {};
  S.tenant.settings.branding = { ...brand(), logo: '' };
  document.getElementById('b_logo_prev').innerHTML = '<span class="note">No logo set.</span>';
  document.getElementById('b_prev').innerHTML = brandPreview(collectBranding());
}

async function saveBranding() {
  const branding = collectBranding();
  try {
    // Send *only* the branding key. The serializer rejects any key outside
    // WRITABLE_SETTING_KEYS, and `Tenant.settings` also carries platform-owned
    // entries (`demo`, `payroll`) — so echoing the merged object back is
    // refused outright with "These settings are managed by the platform".
    // The server merges what it accepts into the existing blob, so the
    // platform keys survive untouched; they must not be in the request.
    const updated = await api('/api/v1/tenancy/current/', {
      method: 'PATCH', reauth: !!S.reauth,
      body: JSON.stringify({ settings: { branding } }),
    });
    const t = updated && (updated.tenant || updated);
    if (t && t.base_currency) S.tenant = t;
    save();
    toast('Branding saved', 'ok');
    go('branding');
  } catch (e) {
    if (e.code === 'reauth_required' && await ensureReauth()) return saveBranding();
    toast(e.message, 'bad');
  }
}
