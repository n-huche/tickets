"""CLI do monitor: ./tickets <comando>. Use ./tickets ajuda para a lista."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

from . import config as C, db, rules

SUP = C.ROOT / "bin" / "supervisor.sh"
SUP_PID = C.DATA_DIR / "supervisor.pid"
DAEMON_PID = C.DATA_DIR / "daemon.pid"

B, R, G, Y, D, X = "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    B = R = G = Y = D = X = ""

WD = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def fdate(s: str) -> str:
    d = datetime.fromisoformat(s).date()
    return f"{WD[d.weekday()]} {d.strftime('%d/%m')}"


def ftime(s: str | None) -> str:
    if not s:
        return "-"
    return datetime.fromisoformat(s).astimezone().strftime("%d/%m %H:%M:%S")


def ago(s: str | None) -> str:
    if not s:
        return "-"
    sec = time.time() - datetime.fromisoformat(s).timestamp()
    if sec < 90:
        return f"há {int(sec)}s"
    if sec < 5400:
        return f"há {int(sec/60)}min"
    if sec < 172800:
        return f"há {sec/3600:.1f}h"
    return f"há {sec/86400:.1f}d"


def brl(v) -> str:
    return "-" if v is None else f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pid_alive(p) -> int | None:
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def heartbeat() -> dict | None:
    try:
        return json.loads(C.HEARTBEAT_PATH.read_text())
    except Exception:
        return None


def health() -> dict:
    hb = heartbeat()
    sup = _pid_alive(SUP_PID)
    dmn = _pid_alive(DAEMON_PID)
    age = time.time() - hb["heartbeat_epoch"] if hb and hb.get("heartbeat_epoch") else None
    if not sup and not dmn:
        state, color = "PARADO", R
    elif age is None or age > C.HEARTBEAT_STALE_S:
        state, color = "TRAVADO? (heartbeat atrasado)", R
    else:
        state, color = "RODANDO", G
    return dict(hb=hb, sup=sup, dmn=dmn, age=age, state=state, color=color)


def leg_str(o) -> str:
    rev = f" {Y}[REVENDA MILHAS]{X}" if o["seller_kind"] == "revenda_milhas" else ""
    return (f"{fdate(o['query_date'])} {o['dep_time']}→{o['arr_time']} {o['origin']}-{o['dest']} "
            f"{o['airline_name']} {o['flight_no']} {brl(o['price'])} ({o.get('seller') or o['source']}){rev}")


def latest_combos(con, limit=10):
    sw = con.execute("SELECT id FROM sweeps WHERE combos_valid>0 ORDER BY id DESC LIMIT 1").fetchone()
    if not sw:
        return None, []
    rows = con.execute("""SELECT c.*, o.source os, o.seller_kind osk, o.airline_name oa, o.flight_no ofn, o.dep_time odt,
        o.arr_time oat, o.origin oo, o.dest od, o.link ol, o.seller osl, v.source vs, v.seller_kind vsk, v.airline_name va,
        v.flight_no vfn, v.dep_time vdt, v.arr_time vat, v.origin vo, v.dest vd, v.link vl, v.seller vsl
        FROM combos c JOIN observations o ON o.id=c.out_obs_id JOIN observations v ON v.id=c.ret_obs_id
        WHERE c.sweep_id=? ORDER BY c.total LIMIT ?""", (sw["id"], limit)).fetchall()
    return sw["id"], rows


def _leg_from_row(r, p):
    return dict(query_date=r["out_date" if p == "o" else "ret_date"], dep_time=r[p + "dt"], arr_time=r[p + "at"],
                origin=r[p + "o"], dest=r[p + "d"], airline_name=r[p + "a"], flight_no=r[p + "fn"],
                price=r["out_price" if p == "o" else "ret_price"], source=r[p + "s"], seller_kind=r[p + "sk"],
                seller=r[p + "sl"])


def print_combos(rows, links=False):
    for i, r in enumerate(rows, 1):
        mark = f"{G}★{X}" if r["total"] <= C.PRICE_TARGET_BRL else " "
        print(f"{mark}{i:>2}. {B}{brl(r['total'])}{X}  {r['stay_days']} dias  (janela {r['window_idx']+1})")
        print(f"     ida:   {leg_str(_leg_from_row(r, 'o'))}")
        print(f"     volta: {leg_str(_leg_from_row(r, 'v'))}")
        if links:
            print(f"     {D}{r['ol']}{X}\n     {D}{r['vl']}{X}")


def cmd_status(a):
    db.init()
    h = health()
    hb = h["hb"] or {}
    print(f"{B}Monitor de passagens REC ⇄ RIO{X}   alvo: ida+volta ≤ {brl(C.PRICE_TARGET_BRL)}")
    print(f"Serviço: {h['color']}{B}{h['state']}{X}  supervisor pid={h['sup'] or '-'}  daemon pid={h['dmn'] or '-'}")
    if hb:
        print(f"Heartbeat: {ftime(hb.get('heartbeat_at'))} ({int(h['age'])}s atrás)  estado={hb.get('state')}"
              + (f"  próxima varredura {ftime(hb.get('next_sweep_at'))}" if hb.get("state") == "sleeping" else ""))
        if hb.get("state") == "sweeping":
            cur = hb.get("current") or {}
            done = sum(v.get("ok", 0) + v.get("error", 0) + v.get("blocked", 0) for v in (hb.get("sources") or {}).values())
            print(f"  varredura #{hb.get('sweep_id')} em andamento: {done}/{hb.get('queries_planned')} consultas  "
                  + "  ".join(f"{k}: {v}" for k, v in cur.items()))
    with db.tx() as con:
        s = con.execute("SELECT * FROM sweeps WHERE status!='running' ORDER BY id DESC LIMIT 1").fetchone()
        if s:
            col = G if s["status"] == "ok" else (Y if s["status"] == "partial" else R)
            print(f"\nÚltima varredura #{s['id']}: {col}{s['status']}{X}  {ftime(s['started_at'])} → {ftime(s['finished_at'])} ({ago(s['finished_at'])})")
            print(f"  consultas ok={s['queries_ok']}/{s['queries_total']} falhas={s['queries_failed']} bloqueios={s['queries_blocked']} puladas={s['queries_skipped']}")
            print(f"  voos encontrados={s['flights_found']} válidos={s['flights_valid']} combinações={s['combos_valid']} alertas={s['alerts_sent']}")
            print(f"  melhor: {B}{brl(s['best_total'])}{X}  {s['best_combo'] or ''}")
        else:
            print("\nNenhuma varredura concluída ainda.")
        n = con.execute("SELECT COUNT(*) n, MIN(observed_at) a FROM observations").fetchone()
        print(f"\nObservações gravadas: {n['n']} (desde {ftime(n['a'])})  varreduras: "
              f"{con.execute('SELECT COUNT(*) FROM sweeps').fetchone()[0]}")
        ever = cheapest_ever(con)
        if ever:
            print(f"Mais barato já visto (combinação): {B}{brl(ever['total'])}{X} em {ftime(ever['computed_at'])}: "
                  f"ida {fdate(ever['out_date'])} {ever['ofn']} + volta {fdate(ever['ret_date'])} {ever['vfn']}")
        print(f"\n{B}Fontes{X}")
        print_sources(con)
        errs = con.execute("SELECT * FROM queries WHERE status IN ('error','blocked') ORDER BY id DESC LIMIT 5").fetchall()
        if errs:
            print(f"\n{B}Últimos erros/bloqueios{X}")
            for e in errs:
                print(f"  {ftime(e['started_at'])} {e['source']} {e['status']} {e['direction']} {e['query_date']} {e['origin']}-{e['dest']}: {(e['error'] or '')[:120]}")
        al = con.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 1").fetchone()
        url, auth = C.webhook_config()
        print(f"\nWebhook: URL {'configurada' if url else R+'NÃO configurada'+X}, Authorization {'configurado' if auth else R+'NÃO configurado'+X} ({C.ENV_PATH})")
        if al:
            print(f"Último alerta: {ftime(al['created_at'])} total={brl(al['total'])} enviado={'sim' if al['sent_ok'] else 'não'} {'(teste)' if al['is_test'] else ''} {al['error'] or ''}")
    print(f"\nDashboard: {C.DASH_DIR / 'index.html'}  |  http://127.0.0.1:{C.DASHBOARD_PORT}/ (túnel: ssh -L {C.DASHBOARD_PORT}:127.0.0.1:{C.DASHBOARD_PORT} ...)")


def cheapest_ever(con):
    return con.execute("""SELECT c.*, o.flight_no ofn, v.flight_no vfn FROM combos c JOIN observations o ON o.id=c.out_obs_id
                          JOIN observations v ON v.id=c.ret_obs_id ORDER BY c.total, c.id LIMIT 1""").fetchone()


def print_sources(con):
    from .sources import get_sources
    for s in get_sources(include_disabled=True):
        st = con.execute("SELECT * FROM source_state WHERE source=?", (s.name,)).fetchone()
        q = con.execute("""SELECT SUM(status IN ('ok','empty')) ok, SUM(status='error') err, SUM(status='blocked') blk, COUNT(*) n
                           FROM queries WHERE source=? AND started_at >= datetime('now','-1 day')""", (s.name,)).fetchone()
        if not s.enabled:
            tag = f"{D}desativada{X}"
        elif st and st["cooldown_until"] and datetime.fromisoformat(st["cooldown_until"]) > datetime.now().astimezone():
            tag = f"{R}backoff até {ftime(st['cooldown_until'])}{X}"
        elif st and st["last_ok_at"]:
            tag = f"{G}ok{X} (último sucesso {ago(st['last_ok_at'])})"
        elif st and st["last_error"]:
            tag = f"{R}falhando{X}"
        else:
            tag = "sem dados"
        print(f"  {s.label:<22} [{s.seller_kind:<14}] {tag}  24h: ok={q['ok'] or 0} erro={q['err'] or 0} bloqueio={q['blk'] or 0}")
        if not s.enabled and s.note:
            print(f"  {D}{'':<22}  ↳ {s.note}{X}")
        elif st and st["last_error"] and (not st["last_ok_at"] or st["last_error_at"] > st["last_ok_at"]):
            print(f"  {D}{'':<22}  ↳ último erro {ago(st['last_error_at'])}: {st['last_error'][:110]}{X}")


def cmd_best(a):
    db.init()
    with db.tx() as con:
        sid, rows = latest_combos(con, a.n)
        if not rows:
            print("Ainda não há combinações calculadas.")
            return
        print(f"{B}Melhores combinações atuais (varredura #{sid}){X}  ★ = dentro do alvo {brl(C.PRICE_TARGET_BRL)}\n")
        print_combos(rows, links=a.links)


def cmd_history(a):
    db.init()
    with db.tx() as con:
        if a.ida and a.volta:
            rows = con.execute("""SELECT s.started_at, c.total, c.out_price, c.ret_price FROM combos c JOIN sweeps s ON s.id=c.sweep_id
                                  WHERE c.out_date=? AND c.ret_date=? ORDER BY c.id DESC LIMIT ?""", (a.ida, a.volta, a.n)).fetchall()
            print(f"{B}Histórico da combinação ida {a.ida} / volta {a.volta}{X} (mais recente primeiro)")
            for r in rows:
                print(f"  {ftime(r['started_at'])}  total {brl(r['total'])}  (ida {brl(r['out_price'])} + volta {brl(r['ret_price'])})")
            if not rows:
                print("  sem dados (datas fora das regras ou ainda não vistas)")
            return
        if a.ida or a.volta:
            direction, day = ("ida", a.ida) if a.ida else ("volta", a.volta)
            rows = con.execute("""SELECT observed_at, source, airline_name, flight_no, dep_time, arr_time, origin, dest, price, valid, reject_reason
                                  FROM observations WHERE direction=? AND query_date=? ORDER BY id DESC LIMIT ?""",
                               (direction, day, a.n)).fetchall()
            print(f"{B}Observações de {direction} em {day}{X} (mais recente primeiro)")
            for r in rows:
                v = "" if r["valid"] else f" {D}[rejeitado: {r['reject_reason']}]{X}"
                print(f"  {ftime(r['observed_at'])} {r['source']:<14} {r['airline_name'] or '':<6} {r['flight_no']:<8} {r['origin']}-{r['dest']} {r['dep_time']}→{r['arr_time']} {brl(r['price'])}{v}")
            return
        rows = con.execute("SELECT id, started_at, status, best_total, best_combo, combos_valid FROM sweeps ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()
        print(f"{B}Melhor total por varredura{X}")
        for r in rows:
            print(f"  #{r['id']:<5} {ftime(r['started_at'])} {r['status']:<11} {brl(r['best_total']):>12}  {r['best_combo'] or ''}")
        ever = cheapest_ever(con)
        if ever:
            print(f"\nMais barato já visto: {brl(ever['total'])} ({ftime(ever['computed_at'])}) ida {ever['out_date']} {ever['ofn']} / volta {ever['ret_date']} {ever['vfn']}")


def cmd_sweeps(a):
    db.init()
    with db.tx() as con:
        for s in con.execute("SELECT * FROM sweeps ORDER BY id DESC LIMIT ?", (a.n,)).fetchall():
            dur = "-"
            if s["finished_at"]:
                dur = f"{(datetime.fromisoformat(s['finished_at']) - datetime.fromisoformat(s['started_at'])).seconds // 60}min"
            print(f"#{s['id']:<5} {ftime(s['started_at'])} {dur:>6} {s['status']:<11} ok={s['queries_ok']}/{s['queries_total']} "
                  f"falha={s['queries_failed']} bloq={s['queries_blocked']} pul={s['queries_skipped']} voos={s['flights_found']} "
                  f"válidos={s['flights_valid']} comb={s['combos_valid']} melhor={brl(s['best_total'])} alertas={s['alerts_sent']}")


def cmd_sources(a):
    db.init()
    with db.tx() as con:
        print_sources(con)
        from .sources import get_sources
        print()
        for s in get_sources(include_disabled=True):
            print(f"{B}{s.label}{X} ({s.name}, {s.seller_kind}, {'ativa' if s.enabled else 'desativada'}): {s.note}")


def cmd_alerts(a):
    db.init()
    with db.tx() as con:
        for r in con.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (a.n,)).fetchall():
            print(f"{ftime(r['created_at'])} {'TESTE ' if r['is_test'] else ''}{brl(r['total'])} enviado={'sim' if r['sent_ok'] else 'não'} "
                  f"http={r['http_status'] or '-'} {r['combo_key']} {r['error'] or ''}")


def cmd_promos(a):
    db.init()
    with db.tx() as con:
        for r in con.execute("SELECT * FROM promos ORDER BY id DESC LIMIT ?", (a.n,)).fetchall():
            print(f"{ftime(r['first_seen_at'])} [{r['source']}] {r['title']}\n    {D}{r['url']}{X}")


def cmd_logs(a):
    path = C.LOG_DIR / ("tickets.jsonl" if a.json else "tickets.log")
    if not path.exists():
        print("sem logs ainda")
        return
    if a.follow:
        os.execvp("tail", ["tail", "-n", str(a.n), "-F", str(path)])
    with open(path, encoding="utf-8") as f:
        lines = deque(f, maxlen=a.n * 20 if a.errors else a.n)
    if a.errors:
        lines = [l for l in lines if any(k in l for k in ("WARNING", "ERROR", '"level": "WARN', '"level": "ERR'))][-a.n:]
    sys.stdout.writelines(lines)


def cmd_test_alert(a):
    from .alerts import send_test
    from .log import get
    ok, status, err, payload = send_test(get())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nResultado: {'ENVIADO' if ok else 'NÃO enviado'} http={status} {err or ''}")


def _sweep_lock():
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(C.DATA_DIR / "sweep.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Já existe uma varredura rodando (serviço). Veja ./tickets status.")
        sys.exit(1)
    return f


def cmd_sweep(a):
    from .log import get
    from .sweep import run_sweep
    if health()["dmn"]:
        print(f"{Y}Aviso: o serviço está rodando; uma varredura manual dobra o volume de consultas.{X}")
    lk = _sweep_lock()  # noqa: F841
    sid = run_sweep(get(stdout=True))
    print()
    a.n = 5
    a.links = False
    cmd_best(a)
    print(f"\nVarredura #{sid} concluída.")


def cmd_dashboard(a):
    from . import dashboard
    p = dashboard.render()
    print(p)


def cmd_serve(a):
    from . import dashboard
    dashboard.serve(port=a.port)


def _sup_lock_free() -> bool:
    """True se nenhum supervisor segura data/supervisor.lock (flock)."""
    import fcntl
    try:
        with open(C.DATA_DIR / "supervisor.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def cmd_service(a):
    act = a.action
    if act in ("start", "ensure"):
        if _pid_alive(SUP_PID) or not _sup_lock_free():
            print("serviço já está rodando" if act == "start" else "", end="\n" if act == "start" else "")
            return
        C.LOG_DIR.mkdir(exist_ok=True)
        subprocess.Popen(["setsid", "nohup", str(SUP)], stdout=open(C.LOG_DIR / "supervisor.out", "a"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(C.ROOT), start_new_session=True)
        time.sleep(3)
        print("serviço iniciado" if _pid_alive(SUP_PID) else "falha ao iniciar; veja logs/supervisor.out")
    elif act == "stop":
        sp = _pid_alive(SUP_PID)
        if sp:
            os.kill(sp, 15)
        dp = _pid_alive(DAEMON_PID)
        if dp:
            os.kill(dp, 15)
        for _ in range(40):
            if not _pid_alive(SUP_PID) and not _pid_alive(DAEMON_PID) and _sup_lock_free():
                break
            time.sleep(1)
        dp = _pid_alive(DAEMON_PID)
        if dp:
            os.kill(dp, 9)
        print("serviço parado")
    elif act == "restart":
        a.action = "stop"
        cmd_service(a)
        a.action = "start"
        cmd_service(a)
    else:
        h = health()
        print(f"{h['color']}{h['state']}{X} supervisor={h['sup'] or '-'} daemon={h['dmn'] or '-'} heartbeat={int(h['age']) if h['age'] is not None else '-'}s")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tickets", description="Monitor de passagens REC ⇄ Rio (GIG/SDU)")
    sp = p.add_subparsers(dest="cmd")
    sp.add_parser("status", help="saúde do serviço, última varredura, melhor preço, fontes e erros")
    x = sp.add_parser("best", help="melhores combinações atuais")
    x.add_argument("-n", type=int, default=10)
    x.add_argument("--links", action="store_true", help="mostra os links de busca")
    x = sp.add_parser("history", help="histórico: geral, de uma data (--ida/--volta) ou de um par (--ida X --volta Y)")
    x.add_argument("--ida")
    x.add_argument("--volta")
    x.add_argument("-n", type=int, default=30)
    x = sp.add_parser("sweeps", help="lista as varreduras")
    x.add_argument("-n", type=int, default=20)
    sp.add_parser("sources", help="status de cada fonte")
    x = sp.add_parser("alerts", help="alertas enviados")
    x.add_argument("-n", type=int, default=20)
    x = sp.add_parser("promos", help="promoções vistas nos feeds (sinal)")
    x.add_argument("-n", type=int, default=20)
    x = sp.add_parser("logs", help="logs (use -f para acompanhar, --errors, --json)")
    x.add_argument("-n", type=int, default=50)
    x.add_argument("-f", "--follow", action="store_true")
    x.add_argument("--errors", action="store_true")
    x.add_argument("--json", action="store_true")
    sp.add_parser("test-alert", help="envia um webhook de TESTE")
    sp.add_parser("sweep", help="roda uma varredura agora (em primeiro plano)")
    sp.add_parser("dashboard", help="gera dashboard/index.html")
    x = sp.add_parser("serve", help="servidor local do dashboard (o serviço já sobe um)")
    x.add_argument("--port", type=int, default=C.DASHBOARD_PORT + 1)
    x = sp.add_parser("service", help="start | stop | restart | status | ensure")
    x.add_argument("action", choices=["start", "stop", "restart", "status", "ensure"])
    sp.add_parser("daemon", help="(interno) loop contínuo")
    a = p.parse_args(argv)
    if a.cmd is None:
        p.print_help()
        return
    if a.cmd == "daemon":
        from .daemon import main as dmain
        return dmain()
    {"status": cmd_status, "best": cmd_best, "history": cmd_history, "sweeps": cmd_sweeps, "sources": cmd_sources,
     "alerts": cmd_alerts, "promos": cmd_promos, "logs": cmd_logs, "test-alert": cmd_test_alert, "sweep": cmd_sweep,
     "dashboard": cmd_dashboard, "serve": cmd_serve, "service": cmd_service}[a.cmd](a)


if __name__ == "__main__":
    main()
