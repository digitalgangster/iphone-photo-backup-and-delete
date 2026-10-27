import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asyncio, os, sqlite3, struct, sys, tempfile, shutil, hashlib, time
from datetime import datetime
from pathlib import Path
import extractor_core as ec
from pymobiledevice3.exceptions import AfcFileNotFoundError, AfcException

def box(t, payload): return struct.pack(">I4s", 8+len(payload), t) + payload
def heic(n): return box(b"ftyp", b"heic"+b"\0"*8) + box(b"meta", b"m"*50) + box(b"mdat", os.urandom(n))
def mov(n, truncate=0):
    d = box(b"ftyp", b"qt  "+b"\0"*8) + box(b"moov", b"v"*100) + box(b"mdat", os.urandom(n))
    return d[:len(d)-truncate] if truncate else d
def jpg(n): return b"\xff\xd8\xff\xe0" + os.urandom(n) + b"\xff\xd9"
def png(): return b"\x89PNG\r\n\x1a\n" + os.urandom(100) + b"\x00\x00\x00\x00IEND\xaeB`\x82"

root = Path(tempfile.mkdtemp())/"Media"
def mk(p, data): q=root/p; q.parent.mkdir(parents=True, exist_ok=True); q.write_bytes(data)
same = jpg(3000)
mk("DCIM/100APPLE/IMG_0001.HEIC", heic(5000))
mk("DCIM/100APPLE/IMG_0001.MOV", mov(4000))          # live photo half
mk("DCIM/100APPLE/IMG_0001.AAE", b"<plist/>")
mk("DCIM/100APPLE/IMG_0002.JPG", same)
mk("DCIM/100APPLE/IMG_0003.JPG", same)               # identical content -> duplicate
mk("DCIM/100APPLE/IMG_0004.MOV", mov(3*1024*1024))   # multi-chunk video
mk("DCIM/101APPLE/IMG_0005.MOV", mov(2000, truncate=500))  # damaged on phone
mk("DCIM/101APPLE/IMG_0006.PNG", png())
mk("DCIM/101APPLE/IMG_0007.JPG", jpg(100))           # trashed asset
mk("DCIM/101APPLE/STRAY.JPG", jpg(50))               # not in library
mk("PhotoData/CPLAssets/group5/ABCD-1111.HEIC", heic(700))
mk("PhotoData/CPLAssets/group5/ABCD-1111_3.MOV", mov(300))
db = root/"PhotoData/Photos.sqlite"
con = sqlite3.connect(db)
con.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT, ZKIND INT, ZDATECREATED REAL, ZTRASHEDSTATE INT, ZHIDDEN INT)")
con.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INT, ZORIGINALFILESIZE INT, ZORIGINALFILENAME TEXT)")
A=[(1,"u1","DCIM/100APPLE","IMG_0001.HEIC",0, 600000000,0,0,"IMG_0001.HEIC"),
   (2,"u2","DCIM/100APPLE","IMG_0002.JPG",0, 600000100,0,0,"IMG_0002.JPG"),
   (3,"u3","DCIM/100APPLE","IMG_0003.JPG",0, 600000200,0,0,"IMG_0003.JPG"),
   (4,"u4","DCIM/100APPLE","IMG_0004.MOV",1, 700000000,0,0,"IMG_0004.MOV"),
   (5,"u5","DCIM/101APPLE","IMG_0005.MOV",1, 700000100,0,0,"IMG_0005.MOV"),
   (6,"u6","DCIM/101APPLE","IMG_0006.PNG",0, 700000200,0,0,"Screenshot:1.PNG"),
   (7,"u7","DCIM/101APPLE","IMG_0007.JPG",0, 700000300,1,0,"IMG_0007.JPG"),
   (8,"u8","PhotoData/CPLAssets/group5","ABCD-1111.HEIC",0, 300000000,0,0,"IMG_9001.HEIC"),
   (9,"u9","PhotoData/CPLAssets/group6","EEEE-2222.HEIC",0, 300000100,0,0,"IMG_9002.HEIC"),  # iCloud only
   (10,"u10","DCIM/100APPLE","IMG_0001.HEIC",0, 600000000,0,0,"IMG_0001.HEIC")]  # (would collide; harmless)
A=A[:-1]
for r in A:
    con.execute("INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,?)", r[:8])
    con.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES(ZASSET,ZORIGINALFILESIZE,ZORIGINALFILENAME) VALUES(?,?,?)",(r[0],0,r[8]))
con.commit(); con.close()

class FakeAfc:
    hang_once = set(); fail_ops = 0
    def __init__(s): s.h={}; s.n=0
    def _p(s,p): return root/p.lstrip("/")
    async def listdir(s,p):
        q=s._p(p)
        if not q.exists(): raise AfcFileNotFoundError("nf", None)
        return os.listdir(q)
    async def stat(s,p):
        q=s._p(p)
        if not q.exists(): raise AfcFileNotFoundError("nf", None)
        st=q.stat()
        return {"st_ifmt":"S_IFDIR" if q.is_dir() else "S_IFREG","st_size":st.st_size,
                "st_mtime":datetime.fromtimestamp(st.st_mtime),"st_birthtime":datetime.fromtimestamp(st.st_mtime)}
    async def fopen(s,p,mode="r"):
        assert mode=="r"
        q=s._p(p)
        if not q.exists(): raise AfcFileNotFoundError("nf", None)
        s.n+=1; s.h[s.n]=[open(q,"rb"), p]; return s.n
    async def fread(s,h,n):
        f,p = s.h[h]
        if p in FakeAfc.hang_once and f.tell() > 0:
            FakeAfc.hang_once.discard(p); await asyncio.sleep(999)
        return f.read(n)
    async def fclose(s,h): s.h.pop(h)[0].close()

async def run_pass(eng):
    phone = ec.ReadOnlyPhone(FakeAfc())
    await eng.index(phone) if not getattr(eng, "_indexed", False) else None
    eng._indexed = True
    await eng.transfer_all(phone)

def show(eng, title):
    print(f"--- {title}")
    for r in eng.ledger.q("SELECT id, phone_path, role, status, attempts, dest_rel, warning IS NOT NULL, last_error FROM files ORDER BY id"):
        print("  ", r)

async def main():
    dest = Path(tempfile.mkdtemp())/"iPhone Photos"
    ec.OP_TIMEOUT = 0.5
    eng = ec.Engine(dest)
    FakeAfc.hang_once = {"/DCIM/100APPLE/IMG_0004.MOV"}
    try:
        await run_pass(eng)
    except ec.PhoneStall as e:
        print("stall detected as expected:", e)
    # reconnect -> continue
    await run_pass(eng)
    show(eng, "after pass 2")
    # damaged MOV needs a second identical read to be accepted -> run more passes
    for _ in range(2): await run_pass(eng)
    show(eng, "after passes 3-4")
    eng.write_reports()
    print((dest/"_Export Reports/Export Report.txt").read_text())
    for f in sorted((dest/"_Export Reports").iterdir()): print("report file:", f.name)
    print("tree:"); 
    for p in sorted(dest.rglob("*")):
        if ".extractor" not in p.parts and p.is_file(): print("  ", p.relative_to(dest), p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime).date())
    # integrity: compare every done file to phone bytes
    bad=0
    for pp, rel in eng.ledger.q("SELECT phone_path, dest_rel FROM files WHERE status IN ('done','duplicate')"):
        if (root/pp).read_bytes() != (dest/rel).read_bytes(): bad+=1; print("MISMATCH", pp)
    print("byte mismatches:", bad)
    # crash between rename and commit: mark a done row pending and rerun -> must adopt, not duplicate
    eng.ledger.q("UPDATE files SET status='pending' WHERE phone_path='DCIM/100APPLE/IMG_0001.HEIC'")
    await run_pass(eng)
    r = eng.ledger.one("SELECT status, dest_rel FROM files WHERE phone_path='DCIM/100APPLE/IMG_0001.HEIC'")
    print("after simulated crash:", r, "files named IMG_0001*:", sorted(p.name for p in dest.rglob("IMG_0001*")))
    # a foreign file already occupying a target name must not be overwritten
    eng.ledger.q("UPDATE files SET status='pending' WHERE phone_path='DCIM/101APPLE/IMG_0006.PNG'")
    tgt = dest/eng.ledger.one("SELECT dest_rel FROM files WHERE phone_path='DCIM/101APPLE/IMG_0006.PNG'")[0]
    tgt.write_bytes(b"someone else's file")
    await run_pass(eng)
    r = eng.ledger.one("SELECT status, dest_rel FROM files WHERE phone_path='DCIM/101APPLE/IMG_0006.PNG'")
    print("foreign file kept:", tgt.read_bytes()==b"someone else's file", "new copy at:", r)
    # second engine on same folder must refuse
    try: ec.Engine(dest); print("LOCK FAILED")
    except ec.FatalError as e: print("lock ok:", e)
    print("snapshot:", {k:v for k,v in eng.snapshot().items() if k not in ('msg',)})
asyncio.run(main())
