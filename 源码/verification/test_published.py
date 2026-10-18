"""Exercise the shipped parser's pipe, HTTP server, failures and parent cleanup."""
import argparse
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen


def launch(exe, release, output, *, port=0, parent=None, replay=True):
    args = [str(exe), "--output", str(output), "--port", str(port),
            "--asset-pak", "NvaAssets.pak", "--models", "models"]
    if replay:
        args.append("--replay")
    if parent:
        args += ["--parent-pid", str(parent)]
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=release, creationflags=subprocess.CREATE_NO_WINDOW)
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            messages.put(json.loads(line.decode("utf-8")))
    threading.Thread(target=read, daemon=True).start()
    errors = []
    def read_errors():
        errors.extend(line.decode("utf-8", "replace") for line in process.stderr)
    threading.Thread(target=read_errors, daemon=True).start()
    return process, messages, errors


def get(base, path):
    with urlopen(base + path, timeout=15) as response:
        assert response.status == 200
        return response.read()


def await_type(messages, wanted):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
        if message["type"] == wanted:
            return message
    raise TimeoutError(wanted)


def finish(process):
    if process.poll() is None:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    release = args.release.resolve()
    main_exe = release / "Nva本地离线解析.exe"
    bundle = json.loads(subprocess.check_output([str(main_exe), "--extract-nva-parser"], text=True,
                                                creationflags=subprocess.CREATE_NO_WINDOW))
    exe = Path(bundle["path"])
    assert exe.is_file() and bundle["bytes"] == exe.stat().st_size
    assert (release / "NvaAssets.pak").is_file() and (release / "models/manifest.json").is_file()
    assert not any((release / name).exists() for name in ("assets", "parser", "licenses"))
    results = {}
    results["embeddedParser"] = {"bytes": bundle["bytes"], "sha256": bundle["sha256"]}
    proc, messages, errors = launch(exe, release, args.out / "pipe-http")
    try:
        ready = await_type(messages, "parser_ready")
        base = "http://127.0.0.1:%d" % ready["port"]
        assert json.loads(get(base, "/api/state"))["entities"] == []
        html = get(base, "/").decode("utf-8")
        assert "<title>TQ</title>" in html
        assert '/vendor/leaflet/leaflet.js' in html and '<meta name="tq-no3d"' in html
        assert '/ui/app.css?v=1.2.0' in html and '/ui/app.js?v=1.2.0' in html
        assert '/ui/dock.js?v=1.2.0' in html
        assert 'id="s-walltrans"' in html and 'id="s-floortrans"' in html
        appjs = get(base, '/ui/app.js?v=1.2.0').decode("utf-8")
        assert 'korr-adapter.js?v=1.1.0' in appjs and 'const NO3D' in appjs
        assert len(json.loads(get(base, '/m3d/manifest.json'))['maps']) == 6
        assert len(get(base, '/vendor/three/build/three.module.js')) > 1000000
        assert b'gateway.follow' in get(base, '/m3d/korr-adapter.js?v=1.1.0')
        for name in ('korr-adapter.js','korr-renderer.js','gateway-pose.js','character-models.js'):
            assert get(base, '/m3d/'+name+'?v=1.1.0') == (Path(__file__).resolve().parents[1]/'Parser/ErCore/web/m3d'/name).read_bytes()
        results['packaged3dModulesMatchSource'] = True
        assert get(base,'/ui/shubao.css') == (Path(__file__).resolve().parents[1]/'Parser/ErCore/web/ui/shubao.css').read_bytes()
        dock = get(base,'/ui/dock.js?v=1.2.0')
        assert dock == (Path(__file__).resolve().parents[1]/'Parser/ErCore/web/ui/dock.js').read_bytes()
        assert "女娃" not in dock.decode("utf-8") and "TQ" in dock.decode("utf-8")
        assert get(base,'/ui/icons/helmet.png').startswith(b'\x89PNG')
        assert get(base,'/ui/icons/armor.png').startswith(b'\x89PNG')
        assert get(base,'/ui/icons/bag.png').startswith(b'\x89PNG')
        assert get(base,'/ui/icons/rig.png').startswith(b'\x89PNG')
        results['packagedUiMatchesSource'] = True
        maps=json.loads(get(base,'/api/map'))['maps']
        for m in maps:
            assert m['tileUrl'].startswith('/resources/maps2d/')
            assert get(base,m['tileUrl'].format(z=2,x=1,y=1)).startswith(b'\xff\xd8')
        results['sixMapsServedLocally'] = True
        assert len(get(base, "/vendor/leaflet/leaflet.js")) > 100000
        assert len(get(base, "/vendor/leaflet/leaflet.css")) > 10000
        assert json.loads(get(base, "/api/map"))["maps"]
        lines = args.source.read_bytes().splitlines(keepends=True)
        # Split JSONL records arbitrarily to exercise pipe framing, including multi-byte UTF-8.
        raw = b"".join(lines)
        for offset in range(0, len(raw), 997):
            proc.stdin.write(raw[offset:offset + 997])
        proc.stdin.write(b'{"type":"parser_input_complete"}\n')
        proc.stdin.flush()
        await_type(messages, "parser_input_drained")
        state = json.loads(get(base, "/api/state"))
        status = json.loads(get(base, "/api/status"))
        assert len(state["entities"]) == 30
        assert state["self"] == [353249.3, -799345.4, -16749.0]
        assert status["stats"]["plaintextPackets"] == 3854
        assert status['counts']['characters']==11
        assert status['counts']['charactersIncludingSelf']==12
        assert status['counts']['confirmedBots']==0
        assert status['counts']['suspectedBots']==0
        assert status["stats"].get("inputErrors", 0) == 0
        assert status["flows"][0]["selfPoseSamples"] == 927
        assert status["flows"][0]["packetErrors"] == 0
        assert status["flows"][0]["semanticErrors"] == 0
        results["pipeFragmentationAndHttp"] = {"packets": 3854, "entities": 30, "selfPoseSamples": 927}
    finally:
        finish(proc)
    assert proc.returncode == 0, errors
    assert (args.out / "pipe-http/snapshot-final.json").is_file()
    with socket.socket() as check:
        check.bind(("127.0.0.1", ready["port"]))
    results["gracefulExitAndPortRelease"] = True

    missing = subprocess.run([str(exe), "--output", str(args.out / "missing-source"), "--port", "0",
                              "--asset-pak", "NvaAssets.pak", "--models", "models",
                              "--source", str(args.out / "absent.jsonl")], cwd=release,
                             capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert missing.returncode != 0
    assert json.loads((args.out / "missing-source/parser-result.json").read_text(encoding="utf-8"))["error"]
    results["missingSourceFailsWithoutHanging"] = True

    proc, messages, errors = launch(exe, release, args.out / "live-updates", replay=False)
    try:
        ready = await_type(messages, "parser_ready")
        base = "http://127.0.0.1:%d" % ready["port"]
        events = [json.loads(line) for line in lines]
        midpoint = len(events) // 2
        snapshots = []
        expected = 0
        for chunk in (events[:midpoint], events[midpoint:]):
            for event in chunk:
                if event["type"] == "session_stopped":
                    continue
                if event["type"] == "udp_plaintext":
                    event["data"]["timestampMs"] = int(time.time() * 1000)
                    expected += 1
                proc.stdin.write(json.dumps(event).encode("utf-8") + b'\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 20
            while True:
                status = json.loads(get(base, "/api/status"))
                state = json.loads(get(base, "/api/state"))
                previous = snapshots[-1]["cursor"]["seq"] if snapshots else 0
                if (status["stats"].get("plaintextPackets") == expected and state.get("status") == "live"
                        and state["cursor"]["seq"] > previous and state.get("self") is not None):
                    break
                assert time.monotonic() < deadline, "Live state did not advance"
                time.sleep(0.05)
            snapshots.append(state)
        assert snapshots[0]["self"] != snapshots[1]["self"]
        assert snapshots[1]["live_active"]
        results["liveStateAdvances"] = {"firstSequence": snapshots[0]["cursor"]["seq"],
                                       "lastSequence": snapshots[1]["cursor"]["seq"], "packets": expected}
    finally:
        finish(proc)

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        proc, messages, errors = launch(exe, release, args.out / "occupied", port=occupied.getsockname()[1])
        try:
            assert proc.wait(timeout=30) != 0
            results["occupiedPortFails"] = True
        finally:
            finish(proc)

    proc, messages, errors = launch(exe, release, args.out / "bad-json")
    try:
        ready = await_type(messages, "parser_ready")
        proc.stdin.write(b'{broken json}\n{"type":"parser_input_complete"}\n')
        proc.stdin.flush()
        await_type(messages, "parser_input_drained")
        status = json.loads(get("http://127.0.0.1:%d" % ready["port"], "/api/status"))
        assert status["stats"]["inputErrors"] == 1
        results["invalidInputReported"] = True
    finally:
        finish(proc)

    parent = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    proc, messages, errors = launch(exe, release, args.out / "parent-exit", parent=parent.pid)
    try:
        ready = await_type(messages, "parser_ready")
        parent.terminate()
        parent.wait(timeout=10)
        assert proc.wait(timeout=30) == 3
        with socket.socket() as check:
            check.bind(("127.0.0.1", ready["port"]))
        results["parentExitClosesWorkerAndPort"] = True
    finally:
        if parent.poll() is None:
            parent.terminate()
        finish(proc)
    (args.out / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
