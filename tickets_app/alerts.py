"""Alertas via webhook (acorda o Grok Bot, que avisa o Nicolas).

Dispara quando uma combinação válida com total <= PRICE_TARGET_BRL é nova ou
ficou mais barata do que da última vez que foi alertada (dedupe no banco).
Um POST por varredura com todas as novidades (ordenadas pelo total).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config as C, db
from .log import ev

RESELLERS = {"revenda_milhas"}


def combo_key(c: dict) -> str:
    # sem a fonte: o mesmo par de voos visto em outra fonte não gera alerta repetido
    return f"{c['out_date']}:{c['out']['flight_no']}|{c['ret_date']}:{c['ret']['flight_no']}"


def _leg(o: dict) -> dict:
    return {
        "data": o["query_date"], "partida": o["dep_time"], "chegada": o["arr_time"],
        "origem": o["origin"], "destino": o["dest"],
        "cia": o["airline_name"], "cia_codigo": o["airline_code"], "voo": o["flight_no"],
        "preco_brl": o["price"], "fonte": o["source"], "vendedor": o.get("seller") or o["source"],
        "tipo_vendedor": o["seller_kind"],
        "revenda_milhas": o["seller_kind"] in RESELLERS,
        "link": o["link"], "observado_em": o["observed_at"],
    }


def build_combo_payload(c: dict) -> dict:
    ida, volta = _leg(c["out"]), _leg(c["ret"])
    cheapest = ida if ida["preco_brl"] <= volta["preco_brl"] else volta
    flag = ida["revenda_milhas"] or volta["revenda_milhas"]
    return {
        "total_brl": c["total"], "dias_no_rio": c["stay_days"], "janela": c["window_idx"] + 1,
        "ida": ida, "volta": volta,
        "trecho_mais_barato_de_revenda_de_milhas": cheapest["revenda_milhas"],
        "algum_trecho_de_revenda_de_milhas": flag,
        "aviso": ("ATENÇÃO: trecho vendido por revenda de milhas (123milhas/MaxMilhas): risco maior, "
                  "confira regras e reputação antes de comprar.") if flag else None,
        "observado_em": max(ida["observado_em"], volta["observado_em"]),
    }


def post(payload: dict, lg, is_test: bool = False) -> tuple[bool, int | None, str | None]:
    url, auth = C.webhook_config()
    if not url:
        ev(lg, logging.WARNING, "TICKETS_WEBHOOK_URL ausente em .env: alerta NÃO enviado (monitor segue rodando)")
        return False, None, "TICKETS_WEBHOOK_URL ausente"
    headers = {"Content-Type": "application/json", "User-Agent": "tickets-monitor/1.0"}
    if auth:
        headers["Authorization"] = auth
    else:
        ev(lg, logging.WARNING, "TICKETS_WEBHOOK_AUTH ausente em .env: enviando sem Authorization")
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
            ev(lg, logging.INFO if ok else logging.ERROR, "webhook enviado", http=r.status, teste=is_test)
            return ok, r.status, None if ok else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        ev(lg, logging.ERROR, "webhook falhou", http=e.code, teste=is_test)
        return False, e.code, f"HTTP {e.code}"
    except Exception as e:
        ev(lg, logging.ERROR, "webhook falhou", erro=str(e), teste=is_test)
        return False, None, str(e)


def process(con, combos: list[dict], lg) -> int:
    hits = []
    for c in combos:
        if c["total"] > C.PRICE_TARGET_BRL:
            continue
        k = combo_key(c)
        last = con.execute("SELECT total FROM alerts WHERE combo_key=? AND sent_ok=1 AND is_test=0 ORDER BY id DESC LIMIT 1",
                           (k,)).fetchone()
        if last is None or c["total"] < last["total"] - 0.5:
            hits.append((k, c, None if last is None else last["total"]))
    if not hits:
        return 0
    items = []
    for k, c, prev in hits:
        p = build_combo_payload(c)
        p["motivo"] = "nova combinação" if prev is None else f"baixou de R$ {prev:.2f}"
        items.append(p)
    best = items[0]
    payload = {
        "tipo": "alerta_preco", "teste": False,
        "mensagem": f"{len(items)} combinação(ões) REC⇄RIO a até R$ {C.PRICE_TARGET_BRL:.0f}. Melhor: R$ {best['total_brl']:.2f}",
        "alvo_brl": C.PRICE_TARGET_BRL,
        **best,
        "combinacoes": items[:30],
        "total_combinacoes_novas": len(items),
    }
    ok, status, err = post(payload, lg)
    now = db.now_iso()
    for (k, c, _), p in zip(hits, items):
        con.execute("INSERT INTO alerts(combo_key, total, created_at, sent_ok, http_status, error, is_test, payload) VALUES(?,?,?,?,?,?,0,?)",
                    (k, c["total"], now, 1 if ok else 0, status, err, json.dumps(p, ensure_ascii=False)))
    ev(lg, logging.WARNING, "PREÇO-ALVO atingido", combinacoes=len(items), melhor=best["total_brl"], enviado=ok)
    return len(items) if ok else 0


def send_test(lg) -> tuple[bool, int | None, str | None, dict]:
    fake_leg = {"data": "2027-01-05", "partida": "08:20", "chegada": "11:15", "origem": "REC", "destino": "GIG",
                "cia": "TESTE", "cia_codigo": "XX", "voo": "XX0000", "preco_brl": 0.0, "fonte": "teste",
                "tipo_vendedor": "teste", "revenda_milhas": False, "link": "https://www.google.com/travel/flights",
                "observado_em": db.now_iso()}
    payload = {"tipo": "alerta_preco", "teste": True,
               "mensagem": "*** TESTE *** Isto é um alerta de TESTE do monitor de passagens. Ignore os preços.",
               "alvo_brl": C.PRICE_TARGET_BRL, "total_brl": 0.0, "dias_no_rio": 8, "janela": 1,
               "ida": fake_leg, "volta": {**fake_leg, "data": "2027-01-13", "origem": "GIG", "destino": "REC"},
               "trecho_mais_barato_de_revenda_de_milhas": False, "algum_trecho_de_revenda_de_milhas": False,
               "aviso": None, "observado_em": db.now_iso(), "combinacoes": [], "total_combinacoes_novas": 0}
    ok, status, err = post(payload, lg, is_test=True)
    db.init()
    with db.tx() as con:
        con.execute("INSERT INTO alerts(combo_key, total, created_at, sent_ok, http_status, error, is_test, payload) VALUES('TESTE',0,?,?,?,?,1,?)",
                    (db.now_iso(), 1 if ok else 0, status, err, json.dumps(payload, ensure_ascii=False)))
    return ok, status, err, payload
