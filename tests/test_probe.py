import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asyncio, os, sqlite3, sys, shutil, tempfile
from pathlib import Path
import probe
from pymobiledevice3.exceptions import AfcFileNotFoundError

root = Path(tempfile.mkdtemp())/"Media"
(root/"DCIM/100APPLE").mkdir(parents=True); (root/"DCIM/101APPLE").mkdir(parents=True); (root/"PhotoData/CPLAssets/group1").mkdir(parents=True)
def mk(p, n): (root/p).write_bytes(os.urandom(n))
mk("DCIM/100APPLE/IMG_0001.HEIC", 3000)   # full original
mk("DCIM/100APPLE/IMG_0001.MOV", 2000)    # live photo pair (unreferenced)
mk("DCIM/100APPLE/IMG_0002.JPG", 500)     # reduced (orig 4000)
mk("DCIM/101APPLE/IMG_0100.MOV", 5*1024*1024+123)  # video full
mk("DCIM/101APPLE/IMG_0101.AAE", 300)
mk("PhotoData/CPLAssets/group1/AAA.HEIC", 7000)
mk("PhotoData/CPLAssets/group1/BBB.MOV", 100)
db = root/"PhotoData/Photos.sqlite"
con = sqlite3.connect(db)
con.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY TEXT, ZFILENAME TEXT, ZKIND INT, ZTRASHEDSTATE INT, ZHIDDEN INT, ZCLOUDASSETGUID TEXT, ZDATECREATED REAL)")
con.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INT, ZORIGINALFILESIZE INT)")
rows=[(1,"DCIM/100APPLE","IMG_0001.HEIC",0,0,0,"g1",600000000,3000),
      (2,"DCIM/100APPLE","IMG_0002.JPG",0,0,0,"g2",700000000,4000),
      (3,"DCIM/101APPLE","IMG_0100.MOV",1,0,1,"g3",710000000,5*1024*1024+123),
      (4,"DCIM/102APPLE","IMG_0200.HEIC",0,0,0,"g4",720000000,9000),   # cloud only
      (5,"DCIM/102APPLE","IMG_0201.MOV",1,1,0,"g5",720000001,9000),
      (6,"PhotoData/CPLAssets/group1","AAA.HEIC",0,0,0,"g6",730000000,7000),
      (7,"PhotoData/CPLAssets/group1","BBB.MOV",1,0,0,"g7",730000001,90000),
      (8,"PhotoData/CPLAssets/group1","CCC.HEIC",0,0,0,"g8",730000002,5000),
      (9,"7/1","DDD.HEIC",0,0,0,"g9",730000003,5000)]    # trashed
for r in rows:
    con.execute("INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,?)", r[:8])
    con.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZASSET,ZORIGINALFILESIZE) VALUES (?,?)",(r[0],r[8]))
con.commit(); con.close()

class FakeAfc:
    def __init__(s): s.h={}; s.n=0
    def _p(s,p): return root/p.lstrip("/")
    async def listdir(s,p): return os.listdir(s._p(p))
    async def stat(s,p):
        q=s._p(p)
        if not q.exists(): raise AfcFileNotFoundError("nf", None)
        return {"st_ifmt":"S_IFDIR" if q.is_dir() else "S_IFREG","st_size":q.stat().st_size}
    async def fopen(s,p,mode="r"):
        assert mode=="r"; s.n+=1; s.h[s.n]=open(s._p(p),"rb"); return s.n
    async def fread(s,h,n): await asyncio.sleep(0); return s.h[h].read(n)
    async def fclose(s,h): s.h.pop(h).close()

async def main():
    rep = probe.Report()
    probe.OUT_DIR = Path(tempfile.mkdtemp())/"out"
    phone = probe.ReadOnlyPhone(FakeAfc())
    rep.step("open_media", True, "fake")
    files, lv = await probe.scan_dcim(phone, rep)
    got = await probe.copy_photos_db(phone, rep)
    dbc, outside = probe.analyze_db(got[0], files, rep)
    oc = await probe.check_outside_dcim(phone, outside, rep)
    m = await probe.speed_test(phone, lv, rep)
    probe.verdict(rep, sum(files.values()), dbc, oc, m)
    rep.save(); print(rep.render_text())
    # stall detection
    class Hang(FakeAfc):
        async def listdir(s,p): await asyncio.sleep(999)
    probe.OP_TIMEOUT=0.2
    ph=probe.ReadOnlyPhone(Hang()); r2=probe.Report()
    print(await probe.scan_dcim(ph, r2), ph.stalls)
asyncio.run(main())
