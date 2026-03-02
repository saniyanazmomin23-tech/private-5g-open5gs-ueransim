# =============================================================
# config/settings.py
# SHARED CONFIG — same file goes on all 3 VMs
# Core VM:    /opt/agent-core/config/settings.py
# RAN VM:     ~/UERANSIM/config/settings.py
# Central VM: /opt/central/config/settings.py
# =============================================================

# ── LLM Backend ──────────────────────────────────────────────
OLLAMA_BASE_URL = "https://vocalic-gratulant-kim.ngrok-free.dev"
OLLAMA_MODEL    = "mistral:latest"          # confirmed available
EMBED_MODEL     = "nomic-embed-text"

# ngrok requires this header to skip the browser warning page
OLLAMA_HEADERS  = {
    "ngrok-skip-browser-warning": "true",
    "Content-Type": "application/json",
}

# ── LLM Parameters ───────────────────────────────────────────
LLM_TEMPERATURE  = 0.1
LLM_MAX_TOKENS   = 500
REQUEST_TIMEOUT  = 120

# ── VM Network ───────────────────────────────────────────────
CORE_VM_IP     = "192.168.102.161"
RAN_VM_IP      = "192.168.102.166"
CENTRAL_VM_IP  = "192.168.102.162"
CENTRAL_PORT   = 5050

API_TOKEN         = "5g-fault-agent-2025"
CENTRAL_FAULT_URL = f"http://{CENTRAL_VM_IP}:{CENTRAL_PORT}/api/fault"
CENTRAL_CHAT_URL  = f"http://{CENTRAL_VM_IP}:{CENTRAL_PORT}/api/chat"

# ── File Paths ────────────────────────────────────────────────
INCIDENTS_FILE = "data/incidents.json"
VECTOR_DB_PATH = "data/faiss_index"
RESULTS_FILE   = "data/agent_results.json"

# ── All Open5GS Services (confirmed from your systemctl output) ──
# 5G SA Core (NR) services
CORE_5G_SERVICES = [
    "nrf",   # Network Repository Function   — must start first
    "scp",   # Service Communication Proxy   — depends on nrf
    "ausf",  # Authentication Server         — depends on nrf
    "udm",   # Unified Data Management       — depends on nrf
    "udr",   # Unified Data Repository       — depends on nrf
    "pcf",   # Policy Control Function       — depends on nrf
    "nssf",  # Network Slice Selection       — depends on nrf
    "bsf",   # Binding Support Function      — depends on nrf
    "sepp",  # Security Edge Protection      — depends on nrf
    "amf",   # Access & Mobility Mgmt        — depends on nrf
    "smf",   # Session Management            — depends on nrf, upf
    "upf",   # User Plane Function           — no dependencies
]

# 4G EPC (LTE) services — also running on your Core VM
CORE_4G_SERVICES = [
    "mme",   # Mobility Management Entity    — 4G equivalent of AMF
    "sgwc",  # Serving Gateway Control       — 4G
    "sgwu",  # Serving Gateway User Plane    — 4G
    "hss",   # Home Subscriber Server        — 4G equivalent of UDM/UDR
    "pcrf",  # Policy Charging Rules         — 4G equivalent of PCF
]

# RAN services (UERANSIM on RAN VM)
RAN_SERVICES = ["gnb", "ue"]

# Combined lists
CORE_SERVICES = CORE_5G_SERVICES + CORE_4G_SERVICES
ALL_SERVICES  = CORE_SERVICES + RAN_SERVICES

# ── systemctl unit name for EVERY service ─────────────────────
SERVICE_UNIT_MAP = {
    # 5G SA Core
    "nrf":   "open5gs-nrfd",
    "scp":   "open5gs-scpd",
    "ausf":  "open5gs-ausfd",
    "udm":   "open5gs-udmd",
    "udr":   "open5gs-udrd",
    "pcf":   "open5gs-pcfd",
    "nssf":  "open5gs-nssfd",
    "bsf":   "open5gs-bsfd",
    "sepp":  "open5gs-seppd",
    "amf":   "open5gs-amfd",
    "smf":   "open5gs-smfd",
    "upf":   "open5gs-upfd",
    # 4G EPC
    "mme":   "open5gs-mmed",
    "sgwc":  "open5gs-sgwcd",
    "sgwu":  "open5gs-sgwud",
    "hss":   "open5gs-hssd",
    "pcrf":  "open5gs-pcrfd",
    # RAN
    "gnb":   "nr-gnb",
    "ue":    "nr-ue",
}

# ── Open5GS log file paths (on Core VM) ───────────────────────
LOG_PATHS = {
    # 5G SA Core
    "nrf":   "/var/log/open5gs/nrf.log",
    "scp":   "/var/log/open5gs/scp.log",
    "ausf":  "/var/log/open5gs/ausf.log",
    "udm":   "/var/log/open5gs/udm.log",
    "udr":   "/var/log/open5gs/udr.log",
    "pcf":   "/var/log/open5gs/pcf.log",
    "nssf":  "/var/log/open5gs/nssf.log",
    "bsf":   "/var/log/open5gs/bsf.log",
    "sepp":  "/var/log/open5gs/sepp.log",
    "amf":   "/var/log/open5gs/amf.log",
    "smf":   "/var/log/open5gs/smf.log",
    "upf":   "/var/log/open5gs/upf.log",
    # 4G EPC
    "mme":   "/var/log/open5gs/mme.log",
    "sgwc":  "/var/log/open5gs/sgwc.log",
    "sgwu":  "/var/log/open5gs/sgwu.log",
    "hss":   "/var/log/open5gs/hss.log",
    "pcrf":  "/var/log/open5gs/pcrf.log",
}

# ── 5G Dependency Graph ───────────────────────────────────────
# Which services must be running for each service to work.
# Used by detect_cascade tool to find the real root cause.
#
# Open5GS startup order:
#   NRF → SCP → UDR → UDM → AUSF → BSF → PCF → NSSF
#       → AMF → SMF → UPF (independent)
#   4G: MME → SGW-C/U → HSS → PCRF
SERVICE_DEPS = {
    # 5G SA — no dependencies (start first)
    "nrf":  [],
    "upf":  [],

    # 5G SA — depend on NRF
    "scp":  ["nrf"],
    "udr":  ["nrf"],
    "udm":  ["nrf", "udr"],
    "ausf": ["nrf", "udm"],
    "pcf":  ["nrf", "udr"],
    "nssf": ["nrf"],
    "bsf":  ["nrf"],
    "sepp": ["nrf"],
    "amf":  ["nrf"],
    "smf":  ["nrf", "upf"],

    # 4G EPC — independent stack
    "mme":  ["hss"],
    "sgwc": [],
    "sgwu": [],
    "hss":  [],
    "pcrf": [],

    # RAN
    "gnb":  ["amf"],
    "ue":   ["gnb", "amf", "smf"],
}

# ── Layer mapping ─────────────────────────────────────────────
LAYER_MAP = {
    # 5G SA Core
    "nrf":  "core", "scp":  "core", "ausf": "core",
    "udm":  "core", "udr":  "core", "pcf":  "core",
    "nssf": "core", "bsf":  "core", "sepp": "core",
    "amf":  "core", "smf":  "core", "upf":  "core",
    # 4G EPC
    "mme":  "core", "sgwc": "core", "sgwu": "core",
    "hss":  "core", "pcrf": "core",
    # RAN
    "gnb":  "ran",  "ue":   "ran",
}

# ── Service roles (used by classify_layer tool) ───────────────
SERVICE_ROLES = {
    "nrf":  "5G SA | Network Repository — service discovery for ALL NFs, must start first",
    "scp":  "5G SA | Service Communication Proxy — routes NF-to-NF messages via SBI",
    "ausf": "5G SA | Authentication Server — handles UE authentication, depends on UDM",
    "udm":  "5G SA | Unified Data Management — subscriber profile management",
    "udr":  "5G SA | Unified Data Repository — stores subscriber data, UDM reads from here",
    "pcf":  "5G SA | Policy Control Function — QoS and charging policy decisions",
    "nssf": "5G SA | Network Slice Selection — selects network slice for UE",
    "bsf":  "5G SA | Binding Support Function — PCF binding for PDU sessions",
    "sepp": "5G SA | Security Edge Protection — inter-PLMN security gateway",
    "amf":  "5G SA | Access & Mobility Management — UE registration, NGAP with gNB",
    "smf":  "5G SA | Session Management — creates PDU sessions, controls UPF via N4",
    "upf":  "5G SA | User Plane Function — forwards data packets, GTP-U to gNB",
    "mme":  "4G EPC | Mobility Management Entity — 4G control plane, equivalent to AMF",
    "sgwc": "4G EPC | Serving GW Control — 4G session control",
    "sgwu": "4G EPC | Serving GW User Plane — 4G data forwarding",
    "hss":  "4G EPC | Home Subscriber Server — 4G subscriber database, equivalent to UDM+UDR",
    "pcrf": "4G EPC | Policy Charging Rules — 4G policy, equivalent to PCF",
    "gnb":  "RAN | gNodeB (UERANSIM) — 5G base station, NGAP to AMF, GTP-U to UPF",
    "ue":   "RAN | User Equipment (UERANSIM) — connects via gNB, NAS to AMF",
}

# ── Cascade detection window ──────────────────────────────────
CASCADE_WINDOW_MINUTES = 90

# ── LangGraph settings ────────────────────────────────────────
AGENT_MAX_ITERATIONS = 6
AGENT_VERBOSE        = True
