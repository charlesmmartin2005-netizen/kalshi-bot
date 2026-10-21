# Injected with sys.remote_exec: engine state for debugging the dead loops.
import __main__ as _m

_e = _m.engine
with open(r"C:\Users\charl\kalshi-bot\research\engine_dump.txt", "w", encoding="utf-8") as _f:
    _f.write(f"running={_e.running} halted={_e.risk.halted} reason={_e.risk.halt_reason!r}\n")
    _f.write(f"stop_set={_e._stop.is_set()} thread_alive={_e._thread and _e._thread.is_alive()} "
             f"data_alive={_e._data_thread and _e._data_thread.is_alive()}\n")
    _f.write(f"last_error={_e.last_error!r} cycles={_e.cycles} last_cycle_at={_e.last_cycle_at}\n")
    _f.write(f"api_errors={_e.client.consecutive_errors} watched={_e.cfg.watched_tickers}\n")
    for _r in list(_e.logs)[-60:]:
        _f.write(f"{_r['ts']:.0f} {_r['level']} {_r['name']}: {_r['msg']}\n")
