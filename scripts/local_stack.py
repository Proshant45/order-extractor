#!/usr/bin/env python3
import os
import signal
import subprocess
import time
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
python = ROOT / '.venv/bin/python'
children = []
shutdown = False

def stop(*_):
    global shutdown
    shutdown = True
    for child in children:
        if child.poll() is None:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    children.append(subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765', '--no-access-log'],cwd=ROOT,start_new_session=True))
    children.append(subprocess.Popen(['bash', 'worker/tick_loop.sh'],cwd=ROOT,start_new_session=True))
    print('Local app: http://127.0.0.1:8765; worker polls every 15 seconds',flush=True)
    while not shutdown:
        time.sleep(1)
        if children[0].poll() is not None and not shutdown:
            print('Local server stopped; restarting retained batches',flush=True)
            children[0] = subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765', '--no-access-log'],cwd=ROOT,start_new_session=True)
        if children[1].poll() is not None and not shutdown:
            print('Local worker loop stopped; restarting',flush=True)
            children[1] = subprocess.Popen(['bash', 'worker/tick_loop.sh'],cwd=ROOT,start_new_session=True)
finally:
    stop()
    for child in children:
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(child.pid, signal.SIGKILL);child.wait()
