# Injected into the running bot with sys.remote_exec to see where every
# thread is (debugging a hang). Writes research/thread_dump.txt.
import sys
import threading
import traceback

_names = {t.ident: t.name for t in threading.enumerate()}
with open(r"C:\Users\charl\kalshi-bot\research\thread_dump.txt", "w", encoding="utf-8") as _f:
    for _tid, _frame in sys._current_frames().items():
        _f.write(f"\n=== {_names.get(_tid, _tid)} ===\n")
        _f.write("".join(traceback.format_stack(_frame)))
