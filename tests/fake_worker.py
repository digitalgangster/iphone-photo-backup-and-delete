import json, sys, time, os
state = os.environ["FW_STATE"]
n = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(n + 1))
print(json.dumps({"t": "status", "state": "copying", "run": n}), flush=True)
if n == 0:
    time.sleep(999)          # freeze: no more heartbeats
elif n == 1:
    sys.exit(1)              # crash
else:
    print(json.dumps({"t": "status", "state": "complete", "msg": "done"}), flush=True)
    sys.exit(0)
