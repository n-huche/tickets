"""Loop contínuo: varredura a cada ~30 min (com jitter) + heartbeat a cada 15 s."""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timedelta

from . import config as C, db
from .log import ev, get
from .sweep import Progress, run_sweep


def _heartbeat_loop(prog: Progress, stop: threading.Event):
    while not stop.is_set():
        snap = prog.snapshot()
        snap.update(pid=os.getpid(), heartbeat_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    heartbeat_epoch=time.time())
        tmp = C.HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1, default=str))
        os.replace(tmp, C.HEARTBEAT_PATH)
        stop.wait(15)


def main() -> None:
    lg = get()
    db.init()
    stop = threading.Event()
    prog = Progress()
    prog.set(state="starting", daemon_started=db.now_iso())

    def _sig(signum, _frame):
        ev(lg, logging.WARNING, "sinal recebido, encerrando", sinal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    # varreduras que ficaram 'running' de um processo morto
    with db.tx() as con:
        con.execute("UPDATE sweeps SET status='interrupted', finished_at=? WHERE status='running'", (db.now_iso(),))
    # trava exclusiva: impede varredura manual simultânea
    import fcntl
    lockf = open(C.DATA_DIR / "sweep.lock", "w")
    fcntl.flock(lockf, fcntl.LOCK_EX)
    # servidor local do dashboard (127.0.0.1)
    try:
        from .dashboard import make_server
        srv = make_server()
        threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
        ev(lg, logging.INFO, "dashboard HTTP no ar", url=f"http://127.0.0.1:{C.DASHBOARD_PORT}/")
    except Exception as e:
        ev(lg, logging.ERROR, "não foi possível subir o dashboard HTTP", erro=str(e))
    hb = threading.Thread(target=_heartbeat_loop, args=(prog, stop), daemon=True, name="heartbeat")
    hb.start()
    ev(lg, logging.INFO, "daemon iniciado", pid=os.getpid(), intervalo_min=C.SWEEP_INTERVAL_MIN)
    while not stop.is_set():
        try:
            run_sweep(lg, stop, prog)
        except Exception:
            lg.exception("erro inesperado na varredura (o loop continua)")
            prog.set(state="idle", last_error_at=db.now_iso())
        if stop.is_set():
            break
        wait_min = max(5.0, C.SWEEP_INTERVAL_MIN + random.uniform(-C.SWEEP_JITTER_MIN, C.SWEEP_JITTER_MIN))
        nxt = datetime.now().astimezone() + timedelta(minutes=wait_min)
        prog.set(state="sleeping", next_sweep_at=nxt.isoformat(timespec="seconds"))
        ev(lg, logging.INFO, "aguardando próxima varredura", minutos=round(wait_min, 1), proxima=nxt.isoformat(timespec="seconds"))
        stop.wait(wait_min * 60)
    prog.set(state="stopped")
    stop_snap = prog.snapshot()
    stop_snap.update(pid=os.getpid(), heartbeat_at=db.now_iso(), heartbeat_epoch=0)
    C.HEARTBEAT_PATH.write_text(json.dumps(stop_snap, ensure_ascii=False, default=str))
    ev(lg, logging.INFO, "daemon encerrado")


if __name__ == "__main__":
    main()
