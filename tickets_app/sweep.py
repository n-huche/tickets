"""Uma varredura completa: consulta todas as fontes/datas, grava tudo, calcula combinações e alerta."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import date, datetime, timedelta

from . import alerts, config as C, db, rules
from .log import ev
from .sources import SourceBlocked, SourceError, SourceNotFeasible, get_sources
from .sources.base import Source


class Progress:
    """Estado compartilhado para o heartbeat."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict = {"state": "idle"}

    def set(self, **kw) -> None:
        with self.lock:
            self.data.update(kw)

    def bump(self, source: str, key: str) -> None:
        with self.lock:
            s = self.data.setdefault("sources", {}).setdefault(source, {})
            s[key] = s.get(key, 0) + 1

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data, default=str))


def _source_state(con, name: str) -> dict:
    r = con.execute("SELECT * FROM source_state WHERE source=?", (name,)).fetchone()
    return dict(r) if r else {"source": name, "consecutive_blocks": 0, "cooldown_until": None}


def _save_state(con, st: dict) -> None:
    con.execute("""INSERT INTO source_state(source, consecutive_blocks, cooldown_until, last_ok_at,
                   last_error_at, last_error, status_note, last_run_at) VALUES(:source,:consecutive_blocks,:cooldown_until,
                   :last_ok_at,:last_error_at,:last_error,:status_note,:last_run_at)
                   ON CONFLICT(source) DO UPDATE SET consecutive_blocks=excluded.consecutive_blocks,
                   cooldown_until=excluded.cooldown_until, last_ok_at=excluded.last_ok_at,
                   last_error_at=excluded.last_error_at, last_error=excluded.last_error,
                   status_note=excluded.status_note, last_run_at=COALESCE(excluded.last_run_at, source_state.last_run_at)""",
                {"last_ok_at": None, "last_error_at": None, "last_error": None, "status_note": None, "last_run_at": None,
                 **st})


def _record_query(con, sweep_id, src: Source, direction, day, origin, dest, started, dur_ms, status,
                  http_status=None, n=0, error=None, url=None) -> int:
    cur = con.execute("""INSERT INTO queries(sweep_id, source, direction, query_date, origin, dest, started_at,
                         duration_ms, status, http_status, n_flights, error, url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sweep_id, src.name, direction, day, origin, dest, started, dur_ms, status, http_status, n,
                       error, url))
    return cur.lastrowid


def _store_flights(con, sweep_id, qid, src: Source, direction, day, origin, dest, flights) -> tuple[int, int]:
    now = db.now_iso()
    keep = set()
    n_valid = 0
    for f in flights:
        reason = rules.check_flight(f, day)
        if direction == "ida" and f.get("dest") and f["dest"] not in C.RIO_AIRPORTS:
            reason = reason or f"destino {f['dest']} fora do Rio"
        if direction == "volta" and f.get("origin") and f["origin"] not in C.RIO_AIRPORTS:
            reason = reason or f"origem {f['origin']} fora do Rio"
        if direction == "ida" and f.get("origin") and f["origin"] != C.ORIGIN:
            reason = reason or f"origem {f['origin']} não é {C.ORIGIN}"
        if direction == "volta" and f.get("dest") and f["dest"] != C.ORIGIN:
            reason = reason or f"destino {f['dest']} não é {C.ORIGIN}"
        cur = con.execute("""INSERT INTO observations(sweep_id, query_id, observed_at, source, seller_kind, direction,
                 query_date, origin, dest, airline_code, airline_name, flight_no, dep_date, dep_time, arr_date, arr_time,
                 duration_min, stops, price, currency, fare_note, link, valid, reject_reason, seller)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'BRL',?,?,?,?,?)""",
                          (sweep_id, qid, now, src.name, f.get("seller_kind") or src.seller_kind, direction, day, f.get("origin") or origin,
                           f.get("dest") or dest, f.get("airline_code"), f.get("airline_name"), f.get("flight_no"),
                           f.get("dep_date"), f.get("dep_time"), f.get("arr_date"), f.get("arr_time"),
                           f.get("duration_min"), f.get("stops"), f.get("price"), f.get("fare_note"), f.get("link"),
                           0 if reason else 1, reason, f.get("seller") or src.label))
        if reason is None:
            n_valid += 1
            keep.add(f["flight_no"])
            con.execute("""INSERT INTO current_fares(source, direction, query_date, origin, dest, flight_no,
                           observation_id, observed_at, price) VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(source, direction, query_date, origin, dest, flight_no) DO UPDATE SET
                           observation_id=excluded.observation_id, observed_at=excluded.observed_at, price=excluded.price""",
                        (src.name, direction, day, origin, dest, f["flight_no"], cur.lastrowid, now, f["price"]))
    # voos que sumiram da consulta (esgotados/removidos) saem das tarifas vigentes
    rows = con.execute("SELECT flight_no FROM current_fares WHERE source=? AND direction=? AND query_date=? AND origin=? AND dest=?",
                       (src.name, direction, day, origin, dest)).fetchall()
    for r in rows:
        if r["flight_no"] not in keep:
            con.execute("DELETE FROM current_fares WHERE source=? AND direction=? AND query_date=? AND origin=? AND dest=? AND flight_no=?",
                        (src.name, direction, day, origin, dest, r["flight_no"]))
    return len(flights), n_valid


def _run_source(src: Source, tasks, sweep_id, lg, prog: Progress, stop: threading.Event, totals: dict, tl: threading.Lock):
    con = db.connect()
    try:
        st = _source_state(con, src.name)
        cd = st.get("cooldown_until")
        if cd and datetime.fromisoformat(cd) > datetime.now().astimezone():
            ev(lg, logging.WARNING, "fonte em backoff, pulando", source=src.name, ate=cd)
            _record_query(con, sweep_id, src, "-", "-", "-", "-", db.now_iso(), 0, "skipped",
                          error=f"backoff até {cd} ({len(tasks)} consultas puladas)")
            con.commit()
            with tl:
                totals["skipped"] += len(tasks)
            return
        consecutive_err = 0
        for i, (direction, day, origin, dest) in enumerate(tasks):
            if stop.is_set():
                break
            prog.set(current={**prog.snapshot().get("current", {}), src.name: f"{direction} {day} {origin}-{dest}"})
            started = db.now_iso()
            t0 = time.monotonic()
            try:
                res = src.search(origin, dest, day)
                dur = int((time.monotonic() - t0) * 1000)
                status = "ok" if res.flights else "empty"
                qid = _record_query(con, sweep_id, src, direction, day, origin, dest, started, dur, status,
                                    res.http_status, len(res.flights), None, res.url)
                n, nv = _store_flights(con, sweep_id, qid, src, direction, day, origin, dest, res.flights)
                st.update(consecutive_blocks=0, cooldown_until=None, last_ok_at=db.now_iso())
                _save_state(con, st)
                con.commit()
                consecutive_err = 0
                with tl:
                    totals["ok"] += 1
                    totals["flights"] += n
                    totals["valid"] += nv
                prog.bump(src.name, "ok")
                ev(lg, logging.INFO, "consulta ok", source=src.name, dir=direction, data=day, rota=f"{origin}-{dest}",
                   voos=n, validos=nv, ms=dur)
            except SourceBlocked as e:
                dur = int((time.monotonic() - t0) * 1000)
                _record_query(con, sweep_id, src, direction, day, origin, dest, started, dur, "blocked", error=str(e))
                nblk = int(st.get("consecutive_blocks") or 0) + 1
                mins = min(random.uniform(*C.BLOCK_COOLDOWN_MIN) * (2 ** (nblk - 1)), 12 * 60)
                until = (datetime.now().astimezone() + timedelta(minutes=mins)).isoformat(timespec="seconds")
                st.update(consecutive_blocks=nblk, cooldown_until=until, last_error_at=db.now_iso(),
                          last_error=f"BLOQUEIO: {e}")
                _save_state(con, st)
                con.commit()
                with tl:
                    totals["blocked"] += 1
                    totals["skipped"] += len(tasks) - i - 1
                prog.bump(src.name, "blocked")
                ev(lg, logging.ERROR, "BLOQUEIO detectado; fonte em backoff", source=src.name, motivo=str(e),
                   bloqueios_seguidos=nblk, backoff_min=round(mins), ate=until)
                return
            except SourceNotFeasible as e:
                _record_query(con, sweep_id, src, direction, day, origin, dest, started, 0, "error", error=str(e))
                st.update(last_error_at=db.now_iso(), last_error=f"INVIÁVEL: {e}", status_note=str(e))
                _save_state(con, st)
                con.commit()
                with tl:
                    totals["failed"] += 1
                    totals["skipped"] += len(tasks) - i - 1
                ev(lg, logging.WARNING, "fonte inviável nesta varredura", source=src.name, motivo=str(e))
                return
            except Exception as e:  # SourceError e qualquer bug de parser: isolado por fonte
                dur = int((time.monotonic() - t0) * 1000)
                _record_query(con, sweep_id, src, direction, day, origin, dest, started, dur, "error", error=str(e)[:500])
                st.update(last_error_at=db.now_iso(), last_error=str(e)[:500])
                _save_state(con, st)
                con.commit()
                consecutive_err += 1
                with tl:
                    totals["failed"] += 1
                prog.bump(src.name, "error")
                ev(lg, logging.WARNING, "consulta falhou", source=src.name, dir=direction, data=day,
                   rota=f"{origin}-{dest}", erro=str(e)[:300], tipo=type(e).__name__)
                if consecutive_err >= 5:
                    with tl:
                        totals["skipped"] += len(tasks) - i - 1
                    ev(lg, logging.ERROR, "5 falhas seguidas; abandonando fonte nesta varredura", source=src.name)
                    return
            if i < len(tasks) - 1 and not stop.is_set():
                stop.wait(random.uniform(*src.delay_s))
    finally:
        try:
            src.close()
        except Exception:
            pass
        con.close()


def _run_feed(src: Source, sweep_id, lg, totals, tl):
    con = db.connect()
    started = db.now_iso()
    t0 = time.monotonic()
    try:
        items = src.fetch_feed()
        dur = int((time.monotonic() - t0) * 1000)
        _record_query(con, sweep_id, src, "feed", "-", "-", "-", started, dur, "ok" if items else "empty", n=len(items))
        new = 0
        for it in items:
            cur = con.execute("INSERT OR IGNORE INTO promos(source, url, title, published, price, first_seen_at) VALUES(?,?,?,?,?,?)",
                              (src.name, it.url, it.title, it.published, it.price, db.now_iso()))
            new += cur.rowcount
        st = _source_state(con, src.name)
        st.update(last_ok_at=db.now_iso(), consecutive_blocks=0, cooldown_until=None)
        _save_state(con, st)
        con.commit()
        with tl:
            totals["ok"] += 1
        ev(lg, logging.INFO, "feed de promoções lido", source=src.name, itens_relevantes=len(items), novos=new)
    except Exception as e:
        dur = int((time.monotonic() - t0) * 1000)
        _record_query(con, sweep_id, src, "feed", "-", "-", "-", started, dur,
                      "blocked" if isinstance(e, SourceBlocked) else "error", error=str(e)[:500])
        st = _source_state(con, src.name)
        st.update(last_error_at=db.now_iso(), last_error=str(e)[:500])
        _save_state(con, st)
        con.commit()
        with tl:
            totals["failed"] += 1
        ev(lg, logging.WARNING, "feed falhou", source=src.name, erro=str(e)[:300])
    finally:
        con.close()


def compute_combos(con, sweep_id: int) -> list[dict]:
    """Melhor ida + melhor volta para cada par de datas válido (pode misturar cias e fontes)."""
    cutoff = (datetime.now().astimezone() - timedelta(hours=C.FARE_MAX_AGE_H)).isoformat(timespec="seconds")
    rows = con.execute("""SELECT cf.direction, cf.query_date, cf.price, o.* FROM current_fares cf
                          JOIN observations o ON o.id = cf.observation_id
                          WHERE cf.observed_at >= ? ORDER BY cf.price ASC""", (cutoff,)).fetchall()
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["direction"], r["query_date"])
        if k not in best:
            best[k] = dict(r)
    combos = []
    now = db.now_iso()
    for d, r, wi in rules.candidate_pairs():
        o = best.get(("ida", d.isoformat()))
        v = best.get(("volta", r.isoformat()))
        if not o or not v:
            continue
        c = dict(sweep_id=sweep_id, computed_at=now, out_date=d.isoformat(), ret_date=r.isoformat(),
                 stay_days=(r - d).days, window_idx=wi, out_obs_id=o["id"], ret_obs_id=v["id"],
                 out_price=o["price"], ret_price=v["price"], total=round(o["price"] + v["price"], 2))
        con.execute("""INSERT INTO combos(sweep_id, computed_at, out_date, ret_date, stay_days, window_idx, out_obs_id,
                       ret_obs_id, out_price, ret_price, total) VALUES(:sweep_id,:computed_at,:out_date,:ret_date,
                       :stay_days,:window_idx,:out_obs_id,:ret_obs_id,:out_price,:ret_price,:total)""", c)
        c["out"], c["ret"] = o, v
        combos.append(c)
    combos.sort(key=lambda c: c["total"])
    return combos


def run_sweep(lg, stop: threading.Event | None = None, prog: Progress | None = None) -> int:
    stop = stop or threading.Event()
    prog = prog or Progress()
    db.init()
    with db.tx() as con:
        sweep_id = con.execute("INSERT INTO sweeps(started_at) VALUES(?)", (db.now_iso(),)).lastrowid
    outs, rets = rules.candidate_dates()
    sources = get_sources()
    plan = {}
    for s in sources:
        if s.mode != "search":
            continue
        tasks = []
        for d in outs:
            for rio in s.rio_codes():
                tasks.append(("ida", d.isoformat(), C.ORIGIN, rio))
        for d in rets:
            for rio in s.rio_codes():
                tasks.append(("volta", d.isoformat(), rio, C.ORIGIN))
        random.shuffle(tasks)  # ordem aleatória: menos padrão de robô
        if s.max_queries_per_sweep and len(tasks) > s.max_queries_per_sweep:
            # fontes lentas (navegador): a cada varredura, só as N consultas mais "velhas" desta fonte.
            # Em ~3 varreduras todas as datas são cobertas; tarifas valem por FARE_MAX_AGE_H.
            with db.tx() as con:
                last = {(r[0], r[1], r[2], r[3]): r[4] for r in con.execute(
                    """SELECT direction, query_date, origin, dest, MAX(started_at) FROM queries
                       WHERE source=? AND status IN ('ok','empty') GROUP BY 1,2,3,4""", (s.name,))}
            tasks.sort(key=lambda t: last.get(t) or "")
            tasks = tasks[:s.max_queries_per_sweep]
        plan[s.name] = (s, tasks)
    total_q = sum(len(t) for _, t in plan.values())
    prog.set(state="sweeping", sweep_id=sweep_id, sweep_started=db.now_iso(), queries_planned=total_q, sources={},
             current={})
    ev(lg, logging.INFO, "varredura iniciada", sweep=sweep_id, datas_ida=len(outs), datas_volta=len(rets),
       consultas_planejadas=total_q, fontes=",".join(s.name for s in sources))

    totals = dict(ok=0, failed=0, blocked=0, skipped=0, flights=0, valid=0)
    tl = threading.Lock()
    threads = [threading.Thread(target=_run_source, args=(s, t, sweep_id, lg, prog, stop, totals, tl),
                                name=f"src-{s.name}", daemon=True) for s, t in plan.values()]
    threads += [threading.Thread(target=_run_feed, args=(s, sweep_id, lg, totals, tl), name=f"feed-{s.name}",
                                 daemon=True) for s in sources if s.mode == "feed"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with db.tx() as con:
        combos = compute_combos(con, sweep_id)
        best = combos[0] if combos else None
        n_alerts = 0
        try:
            n_alerts = alerts.process(con, combos, lg)
        except Exception as e:
            ev(lg, logging.ERROR, "falha ao processar alertas", erro=str(e))
        if stop.is_set():
            status = "interrupted"
        elif totals["ok"] == 0:
            status = "failed"
        elif totals["failed"] or totals["blocked"] or totals["skipped"]:
            status = "partial"
        else:
            status = "ok"
        best_desc = None
        if best:
            best_desc = (f"{best['out_date']} {best['out']['flight_no']} {best['out']['dep_time']} R${best['out_price']:.0f}"
                         f" + {best['ret_date']} {best['ret']['flight_no']} {best['ret']['dep_time']} R${best['ret_price']:.0f}")
        con.execute("""UPDATE sweeps SET finished_at=?, status=?, queries_total=?, queries_ok=?, queries_failed=?,
                       queries_blocked=?, queries_skipped=?, flights_found=?, flights_valid=?, combos_valid=?,
                       best_total=?, best_combo=?, alerts_sent=? WHERE id=?""",
                    (db.now_iso(), status, total_q, totals["ok"], totals["failed"], totals["blocked"],
                     totals["skipped"], totals["flights"], totals["valid"], len(combos),
                     best["total"] if best else None, best_desc, n_alerts, sweep_id))
    ev(lg, logging.INFO if status in ("ok", "partial") else logging.ERROR, "varredura finalizada", sweep=sweep_id,
       status=status, ok=totals["ok"], falhas=totals["failed"], bloqueios=totals["blocked"],
       puladas=totals["skipped"], voos=totals["flights"], validos=totals["valid"], combinacoes=len(combos),
       melhor=(best["total"] if best else None), alertas=n_alerts)
    try:
        from . import dashboard
        dashboard.render()
    except Exception as e:
        ev(lg, logging.ERROR, "falha ao gerar dashboard", erro=str(e))
    prog.set(state="idle", last_sweep_id=sweep_id, last_sweep_status=status, current={})
    return sweep_id
