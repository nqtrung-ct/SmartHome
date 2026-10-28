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
    ("tuya", "Tuya/SmartLife"),
    ("smartlife", "Tuya/SmartLife"),
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
    try:
        import tinytuya
    except ImportError:
        return
    found = tinytuya.deviceScan(maxRetry=1) or {}
    for dev_id, info in found.items():
        upsert(
            db, "tuya", dev_id,
            ip=info.get("ip", ""), brand="Tuya", supported=1,
            name=info.get("name") or info.get("product") or f"Tuya {dev_id}",
            raw={
                "id": dev_id, "ip": info.get("ip"), "ver": info.get("version"),
                "product": info.get("product"), "name": info.get("name"),
            },
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
        name = "Camera ONVIF " + info["ip"]
        upsert(db, "wsdiscovery", endpoint, ip=info["ip"], name=name, brand=brand, supported=1, raw=info)


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


def _vendor(mac: str) -> str:
    if not mac:
        return ""
    return OUI_VENDORS.get(mac[:8].upper(), "")


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
    arp = _arp_table()
    for ip in alive:
        mac = arp.get(ip, "")
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
        ("mdns", scan_mdns),
        ("ssdp", scan_ssdp),
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
    db.execute(
        """DELETE FROM discovered
           WHERE method='network' AND adopted_device_id IS NULL AND ip != ''
             AND ip IN (SELECT ip FROM discovered WHERE method != 'network' AND ip != '')"""
    )
    with db.conn() as c:
        c.execute(
            """UPDATE discovered SET mac = COALESCE((
                   SELECT n.mac FROM discovered n
                   WHERE n.method='network' AND n.mac != '' AND n.ip = discovered.ip LIMIT 1), mac)
               WHERE mac = '' AND method != 'network'"""
        )
