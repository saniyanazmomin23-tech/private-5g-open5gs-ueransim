# =============================================================
# tools/langchain_tools.py
# ALL 7 LANGCHAIN @tool FUNCTIONS
#
# WHY @tool?
#   The @tool decorator from LangChain does two things:
#   1. Wraps your function so the LangChain ReAct agent can call it
#   2. Exposes the docstring as the "description" the LLM reads
#      to decide WHEN and HOW to use this tool
#
#   Without @tool, these are just regular Python functions.
#   With @tool, the LLM can autonomously decide:
#     "I need to check cascade" → calls detect_cascade
#     "I need past similar faults" → calls search_incidents
#
# WHICH VMs USE THIS FILE?
#   Core VM (/opt/agent-core/tools/langchain_tools.py):
#     Uses: classify_layer, detect_cascade, generate_recovery_steps
#   RAN VM (~/UERANSIM/tools/langchain_tools.py):
#     Uses: classify_layer, detect_cascade, generate_recovery_steps
#   Central VM (/opt/central/tools/langchain_tools.py):
#     Uses: ALL 7 tools inside LangGraph Node 5 (LangChain agent)
# =============================================================

import json
import os
import datetime
from langchain.tools import tool
from config.settings import (
    INCIDENTS_FILE, RESULTS_FILE, ALL_SERVICES,
    LAYER_MAP, SERVICE_DEPS, CASCADE_WINDOW_MINUTES,
    SERVICE_UNIT_MAP
)


# ═══════════════════════════════════════════════════════════════
# TOOL 1 — search_incidents
# Uses FAISS vector store (RAG) to find past similar incidents
# ═══════════════════════════════════════════════════════════════
@tool
def search_incidents(query: str) -> str:
    """
    Semantically searches ALL past 5G fault incidents for cases
    similar to the current fault. Uses FAISS vector embeddings so
    it finds matches even when exact words don't match.
    Example: "core network problem" matches "SMF service crash".

    Input : free text description of the current fault.
    Returns: top 3 most similar past incidents with service name,
             fault type, status, and what recovery action was used.

    Use this FIRST for any new fault — it shows what worked before.
    """
    try:
        from agents.memory_builder import get_retriever
        retriever = get_retriever(k=3)
        docs = retriever.get_relevant_documents(query)
        if not docs:
            return "No similar past incidents found in the database."
        out = [f"Found {len(docs)} similar past incidents:\n"]
        for i, d in enumerate(docs, 1):
            m = d.metadata
            out.append(
                f"--- Past Incident #{i} (ID: {m.get('id','?')}) ---\n"
                f"  Service:  {m.get('service','?').upper()}\n"
                f"  Fault:    {m.get('fault','?')}\n"
                f"  Status:   {m.get('status','?')}\n"
                f"  Recovery: {m.get('recovery','?')}\n"
                f"  Hint:     {m.get('suggestion','?')[:100]}"
            )
        return "\n".join(out)
    except Exception as e:
        return f"RAG search error: {e}. Make sure FAISS index is built first."


# ═══════════════════════════════════════════════════════════════
# TOOL 2 — get_service_history
# Returns all recorded faults for one service
# ═══════════════════════════════════════════════════════════════
@tool
def get_service_history(service_name: str) -> str:
    """
    Returns the complete fault history for a specific 5G service.
    Shows every recorded incident — date, fault type, status, action.

    Input : service short name: amf, smf, upf, ausf, nrf, gnb, ue
    Returns: all incidents for that service + resolved vs failed count.

    Use this to check if a service is a repeat offender,
    and to see which recovery actions worked in the past.
    """
    svc = service_name.lower().strip()
    try:
        with open(INCIDENTS_FILE, "r") as f:
            all_inc = json.load(f)
    except FileNotFoundError:
        return f"incidents.json not found at {INCIDENTS_FILE}"

    matches = [i for i in all_inc if i.get("service", "").lower() == svc]
    if not matches:
        return f"No incidents found for {svc.upper()}."

    resolved = sum(1 for i in matches if "resolved" in i.get("status","").lower())
    failed   = sum(1 for i in matches if "failed"   in i.get("status","").lower())

    lines = [f"History for {svc.upper()} — {len(matches)} total "
             f"(Resolved: {resolved}, Failed: {failed})\n"]
    for inc in matches:
        inc_id = inc.get("id") or inc.get("incident_id", "?")
        ts     = (inc.get("created_at") or inc.get("timestamp",""))[:19]
        fault  = inc.get("message") or inc.get("fault_type","")
        status = inc.get("status","?")
        action = inc.get("action_log") or inc.get("recovery_action","none")
        lines.append(
            f"  [ID {inc_id}] {ts}\n"
            f"    Fault:  {fault[:80]}\n"
            f"    Status: {status} | Action: {action[:80]}"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# TOOL 3 — detect_cascade
# Cross-layer analysis — did a dependency cause this fault?
# ═══════════════════════════════════════════════════════════════
@tool
def detect_cascade(service_name: str) -> str:
    """
    CROSS-LAYER ANALYSIS: Checks whether the fault in this service
    was CAUSED by a failure in an upstream dependent service.

    Uses the 5G dependency graph:
      NRF  → needed by: AUSF, AMF, SMF, PCRF
      AMF  → needed by: gNB
      UPF  → needed by: SMF
      gNB  → needed by: UE

    If a dependency failed within 90 minutes before this service's
    fault, it's flagged as a CASCADE — the visible fault is not
    the root cause.

    Input : service short name that is currently faulted.
    Returns: CASCADE DETECTED or no cascade, with explanation.

    ALWAYS call this for any fault — finding the true root cause
    is more important than treating the visible symptom.
    """
    svc  = service_name.lower().strip()
    deps = SERVICE_DEPS.get(svc, [])

    if not deps:
        return (f"{svc.upper()} has no upstream dependencies. "
                f"It is a root-level service. Fault is self-caused.")

    try:
        with open(INCIDENTS_FILE, "r") as f:
            all_inc = json.load(f)
    except FileNotFoundError:
        return f"incidents.json not found at {INCIDENTS_FILE}"

    def parse_ts(inc):
        ts = inc.get("created_at") or inc.get("timestamp","")
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
        except Exception:
            return datetime.datetime.min

    # Find most recent fault for the target service
    target = [i for i in all_inc if i.get("service","").lower() == svc]
    if not target:
        return f"No recorded faults found for {svc.upper()}."

    target.sort(key=parse_ts, reverse=True)
    latest_ts = parse_ts(target[0])

    cascades = []
    for dep in deps:
        dep_faults = [i for i in all_inc if i.get("service","").lower() == dep]
        for df in dep_faults:
            dep_ts = parse_ts(df)
            if dep_ts == datetime.datetime.min:
                continue
            delta_min = (latest_ts - dep_ts).total_seconds() / 60
            if 0 <= delta_min <= CASCADE_WINDOW_MINUTES:
                cascades.append({
                    "dep":     dep,
                    "fault":   (df.get("message") or df.get("fault_type","?"))[:80],
                    "minutes": round(delta_min, 1),
                    "id":      df.get("id") or df.get("incident_id","?"),
                })

    if not cascades:
        return (
            f"No cascade detected for {svc.upper()}.\n"
            f"Checked dependencies: {', '.join(d.upper() for d in deps)}\n"
            f"None had a fault within {CASCADE_WINDOW_MINUTES} min before "
            f"{svc.upper()}.\nLikely self-caused fault."
        )

    lines = [f"⚠ CASCADE DETECTED for {svc.upper()}!\n",
             f"Root cause is upstream, not {svc.upper()} itself.\n"]
    for c in cascades:
        lines.append(
            f"  {c['dep'].upper()} failed {c['minutes']} min before "
            f"{svc.upper()}\n"
            f"  Dep Fault: {c['fault']}\n"
            f"  Dep ID: {c['id']}"
        )
    chain = " → ".join(c['dep'].upper() for c in cascades) + f" → {svc.upper()}"
    lines.append(f"\nDependency chain: {chain}")
    lines.append(
        f"\nFix upstream first: "
        + ", ".join(c['dep'].upper() for c in cascades)
        + f", then restart {svc.upper()}."
    )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# TOOL 4 — classify_layer
# Identifies Core vs RAN + service role in 5G architecture
# ═══════════════════════════════════════════════════════════════
@tool
def classify_layer(service_name: str) -> str:
    """
    Classifies which 5G network layer a service belongs to:
    - CORE: AMF, SMF, UPF, AUSF, NRF, PCRF (Open5GS)
    - RAN:  gNB, UE (UERANSIM)

    Input : service short name.
    Returns: layer, what the service does, dependencies, impact scope.

    Core faults affect ALL UEs. RAN faults affect only attached UEs.
    Use this to understand architectural impact before recommending fix.
    """
    svc   = service_name.lower().strip()
    layer = LAYER_MAP.get(svc, "unknown")

    roles = {
        "amf":  "Core | Access & Mobility Mgmt — UE registration, auth coordination, NGAP with gNB",
        "smf":  "Core | Session Management — creates PDU sessions, controls UPF via N4/PFCP",
        "upf":  "Core | User Plane — forwards data packets, GTP-U tunnels to gNB",
        "ausf": "Core | Authentication Server — UE auth, depends on NRF for discovery",
        "nrf":  "Core | Network Repository — service discovery for ALL NFs, must start FIRST",
        "pcrf": "Core | Policy/Charging — QoS decisions",
        "gnb":  "RAN  | gNodeB (UERANSIM) — base station, NGAP to AMF, GTP-U to UPF",
        "ue":   "RAN  | User Equipment (UERANSIM) — connects via gNB, NAS to AMF",
    }

    deps     = SERVICE_DEPS.get(svc, [])
    dep_str  = f"Depends on: {', '.join(d.upper() for d in deps)}" if deps else "No upstream dependencies"
    role     = roles.get(svc, f"Layer: {layer}")
    impact   = "Affects ALL UEs — critical path service" if layer == "core" else "Affects UEs on this RAN node"

    return f"Service: {svc.upper()}\nRole: {role}\n{dep_str}\nImpact: {impact}"


# ═══════════════════════════════════════════════════════════════
# TOOL 5 — get_fault_stats
# Pattern analysis across all incidents.json
# ═══════════════════════════════════════════════════════════════
@tool
def get_fault_stats(dummy: str = "") -> str:
    """
    Calculates fault statistics across ALL incidents in incidents.json.
    Shows fault count per service, resolution rate, most faulty service,
    fault type breakdown.

    Input : empty string "".
    Returns: complete fault pattern analysis with counts.

    Use this to understand overall network reliability and identify
    the most problematic service. Good for reports.
    """
    try:
        with open(INCIDENTS_FILE, "r") as f:
            all_inc = json.load(f)
    except FileNotFoundError:
        return f"incidents.json not found at {INCIDENTS_FILE}"

    total   = len(all_inc)
    by_svc  = {}
    by_stat = {}
    by_type = {}

    for inc in all_inc:
        svc    = inc.get("service","unknown").lower()
        status = inc.get("status","unknown").upper()
        msg    = (inc.get("message") or inc.get("fault_type","")).lower()

        by_svc[svc]   = by_svc.get(svc, 0) + 1
        by_stat[status] = by_stat.get(status, 0) + 1

        if any(k in msg for k in ["crash","down","fail","terminate"]):
            ft = "Service Crash"
        elif any(k in msg for k in ["latency","slow","backlog"]):
            ft = "High Latency"
        elif any(k in msg for k in ["auth","ausf"]):
            ft = "Auth Failure"
        elif any(k in msg for k in ["gnb","ngap","disconn"]):
            ft = "RAN Disconnection"
        elif any(k in msg for k in ["memory","oom"]):
            ft = "Memory Exhaustion"
        else:
            ft = "Other"
        by_type[ft] = by_type.get(ft, 0) + 1

    resolved   = by_stat.get("RESOLVED", 0)
    res_rate   = round(resolved / total * 100, 1) if total else 0
    most_faulty = max(by_svc, key=by_svc.get) if by_svc else "none"

    lines = [f"=== Fault Statistics ({total} total incidents) ===\n"]
    lines.append("By Service:")
    for svc, cnt in sorted(by_svc.items(), key=lambda x: -x[1]):
        lines.append(f"  {svc.upper():<10} {'█'*cnt} ({cnt})")
    lines.append("\nBy Fault Type:")
    for ft, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"  {ft:<22} {cnt}")
    lines.append(f"\nResolution rate: {res_rate}% | "
                 f"Most faulty: {most_faulty.upper()}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# TOOL 6 — generate_recovery_steps
# Dependency-aware ordered recovery plan
# ═══════════════════════════════════════════════════════════════
@tool
def generate_recovery_steps(context: str) -> str:
    """
    Generates a dependency-aware, ordered recovery plan.
    Recovery ORDER matters in 5G — NRF must restart before AUSF, etc.

    Input : context string like "service: smf, fault: Service Crash"
            or "service: ausf, cascade from nrf"
    Returns: step-by-step systemctl commands in correct dependency order.

    IMPORTANT: Steps account for cascades — if NRF caused AUSF failure,
    the plan says restart NRF first, THEN AUSF.
    """
    ctx = context.lower()
    svc = "unknown"
    for s in ALL_SERVICES:
        if s in ctx:
            svc = s
            break

    cascade_from = None
    for dep in SERVICE_DEPS.get(svc, []):
        if dep in ctx:
            cascade_from = dep
            break

    unit = SERVICE_UNIT_MAP.get(svc, f"open5gs-{svc}d")

    if cascade_from:
        dep_unit = SERVICE_UNIT_MAP.get(cascade_from, f"open5gs-{cascade_from}d")
        steps = [
            f"# CASCADE RECOVERY — fix root cause first, then {svc.upper()}",
            f"1. Check root:   sudo systemctl status {dep_unit}",
            f"2. Restart root: sudo systemctl restart {dep_unit}",
            f"3. Verify root:  sudo systemctl is-active {dep_unit}",
            f"4. Wait 5 sec:   sleep 5",
            f"5. Restart {svc.upper()}: sudo systemctl restart {unit}",
            f"6. Verify {svc.upper()}: sudo systemctl is-active {unit}",
            f"7. Check logs:   journalctl -u {unit} -n 30",
        ]
    elif any(k in ctx for k in ["crash","down","fail","terminate"]):
        steps = [
            f"1. Check status:  sudo systemctl status {unit}",
            f"2. Read logs:     journalctl -u {unit} -n 50",
            f"3. Restart:       sudo systemctl restart {unit}",
            f"4. Verify active: sudo systemctl is-active {unit}",
            f"5. Monitor:       journalctl -f -u {unit}",
        ]
    elif any(k in ctx for k in ["latency","cpu","slow"]):
        steps = [
            f"1. Check CPU:     top -u open5gs",
            f"2. Check mem:     free -h",
            f"3. Check logs:    journalctl -u {unit} -n 50",
            f"4. Restart:       sudo systemctl restart {unit}",
            f"5. Watch:         watch -n 5 systemctl status {unit}",
        ]
    elif any(k in ctx for k in ["port","bind","address"]):
        steps = [
            f"1. Find process:  sudo ss -tlnp | grep {svc}",
            f"2. Kill stale:    sudo pkill -f {svc}",
            f"3. Restart:       sudo systemctl restart {unit}",
            f"4. Verify port:   sudo ss -tlnp",
        ]
    else:
        steps = [
            f"1. Check:   sudo systemctl status {unit}",
            f"2. Logs:    journalctl -u {unit} -n 50",
            f"3. Restart: sudo systemctl restart {unit}",
            f"4. Verify:  sudo systemctl is-active {unit}",
        ]

    return (
        f"Recovery Plan for {svc.upper()}:\n"
        + "\n".join(steps)
        + "\n\nNote: Run these on the actual VM where the service runs."
    )


# ═══════════════════════════════════════════════════════════════
# TOOL 7 — log_result
# Saves final diagnosis to agent_results.json
# ═══════════════════════════════════════════════════════════════
@tool
def log_result(result_json: str) -> str:
    """
    Saves the agent's final diagnosis to agent_results.json.
    ALWAYS call this as the LAST step after completing analysis.

    Input : JSON string with these keys:
      service, fault_type, severity, root_cause,
      recovery_steps, cascade_detected
    Example:
      {"service":"smf","fault_type":"Service Crash","severity":"critical",
       "root_cause":"port 8805 conflict","recovery_steps":"1. lsof....",
       "cascade_detected":false}

    Returns: confirmation with result ID.
    """
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON input: {e}"

    try:
        existing = json.load(open(RESULTS_FILE, "r"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    entry = {
        "result_id":  len(existing) + 1,
        "logged_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "status":     "diagnosed",
        **result
    }
    existing.append(entry)
    os.makedirs(os.path.dirname(RESULTS_FILE) if os.path.dirname(RESULTS_FILE) else ".", exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    return (
        f"Result #{entry['result_id']} saved to {RESULTS_FILE}\n"
        f"Service: {result.get('service','?').upper()} | "
        f"Severity: {result.get('severity','?').upper()}"
    )


# Export all tools as list — used by AgentExecutor in LangGraph Node 5
ALL_TOOLS = [
    search_incidents,
    get_service_history,
    detect_cascade,
    classify_layer,
    get_fault_stats,
    generate_recovery_steps,
    log_result,
]
