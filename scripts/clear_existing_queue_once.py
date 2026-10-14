#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
load_dotenv(ROOT/'.env')
from app.maintenance import clear_existing_queue_once

if __name__=='__main__':
    print(json.dumps(clear_existing_queue_once()))
