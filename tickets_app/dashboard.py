"""Dashboard HTML autocontido (sem CDN): saúde, melhores combinações, gráficos de histórico."""
from __future__ import annotations

import html
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C, db, rules

_lock = threading.Lock()
_last_render = 0.0


def _data() -> dict:
    from .cli import cheapest_ever, health, latest_combos
    from .sources import get_sources
    db.init()
    h = health()
    with db.tx() as con:
        sweeps = [dict(r) for r in con.execute("SELECT * FROM sweeps ORDER BY id DESC LIMIT 200").fetchall()]
        sid, rows = latest_combos(con, 400)
        combos = [dict(r) for r in rows]
        ever = cheapest_ever(con)
        # menor preço válido por (direção, data) em cada varredura, últimos 14 dias
        legs = [dict(r) for r in con.execute("""SELECT o.sweep_id, s.started_at, o.direction, o.query_date, MIN(o.price) price
            FROM observations o JOIN sweeps s ON s.id=o.sweep_id WHERE o.valid=1 AND s.started_at >= datetime('now','-14 days')
            GROUP BY o.sweep_id, o.direction, o.query_date ORDER BY o.sweep_id""").fetchall()]
        cur = [dict(r) for r in con.execute("""SELECT o.* FROM current_fares cf JOIN observations o ON o.id=cf.observation_id
            ORDER BY o.direction, o.query_date, o.price""").fetchall()]
        srcs = []
        for s in get_sources(include_disabled=True):
            st = con.execute("SELECT * FROM source_state WHERE source=?", (s.name,)).fetchone()
            q = con.execute("""SELECT SUM(status IN ('ok','empty')) ok, SUM(status='error') err, SUM(status='blocked') blk
                               FROM queries WHERE source=? AND started_at >= datetime('now','-1 day')""", (s.name,)).fetchone()
            srcs.append(dict(name=s.name, label=s.label, kind=s.seller_kind, enabled=s.enabled, note=s.note,
                             state=dict(st) if st else None, ok=q["ok"] or 0, err=q["err"] or 0, blk=q["blk"] or 0))
        errors = [dict(r) for r in con.execute("""SELECT started_at, source, status, direction, query_date, origin, dest, error
            FROM queries WHERE status IN ('error','blocked') ORDER BY id DESC LIMIT 30""").fetchall()]
        alerts = [dict(r) for r in con.execute("SELECT id, combo_key, total, created_at, sent_ok, http_status, error, is_test FROM alerts ORDER BY id DESC LIMIT 30").fetchall()]
        promos = [dict(r) for r in con.execute("SELECT * FROM promos ORDER BY id DESC LIMIT 30").fetchall()]
        nobs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    return dict(generated_at=datetime.now().astimezone().isoformat(timespec="seconds"), target=C.PRICE_TARGET_BRL,
                health=dict(state=h["state"], sup=h["sup"], dmn=h["dmn"], age=h["age"], hb=h["hb"]),
                stale_s=C.HEARTBEAT_STALE_S, sweeps=sweeps, combo_sweep=sid, combos=combos,
                ever=dict(ever) if ever else None, legs=legs, current=cur, sources=srcs, errors=errors, alerts=alerts,
                promos=promos, nobs=nobs, windows=[[a.isoformat(), b.isoformat()] for a, b in C.WINDOWS],
                pairs=[[a.isoformat(), b.isoformat(), w] for a, b, w in rules.candidate_pairs()])


TEMPLATE = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="120">
<title>Passagens REC ⇄ RIO</title><style>
:root{--bg:#0f1419;--card:#182029;--fg:#e6edf3;--mut:#8b98a5;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px 22px;border-bottom:1px solid #2a3440;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 10px;color:var(--acc)}main{padding:18px 22px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.card{background:var(--card);border-radius:10px;padding:14px 16px;overflow:auto}.wide{grid-column:1/-1}
.kpis{display:flex;gap:12px;flex-wrap:wrap}.kpi{background:#0f1419;border-radius:8px;padding:10px 14px;min-width:150px}
.kpi b{display:block;font-size:20px}.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 7px;border-bottom:1px solid #26303b;text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:600}tr.hit td{background:#12301c}a{color:var(--acc)}.tag{font-size:11px;padding:1px 6px;border-radius:9px;background:#33404d}
.rev{background:#5a4200;color:#ffd77a}svg text{fill:var(--mut);font-size:11px}select{background:#0f1419;color:var(--fg);border:1px solid #33404d;padding:4px}
.hm td{padding:0;text-align:center;font-size:11px;width:44px;height:24px;border:1px solid #0f1419}
</style></head><body>
<header><h1>✈️ Passagens Recife ⇄ Rio (GIG/SDU)</h1><span class="mut" id="gen"></span></header>
<main>
<section class="card wide"><h2>Saúde</h2><div class="kpis" id="kpis"></div></section>
<section class="card wide"><h2>Melhores combinações atuais <span class="mut" id="csub"></span></h2><div id="combos"></div></section>
<section class="card"><h2>Melhor total por varredura</h2><div id="chart_best"></div></section>
<section class="card"><h2>Histórico de uma combinação</h2>
<div>Ida <select id="selo"></select> Volta <select id="selr"></select></div><div id="chart_pair"></div></section>
<section class="card wide"><h2>Mapa de preços (ida × volta) — total atual</h2><div id="heat"></div></section>
<section class="card"><h2>Trecho mais barato por data — IDA (atual)</h2><div id="bars_ida"></div></section>
<section class="card"><h2>Trecho mais barato por data — VOLTA (atual)</h2><div id="bars_volta"></div></section>
<section class="card wide"><h2>Fontes</h2><div id="sources"></div></section>
<section class="card wide"><h2>Varreduras recentes</h2><div id="sweeps"></div></section>
<section class="card"><h2>Erros e bloqueios recentes</h2><div id="errors"></div></section>
<section class="card"><h2>Alertas</h2><div id="alerts"></div></section>
<section class="card wide"><h2>Promoções em feeds (sinal)</h2><div id="promos"></div></section>
</main>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const brl=v=>v==null?'-':'R$ '+v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
const WD=['dom','seg','ter','qua','qui','sex','sáb'];
const fd=s=>{const d=new Date(s+'T12:00:00');return WD[d.getDay()]+' '+s.slice(8,10)+'/'+s.slice(5,7)};
const ft=s=>s?new Date(s).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'-';
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('gen').textContent='gerado em '+ft(D.generated_at)+' (horário de Brasília) · atualiza a cada 2 min · alvo ida+volta ≤ '+brl(D.target);
function tbl(h,rows,cls){return '<table><tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+rows.map((r,i)=>'<tr class="'+(cls?cls(i):'')+'">'+r.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</table>'}
// KPIs
const H=D.health,last=D.sweeps.find(s=>s.status!=='running'),run=D.sweeps.find(s=>s.status==='running');
const stc=H.state==='RODANDO'?'ok':'bad';
const k=[['Serviço','<span class="'+stc+'">'+H.state+'</span>'],['Heartbeat',H.age==null?'-':Math.round(H.age)+'s atrás'],
['Estado',H.hb?esc(H.hb.state)+(H.hb.next_sweep_at&&H.hb.state==='sleeping'?' · próx. '+ft(H.hb.next_sweep_at):''):'-'],
['Última varredura',last?'#'+last.id+' '+last.status+' · '+ft(last.finished_at):'-'],
['Consultas (última)',last?last.queries_ok+'/'+last.queries_total+' ok · '+last.queries_blocked+' bloq':'-'],
['Melhor atual',last?brl(last.best_total):'-'],['Mais barato já visto',D.ever?brl(D.ever.total)+' <span class="mut">'+ft(D.ever.computed_at)+'</span>':'-'],
['Observações',D.nobs.toLocaleString('pt-BR')]];
if(run)k.splice(3,0,['Em andamento','#'+run.id+' desde '+ft(run.started_at)]);
$('kpis').innerHTML=k.map(([a,b])=>'<div class="kpi"><span class="mut">'+a+'</span><b>'+b+'</b></div>').join('');
// combos
const rv=k=>k==='revenda_milhas'?' <span class="tag rev">revenda milhas</span>':'';
$('csub').textContent=D.combo_sweep?'(varredura #'+D.combo_sweep+', '+D.combos.length+' pares de datas)':'';
$('combos').innerHTML=D.combos.length?tbl(['#','Total','Dias','Ida','Voo ida','Volta','Voo volta'],D.combos.slice(0,25).map((c,i)=>[i+1,'<b>'+brl(c.total)+'</b>',c.stay_days,
 fd(c.out_date)+' '+c.odt+'→'+c.oat+' '+c.oo+'-'+c.od,'<a href="'+esc(c.ol)+'" target=_blank>'+esc(c.oa)+' '+esc(c.ofn)+'</a> '+brl(c.out_price)+' <span class="tag">'+esc(c.osl||c.os)+'</span>'+rv(c.osk),
 fd(c.ret_date)+' '+c.vdt+'→'+c.vat+' '+c.vo+'-'+c.vd,'<a href="'+esc(c.vl)+'" target=_blank>'+esc(c.va)+' '+esc(c.vfn)+'</a> '+brl(c.ret_price)+' <span class="tag">'+esc(c.vsl||c.vs)+'</span>'+rv(c.vsk)]),
 i=>D.combos[i].total<=D.target?'hit':''):'<span class="mut">ainda sem combinações</span>';
// gráfico de linha genérico
function line(series,opts){const W=560,Hh=220,p={l:56,r:10,t:10,b:28};let xs=[],ys=[];series.forEach(s=>s.pts.forEach(([x,y])=>{xs.push(x);ys.push(y)}));
 if(!xs.length)return '<span class="mut">sem dados ainda</span>';ys.push(D.target);let x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys)*0.95,y1=Math.max(...ys)*1.03;if(x1==x0)x1=x0+1;
 const X=x=>p.l+(x-x0)/(x1-x0)*(W-p.l-p.r),Y=y=>Hh-p.b-(y-y0)/(y1-y0)*(Hh-p.t-p.b);let g='';
 for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;g+='<line x1='+p.l+' x2='+(W-p.r)+' y1='+Y(v)+' y2='+Y(v)+' stroke="#26303b"/><text x=2 y='+(Y(v)+4)+'>'+Math.round(v)+'</text>'}
 for(let i=0;i<=3;i++){const v=x0+(x1-x0)*i/3;g+='<text x='+(X(v)-30)+' y='+(Hh-8)+'>'+ft(new Date(v).toISOString())+'</text>'}
 g+='<line x1='+p.l+' x2='+(W-p.r)+' y1='+Y(D.target)+' y2='+Y(D.target)+' stroke="#3fb950" stroke-dasharray="4 3"/><text x='+(W-90)+' y='+(Y(D.target)-4)+' style="fill:#3fb950">alvo '+D.target+'</text>';
 series.forEach(s=>{g+='<polyline fill=none stroke="'+s.c+'" stroke-width=2 points="'+s.pts.map(([x,y])=>X(x)+','+Y(y)).join(' ')+'"/>';s.pts.forEach(([x,y])=>g+='<circle r=2.5 fill="'+s.c+'" cx='+X(x)+' cy='+Y(y)+'><title>'+ft(new Date(x).toISOString())+' '+brl(y)+'</title></circle>')});
 return '<svg viewBox="0 0 '+W+' '+Hh+'" width="100%">'+g+'</svg>'+(series.length>1?'<div>'+series.map(s=>'<span style="color:'+s.c+'">■ '+s.n+'</span> ').join('')+'</div>':'')}
$('chart_best').innerHTML=line([{n:'melhor total',c:'#58a6ff',pts:D.sweeps.filter(s=>s.best_total!=null).map(s=>[Date.parse(s.started_at),s.best_total]).reverse()}]);
// histórico de um par (soma do trecho mais barato de cada data em cada varredura)
const L={};D.legs.forEach(r=>{(L[r.direction+r.query_date]??=[]).push(r)});
const outs=[...new Set(D.pairs.map(p=>p[0]))],selo=$('selo'),selr=$('selr');
selo.innerHTML=outs.map(o=>'<option>'+o+'</option>').join('');
function fillR(){const o=selo.value;selr.innerHTML=D.pairs.filter(p=>p[0]==o).map(p=>'<option>'+p[1]+'</option>').join('');drawPair()}
function drawPair(){const a=L['ida'+selo.value]||[],b=L['volta'+selr.value]||[],bm={};b.forEach(r=>bm[r.sweep_id]=r.price);
 const tot=a.filter(r=>bm[r.sweep_id]!=null).map(r=>[Date.parse(r.started_at),r.price+bm[r.sweep_id]]);
 $('chart_pair').innerHTML=line([{n:'total',c:'#58a6ff',pts:tot},{n:'ida',c:'#d29922',pts:a.map(r=>[Date.parse(r.started_at),r.price])},{n:'volta',c:'#a371f7',pts:b.map(r=>[Date.parse(r.started_at),r.price])}])}
selo.onchange=fillR;selr.onchange=drawPair;if(D.combos.length){selo.value=D.combos[0].out_date}fillR();if(D.combos.length){selr.value=D.combos[0].ret_date;drawPair()}
// heatmap
const cm={};D.combos.forEach(c=>cm[c.out_date+c.ret_date]=c.total);const rets=[...new Set(D.pairs.map(p=>p[1]))].sort();
const vals=D.combos.map(c=>c.total),mn=Math.min(...vals),mx=Math.max(...vals);
const col=v=>{if(v==null)return '#0f1419';const t=mx>mn?(v-mn)/(mx-mn):0;return v<=D.target?'#1f7a3a':'hsl('+(120-120*t)+',55%,'+(30+8*(1-t))+'%)'};
let hm='<table class="hm"><tr><th>ida \\ volta</th>'+rets.map(r=>'<th>'+r.slice(8,10)+'/'+r.slice(5,7)+'</th>').join('')+'</tr>';
outs.forEach(o=>{hm+='<tr><th>'+fd(o)+'</th>'+rets.map(r=>{const v=cm[o+r];return '<td style="background:'+col(v)+'" title="'+o+' → '+r+' '+brl(v)+'">'+(v==null?'':Math.round(v))+'</td>'}).join('')+'</tr>'});
$('heat').innerHTML=vals.length?hm+'</table>':'<span class="mut">sem dados</span>';
// barras por data
function bars(dir){const m={};D.current.filter(f=>f.direction==dir).forEach(f=>{if(!m[f.query_date]||f.price<m[f.query_date].price)m[f.query_date]=f});
 const ks=Object.keys(m).sort();if(!ks.length)return '<span class="mut">sem dados</span>';const mxv=Math.max(...ks.map(k=>m[k].price));
 return '<table>'+ks.map(k=>{const f=m[k];return '<tr><td>'+fd(k)+'</td><td style="width:55%"><div style="background:#58a6ff;height:12px;width:'+(100*f.price/mxv)+'%"></div></td><td>'+brl(f.price)+'</td><td class="mut">'+esc(f.airline_name)+' '+esc(f.flight_no)+' '+f.dep_time+' '+f.origin+'-'+f.dest+' · '+esc(f.seller||f.source)+'</td></tr>'}).join('')+'</table>'}
$('bars_ida').innerHTML=bars('ida');$('bars_volta').innerHTML=bars('volta');
// fontes
$('sources').innerHTML=tbl(['Fonte','Tipo','Status','24h ok/erro/bloq','Último sucesso','Último erro','Nota'],D.sources.map(s=>{const st=s.state||{};let t;
 if(!s.enabled)t='<span class="mut">desativada</span>';else if(st.cooldown_until&&Date.parse(st.cooldown_until)>Date.now())t='<span class="bad">backoff até '+ft(st.cooldown_until)+'</span>';
 else if(st.last_ok_at&&(!st.last_error_at||st.last_ok_at>=st.last_error_at))t='<span class="ok">ok</span>';else if(st.last_error)t='<span class="warn">falhando</span>';else t='<span class="mut">sem dados</span>';
 return [esc(s.label),s.kind,t,s.ok+' / '+s.err+' / '+s.blk,ft(st.last_ok_at),'<span title="'+esc(st.last_error)+'">'+esc((st.last_error||'').slice(0,60))+'</span>','<span class="mut" style="white-space:normal">'+esc(s.note)+'</span>']}));
$('sweeps').innerHTML=tbl(['#','Início','Fim','Status','ok/total','falhas','bloq','puladas','voos','válidos','comb.','melhor','alertas'],D.sweeps.slice(0,30).map(s=>['#'+s.id,ft(s.started_at),ft(s.finished_at),
 '<span class="'+(s.status=='ok'?'ok':s.status=='partial'||s.status=='running'?'warn':'bad')+'">'+s.status+'</span>',s.queries_ok+'/'+s.queries_total,s.queries_failed,s.queries_blocked,s.queries_skipped,s.flights_found,s.flights_valid,s.combos_valid,brl(s.best_total),s.alerts_sent]));
$('errors').innerHTML=D.errors.length?tbl(['Quando','Fonte','Tipo','Consulta','Erro'],D.errors.map(e=>[ft(e.started_at),esc(e.source),'<span class="'+(e.status=='blocked'?'bad':'warn')+'">'+e.status+'</span>',e.direction+' '+e.query_date+' '+e.origin+'-'+e.dest,'<span title="'+esc(e.error)+'">'+esc((e.error||'').slice(0,70))+'</span>'])):'<span class="ok">nenhum</span>';
$('alerts').innerHTML=D.alerts.length?tbl(['Quando','Total','Enviado','HTTP','Chave'],D.alerts.map(a=>[ft(a.created_at),brl(a.total)+(a.is_test?' <span class="tag">teste</span>':''),a.sent_ok?'<span class="ok">sim</span>':'<span class="bad">não</span> '+esc(a.error||''),a.http_status||'-','<span class="mut">'+esc(a.combo_key)+'</span>'])):'<span class="mut">nenhum ainda</span>';
$('promos').innerHTML=D.promos.length?tbl(['Visto em','Fonte','Título'],D.promos.map(p=>[ft(p.first_seen_at),esc(p.source),'<a href="'+esc(p.url)+'" target=_blank>'+esc(p.title)+'</a>'])):'<span class="mut">nenhuma promoção REC⇄RIO nos feeds ainda</span>';
</script></body></html>"""


def render() -> str:
    global _last_render
    with _lock:
        data = json.dumps(_data(), ensure_ascii=False, default=str).replace("</", "<\\/")
        C.DASH_DIR.mkdir(parents=True, exist_ok=True)
        out = C.DASH_DIR / "index.html"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
        tmp.replace(out)
        _last_render = time.time()
        return str(out)


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html", "/health"):
            self.send_error(404)
            return
        if self.path.startswith("/health"):
            from .cli import health
            h = health()
            body = json.dumps({"state": h["state"], "heartbeat_age_s": h["age"], "daemon_pid": h["dmn"]}).encode()
            ctype = "application/json"
        else:
            if time.time() - _last_render > 20:
                try:
                    render()
                except Exception as e:  # noqa: BLE001
                    body = f"erro ao gerar dashboard: {html.escape(str(e))}".encode()
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(body)
                    return
            body = (C.DASH_DIR / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def make_server(port: int | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port or C.DASHBOARD_PORT), _H)


def serve(port: int | None = None) -> None:
    srv = make_server(port)
    print(f"Dashboard em http://127.0.0.1:{srv.server_address[1]}/  (Ctrl+C para sair)")
    srv.serve_forever()
