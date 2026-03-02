import subprocess
import requests
import os
import time
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
CENTRAL_URL = os.getenv("CENTRAL_SERVER_URL")
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

gnb_cmd = ["./build/nr-gnb", "-c", "config/open5gs-gnb.yaml"]
ue_cmd  = ["./build/nr-ue", "-c", "config/open5gs-ue.yaml"]

def send_log(instance, msg, typ):
    data = {
        "source": "ran",
        "service": instance,   # gNB or UE
        "type": typ,
        "message": msg,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    requests.post(f"{CENTRAL_URL}/api/log", json=data)

def send_approval(instance, cmd, suggestions):
    data = {
        "source": "ran",
        "service": instance,
        "command": cmd,
        "suggestions": suggestions,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    requests.post(f"{CENTRAL_URL}/api/approval", json=data)

def monitor(instance, popen):
    while True:
        line = popen.stdout.readline().strip()
        if not line:
            break

        send_log(instance, line, "info")

        if any(k in line.upper() for k in ["ERROR", "FAIL", "AUTH"]):

            model = genai.GenerativeModel("gemini-1.5-flash")
            prompt = f"""
Error detected in {instance}:

{line}

Provide:
COMMAND: <safe restart command>
SUGGESTIONS:
- bullet list
"""
            resp = model.generate_content(prompt).text.split("\n")
            cmd = ""
            sug = ""
            for r in resp:
                if r.startswith("COMMAND:"):
                    cmd = r.replace("COMMAND:", "").strip()
                elif r.startswith("SUGGESTIONS:"):
                    sug = ""
                elif r.startswith("-"):
                    sug += r + "\n"

            send_approval(instance, cmd, sug)

def start_agent():
    gnb = subprocess.Popen(gnb_cmd, stdout=subprocess.PIPE, text=True)
    ue  = subprocess.Popen(ue_cmd,  stdout=subprocess.PIPE, text=True)

    monitor("gNB", gnb)
    monitor("UE", ue)

if __name__ == "__main__":
    print("UERANSIM agent started...")
    start_agent()

