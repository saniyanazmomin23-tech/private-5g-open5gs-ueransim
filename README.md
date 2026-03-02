# 🚀 Private 5G Network Setup (Open5GS + UERANSIM)

# 🛜 Private 5G Network Deployment Guide
### Open5GS + UERANSIM — Complete Setup, Configuration & Testing
> Ubuntu 22.04 LTS | Open5GS | UERANSIM | Network Slicing

---

## 📋 Table of Contents
- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Step 1 — MongoDB](#step-1--mongodb)
  - [Step 2 — Open5GS](#step-2--open5gs)
  - [Step 3 — WebUI](#step-3--webui)
  - [Step 4 — UERANSIM](#step-4--ueransim)
- [Configuration Files — What to Change](#configuration-files--what-to-change)
  - [AMF](#51--amf-configuration)
  - [SMF](#52--smf-configuration)
  - [UPF](#53--upf-configuration)
  - [gNB](#54--ueransim-gnb-configuration)
  - [UE](#55--ueransim-ue-configuration)
- [Network Slicing](#network-slicing)
- [NAT & IP Forwarding](#nat--ip-forwarding)
- [Adding Subscribers via WebUI](#adding-subscribers-via-webui)
- [Running the Network](#running-the-network)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Quick Reference](#quick-reference)

---

## Overview

This repository contains a complete step-by-step guide for deploying a private 5G network using open-source components on Ubuntu 22.04 LTS.

| Component | Role | Default IP |
|-----------|------|------------|
| Open5GS AMF | Access & Mobility Management | `127.0.0.5` |
| Open5GS SMF | Session Management | `127.0.0.4` |
| Open5GS UPF | User Plane Function | `127.0.0.7` |
| Open5GS NRF | Network Repository Function | `127.0.0.10` |
| Open5GS WebUI | Subscriber Management Portal | `127.0.0.1:9999` |
| UERANSIM gNB | Simulated 5G Base Station | `127.0.0.1` |
| UERANSIM UE | Simulated User Equipment | `127.0.0.1` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Internet / Public Network                │
└─────────────────────────────┬───────────────────────────────┘
                              │ NAT (iptables MASQUERADE)
                ┌─────────────▼────────────┐
                │     Core VM (Open5GS)    │
                │                          │
                │  AMF ── SMF ── UPF       │
                │  NRF    AUSF   UDM       │
                │  MongoDB + WebUI         │
                └─────────────┬────────────┘
                    NGAP/N2   │   GTP-U/N3
                ┌─────────────▼────────────┐
                │     RAN VM (UERANSIM)    │
                │                          │
                │  gNodeB (nr-gnb)         │
                │  UE1 / UE2 / UE3         │
                │  (nr-ue instances)       │
                └──────────────────────────┘
```

> **Note:** Both Core and RAN can run on the **same machine** for lab/testing.  
> For production-like testing, use two separate VMs on the same internal network.

---

## Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| RAM | 4 GB | 8 GB |
| CPU | 2 vCPUs | 4 vCPUs |
| Disk | 20 GB | 40 GB |
| Network | Single NIC | Two NICs (Core + RAN) |

---

## Installation

### Step 1 — MongoDB

MongoDB stores all subscriber data and network configuration for Open5GS.

```bash
sudo apt update && sudo apt install -y gnupg

curl -fsSL https://pgp.mongodb.com/server-8.0.asc | \
  sudo gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor

echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] \
  https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/8.0 multiverse" | \
  sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list

sudo apt update
sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
```

> ✅ MongoDB should show **active (running)**. If it fails: `sudo journalctl -u mongod`

---

### Step 2 — Open5GS

```bash
sudo add-apt-repository ppa:open5gs/latest
sudo apt update
sudo apt install -y open5gs

# Verify — should show 17 active services
sudo systemctl status open5gs-* | grep -c "active"
```

---

### Step 3 — WebUI

The WebUI is used to add and manage subscribers (IMSI, keys, slice config).

```bash
sudo apt install -y ca-certificates curl gnupg
sudo mkdir -p /etc/apt/keyrings

curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg

NODE_MAJOR=20
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] \
  https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | \
  sudo tee /etc/apt/sources.list.d/nodesource.list

sudo apt update && sudo apt install -y nodejs
curl -fsSL https://open5gs.org/open5gs/assets/webui/install | sudo -E bash -
```

| Detail | Value |
|--------|-------|
| URL | http://localhost:9999 |
| Username | `admin` |
| Password | `1423` ⚠️ Change this immediately! |

---

### Step 4 — UERANSIM

```bash
sudo apt install -y make gcc g++ libsctp-dev lksctp-tools iproute2
sudo snap install cmake --classic

cd ~
git clone https://github.com/aligungr/UERANSIM
cd UERANSIM
make
```

> ✅ After `make` completes, binaries are in `~/UERANSIM/build/` — you will find `nr-gnb` and `nr-ue` executables.

---

## Configuration Files — What to Change

> ⚠️ **Key Rule:** The IPs in `amf.yaml`, `smf.yaml`, `upf.yaml` must all be your **Core VM IP**.  
> The `amfConfigs.address` in `gnb.yaml` and `gnbSearchList` in `ue.yaml` must also point to the **Core VM IP**.

---

### 5.1 — AMF Configuration

**File:** `/etc/open5gs/amf.yaml`

| Field | Default | Change To | Notes |
|-------|---------|-----------|-------|
| `amf.ngap[0].addr` | `127.0.0.5` | Your Core VM IP | IP that gNB connects to |
| `amf.guami[0].plmn_id.mcc` | `999` | Your MCC | Must match gNB config |
| `amf.guami[0].plmn_id.mnc` | `70` | Your MNC | Must match gNB config |
| `amf.tai[0].plmn_id.mcc` | `999` | Your MCC | Tracking area |
| `amf.tai[0].plmn_id.mnc` | `70` | Your MNC | Tracking area |
| `amf.tai[0].tac` | `1` | Your TAC | Must match gNB TAC |
| `amf.plmn_support[0].s_nssai[0].sst` | `1` | `1` (eMBB) | Slice type |

**Example snippet:**
```yaml
amf:
  ngap:
    - addr: 192.168.1.10        # ← Change to your Core VM IP
  guami:
    - plmn_id:
        mcc: 999                # ← Your MCC
        mnc: 70                 # ← Your MNC
      amf_id:
        region: 2
        set: 1
  tai:
    - plmn_id:
        mcc: 999                # ← Your MCC
        mnc: 70                 # ← Your MNC
      tac: 1                    # ← Must match gNB TAC
  plmn_support:
    - plmn_id:
        mcc: 999
        mnc: 70
      s_nssai:
        - sst: 1                # eMBB slice
        - sst: 2                # URLLC slice
        - sst: 3                # mMTC slice
```

---

### 5.2 — SMF Configuration

**File:** `/etc/open5gs/smf.yaml`

| Field | Default | Change To | Notes |
|-------|---------|-----------|-------|
| `smf.pfcp[0].addr` | `127.0.0.4` | Your Core VM IP | PFCP interface for UPF |
| `smf.subnet[0].addr` | `10.45.0.1/16` | Your UE subnet | IP pool for UEs |
| `smf.subnet[0].dnn` | `internet` | Your DNN name | Data Network Name |
| `smf.dns[0]` | `8.8.8.8` | Your DNS | Primary DNS for UEs |
| `smf.dns[1]` | `8.8.4.4` | Your DNS | Secondary DNS for UEs |

**Example snippet:**
```yaml
smf:
  pfcp:
    - addr: 192.168.1.10        # ← Your Core VM IP
  subnet:
    - addr: 10.45.0.1/16        # ← UE IP pool
      dnn: internet             # ← Data Network Name
  dns:
    - 8.8.8.8
    - 8.8.4.4
```

---

### 5.3 — UPF Configuration

**File:** `/etc/open5gs/upf.yaml`

| Field | Default | Change To | Notes |
|-------|---------|-----------|-------|
| `upf.pfcp[0].addr` | `127.0.0.7` | Your Core VM IP | Must match SMF PFCP addr |
| `upf.gtpu[0].addr` | `127.0.0.7` | Your Core VM IP | GTP-U tunnel endpoint |
| `upf.subnet[0].addr` | `10.45.0.1/16` | Your UE subnet | Must match SMF subnet |
| `upf.subnet[0].dnn` | `internet` | Your DNN name | Must match SMF DNN |

**Example snippet:**
```yaml
upf:
  pfcp:
    - addr: 192.168.1.10        # ← Your Core VM IP
  gtpu:
    - addr: 192.168.1.10        # ← Your Core VM IP
  subnet:
    - addr: 10.45.0.1/16
      dnn: internet
```

---

### 5.4 — UERANSIM gNB Configuration

**File:** `~/UERANSIM/config/open5gs-gnb.yaml`

| Field | Default | Change To | Notes |
|-------|---------|-----------|-------|
| `mcc` | `999` | Your MCC | Must match AMF |
| `mnc` | `70` | Your MNC | Must match AMF |
| `tac` | `1` | Your TAC | Must match AMF TAC |
| `linkIp` | `127.0.0.1` | RAN VM IP | gNB local IP |
| `ngapIp` | `127.0.0.1` | RAN VM IP | NGAP interface IP |
| `gtpIp` | `127.0.0.1` | RAN VM IP | GTP-U interface IP |
| `amfConfigs[0].address` | `127.0.0.5` | Core VM IP | ⚠️ AMF IP address |
| `amfConfigs[0].port` | `38412` | `38412` | NGAP port — do not change |
| `slices[0].sst` | `1` | `1` (eMBB) | Supported slice |

**Example snippet:**
```yaml
mcc: '999'                      # ← Must match AMF
mnc: '70'                       # ← Must match AMF
nci: '0x000000010'
idLength: 32
tac: 1                          # ← Must match AMF TAC

linkIp: 192.168.1.20            # ← Your RAN VM IP
ngapIp: 192.168.1.20            # ← Your RAN VM IP
gtpIp: 192.168.1.20             # ← Your RAN VM IP

amfConfigs:
  - address: 192.168.1.10       # ← Your Core VM IP
    port: 38412

slices:
  - sst: 0x1                    # eMBB
```

---

### 5.5 — UERANSIM UE Configuration

**File:** `~/UERANSIM/config/open5gs-ue.yaml`

| Field | Default | Change To | Notes |
|-------|---------|-----------|-------|
| `supi` | `imsi-999700000000001` | `imsi-<your IMSI>` | Must match WebUI subscriber |
| `mcc` | `999` | Your MCC | Must match AMF + gNB |
| `mnc` | `70` | Your MNC | Must match AMF + gNB |
| `key` | `465B5CE8...` | Your subscriber key | 32 hex chars — match WebUI |
| `op` / `opc` | `E8ED289D...` | Your OPc value | 32 hex chars — match WebUI |
| `opType` | `OPC` | `OPC` | Use OPC not OP |
| `dnn` | `internet` | Your DNN name | Must match SMF/UPF DNN |
| `gnbSearchList[0]` | `127.0.0.1` | RAN VM IP | IP of your gNB |
| `sessions[0].slice.sst` | `1` | `1` | Slice to connect to |

**Example snippet:**
```yaml
supi: 'imsi-999700000000001'    # ← Your IMSI — must match WebUI
mcc: '999'                      # ← Must match AMF + gNB
mnc: '70'                       # ← Must match AMF + gNB

key: '465B5CE8B199B49FAA5F0A2EE238A6BC'   # ← Match WebUI key
op: 'E8ED289DEBA952E4283B54E88E6183CA'    # ← Match WebUI OPc
opType: 'OPC'

sessions:
  - type: 'IPv4'
    apn: 'internet'             # ← Your DNN name
    slice:
      sst: 0x1

gnbSearchList:
  - 192.168.1.20                # ← Your RAN VM IP
```

---

## Network Slicing

Three slices are supported out of the box:

| Slice | SST | SD | Use Case | QoS |
|-------|-----|----|----------|-----|
| eMBB | `1` | `0x000001` | Video streaming, downloads | High bandwidth |
| URLLC | `2` | `0x000002` | Industrial control, surgery | Ultra-low latency |
| mMTC | `3` | `0x000003` | IoT sensors, smart meters | Low power, best effort |

### Create Separate UE Config Files for Each Slice

```bash
cp ~/UERANSIM/config/open5gs-ue.yaml ~/UERANSIM/config/open5gs-ue-embb.yaml
cp ~/UERANSIM/config/open5gs-ue.yaml ~/UERANSIM/config/open5gs-ue-urllc.yaml
cp ~/UERANSIM/config/open5gs-ue.yaml ~/UERANSIM/config/open5gs-ue-mmtc.yaml
```

Edit each file and set the `sessions[0].slice` field:

**open5gs-ue-embb.yaml**
```yaml
sessions:
  - slice:
      sst: 0x1
      sd: 0x000001
```

**open5gs-ue-urllc.yaml**
```yaml
sessions:
  - slice:
      sst: 0x2
      sd: 0x000002
```

**open5gs-ue-mmtc.yaml**
```yaml
sessions:
  - slice:
      sst: 0x3
      sd: 0x000003
```

### Add All Slices in AMF

In `/etc/open5gs/amf.yaml`:
```yaml
plmn_support:
  - plmn_id:
      mcc: 999
      mnc: 70
    s_nssai:
      - sst: 1
        sd: '000001'
      - sst: 2
        sd: '000002'
      - sst: 3
        sd: '000003'
```

---

## NAT & IP Forwarding

Run these on the **Core VM** to allow UE traffic to reach the internet through UPF:

```bash
# Enable IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf

# Set up NAT — replace eth0 with your actual internet-facing interface
sudo iptables -t nat -A POSTROUTING -s 10.45.0.0/16 -o eth0 -j MASQUERADE
sudo iptables -I FORWARD 1 -j ACCEPT

# Disable UFW (it blocks GTP-U traffic)
sudo systemctl stop ufw
sudo systemctl disable ufw

# Persist rules across reboots
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

> ⚠️ Find your interface name with: `ip route show default | awk '/default/ {print $5}'`

---

## Adding Subscribers via WebUI

Before starting UERANSIM, add your subscriber so the core can authenticate the UE.

1. Open browser → `http://localhost:9999`
2. Login: `admin` / `1423`
3. Click **Subscribers** → **Add Subscriber**
4. Fill in the fields:

| Field | Example Value | Notes |
|-------|--------------|-------|
| IMSI | `999700000000001` | Must match `supi` in UE config |
| Subscriber Key (K) | `465B5CE8B199B49FAA5F0A2EE238A6BC` | 32 hex chars — must match `key` in UE config |
| Operator Key (OPc) | `E8ED289DEBA952E4283B54E88E6183CA` | 32 hex chars — must match `op` in UE config |
| AMF | `8000` | Leave as default |
| DNN/APN | `internet` | Must match SMF/UPF DNN |
| S-NSSAI SST | `1` | 1=eMBB, 2=URLLC, 3=mMTC |
| IP Allocation | Dynamic | Auto-assign from UPF subnet pool |

5. Click **Save**. Repeat for each simulated UE with a unique IMSI.

---

## Running the Network

### 1 — Start Open5GS Core

```bash
sudo systemctl restart open5gs-*

# Verify all 17 services are active
sudo systemctl status open5gs-* | grep "active (running)"

# Watch AMF logs
sudo tail -f /var/log/open5gs/amf.log
```

### 2 — Start gNB

```bash
cd ~/UERANSIM
sudo build/nr-gnb -c config/open5gs-gnb.yaml
```

Expected output:
```
[ngap] [info] NG Setup procedure is successful
[sctp] [info] Sctp association setup — AMF connected
```

> ✅ `NG Setup procedure is successful` means the gNB is connected to your AMF.

### 3 — Start UE (new terminal)

```bash
cd ~/UERANSIM
sudo build/nr-ue -c config/open5gs-ue.yaml
```

Expected output:
```
[rrc]  [info] RRC connection established
[nas]  [info] UE switches to state: MM-REGISTERED
[app]  [info] Connection setup for PDU session[1]
[app]  [info] TUN interface uesimtun0 configured
```

> ✅ `uesimtun0 configured` means the UE has a data connection and an IP from the UPF subnet.

---

## Testing

### Check TUN Interface

```bash
ip addr show uesimtun0
# Expected: inet 10.45.0.2/16
```

### Ping Test (UE → Internet)

```bash
ping -I uesimtun0 8.8.8.8

# Expected:
# 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=12.5 ms
```

### HTTP Test

```bash
curl --interface uesimtun0 http://example.com
```

### Throughput Test with iperf3

```bash
# On server side
iperf3 -s

# On UE side
iperf3 -c <server_ip> -B 10.45.0.2
```

### Network Slice Test

Start a UE per slice in separate terminals:

```bash
# Terminal 1 — eMBB
sudo build/nr-ue -c config/open5gs-ue-embb.yaml

# Terminal 2 — URLLC
sudo build/nr-ue -c config/open5gs-ue-urllc.yaml

# Terminal 3 — mMTC
sudo build/nr-ue -c config/open5gs-ue-mmtc.yaml

# Verify all tunnel interfaces
ip addr | grep uesimtun
# Should show uesimtun0, uesimtun1, uesimtun2
```

### Check Core Logs

```bash
sudo tail -f /var/log/open5gs/amf.log   # UE registrations
sudo tail -f /var/log/open5gs/smf.log   # PDU session creation
sudo tail -f /var/log/open5gs/upf.log   # Data plane traffic
```

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| gNB can't connect to AMF | Wrong IP in gNB config | Check `amfConfigs.address` in `gnb.yaml` matches Core VM IP |
| UE registration fails | IMSI/key mismatch | Verify `supi`, `key`, `op` in `ue.yaml` exactly match WebUI subscriber |
| No `uesimtun0` interface | PDU session failed | Check SMF logs for DNN or slice mismatch |
| Ping fails through tunnel | NAT not configured | Run `iptables MASQUERADE` rule on Core VM |
| Less than 17 services active | MongoDB not running | `sudo systemctl restart mongod` then restart `open5gs-*` |
| AMF NGAP bind error | Port 38412 in use | `sudo ss -tulnp \| grep 38412` |
| UPF PFCP error | IP mismatch | Ensure `upf.yaml pfcp.addr` matches `smf.yaml pfcp addr` |
| WebUI not loading | Node.js service down | `sudo systemctl restart open5gs-webui` |

---

## Quick Reference

| Config File | Location | Key Fields to Change |
|-------------|----------|---------------------|
| AMF | `/etc/open5gs/amf.yaml` | `ngap.addr`, `plmn_id (mcc/mnc)`, `tac`, `s_nssai` |
| SMF | `/etc/open5gs/smf.yaml` | `pfcp.addr`, `subnet.addr`, `dnn`, `dns` |
| UPF | `/etc/open5gs/upf.yaml` | `pfcp.addr`, `gtpu.addr`, `subnet.addr`, `dnn` |
| NRF | `/etc/open5gs/nrf.yaml` | `sbi.addr` (if not localhost) |
| gNB | `~/UERANSIM/config/open5gs-gnb.yaml` | `mcc`, `mnc`, `tac`, `linkIp`, `ngapIp`, `gtpIp`, `amfConfigs.address` |
| UE | `~/UERANSIM/config/open5gs-ue.yaml` | `supi`, `mcc`, `mnc`, `key`, `op`, `dnn`, `gnbSearchList` |

---

## Security Checklist

- [ ] Change WebUI default password (`admin` / `1423`) immediately after setup
- [ ] Restrict WebUI port `9999` to localhost or trusted IPs only
- [ ] Use strong, unique keys and OPc values for all subscribers
- [ ] Regularly backup MongoDB: `mongodump --out /backup/open5gs-$(date +%F)`
- [ ] Do not expose NGAP port `38412` to the public internet
- [ ] Comply with local RF/radio regulations if using real hardware

---

## References

- [Open5GS Documentation](https://open5gs.org/open5gs/docs/)
- [UERANSIM GitHub](https://github.com/aligungr/UERANSIM)
- [Open5GS WebUI](https://github.com/open5gs/open5gs/tree/main/webui)
- [3GPP 5G Standards](https://www.3gpp.org/technologies/5g-system-overview)

---

*For issues, open a GitHub Issue with your log output from `/var/log/open5gs/` and your sanitized config files.*

