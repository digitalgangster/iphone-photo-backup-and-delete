import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys, queue, time, os, tempfile
import extractor as ex
from pathlib import Path
ex.CORE = Path(__file__).resolve().parent / "fake_worker.py"
ex.HEARTBEAT_TIMEOUT = 3
os.environ["FW_STATE"] = tempfile.mktemp()
q = queue.Queue()
s = ex.Supervisor("/tmp/x", q); t0 = time.time(); s.start()
while True:
    ev = q.get(timeout=60)
    print(round(time.time()-t0,1), ev)
    if ev["t"] in ("finished", "fatal", "stopped"): break
