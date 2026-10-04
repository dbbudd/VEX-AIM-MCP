"""Getting AIM robots onto a network: finding them, and moving them to another Wi-Fi network, e.g.
when setting up at a new venue.

Every robot serves a small setup page on port 80, on whatever network it's on: its own hotspot at
192.168.4.1 in Access Point mode, or its address on the network it joined. Saving that page is a
plain GET, of just what its form sends:
    /wifi?flags=1&s1=<network>&p1=<password>    join an existing network (Station mode)
    /wifi?flags=0&a1=<address>                  start its own hotspot (Access Point mode)
There's no login, and the page shows the saved network AND its password in plain text: this module
reads the network's name from it, and the password only to copy it straight into the keychain
(remember_robot_network). It never returns, logs or shows a password.

The robot can't scan for networks (its page has no list), and macOS hides nearby networks' names from
programs without Location Services. So scan_wifi() asks a tiny helper app (wifiscan/, built here with
Xcode's tools) that macOS lets the person allow once. The networks this Mac has joined are offered too.

- find_robots(): look for robots on this computer's local network.
- robot_network(host): the network a robot is set to join, and its firmware.
- send_to_network(host, network, password): tell a robot to join a network (it then leaves this one).
- send_to_hotspot(host): tell a robot to start its own hotspot instead.
- same_radio(mac, mac): whether two hardware addresses are one robot's, to recognise it at a new address.
- known_networks(): the Wi-Fi networks this Mac has joined, most preferred first.
- scan_wifi(): the Wi-Fi networks near this Mac, and whether a robot can join each (2.4 GHz).
- mac_saved_password(network): the password this Mac saved for a network, once the person approves in macOS's dialog.
- this_computer_network(): the Wi-Fi network this Mac is on (usually unknown: see above).
- Remembered networks: names in the panel's setup file; passwords in the macOS keychain.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from .paths import data_dir

log = logging.getLogger("vex_aim.network")

AP_HOST = os.environ.get("AIM_HOTSPOT_HOST", "192.168.4.1")  # a robot on its own hotspot (Access Point mode); tests use their own
MAX_LEN = 20  # the robot's setup page takes network names and passwords up to 20 characters
KEYCHAIN_SERVICE = os.environ.get("AIM_KEYCHAIN_SERVICE", "VEX AIM venue Wi-Fi")  # tests use their own
PAGE_TITLE = "AIM - Configure WiFi"


def _split(host: str) -> tuple[str, int]:
    name, _, port = host.partition(":")
    return name, int(port or 80)


async def _get(host: str, path: str, timeout: float = 3.0) -> str:
    """A minimal HTTP GET (the robot's web server is tiny, and sends its pages in chunks); returns the body."""
    name, port = _split(host)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(name, port), timeout)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        data = b""
        while len(data) < 262144 and (chunk := await asyncio.wait_for(reader.read(65536), timeout)):
            data += chunk
    finally:
        writer.close()
    head, _, body = data.decode("latin-1").partition("\r\n\r\n")
    if " 200 " not in head.split("\r\n", 1)[0] + " ":
        raise OSError(f"the robot answered {head.split(chr(13), 1)[0]!r}")
    if "transfer-encoding: chunked" in head.lower():  # size line, data, repeat; a zero size ends it
        out, rest = [], body
        while rest:
            size_line, _, rest = rest.partition("\r\n")
            size = int(size_line.split(";")[0].strip() or "0", 16)
            if size == 0:
                break
            out.append(rest[:size])
            rest = rest[size + 2:]
        body = "".join(out)
    return body


def parse_setup_page(page: str) -> dict | None:
    """The useful, non-secret parts of a robot's setup page: whether it joins a network, which one,
    and its firmware. None if it isn't an AIM setup page. (The page also holds the password: ignored.)"""
    if PAGE_TITLE not in page:
        return None
    ssid = re.search(r"id='s1'[^>]*value='([^']*)'", page)
    vexos = re.search(r"VEXOS&nbsp;version&nbsp;:&nbsp;([\d.]+)", page)
    radio = re.search(r"Radio&nbsp;version&nbsp;:&nbsp;([\d.]+)", page)
    joins = re.search(r"id='f1' type='checkbox'[^>]*\bchecked\b", page) is not None
    return {"mode": "joins a network" if joins else "its own hotspot", "network": html.unescape(ssid.group(1)) if ssid else None,
            "firmware": vexos.group(1) if vexos else None, "radio": radio.group(1) if radio else None}


def _saved_login(page: str) -> tuple[str, str]:
    """The network a robot is set to join and its password, from its setup page: only for copying
    straight into the keychain."""
    ssid = re.search(r"id='s1'[^>]*value='([^']*)'", page)
    secret = re.search(r"id='p1'[^>]*value='([^']*)'", page)
    if PAGE_TITLE not in page or not ssid or not ssid.group(1) or not secret:
        raise OSError("the robot isn't set to join a network, so there's none to remember")
    return html.unescape(ssid.group(1)), html.unescape(secret.group(1))


async def remember_robot_network(host: str) -> str:
    """Remember the network the robot at host is set to join: its password goes from the robot's setup
    page into the keychain, unseen. Returns the network's name."""
    name, password = _saved_login(await _get(host, "/"))
    check_details(name, password)
    await asyncio.to_thread(save_venue_password, name, password)
    return name


async def robot_network(host: str, timeout: float = 3.0) -> dict:
    """Which network a robot is set to join, and its firmware."""
    info = parse_setup_page(await _get(host, "/", timeout))
    if info is None:
        raise OSError(f"{host} isn't showing an AIM setup page")
    return {"host": host, **info}


def local_hosts() -> list[str]:
    """Every address on this computer's local networks (each up to a /24 around this computer)."""
    out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    hosts: list[str] = []
    for ip, mask in re.findall(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", out):
        if ip.startswith(("127.", "169.254.")) or mask == "0xffffffff":  # loopback, link-local, VPN tunnels
            continue
        net = ipaddress.IPv4Network(f"{ip}/{bin(int(mask, 16)).count('1')}", strict=False)
        if net.prefixlen < 24:  # a big network: just the /24 this computer is in
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        hosts += [str(h) for h in net.hosts() if str(h) != ip]
    return hosts


def normal_mac(mac: str) -> str:
    """aa:b:cc… → aa:0b:cc… (macOS's arp leaves out leading zeros)."""
    return ":".join(f"{int(part, 16):02x}" for part in mac.split(":"))


def mac_addresses() -> dict[str, str]:
    """IP → hardware address, from this computer's ARP table (robots' Wi-Fi chips start f4:12:fa).
    AIM_TEST_MACS ("host=mac,…", a host may include a port) overrides, for tests."""
    if env := os.environ.get("AIM_TEST_MACS"):
        return dict(pair.split("=", 1) for pair in env.split(",") if "=" in pair)
    out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    return {ip: normal_mac(mac) for ip, mac in re.findall(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", out)}


def mac_of(host: str, macs: dict[str, str]) -> str | None:
    return macs.get(host) or macs.get(_split(host)[0])


def same_radio(a: str | None, b: str | None) -> bool:
    """Whether two hardware addresses belong to one robot: the same, or its Wi-Fi and hotspot addresses
    (its radio's hotspot address is one more than its Wi-Fi one)."""
    try:
        return bool(a and b) and abs(int(normal_mac(a).replace(":", ""), 16) - int(normal_mac(b).replace(":", ""), 16)) <= 1
    except ValueError:
        return False


async def find_robots(hosts: list[str] | None = None, timeout: float = 0.5) -> list[dict]:
    """Look for AIM robots: anything answering on port 80 with the AIM setup page. Scans this
    computer's local networks unless hosts are given (AIM_SCAN_HOSTS, comma-separated, overrides)."""
    if hosts is None:
        env = os.environ.get("AIM_SCAN_HOSTS")
        hosts = [h.strip() for h in env.split(",")] if env else await asyncio.to_thread(local_hosts)
    gate = asyncio.Semaphore(200)

    async def probe(host: str) -> dict | None:
        async with gate:
            try:
                name, port = _split(host)
                _, writer = await asyncio.wait_for(asyncio.open_connection(name, port), timeout)
                writer.close()
                return await robot_network(host)
            except (OSError, asyncio.TimeoutError, ValueError):
                return None

    found = [r for r in await asyncio.gather(*(probe(h) for h in hosts)) if r]
    macs = await asyncio.to_thread(mac_addresses)
    for r in found:
        r["mac"] = mac_of(r["host"], macs)
    return sorted(found, key=lambda r: tuple(int(p) for p in _split(r["host"])[0].split(".")) + (_split(r["host"])[1],))


def check_details(network: str, password: str) -> None:
    if not network or len(network) > MAX_LEN:
        raise ValueError(f"the network's name must be 1 to {MAX_LEN} characters (the robot can't take longer ones)")
    if len(password) > MAX_LEN:
        raise ValueError(f"the robot can only take passwords up to {MAX_LEN} characters: this network won't work with it")
    if 0 < len(password) < 8:
        raise ValueError("Wi-Fi passwords are at least 8 characters (or empty, for an open network)")


async def send_to_network(host: str, network: str, password: str) -> str:
    """Tell the robot at host to join a Wi-Fi network. It restarts its radio and leaves the network it's on,
    so it can't be reached here any more (unless that's the same network)."""
    check_details(network, password)
    await robot_network(host)  # it's there, and it's a robot: so if it hangs up below, it's switching
    query = urlencode({"flags": 1, "s1": network, "p1": password})
    try:
        await _get(host, f"/wifi?{query}", timeout=5)
    except (OSError, asyncio.TimeoutError) as e:  # it may drop the connection as it switches: not a failure
        log.info("the robot at %s didn't answer after saving (%s); it's probably switching", host, e)
    return f"Told the robot at {host} to join “{network}”."


async def send_to_hotspot(host: str) -> str:
    """Tell the robot at host to start its own hotspot (AIM-…; the password is on its screen). It leaves the
    network it's on. Returns the address it will have on its hotspot."""
    page = await _get(host, "/")
    if parse_setup_page(page) is None:
        raise OSError(f"{host} isn't showing an AIM setup page")
    m = re.search(r"id='a1'[^>]*value='([^']*)'", page)
    a1 = m.group(1) if m else "0.0.0.0"  # its form sends what's in the box: 0.0.0.0, its usual address, unless someone set one
    try:
        await _get(host, "/wifi?" + urlencode({"flags": 0, "a1": a1}), timeout=5)
    except (OSError, asyncio.TimeoutError) as e:  # it may drop the connection as it switches: not a failure
        log.info("the robot at %s didn't answer after saving (%s); it's probably switching", host, e)
    return a1 if a1 not in ("", "0.0.0.0") else AP_HOST


def wifi_device() -> str:
    out = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\S+)", out)
    return m.group(1) if m else "en0"


def known_networks() -> list[str]:
    """The Wi-Fi networks this Mac has joined, most preferred first (the one it's on is usually first).
    AIM_KNOWN_NETWORKS (comma-separated) overrides, for tests."""
    if (env := os.environ.get("AIM_KNOWN_NETWORKS")) is not None:
        return [n for n in env.split(",") if n]
    out = subprocess.run(["networksetup", "-listpreferredwirelessnetworks", wifi_device()],
                         capture_output=True, text=True, timeout=5).stdout
    names = [line.strip() for line in out.splitlines()[1:]]
    return list(dict.fromkeys(n for n in names if n and n != "<redacted>"))


SCANNER_SOURCE = Path(__file__).with_name("wifiscan")
SCANNER_APP = data_dir() / "AIM Wi-Fi Scan.app"


def build_scanner() -> Path:
    """Build the scan helper app from wifiscan/ (needs Xcode's command-line tools), unless it's up to date.
    It lives outside iCloud, in Application Support, signed for this Mac only."""
    exe = SCANNER_APP / "Contents" / "MacOS" / "wifiscan"
    sources = [SCANNER_SOURCE / "main.swift", SCANNER_SOURCE / "Info.plist"]
    if exe.exists() and exe.stat().st_mtime >= max(s.stat().st_mtime for s in sources):
        return SCANNER_APP
    exe.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-O", "-o", str(exe), str(sources[0]),
                    "-framework", "CoreWLAN", "-framework", "CoreLocation"], check=True, capture_output=True, timeout=300)
    shutil.copy(sources[1], SCANNER_APP / "Contents" / "Info.plist")
    subprocess.run(["codesign", "--force", "--sign", "-", str(SCANNER_APP)], check=True, capture_output=True, timeout=60)
    return SCANNER_APP


async def scan_wifi(check_only: bool = False, timeout: float = 150) -> dict:
    """The Wi-Fi networks near this Mac: {"permission", "networks": [{"ssid", "rssi", "bands", "open", "robot_ok"}],
    "current"}, strongest first, one entry per name. The first time, macOS asks the person to let the helper use
    Location Services (that's what unlocks network names); permission is then "allowed", "denied" or "no answer".
    AIM_TEST_SCAN (the helper's JSON) stands in for it, for tests."""
    if env := os.environ.get("AIM_TEST_SCAN"):
        result = json.loads(env)
    else:
        try:
            app = await asyncio.to_thread(build_scanner)
        except (OSError, subprocess.SubprocessError) as e:
            return {"permission": "unavailable", "error": f"Couldn't build the scan helper (it needs Xcode's tools): {e}"}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "scan.json"
            proc = await asyncio.create_subprocess_exec("open", "-W", "-n", "-g", str(app), "--args", "--out", str(out),
                                                        *(["--check"] if check_only else []))
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                return {"permission": "no answer"}
            result = json.loads(out.read_text()) if out.exists() else {"permission": "no answer"}
    by_name: dict[str, dict] = {}
    for n in result.get("networks", []):
        entry = by_name.setdefault(n["ssid"], {"ssid": n["ssid"], "rssi": n["rssi"], "bands": [], "open": n.get("open", False)})
        entry["rssi"] = max(entry["rssi"], n["rssi"])
        if n.get("band") not in entry["bands"]:
            entry["bands"].append(n.get("band"))
    for entry in by_name.values():
        entry["bands"].sort()
        entry["robot_ok"] = "2.4 GHz" in entry["bands"] and len(entry["ssid"]) <= MAX_LEN
    networks = sorted(by_name.values(), key=lambda n: (not n["robot_ok"], -n["rssi"]))
    return {**{k: v for k, v in result.items() if k != "networks"}, "networks": networks}


def mac_saved_password(network: str) -> str | None:
    """The password this Mac saved for a Wi-Fi network it has joined. macOS asks the person to approve, in its own
    dialog; None if they cancel or there's none. Never log or show it. AIM_TEST_MAC_PASSWORDS ("name=password,…")
    stands in, for tests."""
    if (env := os.environ.get("AIM_TEST_MAC_PASSWORDS")) is not None:
        return dict(pair.split("=", 1) for pair in env.split(",") if "=" in pair).get(network)
    r = subprocess.run(["security", "find-generic-password", "-D", "AirPort network password", "-a", network, "-w",
                        "/Library/Keychains/System.keychain"], capture_output=True, text=True, timeout=180)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def this_computer_network() -> str | None:
    """The Wi-Fi network this Mac is on, or None. macOS only tells programs that have Location Services
    permission; otherwise it says "<redacted>". (If this computer can reach a robot, it's on that robot's network.)"""
    out = subprocess.run(["ipconfig", "getsummary", "en0"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"^\s*SSID : (.+)$", out, re.M)
    name = m.group(1).strip() if m else None
    return None if not name or name == "<redacted>" else name


# Venue passwords live in the macOS keychain, never in the setup file or the page.
def save_venue_password(network: str, password: str) -> None:
    subprocess.run(["security", "add-generic-password", "-U", "-a", network, "-s", KEYCHAIN_SERVICE, "-w", password],
                   capture_output=True, check=True, timeout=10)


def venue_password(network: str) -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-a", network, "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def forget_venue_password(network: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-a", network, "-s", KEYCHAIN_SERVICE],
                   capture_output=True, timeout=10)
