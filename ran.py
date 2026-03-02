#!/usr/bin/env python3
# =============================================================
# ran.py — RAN VM: ~/UERANSIM/ran.py  (192.168.102.135)
#
# CONTEXT COLLECTION — NO LOG FILES NEEDED
#
# Every source used here exists on any Linux system running
# UERANSIM. None of them require pre-configured log files.
#
# SOURCE 1: /proc/net/sctp/assocs  — kernel SCTP connection table
#           Shows live SCTP state between gNB and AMF right now
#
# SOURCE 2: /proc/net/udp          — kernel UDP socket table
#           GTP-U port 2152 entries = active UE data tunnels
#
# SOURCE 3: /proc/[pid]/ for nr-gnb process
#           cmdline = config file path actually being used
#           status  = memory, threads, process state
#
# SOURCE 4: ss -tn                 — socket statistics (kernel)
#           All connections to/from Core VM IP
#
# SOURCE 5: gNB config YAML       — always exists in UERANSIM dir
#           AMF address, MCC, MNC, TAC, gNB ID
#
# SOURCE 6: journalctl -u nr-gnb  — systemd journal
#           stdout of gNB if run as service (no separate log file)
#
# SOURCE 7: dmesg                 — kernel ring buffer
#           SCTP module errors, network resets
#
# SOURCE 8: ip route / ip link    — routing table, interface state
#           Is the network interface even up?
#
# SOURCE 9: ping Core VM          — is the problem network-level?
#
# SOURCE 10: /proc/net/dev        — packet counters per interface
#            RX/TX bytes, errors, drops
# =============================================================

import subprocess, json, time, datetime, os, sys
import socket, requests, re, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    CENTRAL_FAULT_URL, API_TOKEN, INCIDENTS_FILE,
    SERVICE_UNIT_MAP, CORE_VM_IP,
)
from tools.langchain_tools import (
    classify_layer,
    detect_cascade,
    generate_recovery_steps,
)

POLL_SECONDS = 30
HOSTNAME     = socket.gethostname()

# Where UERANSIM binary and config live on your RAN VM
UERANSIM_DIR    = os.path.expanduser("~/UERANSIM")
GNB_CONFIG_NAME = "open5gs-gnb.yaml"   # change if yours is named differently
UE_CONFIG_NAME  = "open5gs-ue.yaml"

# NGAP port (standard — do not change)
NGAP_PORT       = 38412
GTP_U_PORT      = 2152
GTP_U_PORT_HEX  = "0868"   # 2152 in hex — used in /proc/net/udp


# =============================================================
# SOURCE 1 — /proc/net/sctp/assocs
# Linux kernel tracks every SCTP association here.
# NGAP between gNB and AMF is SCTP-based.
# This is the most direct evidence of gNB↔AMF connection state.
# =============================================================

def get_sctp_state() -> dict:
    """
    Reads /proc/net/sctp/assocs to find the NGAP association
    between this gNB and the AMF on Core VM.

    SCTP states:
      1 = CLOSED
      2 = COOKIE_WAIT
      3 = COOKIE_ECHOED (connecting)
      4 = ESTABLISHED   ← this is what you want
      7 = SHUTDOWN_SENT
    """
    result = {
        "found":               False,
        "state_id":            None,
        "state_name":          "UNKNOWN",
        "local_ip":            None,
        "remote_ip":           None,
        "remote_port":         None,
        "in_streams":          None,
        "out_streams":         None,
        "assoc_id":            None,
        "raw_line":            None,
        "error":               None,
    }

    STATE_NAMES = {
        "1": "CLOSED", "2": "COOKIE_WAIT", "3": "COOKIE_ECHOED",
        "4": "ESTABLISHED", "5": "ESTABLISHED", "6": "SHUTDOWN_PENDING",
        "7": "SHUTDOWN_SENT", "8": "SHUTDOWN_RECEIVED", "9": "SHUTDOWN_ACK_SENT",
    }

    try:
        with open("/proc/net/sctp/assocs", "r") as f:
            lines = f.readlines()

        # Header line first, then data lines
        # Format: ASSOC  SOCK  STY  SST  ST  HBKT  ASSOC-ID  TX_QUEUE  RX_QUEUE  UID
        #         INODE  LPORT  RPORT  LADDRS  RADDRS  HBINT  INS  OUTS  MAXRT  T1X
        #         T2X  RTXC  wmema  wmemq  sndbuf  rcvbuf  STATUS  BIND_ADDR
        # (format varies by kernel version — we search for Core VM IP)

        for line in lines[1:]:   # skip header
            if CORE_VM_IP in line:
                result["found"]   = True
                result["raw_line"] = line.strip()[:120]

                parts = line.split()
                # Extract state
                for i, p in enumerate(parts):
                    if p in STATE_NAMES:
                        result["state_id"]   = p
                        result["state_name"] = STATE_NAMES[p]
                        break

                # Extract remote port
                m = re.search(rf"{re.escape(CORE_VM_IP)}:(\d+)", line)
                if m:
                    result["remote_ip"]   = CORE_VM_IP
                    result["remote_port"] = m.group(1)

                # Extract local IP
                local_ips = re.findall(
                    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", line
                )
                for ip in local_ips:
                    if ip != CORE_VM_IP:
                        result["local_ip"] = ip
                        break
                break

    except FileNotFoundError:
        result["error"] = (
            "/proc/net/sctp/assocs not found — "
            "SCTP kernel module may not be loaded. "
            "Fix: sudo modprobe sctp"
        )
    except PermissionError:
        result["error"] = "Permission denied reading /proc/net/sctp/assocs"
    except Exception as e:
        result["error"] = str(e)

    return result


# =============================================================
# SOURCE 2 — /proc/net/udp
# GTP-U (user plane) runs on UDP port 2152.
# Active entries here = active UE data sessions right now.
# =============================================================

def get_gtp_tunnels() -> dict:
    """
    Counts active GTP-U tunnels from /proc/net/udp.
    Port 2152 (0x0868) = GTP-U = one entry per active UE data session.
    Also checks /proc/net/udp6 for IPv6.
    """
    result = {
        "active_tunnels":  0,
        "local_port_open": False,
        "entries":         [],
        "error":           None,
    }

    for proc_file in ["/proc/net/udp", "/proc/net/udp6"]:
        try:
            with open(proc_file, "r") as f:
                lines = f.readlines()
            for line in lines[1:]:   # skip header
                if GTP_U_PORT_HEX.upper() in line.upper():
                    result["active_tunnels"]  += 1
                    result["local_port_open"]  = True
                    parts = line.split()
                    if len(parts) > 2:
                        result["entries"].append(parts[1])  # local_address:port
        except FileNotFoundError:
            pass
        except Exception as e:
            result["error"] = str(e)

    return result


# =============================================================
# SOURCE 3 — /proc/[pid]/ for nr-gnb process
# Even after a crash, /proc may still have the entry briefly.
# While running, this gives config path and memory usage.
# =============================================================

def get_gnb_process_info() -> dict:
    """
    Finds the nr-gnb process in /proc and reads its details.
    Works while process is running. After crash, tries to find
    any zombie/lingering entry.
    """
    info = {
        "pid":          None,
        "running":      False,
        "cmdline":      None,
        "config_path":  None,
        "mem_rss_mb":   None,
        "threads":      None,
        "state":        None,
        "open_fds":     0,
    }

    # Find nr-gnb PID by scanning /proc/*/comm
    target_pid = None
    for comm_path in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm_path, "r") as f:
                comm = f.read().strip()
            if comm in ("nr-gnb", "nr_gnb"):
                target_pid = comm_path.split("/")[2]
                break
        except Exception:
            pass

    # Also try pgrep as fallback
    if not target_pid:
        try:
            out = subprocess.check_output(
                ["pgrep", "-x", "nr-gnb"],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode().strip()
            if out:
                target_pid = out.split("\n")[0]
        except Exception:
            pass

    if not target_pid:
        info["running"] = False
        return info

    info["pid"]     = int(target_pid)
    info["running"] = True
    base            = f"/proc/{target_pid}"

    # Read cmdline — shows exact command + config file
    try:
        with open(f"{base}/cmdline", "r") as f:
            cmdline = f.read().replace("\x00", " ").strip()
        info["cmdline"] = cmdline[:200]
        # Extract config file path from cmdline
        m = re.search(r"-c\s+(\S+\.yaml)", cmdline)
        if m:
            info["config_path"] = m.group(1)
    except Exception:
        pass

    # Read status — memory, threads, state
    try:
        with open(f"{base}/status", "r") as f:
            status_text = f.read()
        m = re.search(r"VmRSS:\s+(\d+)\s+kB", status_text)
        if m:
            info["mem_rss_mb"] = round(int(m.group(1)) / 1024, 1)
        m = re.search(r"Threads:\s+(\d+)", status_text)
        if m:
            info["threads"] = int(m.group(1))
        m = re.search(r"State:\s+(\S+)", status_text)
        if m:
            info["state"] = m.group(1)
    except Exception:
        pass

    # Count open file descriptors
    try:
        fds = glob.glob(f"{base}/fd/*")
        info["open_fds"] = len(fds)
    except Exception:
        pass

    return info


# =============================================================
# SOURCE 4 — ss -tn  (socket statistics, kernel-level)
# Shows all TCP connections involving Core VM IP.
# Also shows if NGAP port 38412 is in ESTABLISHED or CLOSE_WAIT.
# =============================================================

def get_socket_state() -> dict:
    """
    Uses 'ss' to check socket state for connections to/from Core VM.
    ss reads directly from kernel netlink — no log file needed.
    """
    result = {
        "connections_to_core": [],
        "ngap_established":    False,
        "ngap_port_state":     "NOT_FOUND",
        "error":               None,
    }

    try:
        # -t = TCP, -n = numeric, -p = process (may need root)
        out = subprocess.check_output(
            ["ss", "-tn", f"dst {CORE_VM_IP}"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode()

        for line in out.strip().split("\n")[1:]:  # skip header
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 4:
                state       = parts[0]
                local_addr  = parts[3] if len(parts) > 3 else ""
                remote_addr = parts[4] if len(parts) > 4 else ""
                result["connections_to_core"].append({
                    "state":  state,
                    "local":  local_addr,
                    "remote": remote_addr,
                })
                if str(NGAP_PORT) in remote_addr:
                    result["ngap_port_state"]  = state
                    result["ngap_established"] = (state == "ESTAB")

    except FileNotFoundError:
        result["error"] = "ss command not found"
    except Exception as e:
        result["error"] = str(e)

    return result


# =============================================================
# SOURCE 5 — gNB config YAML file
# Always exists in UERANSIM directory.
# Critical: AMF address, MCC, MNC, TAC must match Core VM config.
# =============================================================

def parse_gnb_config() -> dict:
    """
    Parses open5gs-gnb.yaml to extract all config parameters.
    These values must match /etc/open5gs/amf.yaml on Core VM.
    A mismatch here = NG Setup failure even if network is fine.
    """
    ctx = {
        "found":       False,
        "path":        "",
        "amf_address": "unknown",
        "amf_port":    str(NGAP_PORT),
        "mcc":         "unknown",
        "mnc":         "unknown",
        "tac":         "unknown",
        "gnb_id":      "unknown",
        "link_ip":     "unknown",
        "ngap_ip":     "unknown",
        "gtp_ip":      "unknown",
        "slice_sst":   "unknown",
    }

    # Find config file
    candidates = [
        os.path.join(UERANSIM_DIR, "config", GNB_CONFIG_NAME),
        os.path.join(UERANSIM_DIR, GNB_CONFIG_NAME),
        f"/etc/ueransim/{GNB_CONFIG_NAME}",
    ]
    # Also use path discovered from process cmdline
    proc_info = get_gnb_process_info()
    if proc_info.get("config_path"):
        candidates.insert(0, proc_info["config_path"])

    config_path = None
    for p in candidates:
        if os.path.exists(p):
            config_path = p
            break

    if not config_path:
        ctx["error"] = (
            f"Config file not found. Looked in: {candidates}"
        )
        return ctx

    ctx["found"] = True
    ctx["path"]  = config_path

    try:
        with open(config_path, "r") as f:
            content = f.read()

        # AMF address
        m = re.search(
            r"amfConfigs.*?address:\s*['\"]?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})",
            content, re.DOTALL
        )
        if not m:
            m = re.search(
                r"address:\s*['\"]?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})['\"]?",
                content
            )
        if m:
            ctx["amf_address"] = m.group(1)

        # MCC
        m = re.search(r"mcc:\s*['\"]?(\w+)['\"]?", content)
        if m:
            ctx["mcc"] = m.group(1)

        # MNC
        m = re.search(r"mnc:\s*['\"]?(\w+)['\"]?", content)
        if m:
            ctx["mnc"] = m.group(1)

        # TAC
        m = re.search(r"tac:\s*([0-9a-fxA-FX]+)", content)
        if m:
            ctx["tac"] = m.group(1)

        # gNB ID
        m = re.search(r"gnbId:\s*(\d+)", content)
        if m:
            ctx["gnb_id"] = m.group(1)

        # IPs
        for key, pattern in [
            ("link_ip", r"linkIp:\s*['\"]?(\S+?)['\"]?\s"),
            ("ngap_ip", r"ngapIp:\s*['\"]?(\S+?)['\"]?\s"),
            ("gtp_ip",  r"gtpIp:\s*['\"]?(\S+?)['\"]?\s"),
        ]:
            m = re.search(pattern, content)
            if m:
                ctx[key] = m.group(1)

        # Slice SST
        m = re.search(r"sst:\s*(\d+)", content)
        if m:
            ctx["slice_sst"] = m.group(1)

    except Exception as e:
        ctx["parse_error"] = str(e)

    return ctx


# =============================================================
# SOURCE 6 — journalctl for nr-gnb
# If gNB runs as systemd service, all stdout is in journal.
# This is the standard way to run UERANSIM in production.
# =============================================================

def get_journal_logs(service: str = "gnb", lines: int = 40) -> list[str]:
    """
    Reads systemd journal for nr-gnb or nr-ue service.
    This captures all stdout output without needing a separate log file.
    Only works if gNB/UE is started via systemd (recommended).
    """
    unit_names = {
        "gnb": ["nr-gnb", "nr-gnb.service", "ueransim-gnb"],
        "ue":  ["nr-ue",  "nr-ue.service",  "ueransim-ue"],
    }

    for unit in unit_names.get(service, []):
        try:
            out = subprocess.check_output(
                ["journalctl", "-u", unit, "-n", str(lines),
                 "--no-pager", "--output=short-precise"],
                stderr=subprocess.DEVNULL, timeout=10
            ).decode(errors="replace")
            result = [l for l in out.strip().split("\n") if l.strip()]
            if len(result) > 2:   # more than just header lines
                return result
        except Exception:
            pass

    return []


# =============================================================
# SOURCE 7 — dmesg kernel ring buffer
# SCTP errors, connection resets, memory issues appear here.
# Especially useful for: SCTP module errors, ENOMEM, TCP resets.
# =============================================================

def get_dmesg_sctp_errors() -> list[str]:
    """
    Filters dmesg for SCTP and network-related kernel messages
    from the last 10 minutes. No log file needed — kernel buffer.
    """
    try:
        out = subprocess.check_output(
            ["dmesg", "--time-format=iso", "--since", "-10min"],
            stderr=subprocess.DEVNULL, timeout=10
        ).decode(errors="replace")
    except Exception:
        try:
            # Older dmesg without --since support
            out = subprocess.check_output(
                ["dmesg", "-T"],
                stderr=subprocess.DEVNULL, timeout=10
            ).decode(errors="replace")
            # Take last 50 lines
            out = "\n".join(out.strip().split("\n")[-50:])
        except Exception:
            return []

    keywords = ["sctp", "ngap", "gtp", "ueransim", "nr-gnb",
                "enomem", "connection reset", "port unreachable"]
    relevant = [
        line for line in out.split("\n")
        if any(kw in line.lower() for kw in keywords)
    ]
    return relevant[-10:]   # last 10 relevant lines


# =============================================================
# SOURCE 8 — ip route + ip link
# Is the network interface even up? Can it reach Core VM subnet?
# =============================================================

def get_network_interface_state() -> dict:
    """
    Checks if the RAN VM's network interface is up and has a route
    to the Core VM subnet (192.168.102.0/24).
    """
    result = {
        "interface_up":      False,
        "interface_name":    None,
        "interface_ip":      None,
        "route_to_core":     False,
        "route_detail":      None,
        "error":             None,
    }

    # Check route to Core VM
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", CORE_VM_IP],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode()
        result["route_to_core"] = True
        result["route_detail"]  = out.strip()[:120]

        # Extract interface name
        m = re.search(r"dev\s+(\S+)", out)
        if m:
            result["interface_name"] = m.group(1)

        # Extract source IP
        m = re.search(r"src\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out)
        if m:
            result["interface_ip"] = m.group(1)

    except subprocess.CalledProcessError:
        result["error"] = f"No route to {CORE_VM_IP}"
        result["route_to_core"] = False
    except Exception as e:
        result["error"] = str(e)

    # Check interface state
    if result["interface_name"]:
        try:
            out = subprocess.check_output(
                ["ip", "link", "show", result["interface_name"]],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode()
            result["interface_up"] = "state UP" in out
        except Exception:
            pass

    return result


# =============================================================
# SOURCE 9 — ping Core VM
# Four ICMP packets. Most reliable yes/no for network reachability.
# =============================================================

def ping_core_vm() -> dict:
    result = {
        "reachable":       False,
        "latency_ms":      None,
        "packet_loss_pct": 100,
    }
    try:
        out = subprocess.check_output(
            ["ping", "-c", "4", "-W", "2", CORE_VM_IP],
            stderr=subprocess.DEVNULL, timeout=15
        ).decode()

        m = re.search(r"(\d+)% packet loss", out)
        if m:
            result["packet_loss_pct"] = int(m.group(1))
            result["reachable"]       = result["packet_loss_pct"] < 50

        m = re.search(r"rtt.+?=\s*[\d.]+/([\d.]+)/", out)
        if m:
            result["latency_ms"] = float(m.group(1))

    except Exception:
        pass
    return result


# =============================================================
# SOURCE 10 — /proc/net/dev (network packet counters)
# Shows RX/TX bytes, errors, drops per interface.
# Drop counter increasing = network congestion or MTU issue.
# =============================================================

def get_interface_counters(interface: str = None) -> dict:
    """
    Reads /proc/net/dev for packet counters on the relevant interface.
    Errors and drops here indicate network-level problems.
    """
    counters = {}
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()

        for line in lines[2:]:   # skip two header lines
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue   # skip loopback
            if interface and iface != interface:
                continue

            vals = data.split()
            if len(vals) >= 16:
                counters[iface] = {
                    "rx_bytes":   int(vals[0]),
                    "rx_packets": int(vals[1]),
                    "rx_errors":  int(vals[2]),
                    "rx_drops":   int(vals[3]),
                    "tx_bytes":   int(vals[8]),
                    "tx_packets": int(vals[9]),
                    "tx_errors":  int(vals[10]),
                    "tx_drops":   int(vals[11]),
                }

    except Exception as e:
        counters["error"] = str(e)

    return counters


# =============================================================
# SYSTEM METRICS from /proc directly
# =============================================================

def get_system_metrics() -> dict:
    metrics = {}

    # CPU from /proc/stat
    try:
        with open("/proc/stat", "r") as f:
            cpu_line = f.readline()
        parts = [int(x) for x in cpu_line.split()[1:8]]
        total = sum(parts)
        idle  = parts[3]
        metrics["cpu_pct"] = round((1 - idle / total) * 100, 1) if total else 0
    except Exception:
        pass

    # Memory from /proc/meminfo
    try:
        mem = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.strip().split()[0])
        if "MemTotal" in mem and "MemAvailable" in mem:
            total_mb = mem["MemTotal"] // 1024
            used_mb  = total_mb - mem["MemAvailable"] // 1024
            metrics["mem_used_mb"]  = used_mb
            metrics["mem_total_mb"] = total_mb
            metrics["mem_pct"]      = round(used_mb / total_mb * 100, 1)
    except Exception:
        pass

    # Uptime from /proc/uptime
    try:
        with open("/proc/uptime", "r") as f:
            secs = float(f.read().split()[0])
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        metrics["uptime"] = f"{h}h {m}m"
    except Exception:
        pass

    return metrics


# =============================================================
# UNRECOVERABLE FAULT CLASSIFICATION
# Combines all sources to determine if restart will fix the fault
# =============================================================

UNRECOVERABLE_PATTERNS = {
    "no_route_to_core": {
        "check": lambda d: not d["net_iface"]["route_to_core"],
        "tier":  3,
        "cause": "No network route to Core VM — VMware network adapter issue",
        "action": (
            "1. In VMware: check both VMs use same network adapter (NAT or Host-only)\n"
            "2. On RAN VM: ip route show — verify 192.168.102.0/24 route exists\n"
            "3. On RAN VM: ip link show — verify interface is UP\n"
            "4. Try: sudo dhclient <interface> to renew IP"
        ),
    },
    "core_vm_unreachable": {
        "check": lambda d: (d["net_iface"]["route_to_core"]
                            and not d["ping"]["reachable"]),
        "tier":  3,
        "cause": f"Route exists but Core VM ({CORE_VM_IP}) not responding to ping",
        "action": (
            "1. Check Core VM is powered on in VMware\n"
            "2. On Core VM: check ip link show — interface must be UP\n"
            "3. Check open5gs-amfd is running on Core VM\n"
            "4. sudo systemctl status open5gs-amfd"
        ),
    },
    "sctp_no_association": {
        "check": lambda d: (d["ping"]["reachable"]
                            and not d["sctp"]["found"]
                            and d["socket"]["ngap_port_state"] not in ("ESTAB",)),
        "tier":  2,
        "cause": "Network reachable but no SCTP association to AMF — NGAP not connected",
        "action": (
            "1. On Core VM: sudo ss -tlnp | grep 38412 — AMF must be listening\n"
            "2. If empty: sudo systemctl restart open5gs-amfd\n"
            "3. Check firewall: sudo ufw status (38412 must allow SCTP)\n"
            "4. Compare MCC/MNC in gNB config vs AMF config"
        ),
    },
    "config_amf_address_wrong": {
        "check": lambda d: (d["ping"]["reachable"]
                            and d["gnb_cfg"]["amf_address"] != CORE_VM_IP
                            and d["gnb_cfg"]["found"]),
        "tier":  2,
        "cause": (f"gNB config points to wrong AMF address: "
                  f"config says {{d['gnb_cfg']['amf_address']}} "
                  f"but Core VM is {CORE_VM_IP}"),
        "action": (
            f"Edit ~/UERANSIM/config/open5gs-gnb.yaml:\n"
            f"  Find 'amfConfigs' section\n"
            f"  Change address to {CORE_VM_IP}\n"
            f"  Then restart gNB"
        ),
    },
}


def classify_recoverability(collected: dict) -> dict:
    """
    Checks all collected data against unrecoverable patterns.
    Returns tier (1/2/3), cause, and required action.
    """
    for name, pattern in UNRECOVERABLE_PATTERNS.items():
        try:
            # For config_amf_address_wrong, format the cause string
            cause = pattern["cause"]
            if "{d[" in cause:
                cause = cause.format(d=collected)

            if pattern["check"](collected):
                return {
                    "tier":                pattern["tier"],
                    "pattern":             name,
                    "cause":               cause,
                    "action":              pattern["action"],
                    "escalation_required": pattern["tier"] == 3,
                }
        except Exception:
            pass

    return {
        "tier":                1,
        "pattern":             "process_crash",
        "cause":               "UERANSIM process terminated — restart may fix it",
        "action":              "sudo systemctl restart nr-gnb",
        "escalation_required": False,
    }


# =============================================================
# BUILD FULL ENRICHED PAYLOAD
# Collects from all 10 sources and assembles incident payload
# =============================================================

def build_payload(service: str, status: str) -> dict:
    print(f"\n[RAN Agent] Collecting context for {service.upper()} fault...")

    # Collect from all sources
    print(f"[RAN Agent]   [1/8] Reading /proc/net/sctp/assocs...")
    sctp        = get_sctp_state()

    print(f"[RAN Agent]   [2/8] Reading /proc/net/udp (GTP-U tunnels)...")
    gtp         = get_gtp_tunnels()

    print(f"[RAN Agent]   [3/8] Reading /proc/[pid] for nr-gnb...")
    proc        = get_gnb_process_info()

    print(f"[RAN Agent]   [4/8] Running ss -tn to Core VM...")
    socket_st   = get_socket_state()

    print(f"[RAN Agent]   [5/8] Parsing gNB config YAML...")
    gnb_cfg     = parse_gnb_config()

    print(f"[RAN Agent]   [6/8] Reading journalctl for nr-gnb...")
    journal     = get_journal_logs(service)

    print(f"[RAN Agent]   [7/8] Reading dmesg for SCTP errors...")
    dmesg_errs  = get_dmesg_sctp_errors()

    print(f"[RAN Agent]   [8/8] Checking network + ping Core VM...")
    net_iface   = get_network_interface_state()
    ping        = ping_core_vm()
    iface_name  = net_iface.get("interface_name","")
    if_counters = get_interface_counters(iface_name) if iface_name else {}
    sys_metrics = get_system_metrics()

    print(f"[RAN Agent] SCTP state: {sctp['state_name']} | "
          f"GTP tunnels: {gtp['active_tunnels']} | "
          f"Ping: {'OK' if ping['reachable'] else 'FAIL'} | "
          f"Journal lines: {len(journal)}")

    # Bundle for classifier
    collected = {
        "sctp": sctp, "gtp": gtp, "proc": proc,
        "socket": socket_st, "gnb_cfg": gnb_cfg,
        "net_iface": net_iface, "ping": ping,
    }

    # Classify recoverability
    recov = classify_recoverability(collected)
    print(f"[RAN Agent] Tier: {recov['tier']} | "
          f"Pattern: {recov['pattern']} | "
          f"Escalation: {recov['escalation_required']}")

    # Build fault message from real collected data
    if not net_iface["route_to_core"]:
        fault_msg = f"gnb FAULT: No network route to Core VM {CORE_VM_IP}"
    elif not ping["reachable"]:
        fault_msg = (f"gnb NGAP LOST: Core VM {CORE_VM_IP} unreachable "
                     f"({ping['packet_loss_pct']}% packet loss)")
    elif sctp["found"] and sctp["state_name"] != "ESTABLISHED":
        fault_msg = (f"gnb SCTP association in state {sctp['state_name']} "
                     f"(expected ESTABLISHED)")
    elif journal:
        # Use last error line from journal as message
        last_err = next(
            (l for l in reversed(journal) if "error" in l.lower()), journal[-1]
        )
        fault_msg = f"gnb fault: {last_err[-100:]}"
    else:
        fault_msg = f"{service} service is {status.upper()} (Service Crash)"

    # LangChain tools
    layer_ctx        = classify_layer.invoke(service)
    cascade_result   = detect_cascade.invoke(service)
    cascade_detected = "CASCADE DETECTED" in cascade_result

    if recov["tier"] == 3:
        suggestion = (
            f"UNRECOVERABLE — restart will NOT fix this.\n"
            f"Cause: {recov['cause']}\n"
            f"Required action:\n{recov['action']}"
        )
    else:
        ctx = f"service: {service}, fault: Service Crash"
        if cascade_detected:
            ctx += ", cascade from amf"
        suggestion = generate_recovery_steps.invoke(ctx)

    return {
        # Standard fields
        "source":    "ran-agent",
        "host":      HOSTNAME,
        "vm_ip":     "192.168.102.135",
        "service":   service,
        "type":      "ran",
        "layer":     "ran",
        "level":     "CRITICAL" if recov["tier"] == 3 else "ERROR",
        "status":    "FAILED",
        "message":   fault_msg,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "action_log": "pending — LangGraph on Central VM",
        "suggestion": suggestion[:600],

        # Recoverability
        "fault_tier":          recov["tier"],
        "fault_pattern":       recov["pattern"],
        "escalation_required": recov["escalation_required"],
        "unrecoverable_cause": recov["cause"],
        "escalation_action":   recov["action"] if recov["tier"] == 3 else "",

        # Source 1: /proc/net/sctp/assocs
        "sctp": {
            "association_found":   sctp["found"],
            "state":               sctp["state_name"],
            "local_ip":            sctp["local_ip"],
            "remote_ip":           sctp["remote_ip"],
            "remote_port":         sctp["remote_port"],
            "error":               sctp.get("error"),
        },

        # Source 2: /proc/net/udp (GTP-U)
        "gtp": {
            "active_tunnels":      gtp["active_tunnels"],
            "port_2152_open":      gtp["local_port_open"],
        },

        # Source 3: /proc/[pid]
        "process": {
            "pid":                 proc["pid"],
            "was_running":         proc["running"],
            "cmdline":             proc.get("cmdline",""),
            "config_used":         proc.get("config_path",""),
            "mem_rss_mb":          proc.get("mem_rss_mb"),
            "threads":             proc.get("threads"),
        },

        # Source 4: ss output
        "sockets": {
            "ngap_state":          socket_st["ngap_port_state"],
            "ngap_established":    socket_st["ngap_established"],
            "connections_to_core": len(socket_st["connections_to_core"]),
        },

        # Source 5: gNB config YAML
        "gnb_config": {
            "file_found":   gnb_cfg["found"],
            "file_path":    gnb_cfg["path"],
            "amf_address":  gnb_cfg["amf_address"],
            "mcc":          gnb_cfg["mcc"],
            "mnc":          gnb_cfg["mnc"],
            "tac":          gnb_cfg["tac"],
            "gnb_id":       gnb_cfg["gnb_id"],
            "ngap_ip":      gnb_cfg["ngap_ip"],
            "gtp_ip":       gnb_cfg["gtp_ip"],
        },

        # Source 6: journalctl
        "journal": {
            "lines_found":  len(journal),
            "last_10_lines": journal[-10:],
        },

        # Source 7: dmesg
        "dmesg_errors": dmesg_errs,

        # Source 8: network interface
        "network": {
            "interface":              net_iface["interface_name"],
            "interface_ip":           net_iface["interface_ip"],
            "interface_up":           net_iface["interface_up"],
            "route_to_core_exists":   net_iface["route_to_core"],
            "ping_reachable":         ping["reachable"],
            "ping_loss_pct":          ping["packet_loss_pct"],
            "ping_latency_ms":        ping.get("latency_ms"),
            "rx_errors":              if_counters.get(iface_name,{}).get("rx_errors",0),
            "tx_errors":              if_counters.get(iface_name,{}).get("tx_errors",0),
            "rx_drops":               if_counters.get(iface_name,{}).get("rx_drops",0),
        },

        # System metrics
        "system": {
            "cpu_pct":      sys_metrics.get("cpu_pct"),
            "mem_used_mb":  sys_metrics.get("mem_used_mb"),
            "mem_pct":      sys_metrics.get("mem_pct"),
            "uptime":       sys_metrics.get("uptime"),
        },

        # LangChain
        "layer_context":    layer_ctx[:400],
        "cascade_detected": cascade_detected,
        "cascade_detail":   cascade_result[:400],
    }


# =============================================================
# SEND + SAVE + MONITOR
# =============================================================

def send_to_central(payload: dict) -> bool:
    try:
        resp = requests.post(
            CENTRAL_FAULT_URL, json=payload,
            headers={
                "Content-Type":   "application/json",
                "Authorization":  f"Bearer {API_TOKEN}",
                "X-Agent-Source": "ran-agent",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"[RAN Agent] Sent → Central ID={resp.json().get('id','?')}")
            return True
        print(f"[RAN Agent] Central returned {resp.status_code}")
        return False
    except requests.ConnectionError:
        print(f"[RAN Agent] Central unreachable — local save only")
        return False
    except Exception as e:
        print(f"[RAN Agent] Send error: {e}")
        return False


def save_locally(payload: dict):
    try:
        existing = []
        os.makedirs("data", exist_ok=True)
        if os.path.exists(INCIDENTS_FILE):
            with open(INCIDENTS_FILE, "r") as f:
                existing = json.load(f)
        existing.append({"id": len(existing) + 1, **payload})
        with open(INCIDENTS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"[RAN Agent] Local save error: {e}")


def check_ran_process(service: str) -> str:
    unit = SERVICE_UNIT_MAP.get(service, f"nr-{service}")
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", unit],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        return out
    except subprocess.CalledProcessError as e:
        return (e.output or b"failed").decode().strip()
    except Exception:
        pass
    try:
        subprocess.check_output(
            ["pgrep", "-x", unit], stderr=subprocess.DEVNULL, timeout=5
        )
        return "active"
    except Exception:
        return "failed"


known_faults: set[str] = set()


def monitor_loop():
    print(f"[RAN Agent] Starting on {HOSTNAME} (192.168.102.135)")
    print(f"[RAN Agent] Context sources: sctp/proc/ss/config/journal/dmesg/ping")
    print(f"[RAN Agent] Central: {CENTRAL_FAULT_URL}\n")

    while True:
        for service in ["gnb", "ue"]:
            status = check_ran_process(service)

            if status in ("failed", "inactive") and service not in known_faults:
                print(f"[RAN Agent] FAULT: {service.upper()} is {status}")
                known_faults.add(service)

                payload = build_payload(service, status)
                save_locally(payload)
                sent = send_to_central(payload)

                if payload.get("escalation_required"):
                    print(f"\n[RAN Agent] ESCALATION REQUIRED:")
                    print(f"  Cause:  {payload['unrecoverable_cause']}")
                    print(f"  Action: {payload['escalation_action'][:100]}")

                if not sent:
                    print("[RAN Agent] Saved locally (Central unreachable)")

            elif status == "active" and service in known_faults:
                known_faults.discard(service)
                print(f"[RAN Agent] RECOVERED: {service.upper()}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        monitor_loop()
    except KeyboardInterrupt:
        print("\n[RAN Agent] Stopped.")
