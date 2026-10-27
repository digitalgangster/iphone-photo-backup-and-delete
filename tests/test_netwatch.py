import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys, types, tempfile
from pathlib import Path
import netwatch as nw
from scapy.all import Ether, IP, IPv6, UDP, TCP, DNS, DNSQR, DNSRR, Raw, raw
from scapy.layers.tls.handshake import TLSClientHello
from scapy.layers.tls.record import TLS
from scapy.layers.tls.extensions import TLS_Ext_ServerName, ServerName

PH="192.168.1.5"; FB="31.13.1.1"; G6="2607:f8b0::1"; PH6="2600:1::5"
def P(pkt, comm, ecomm="", iface="en0", fam=2, io=0, real_eth=True):
    data = raw(Ether()/pkt) if real_eth else nw.FAKE_ETHERNET + raw(pkt)
    return types.SimpleNamespace(data=data, protocol_family=fam, interface_name=iface, comm=comm, ecomm=ecomm, pid=1, io=io)

hello = TLS(msg=[TLSClientHello(ext=[TLS_Ext_ServerName(servernames=[ServerName(servername=b"graph.instagram.com")])])])
pkts = [
 P(IP(src=PH,dst="8.8.8.8")/UDP(sport=50000,dport=53)/DNS(rd=1,qd=DNSQR(qname="graph.facebook.com")), "mDNSResponder", io=1),
 P(IP(src="8.8.8.8",dst=PH)/UDP(sport=53,dport=50000)/DNS(qr=1,qd=DNSQR(qname="graph.facebook.com"),an=DNSRR(rrname="graph.facebook.com",rdata=FB)), "mDNSResponder", io=0),
 P(IP(src=PH,dst=FB)/TCP(sport=51000,dport=443,flags="S"), "nsurlsessiond", "Instagram", io=1),
 P(IP(src=PH,dst="157.240.2.2")/TCP(sport=51001,dport=443,flags="PA")/Raw(raw(hello)), "Facebook", io=1),
 P(IP(src=PH,dst=FB)/TCP(sport=51000,dport=443,flags="PA")/Raw(b"x"*1400), "nsurlsessiond", "Instagram", io=1),
 P(IP(src=FB,dst=PH)/TCP(sport=443,dport=51000,flags="A")/Raw(b"y"*200), "nsurlsessiond", "Instagram", io=0),
 # cellular, fake ethernet, IPv6
 P(IPv6(src=PH6,dst=G6)/TCP(sport=52000,dport=443,flags="S"), "Gmail", iface="pdp_ip0", fam=30, io=1, real_eth=False),
 P(IPv6(src=PH6,dst=G6)/UDP(sport=52001,dport=443)/Raw(b"q"*900), "Gmail", iface="pdp_ip0", fam=30, io=1, real_eth=False),
 P(IPv6(src=G6,dst=PH6)/UDP(sport=443,dport=52001)/Raw(b"r"*300), "Gmail", iface="pdp_ip0", fam=30, io=0, real_eth=False),
]
st = nw.Stats()
for p in pkts: st.add(p)
print("local:", st.local_ips, "names:", st.names, "unparsed:", st.unparsed)
nw.render(st, "test")
nw.OUT_DIR = Path(tempfile.mkdtemp())
t,c = nw.save_report(st, "T", "iPhone15,3 iOS 26.6.2")
print(t.read_text()); print(c.read_text())
