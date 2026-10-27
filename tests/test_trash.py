import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys, types, builtins
sys.argv=["x"]
src=open(Path(__file__).resolve().parent / "test_cleaner.py").read().split("ic.time.sleep")[0]
exec(src)
import icloud_cleaner as ic
ic.time.sleep=lambda s: None
state["rl"]=0; state["err_once"]=set()
seen_fields=[]
orig=Client.modify
def modify(self, operations, zone_id, atomic):
    seen_fields.extend(list(o.model_dump(exclude_none=True)["record"]["fields"].keys()) for o in operations)
    out=[]
    for o in operations:
        icloud.pop(o.record.recordName,None); out.append(types.SimpleNamespace(recordName=o.record.recordName))
    return types.SimpleNamespace(records=out)
Client.modify=modify
fake = types.SimpleNamespace(albums={"Recently Deleted": Lib()}, private_client=Client())
answers=iter(["DELETE ONE","YES"]); builtins.input=lambda p="": (print(p, end=""), next(answers))[1]
b=ic.BackupIndex(dest)
c=ic.Cleaner(dest,"me@example.com","trash"); c.photos=fake
c.scan(b); print("summary:", c.summary()); c.test_one(); c.delete_all(); c.report()
print("fields used:", {f for fs in seen_fields for f in fs}); print("left:", sorted(icloud))
import os; print(sorted(os.listdir(dest/"_Export Reports")))
