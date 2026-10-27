import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys, os, tempfile, sqlite3, types, builtins, json
from pathlib import Path
import extractor_core as ec, icloud_cleaner as ic
from pyicloud.common.cloudkit import CKErrorItem
from pyicloud.common.cloudkit.client import CloudKitRateLimited
from datetime import datetime, timezone

dest = Path(tempfile.mkdtemp())/"iPhone Photos"
L = ec.Ledger((dest/".extractor").mkdir(parents=True) or dest/".extractor"/"transfer.db")
# 6 backed-up items; one recorded but missing on disk
items = [("IMG_0001.HEIC",1000,1600000000.0),("IMG_0002.JPG",2000,1600000100.0),("IMG_0003.MOV",3000,1600000200.0),
         ("IMG_0004.PNG",None,1600000300.0),("IMG_0005.HEIC",5000,1600000400.0),("GONE.JPG",6000,1600000500.0)]
for i,(n,sz,t) in enumerate(items):
    L.q("INSERT INTO assets(uuid,orig_name,orig_size,created,trashed) VALUES(?,?,?,?,0)",(f"u{i}",n,sz,t))
    rel=f"2020/09/{n}"
    L.q("INSERT INTO files(phone_path,size,uuid,role,dest_rel,dest_key,status) VALUES(?,?,?,?,?,?,'done')",(f"p{i}",sz or 1,f"u{i}","main",rel,rel.lower()))
    if n!="GONE.JPG":
        (dest/rel).parent.mkdir(parents=True,exist_ok=True); (dest/rel).write_bytes(b"x")

Z={"zoneName":"PrimarySync","ownerRecordName":"_abc","zoneType":"REGULAR_CUSTOM_ZONE"}
def A(rn,name,size,ts):
    return types.SimpleNamespace(asset_record={"recordName":rn,"recordType":"CPLAsset","recordChangeTag":"t"+rn,"zoneID":Z},
        master_record={"recordName":"m"+rn,"recordChangeTag":"mt"}, filename=name, size=size,
        asset_date=datetime.fromtimestamp(ts,timezone.utc))
icloud = {a.asset_record["recordName"]:a for a in [
    A("r1","IMG_0001.HEIC",1000,1600000000), A("r2","IMG_0002.JPG",2000,1600000100),
    A("r3","IMG_0003.MOV",3000,1600000200), A("r4","IMG_0004.PNG",4444,1600000301),   # size unknown in ledger -> name+date
    A("r5","IMG_0005.HEIC",9999,1700000000),   # size & date differ -> KEEP
    A("r6","GONE.JPG",6000,1600000500),        # backup file missing on disk -> KEEP
    A("r7","NOTBACKEDUP.JPG",7,1600000600),    # unknown -> KEEP
    A("r8","IMG_0001.HEIC",1000,1600000000)]}  # duplicate of r1 -> delete
calls=[]; state={"rl":1,"err_once":{"r3"}}
class Client:
    def modify(self, operations, zone_id, atomic):
        calls.append([o.record.recordName for o in operations])
        assert atomic is False and all(o.model_dump(exclude_none=True)["record"]["fields"]["isDeleted"]["value"]==1 for o in operations)
        if state["rl"]: state["rl"]-=1; raise CloudKitRateLimited("slow", retry_after=0)
        out=[]
        for o in operations:
            rn=o.record.recordName
            if rn in state["err_once"]: state["err_once"].discard(rn); out.append(CKErrorItem(serverErrorCode="CONFLICT",reason="tag",recordName=rn))
            else: icloud.pop(rn,None); out.append(types.SimpleNamespace(recordName=rn))
        return types.SimpleNamespace(records=out)
class Lib:
    def __iter__(self): return iter(list(icloud.values()))
    def __len__(self): return len(icloud)
fake = types.SimpleNamespace(all=Lib(), private_client=Client())
ic.time.sleep=lambda s: None
answers=iter(["DELETE ONE","YES"]); builtins.input=lambda p="": (print(p, end=""), next(answers))[1]

b=ic.BackupIndex(dest); print("backup count", b.count, "missing", b.missing_on_disk)
c=ic.Cleaner(dest,"me@example.com"); c.photos=fake
c.scan(b); print("summary pending/skipped/error:", c.summary())
c.test_one(); c.delete_all(); c.report()
print("calls:", calls)
print("left in iCloud:", sorted(icloud))
# second run: r3 errored -> retried
c.scan(b); c.progress.db.execute("UPDATE items SET status='pending' WHERE status='error'"); print("rerun summary:", c.summary())
c.delete_all(); print("left in iCloud after rerun:", sorted(icloud)); c.report()
print(open(dest/"_Export Reports"/"iCloud - not deleted (no matching backup).csv", encoding="utf-8-sig").read())
