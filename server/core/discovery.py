"""Network discovery: several scanners upserting rows into `discovered`.

Discovery can *find* almost anything; *controlling* still requires a driver
for that brand. Unknown devices stay in the list as "found, not supported".
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from server.config import Config
from server.db import Database, log_event
from server.util import lan_ip, now_iso

# A few common OUI prefixes so "unknown device" can at least say who made it.
OUI_VENDORS = {
    "34:EA:34": "Broadlink",
    "5C:CF:7F": "Sonoff/ITEAD",
    "D0:73:D5": "Tuya/SmartLife",
    "10:D5:61": "Tuya/SmartLife",
    "84:E3:42": "Tuya/SmartLife",
    "50:C7:BF": "TP-Link",
    "98:DA:C4": "TP-Link",
    "14:CC:20": "TP-Link",
    "5C:E9:31": "TP-Link",
    "A8:57:4E": "TP-Link",
    "E8:CC:18": "D-Link",
    "34:08:04": "D-Link",
    "24:6F:28": "Espressif (ESP32)",
    "30:AE:A4": "Espressif (ESP32)",
    "A4:CF:12": "Espressif (ESP32)",
    "48:E7:29": "Espressif (ESP32)",
    "7C:DF:A1": "Espressif (ESP32)",
    "A0:F2:62": "Espressif (ESP32)",  # dongle WiFi của inverter Senergy
    "EC:FA:BC": "Espressif (ESP32)",
    "28:6C:07": "Xiaomi",
    "64:09:80": "Xiaomi",
    "78:11:DC": "Xiaomi",
    "F8:A4:5F": "Xiaomi",
    "A0:BD:1D": "Dahua/Imou",
    "3C:2E:F5": "Dahua/Imou",
    "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
}

NAME_BRANDS = [
    ("broadlink", "Broadlink"),
    ("chromecast", "Google"),
    ("googlecast", "Google"),
    ("androidtv", "Android TV"),
    ("google tv", "Google TV"),
    ("tuya", "Tuya/SmartLife"),
    ("smartlife", "Tuya/SmartLife"),
    ("cozylife", "CozyLife"),
    ("sonoff", "Sonoff"),
    ("ewelink", "Sonoff/eWeLink"),
    ("imou", "Imou"),
    ("dahua", "Dahua"),
    ("aquara", "Aqara"),
    ("lumi", "Aqara"),
    ("yeelight", "Yeelight"),
    ("xiaomi", "Xiaomi"),
    ("philips", "Philips Hue"),
    ("shelly", "Shelly"),
    ("tasmota", "Tasmota"),
    ("esp", "Espressif (ESP)"),
    # TV Android hay gặp (Chromecast built-in không nói lên hãng — phải đọc XML)
    ("tcl", "TCL"),
    ("ffalcon", "TCL (FFalcon)"),
    ("coocaa", "Coocaa"),
    ("casper", "Casper"),
    ("sony", "Sony"),
    ("hisense", "Hisense"),
    ("panasonic", "Panasonic"),
    ("sharp", "Sharp"),
    ("toshiba", "Toshiba"),
]


def _brand_from_name(name: str) -> str:
    low = (name or "").lower()
    for needle, brand in NAME_BRANDS:
        if needle in low:
            return brand
    return ""


def upsert(db: Database, method: str, ident: str, **fields) -> None:
    cols = {
        "ip": "", "mac": "", "name": "", "brand": "",
        "supported": 0, "raw": "{}",
    }
    cols.update({k: v for k, v in fields.items() if k in cols})
    cols["raw"] = cols["raw"] if isinstance(cols["raw"], str) else json.dumps(cols["raw"], ensure_ascii=False)
    ip_fixed = None
    with db.conn() as c:
        c.execute(
            f"""
            INSERT INTO discovered(method, ident, {', '.join(cols)}, first_seen, last_seen)
            VALUES (?, ?, {', '.join('?' for _ in cols)}, ?, ?)
            ON CONFLICT(method, ident) DO UPDATE SET
              ip=excluded.ip, mac=CASE WHEN excluded.mac != '' THEN excluded.mac ELSE discovered.mac END,
              name=CASE WHEN excluded.name != '' THEN excluded.name ELSE discovered.name END,
              brand=CASE WHEN excluded.brand != '' THEN excluded.brand ELSE discovered.brand END,
              supported=excluded.supported, raw=excluded.raw, last_seen=excluded.last_seen
            """,
            (method, ident, *cols.values(), now_iso(), now_iso()),
        )
        # thiết bị đã thêm mà router cấp lại IP (hay xảy ra sau cúp điện):
        # khớp qua định danh ổn định (MAC/id) và cập nhật host mới luôn
        new_ip = cols.get("ip") or ""
        if new_ip:
            row = c.execute(
                """SELECT d.id AS dev, d.host, d.name FROM discovered x
                   JOIN devices d ON d.id = x.adopted_device_id
                   WHERE x.method=? AND x.ident=?""",
                (method, ident),
            ).fetchone()
            if row and row["host"] and row["host"] != new_ip:
                c.execute("UPDATE devices SET host=? WHERE id=?", (new_ip, row["dev"]))
                ip_fixed = f"'{row['name']}' đổi IP {row['host']} → {new_ip} — đã tự cập nhật"
    if ip_fixed:
        log_event(db, "discovery", ip_fixed)


# ---------------------------------------------------------------------------
# Scanners


def scan_broadlink(db: Database, cfg: Config) -> None:
    """Broadlink UDP discovery.

    The library guesses the broadcast interface when local_ip is not given, and
    guesses wrong on machines with extra adapters (VirtualBox, WSL, VPN). Try the
    configured IP, then the auto-detected LAN IP, then the library default, and
    merge everything found.
    """
    import broadlink

    timeout = int(cfg.get("broadlink.timeout", 5))
    candidates: list[str | None] = []
    configured = cfg.get("broadlink.local_ip")
    if configured:
        candidates.append(configured)
    auto = lan_ip()
    if auto and auto not in candidates:
        candidates.append(auto)
    candidates.append(None)

    found: dict[str, object] = {}
    for local_ip in candidates:
        try:
            devices = broadlink.discover(timeout=timeout, local_ip_address=local_ip) or []
        except Exception:
            devices = []
        for dev in devices:
            found.setdefault(dev.mac.hex(), dev)

    for dev in found.values():
        mac = dev.mac.hex()
        upsert(
            db, "broadlink", mac,
            ip=dev.host[0], mac=mac, brand="Broadlink", supported=1,
            name=dev.name or f"Broadlink {dev.host[0]}",
            raw={"devtype": dev.type, "model": str(dev.model), "name": dev.name, "mac": mac},
        )


def scan_tuya(db: Database, cfg: Config) -> None:
    """Tuya UDP discovery: devices broadcast on 6666/6667.

    Battery-powered sensors only broadcast while awake, so a scan right after
    setup or app activity is the best window to catch them.
    """
    try:
        import tinytuya
    except ImportError:
        log_event(
            db, "discovery",
            "Scanner 'tuya' bỏ qua: chưa cài tinytuya (pip install tinytuya)",
            level="warn",
        )
        return
    # tinytuya's arg is `maxretry`, and it is actually the listen time in seconds
    seconds = int(cfg.get("discovery.tuya_seconds", 15))
    found = tinytuya.deviceScan(maxretry=seconds, poll=False, color=False, byID=True) or {}
    for dev_id, info in found.items():
        upsert(
            db, "tuya", dev_id,
            ip=info.get("ip", ""), brand="Tuya", supported=1,
            name=info.get("name") or info.get("product") or f"Tuya {dev_id[-6:]}",
            raw={
                "id": dev_id, "ip": info.get("ip"), "ver": info.get("version"),
                "product": info.get("product"), "name": info.get("name"),
            },
        )


def scan_cozylife(db: Database, cfg: Config) -> None:
    """CozyLife (DoiT) WiFi: TCP 5555, không phải Tuya nên scanner tuya không thấy.

    Firmware thật thường KHÔNG trả lời UDP 6095 dù đó là phương thức chính thức
    (thiết bị cấp nguồn cho PC chỉ nghe TCP) — nên quét 2 lớp: broadcast UDP cho
    firmware biết trả lời, rồi dò TCP 5555 từng IP và hỏi cmd 0 để lọc CozyLife
    thật (cổng 5555 cũng là cổng ADB của Android — chúng im lặng với JSON).
    """
    from server.drivers.cozylife import cozylife_request
    from server.util import norm_mac

    found: dict[str, dict] = {}  # did -> msg info

    # 1) UDP broadcast 6095 — chính thức, nhanh, bắt firmware tự báo
    sn = str(int(time.time() * 1000))
    payload = json.dumps({"cmd": 0, "pv": 0, "sn": sn, "msg": {}}, separators=(",", ":"))
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        local = lan_ip()
        sock.bind((local, 0))
        bcast = local.rsplit(".", 1)[0] + ".255"
        for _ in range(3):
            for target in ("255.255.255.255", bcast):
                sock.sendto(payload.encode(), (target, 6095))
            time.sleep(0.03)
        sock.settimeout(1.5)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            try:
                msg = json.loads(data).get("msg") or {}
            except (ValueError, TypeError):
                continue
            if isinstance(msg.get("did"), str) and msg["did"]:
                msg.setdefault("ip", addr[0])
                found[msg["did"]] = msg
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()

    # 2) dò TCP 5555 toàn subnet cho firmware im lặng UDP
    net = _lan_subnet(cfg)
    hosts = [str(h) for h in net.hosts()] if net else []

    def probe(ip: str):
        res = cozylife_request(ip, 0, {}, timeout=2.0)
        if not res:
            return None
        msg = res.get("msg") or {}
        if isinstance(msg.get("did"), str) and msg["did"]:
            msg.setdefault("ip", ip)
            return msg["did"], msg
        return None

    if hosts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            for hit in pool.map(probe, hosts):
                if hit:
                    found.setdefault(hit[0], hit[1])

    for did, msg in found.items():
        upsert(
            db, "cozylife", did,
            ip=msg.get("ip", ""), mac=norm_mac(msg.get("mac", "")),
            brand="CozyLife", supported=1,
            name=f"CozyLife {did[-6:]}",
            raw={"did": did, "ip": msg.get("ip"), "pid": msg.get("pid"),
                 "dtp": msg.get("dtp"), "mac": msg.get("mac"), "rssi": msg.get("rssi")},
        )


def scan_mdns(db: Database, cfg: Config) -> None:
    from zeroconf import ServiceBrowser, Zeroconf

    seconds = int(cfg.get("discovery.mdns_seconds", 3))
    found: dict[str, dict] = {}

    class Listener:
        def add_service(self, zc, type_, name):
            self._record(zc, type_, name)

        def update_service(self, zc, type_, name):
            self._record(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

        def _record(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name)
            except Exception:
                return
            if not info or not info.addresses:
                return
            import socket as _s

            ips = [_s.inet_ntoa(a) for a in info.addresses if len(a) == 4]
            if not ips:
                return
            display = name.split(".")[0]
            props = {}
            try:
                props = {k.decode(errors="ignore"): v for k, v in (info.properties or {}).items()}
            except Exception:
                pass
            found[name] = {
                "ip": ips[0], "name": display,
                "port": info.port, "server": (info.server or "")[:-1], "props": props,
            }

    zc = Zeroconf()
    try:
        listener = Listener()
        for stype in (
            "_http._tcp.local.", "_hap._tcp.local.", "_googlecast._tcp.local.",
            "_airplay._tcp.local.", "_spotify-connect._tcp.local.", "_sftp-ssh._tcp.local.",
        ):
            ServiceBrowser(zc, stype, listener)
        time.sleep(seconds)
    finally:
        zc.close()

    for name, info in found.items():
        brand = _brand_from_name(info["name"] + " " + info["server"]) or "mDNS"
        upsert(
            db, "mdns", f"{info['ip']}:{info['name']}",
            ip=info["ip"], name=info["name"], brand=brand, supported=0, raw=info,
        )


def scan_ssdp(db: Database, cfg: Config) -> None:
    seconds = int(cfg.get("discovery.ssdp_seconds", 3))
    group = ("239.255.255.250", 1900)
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {group[0]}:{group[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    seen: set[str] = set()
    results: list[dict] = []
    try:
        sock.sendto(message, group)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                sock.sendto(message, group)  # keep probing
                continue
            headers = {}
            for line in data.decode(errors="ignore").split("\r\n\r\n")[0].split("\r\n")[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().upper()] = v.strip()
            usn = headers.get("USN", "")
            key = usn or f"{addr[0]}:{headers.get('LOCATION', '')}"
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "ip": addr[0], "server": headers.get("SERVER", ""),
                "location": headers.get("LOCATION", ""), "usn": usn,
            })
    finally:
        sock.close()

    for item in results:
        label = re.sub(r"^.*?UPnP.*?,", "", item["server"]).strip() or item["ip"]
        brand = _brand_from_name(item["server"] + " " + item["location"]) or "SSDP/UPnP"
        upsert(
            db, "ssdp", item["usn"] or item["ip"],
            ip=item["ip"], name=label[:60], brand=brand, supported=0, raw=item,
        )


def scan_wsdiscovery(db: Database, cfg: Config) -> None:
    """ONVIF WS-Discovery - finds Imou and most IP cameras."""
    if not cfg.get("discovery.wsdiscovery", True):
        return
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
<s:Header>
<a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
<a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
</s:Header>
<s:Body>
<Probe xmlns="http://schemas.xmlsoap.org/ws/2005/04/discovery">
<Types xmlns:tds="http://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl">tds:NetworkVideoTransmitter</Types>
</Probe>
</s:Body>
</s:Envelope>""".encode()

    group = ("239.255.255.250", 3702)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    results: dict[str, dict] = {}
    try:
        sock.sendto(probe, group)
        deadline = time.time() + 4
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                sock.sendto(probe, group)
                continue
            text = data.decode(errors="ignore")
            addr_m = re.search(r"<a:Address>(.*?)</a:Address>", text)
            xaddrs_m = re.search(r"<d:XAddrs>(.*?)</d:XAddrs>", text)
            scopes_m = re.search(r"<d:Scopes[^>]*>(.*?)</d:Scopes>", text)
            endpoint = xaddrs_m.group(1).split()[0] if xaddrs_m else f"http://{addr[0]}/"
            ip_m = re.match(r"https?://([0-9.]+)", endpoint)
            if not ip_m:
                continue
            results[endpoint] = {
                "ip": ip_m.group(1), "endpoint": endpoint,
                "address": addr_m.group(1) if addr_m else "",
                "scopes": scopes_m.group(1)[:500] if scopes_m else "",
            }
    finally:
        sock.close()

    for endpoint, info in results.items():
        scopes = info["scopes"]
        brand = _brand_from_name(scopes) or ("Imou/ONVIF" if "imou" in scopes.lower() else "ONVIF camera")
        # tên thật từ scopes (VD: hardware/IPC-A32P-PRO_serial -> "Camera A32P-PRO");
        # không ghép IP vào tên — router cấp lại IP là tên lỗi thời ngay
        hw_m = re.search(r"onvif://www\.onvif\.org/hardware/([^\s]+)", scopes)
        name_m = re.search(r"onvif://www\.onvif\.org/name/([^\s]+)", scopes)
        if hw_m:
            model = hw_m.group(1).split("_")[0].removeprefix("IPC-")
            if "imou" in brand.lower():
                vendor = "Imou"
            elif brand and brand != "ONVIF camera":
                vendor = brand.split("/")[0]
            else:
                vendor = ""
            name = f"Camera {vendor} {model}".strip()
        elif name_m:
            name = f"Camera {name_m.group(1)}"
        else:
            name = f"Camera ONVIF {info['ip']}"
        # đuôi UUID WS-Discovery của camera thường là MAC thật (uuid:…-302450cc0909)
        mac = ""
        tail = re.search(r"([0-9A-Fa-f]{12})$", info["address"] or "")
        if tail:
            h = tail.group(1)
            mac = ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()
        upsert(db, "wsdiscovery", endpoint, ip=info["ip"], mac=mac, name=name, brand=brand, supported=1, raw=info)


# ---------------------------------------------------------------------------
# Android TV / Google TV (TCL, Sony, Xiaomi...)


def _port_open(ip: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cast_desc(ip: str) -> dict:
    """Tên/mẫu TV từ Cast device-desc.xml — TV tự khai nên chính xác hơn header SSDP."""
    try:
        import httpx

        text = httpx.get(f"http://{ip}:8008/ssdp/device-desc.xml", timeout=2.5).text
    except Exception:
        return {}
    import html

    out = {}
    for field in ("friendlyName", "manufacturer", "modelName"):
        m = re.search(rf"<{field}>(.*?)</{field}>", text, re.S)
        if m and m.group(1).strip():
            out[field] = html.unescape(m.group(1)).strip()
    return out


TV_BRANDS = {
    "tcl": "TCL", "ffalcon": "TCL (FFalcon)", "coocaa": "Coocaa", "casper": "Casper",
    "sony": "Sony", "xiaomi": "Xiaomi", "hisense": "Hisense", "panasonic": "Panasonic",
    "sharp": "Sharp", "toshiba": "Toshiba", "aqua": "Aqua",
}


def _tv_brand(manufacturer: str, model: str) -> str:
    low = f"{manufacturer} {model}".lower()
    for needle, brand in TV_BRANDS.items():
        if needle in low:
            return brand
    return ""


def scan_androidtv(db: Database, cfg: Config) -> None:
    """Android TV / Google TV: dịch vụ remote v2 (mDNS + cổng 6466/6467) và Cast.

    Chỉ thấy TV đang bật hoặc standby (WiFi còn sống) — tắt ngắt nguồn là không
    quảng bá gì cả, quét lại sau khi bật TV. TV có Cast nhưng cổng remote đóng
    (tắt "Điều khiển từ xa qua mạng" trong Cài đặt TV) vẫn hiện nhưng chưa hỗ trợ.
    """
    if not cfg.get("discovery.androidtv", True):
        return
    candidates: dict[str, dict] = {}

    # mDNS service chính thức của giao thức Android TV Remote v2
    try:
        from zeroconf import ServiceBrowser, Zeroconf

        found: dict[str, dict] = {}

        class L:
            def add_service(self, zc, type_, name):
                self._rec(zc, type_, name)

            def update_service(self, zc, type_, name):
                self._rec(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass

            def _rec(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name)
                except Exception:
                    return
                if info and info.addresses:
                    ips = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
                    if ips:
                        found[ips[0]] = {"name": name.split(".")[0], "port": info.port}

        zc = Zeroconf()
        try:
            ServiceBrowser(zc, "_androidtvremote2._tcp.local.", L())
            time.sleep(int(cfg.get("discovery.mdns_seconds", 3)))
        finally:
            zc.close()
        for ip, info in found.items():
            candidates.setdefault(ip, {}).update({"mdns": info})
    except Exception:
        pass

    # Cast (SSDP đã quét ở scanner trước): TV Android đều có Chromecast built-in;
    # kernel "android14-..." trong header là dấu chắc, còn lại đọc XML lấy hãng
    for row in db.query("SELECT * FROM discovered WHERE method='ssdp' AND ip != ''"):
        try:
            raw = json.loads(row["raw"]) if row["raw"] else {}
        except ValueError:
            raw = {}
        server = str(raw.get("server") or "")
        if "chromecast" not in server.lower() and ":8008" not in str(raw.get("location") or ""):
            continue
        cand = candidates.setdefault(row["ip"], {})
        cand["server"] = server
        if row["mac"]:
            cand["mac"] = row["mac"]
        # đuôi UUID của Cast chính là MAC (uuid:…-eec6531b39d6) — IP đổi vẫn khớp
        tail = re.search(r"([0-9A-Fa-f]{12})$", raw.get("usn") or "")
        if tail:
            h = tail.group(1)
            cand["mac"] = ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()

    def probe(ip: str) -> dict:
        info = dict(candidates[ip])
        info["remote_port"] = _port_open(ip, 6466) or _port_open(ip, 6467)
        info.update(_cast_desc(ip))
        return info

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {ip: pool.submit(probe, ip) for ip in candidates}
        results = {ip: f.result() for ip, f in futures.items()}

    for ip, info in results.items():
        brand = _tv_brand(info.get("manufacturer", ""), info.get("modelName", ""))
        model = (info.get("modelName") or "").lower()
        # nhận là TV khi: biết hãng TV, kernel android, hoặc model có chữ "tv"
        is_tv = bool(brand) or "android" in (info.get("server") or "").lower() or bool(re.search(r"\btv\b", model))
        if not is_tv:
            continue  # Chromecast thường / loa Google — không phải TV
        name = info.get("friendlyName") or (f"TV {brand}" if brand else f"TV Android {ip}")
        mac = info.get("mac", "")
        upsert(
            db, "androidtv", mac or f"androidtv:{ip}",
            ip=ip, mac=mac, name=name, brand=brand or "Android TV",
            supported=1 if info["remote_port"] else 0,
            raw={
                "ip": ip, "manufacturer": info.get("manufacturer", ""),
                "model": info.get("modelName", ""), "friendly": info.get("friendlyName", ""),
                "remote_port": info["remote_port"], "mdns": info.get("mdns"),
            },
        )


# ---------------------------------------------------------------------------
# Network scan (ping sweep + ARP + MAC vendor)


def _lan_subnet(cfg: Config) -> ipaddress.IPv4Network | None:
    configured = cfg.get("discovery.subnet", "auto")
    if configured and configured != "auto":
        return ipaddress.ip_network(configured, strict=False)
    local = lan_ip()
    if not local:
        return None
    return ipaddress.ip_network(f"{local}/24", strict=False)


def _ping(ip: str) -> bool:
    flag = "-n" if sys.platform == "win32" else "-c"
    wait = "-w" if sys.platform == "win32" else "-W"
    ms = "500" if sys.platform == "win32" else "1"
    try:
        return subprocess.run(
            ["ping", flag, "1", wait, ms, str(ip)],
            capture_output=True, timeout=2,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _arp_table() -> dict[str, str]:
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, timeout=10, text=True).stdout
    except (subprocess.TimeoutExpired, OSError):
        return {}
    table: dict[str, str] = {}
    for match in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", out):
        ip = match.group(0).split()[0]
        mac = match.group(0).split()[1].replace("-", ":").upper()
        table[ip] = mac
    return table


def _tcp_open(ip: str, port: int, timeout: float = 0.5) -> bool:
    """Return whether a TCP service is reachable without leaving a socket open."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _vendor(mac: str) -> str:
    if not mac:
        return ""
    return OUI_VENDORS.get(mac[:8].upper(), "")


# thương hiệu theo driver — dùng cho thiết bị ĐÃ thêm mà ping sweep gặp lại
# (hiện đúng tên/hãng thay vì "Thiết bị IP" + hãng theo OUI đôi khi sai lệch)
DRIVER_BRANDS = {
    "senergy": "Senergy",
    "broadlink": "Broadlink",
    "tuya": "Tuya",
    "cozylife": "CozyLife",
    "zigbee": "Zigbee",
    "androidtv": "Android TV",
}
# driver có thiết bị nằm trên LAN nhưng KHÔNG có scanner riêng (không quảng bá
# gì ra mạng) — ping sweep nhận nuôi: upsert thành dòng loại riêng thay vì
# "network" mù mờ, định danh bằng MAC để đổi IP vẫn là một dòng
SWEEP_ADOPTED_METHODS = {"senergy"}


def scan_network(db: Database, cfg: Config) -> None:
    if not cfg.get("discovery.network_scan", True):
        return
    net = _lan_subnet(cfg)
    if not net:
        return
    hosts = [str(h) for h in net.hosts()]
    alive: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for ip, ok in zip(hosts, pool.map(_ping, hosts)):
            if ok:
                alive.append(ip)
    from server.util import norm_mac

    adopted = {
        norm_mac(d["mac"]): d
        for d in db.query("SELECT id, name, mac, driver FROM devices WHERE mac IS NOT NULL AND mac != ''")
    }
    # ARP can be stale during a DHCP move: the first snapshot may still assign
    # the old occupant's MAC to the new IP. Senergy has no discovery protocol,
    # so probe Modbus as well. This also catches an inverter that does not answer
    # ICMP ping, while the MAC check below still prevents adopting another TCP
    # service on port 502.
    if any(d["driver"] == "senergy" for d in adopted.values()):
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            modbus_alive = {
                ip for ip, ok in zip(hosts, pool.map(lambda h: _tcp_open(h, 502), hosts))
                if ok
            }
        alive = sorted(set(alive) | modbus_alive)
    arp = _arp_table()
    for ip in alive:
        mac = arp.get(ip, "")
        dev = adopted.get(norm_mac(mac))
        if dev and dev["driver"] in SWEEP_ADOPTED_METHODS:
            # inverter v.v.: hiện đúng loại thiết bị, hỗ trợ, tên thật
            upsert(
                db, dev["driver"], norm_mac(mac) or ip,
                ip=ip, mac=mac, name=dev["name"],
                brand=DRIVER_BRANDS.get(dev["driver"], _vendor(mac)),
                supported=1, raw={"ip": ip, "mac": mac},
            )
        elif dev:
            # máy đã thêm nhưng scanner riêng của nó không thấy lượt này
            # (offline): vẫn hiện đúng tên, không rơi về "Thiết bị IP"
            upsert(
                db, "network", ip,
                ip=ip, mac=mac,
                brand=DRIVER_BRANDS.get(dev["driver"], _vendor(mac)),
                name=dev["name"], supported=1,
                raw={"ip": ip, "mac": mac},
            )
        else:
            vendor = _vendor(mac)
            upsert(
                db, "network", ip,
                ip=ip, mac=mac, brand=vendor,
                name=vendor or f"Thiết bị {ip}",
                supported=0, raw={"ip": ip, "mac": mac},
            )


# ---------------------------------------------------------------------------


def run_scan(db: Database, cfg: Config, progress=None) -> list[str]:
    """Run all scanners. `progress(done_count)` is called between scanners."""
    scanners = [
        ("broadlink", scan_broadlink),
        ("tuya", scan_tuya),
        ("cozylife", scan_cozylife),
        ("mdns", scan_mdns),
        ("ssdp", scan_ssdp),
        ("androidtv", scan_androidtv),  # sau ssdp: tái dùng dòng Cast SSDP
        ("wsdiscovery", scan_wsdiscovery),
        ("network", scan_network),
    ]
    done: list[str] = []
    for name, fn in scanners:
        try:
            fn(db, cfg)
        except Exception as exc:
            log_event(db, "discovery", f"Scanner '{name}' lỗi: {exc}", level="warn")
        done.append(name)
        if progress:
            progress(done)
    _cleanup_network_rows(db)
    return done


def _cleanup_network_rows(db: Database) -> None:
    """The ping sweep finds everything, which makes the list noisy.

    Drop generic 'network' rows for IPs a specific scanner already identified,
    and lend MAC addresses from the sweep to the specific rows.
    """
    from server.util import norm_mac

    # thiết bị đã xóa nhưng dòng quét còn gắn adopted_device_id cũ (xóa từ trước
    # khi có _unlink_discovered) → bỏ gắn để tab Quét mạng hết badge "đã thêm"
    db.execute(
        """UPDATE discovered SET adopted_device_id=NULL
           WHERE adopted_device_id IS NOT NULL
             AND adopted_device_id NOT IN (SELECT id FROM devices)"""
    )
    # dòng "network" mù mờ phải nhường chỗ khi một scanner cụ thể đã nhận diện
    # IP đó (kể cả dòng cũ đã adopt — dòng cụ thể mang đủ thông tin hơn)
    db.execute(
        """DELETE FROM discovered
           WHERE method='network' AND ip != ''
             AND ip IN (SELECT ip FROM discovered WHERE method != 'network' AND ip != '')"""
    )
    # TV đã nhận diện qua scanner androidtv thì bỏ các dòng "Chromecast" chung
    # chung của scanner ssdp (cùng IP) kẻo danh sách hiện trùng hai lần
    db.execute(
        """DELETE FROM discovered
           WHERE method='ssdp' AND adopted_device_id IS NULL AND ip != ''
             AND ip IN (SELECT ip FROM discovered WHERE method='androidtv')"""
    )
    with db.conn() as c:
        c.execute(
            """UPDATE discovered SET mac = COALESCE((
                   SELECT n.mac FROM discovered n
                   WHERE n.method='network' AND n.mac != '' AND n.ip = discovered.ip LIMIT 1), mac)
               WHERE mac = '' AND method != 'network'"""
        )
        # thiết bị đã thêm mà DB chưa lưu MAC: mượn từ dòng quét đã adopt
        # (để các lần quét sau khớp MAC được)
        c.execute(
            """UPDATE devices SET mac = COALESCE((
                   SELECT x.mac FROM discovered x
                   WHERE x.adopted_device_id = devices.id AND x.mac != '' LIMIT 1), mac)
               WHERE mac IS NULL OR mac = ''"""
        )
        # router cấp lại IP (camera là nạn nhân điển hình): dòng quét mới mang MAC
        # khớp thiết bị đã thêm -> chuyển thiết bị sang IP mới, gắn dòng mới, bỏ dòng cũ.
        # Áp dụng cho MỌI thiết bị (Broadlink/Tuya/camera...), không riêng camera.
        devices_by_mac = {
            norm_mac(d["mac"]): d
            for d in c.execute(
                "SELECT id, host, name, mac FROM devices WHERE mac IS NOT NULL AND mac != ''"
            ).fetchall()
        }
        fixed: set[tuple] = set()
        for row in c.execute(
            """SELECT id, ip, mac, adopted_device_id FROM discovered
               WHERE mac != '' AND ip != ''"""
        ).fetchall():
            dev = devices_by_mac.get(norm_mac(row["mac"]))
            if not dev:
                continue
            if row["adopted_device_id"] != dev["id"]:
                c.execute(
                    "UPDATE discovered SET adopted_device_id=? WHERE id=?", (dev["id"], row["id"])
                )
            if dev["host"] and dev["host"] != row["ip"]:
                c.execute("UPDATE devices SET host=? WHERE id=?", (row["ip"], dev["id"]))
                fixed.add((dev["name"], dev["host"], row["ip"]))
                dev = {**dev, "host": row["ip"]}
            # dòng quét cũ của thiết bị này (IP đã đổi) không còn ý nghĩa
            c.execute(
                "DELETE FROM discovered WHERE adopted_device_id=? AND ip != ?",
                (dev["id"], row["ip"]),
            )
    for name, old_ip, new_ip in sorted(fixed):
        log_event(db, "discovery", f"'{name}' đổi IP {old_ip} → {new_ip} — đã tự cập nhật (khớp MAC)")
