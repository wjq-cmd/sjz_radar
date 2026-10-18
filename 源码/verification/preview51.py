"""Isolated local preview; marker-controlled shutdown, no capture/phone changes."""
import subprocess, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[1]
out = root.parent / '验证' / 'web-preview'
out.mkdir(exist_ok=True)
marker = out / 'stop'
if marker.exists(): marker.unlink()
fixture = root.parent.parent / 'GatewayCapture/verification/live-v4/v4.0.2-field/captures/gateway-20260919-023807-adc7f8c0/realtime.jsonl'
with (out / 'worker.log').open('w', encoding='utf-8') as log:
    if '--published' in sys.argv:
        import json, subprocess
        release = root.parent / '程序'
        bundle = json.loads(subprocess.check_output([str(release/'Nva本地离线解析.exe'), '--extract-nva-parser'], text=True))
        command = [bundle['path']]
    else:
        release = root.parent / '程序'
        command = [sys.executable, str(root/'Parser/gateway_parser.py')]
    p = subprocess.Popen(command + ['--output', str(out / 'semantic'), '--port', '17991', '--timeline', str(fixture), '--cache-only', '--asset-pak', 'NvaAssets.pak', '--models', 'models'], cwd=release, stdin=subprocess.PIPE, stdout=log, stderr=log)
    print('Preview PID', p.pid, 'http://127.0.0.1:17991/', flush=True)
    try:
        until = time.monotonic() + 7200
        while p.poll() is None and not marker.exists() and time.monotonic() < until:
            time.sleep(.25)
    finally:
        p.stdin.close()
        try: p.wait(timeout=40)
        except subprocess.TimeoutExpired: p.terminate(); p.wait(timeout=10)
