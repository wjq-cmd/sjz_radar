"""Windows integration: owner death, owned/foreign PktMon, temporary firewall cleanup.
Uses loopback-only capture and refuses to run if PktMon is already in use.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

CREATE = subprocess.CREATE_NO_WINDOW

def run(*args):
    p = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", creationflags=CREATE, timeout=30)
    if p.returncode:
        raise RuntimeError((args, p.returncode, p.stdout, p.stderr))
    return p.stdout + p.stderr

def inactive(text):
    return "not running" in text.lower() or "没有运行" in text

def wait_for_owned_capture(directory, filter_name, timeout=5):
    deadline = time.monotonic() + timeout
    last_status = last_filters = ""
    while time.monotonic() < deadline:
        last_status = run("pktmon.exe", "status")
        last_filters = run("pktmon.exe", "filter", "list")
        if not inactive(last_status) and filter_name in last_filters:
            return last_status, last_filters
        time.sleep(0.1)
    raise AssertionError(("owned capture did not become ready", str(directory), last_status, last_filters))

def parent():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"], creationflags=CREATE)
    times = [ctypes.c_uint64() for _ in range(4)]
    f = ctypes.windll.kernel32.GetProcessTimes
    f.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    assert f(int(p._handle), *(ctypes.byref(x) for x in times))
    ticks = times[0].value + 504911232000000000
    return p, ticks

def guard(exe, owner, ticks, kind, target):
    p = subprocess.Popen([str(exe), "--cleanup-guardian", str(owner.pid), str(ticks), kind, str(target)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE)
    assert p.stdout.readline().strip() == b"READY"
    return p

def finish(owner, watcher):
    owner.kill()
    owner.wait(timeout=10)
    out, err = watcher.communicate(timeout=30)
    assert watcher.returncode == 0, (out, err)

def metadata(directory, owner, ticks, filter_name):
    (directory / "raw").mkdir(parents=True)
    state = dict(TargetAddress="127.0.0.1", ComponentId=1, AdapterName="guardian-loopback-test",
                 UdpOnly=False, SegmentMiB=16, StartedAt=datetime.now(timezone.utc).isoformat(),
                 Status="recording", FilterName=filter_name, OwnerPid=owner.pid, OwnerStartTicks=ticks)
    (directory / "capture-state.json").write_text(json.dumps(state), encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    assert inactive(run("pktmon.exe", "status")), "An existing capture must be left alone"
    filters = run("pktmon.exe", "filter", "list")
    assert "none" in filters.lower() or "无" in filters, "Existing filters must be left alone"
    suffix = uuid.uuid4().hex
    name = "GatewayCapture-v5-" + suffix
    filter_name = "GC5-" + suffix[:16]
    sentinel = "GatewayCapture-guardian-test-" + suffix
    owners, guards = [], []
    owns_active = False
    checks = []
    try:
        owner, ticks = parent(); owners.append(owner)
        directory = a.out / "owned"
        metadata(directory, owner, ticks, filter_name)
        watcher = guard(a.exe, owner, ticks, "capture", directory); guards.append(watcher)
        run("pktmon.exe", "filter", "add", filter_name, "-i", "127.0.0.1")
        run("pktmon.exe", "start", "--capture", "--comp", "all", "--pkt-size", "0",
            "--file-name", str(directory / "raw/trace.etl"), "--file-size", "16", "--log-mode", "multi-file")
        owns_active = True
        # Another guardian has a valid owner but belongs to a different capture path/filter.
        foreign, foreign_ticks = parent(); owners.append(foreign)
        foreign_dir = a.out / "different-session"
        metadata(foreign_dir, foreign, foreign_ticks, "GC5-" + uuid.uuid4().hex[:16])
        foreign_guard = guard(a.exe, foreign, foreign_ticks, "capture", foreign_dir); guards.append(foreign_guard)
        finish(foreign, foreign_guard)
        status, filters = wait_for_owned_capture(directory, filter_name)
        assert not inactive(status)
        assert filter_name in filters
        checks.append("foreign capture and filter preserved")
        finish(owner, watcher)
        assert inactive(run("pktmon.exe", "status"))
        owns_active = False
        assert filter_name not in run("pktmon.exe", "filter", "list")
        state = json.loads((directory / "capture-state.json").read_text(encoding="utf-8"))
        assert state["Status"] == "stopped"
        assert list((directory / "raw").glob("*.etl"))
        checks.append("owner death stopped owned PktMon, removed its filter, preserved ETL")

        owner, ticks = parent(); owners.append(owner)
        watcher = guard(a.exe, owner, ticks, "firewall", name); guards.append(watcher)
        for rule in (name, sentinel):
            run("netsh.exe", "advfirewall", "firewall", "add", "rule", "name=" + rule,
                "dir=in", "action=allow", "protocol=TCP", "localip=127.0.0.1", "remoteip=127.0.0.1",
                "localport=59998", "profile=any")
        finish(owner, watcher)
        # netsh returns 1 when no rules match, so inspect its text and exit code directly.
        query = subprocess.run(["netsh.exe", "advfirewall", "firewall", "show", "rule", "name=" + name],
                               capture_output=True, encoding="utf-8", errors="replace", creationflags=CREATE)
        assert name not in query.stdout
        assert sentinel in run("netsh.exe", "advfirewall", "firewall", "show", "rule", "name=" + sentinel)
        checks.append("owner death removed only owned firewall rule")
        result = {"passed": True, "checks": checks}
        (a.out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        for p in owners + guards:
            if p.poll() is None:
                p.kill(); p.wait(timeout=10)
        if owns_active:
            status = run("pktmon.exe", "status")
            filters = run("pktmon.exe", "filter", "list")
            if not inactive(status) and (str(a.out / "owned/raw") in status or filter_name in filters):
                run("pktmon.exe", "stop")
        filters = run("pktmon.exe", "filter", "list")
        rows = [l.strip().split() for l in filters.splitlines() if l.strip() and l.strip()[0].isdigit()]
        if len(rows) == 1 and rows[0][1] == filter_name:
            run("pktmon.exe", "filter", "remove")
        for rule in (name, sentinel):
            subprocess.run(["netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=" + rule],
                           capture_output=True, creationflags=CREATE, timeout=20)

if __name__ == "__main__":
    main()
