import os
import subprocess
import signal
import sys
import time
import threading
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# Load API key from .env
load_dotenv()
gemini_api_key = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=gemini_api_key)

# Paths to the binaries and configs
gnb_binary = './build/nr-gnb'
gnb_config = 'config/open5gs-gnb.yaml'
ue_binary = './build/nr-ue'
ue_config = 'config/open5gs-ue.yaml'

START_DELAY = 5  # Delay between gNB and UE start (seconds)
MAX_RESTARTS = 3
BACKOFF_SECONDS = 30

# State
running = True
processes = []
restart_attempts = {'UE': 0, 'gNB': 0}
signal_lock = threading.Lock()

def signal_handler(sig, frame):
    global running
    with signal_lock:
        if running:
            print(f"\n[{datetime.now()}] Ctrl+C detected. Stopping agent...")
            running = False
            for p in processes:
                if p:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
            print(f"[{datetime.now()}] All processes stopped.")
            sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def start_gnb():
    cmd = [gnb_binary, '-c', gnb_config]
    print(f"[{datetime.now()}] Starting gNB: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

def start_ue():
    cmd = ['sudo', ue_binary, '-c', ue_config]
    print(f"[{datetime.now()}] Starting UE: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

def monitor_process(p, instance_name):
    log_buffer = []
    max_buffer_size = 50
    global running

    while running:
        if p.poll() is not None:
            print(f"[{datetime.now()}] {instance_name} stopped (exit code {p.returncode}).")

            if restart_attempts[instance_name] >= MAX_RESTARTS:
                print(f"[{datetime.now()}] Max restarts reached for {instance_name}. Not restarting.")
                running = False
                break

            restart_attempts[instance_name] += 1
            print(f"[{datetime.now()}] Waiting {BACKOFF_SECONDS}s before restarting {instance_name}...")
            time.sleep(BACKOFF_SECONDS)

            p = start_gnb() if instance_name == 'gNB' else start_ue()
            processes[0 if instance_name == 'gNB' else 1] = p
            log_buffer.clear()
            continue

        try:
            line = p.stdout.readline()
            if line:
                line = line.strip()
                timestamped_line = f"[{datetime.now()}] {instance_name} log: {line}"
                log_buffer.append(timestamped_line)
                print(timestamped_line)

                if len(log_buffer) > max_buffer_size:
                    log_buffer.pop(0)

                upper_line = line.upper()

                if any(keyword in upper_line for keyword in ['CONNECTED TO', 'CONNECTION ESTABLISHED', 'NGAP', 'AMF']):
                    print(f"[{datetime.now()}] Detected connection event: {line}")

                # Treat only real issues as errors
                if any(keyword in upper_line for keyword in ['ERROR', 'FAIL', 'CONNECTION REFUSED', 'AUTHENTICATION FAILED']):
                    print(f"[{datetime.now()}] Detected issue in {instance_name}: {line}")

                    if any(keyword in upper_line for keyword in ['AUTHENTICATION', 'PLMN', 'REGISTER']):
                        print(f"[{datetime.now()}] Suggestion: Check Open5GS Web UI: http://localhost:9999")

                    prompt = f"""
{instance_name} in UERANSIM (with Open5GS) has an issue detected in logs.
Here are the recent log lines:
{chr(10).join(log_buffer[-20:])}

Only suggest a restart command if the logs clearly indicate an error, crash, authentication failure, or connection problem.
Do NOT suggest restart for logs showing "UE switches to state [MM-DEREGISTERED/PLMN-SEARCH]".

Provide:
1. A single shell command to fix it (prefix with 'COMMAND: ')
2. Suggestions (prefix with 'SUGGESTIONS: ' and use bullet points)
"""
                    resp_text = query_gemini(prompt)
                    command, suggestions = parse_gemini_response(resp_text)

                    if command:
                        valid_commands = ['pkill -f "nr-gnb"', 'sudo pkill -f "nr-ue"']
                        if any(valid in command for valid in valid_commands):
                            print(f"[{datetime.now()}] Executing command: {command}")
                            try:
                                subprocess.run(command, shell=True, check=True)
                            except subprocess.CalledProcessError as e:
                                print(f"[{datetime.now()}] Command error: {e}")
                        else:
                            print(f"[{datetime.now()}] Manual command suggested: {command}")

                    if suggestions and suggestions != 'None':
                        print(f"[{datetime.now()}] Recommendations:\n{suggestions}")

                # Show PLMN/Register messages as info only
                elif 'PLMN' in upper_line or 'REGISTER' in upper_line:
                    print(f"[{datetime.now()}] Informational PLMN/Register log: {line}")
                    print(f"[{datetime.now()}] Suggestion: Check SIM config in Open5GS UI (http://localhost:9999)")

        except Exception as e:
            print(f"[{datetime.now()}] Error monitoring {instance_name}: {e}")
            time.sleep(1)

        time.sleep(0.1)

def query_gemini(prompt):
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[{datetime.now()}] Gemini query error: {e}")
        return "COMMAND: \nSUGGESTIONS: None"

def parse_gemini_response(resp_text):
    command = ''
    suggestions = ''
    for line in resp_text.splitlines():
        if line.startswith('COMMAND: '):
            command = line.replace('COMMAND: ', '').strip()
        elif line.startswith('SUGGESTIONS: '):
            suggestions = line.replace('SUGGESTIONS: ', '').strip()
        elif suggestions and line.startswith('- '):
            suggestions += '\n' + line
    return command, suggestions

if __name__== '__main__':
    print(f"[{datetime.now()}] Starting UERANSIM Monitoring Agent (Open5GS)")
    print(f"[{datetime.now()}] Press Ctrl+C to stop")

    p_gnb = start_gnb()
    processes.append(p_gnb)

    print(f"[{datetime.now()}] Waiting {START_DELAY}s before starting UE...")
    time.sleep(START_DELAY)

    p_ue = start_ue()
    processes.append(p_ue)

    threads = [
        threading.Thread(target=monitor_process, args=(p_gnb, 'gNB')),
        threading.Thread(target=monitor_process, args=(p_ue, 'UE'))
    ]

    for t in threads:
        t.start()

    while running:
        time.sleep(0.1)

    for t in threads:
        t.join()

    print(f"[{datetime.now()}] Agent stopped.")
