"""V5 local plaintext consumer and ER web server. No capture or key input."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
from urllib.parse import unquote, urlparse
import zipfile
from timeline import Timeline

ROOT = (Path(sys._MEIPASS) / "ErCore") if getattr(sys, "frozen", False) else Path(__file__).parent / "ErCore"
os.environ["ER_ROOT"] = str(ROOT)
sys.path[:0] = [str(ROOT / "src/core"), str(ROOT / "src")]
import flowlive
import liveview
import er_radar

MAX_LINE = 2 * 1024 * 1024
ASSETS = ROOT / "web"


class AssetStore:
    """Read release resources from a directory or a ZIP-compatible .pak file."""
    def __init__(self, *, directory=None, archive=None):
        self.directory = Path(directory).resolve() if directory else None
        self.archive = Path(archive).resolve() if archive else None
        self.zip = zipfile.ZipFile(self.archive, "r") if self.archive else None
        self.entries = ({item.filename: item for item in self.zip.infolist() if not item.is_dir()}
                        if self.zip else {})

    @staticmethod
    def _key(value):
        value = unquote(value).replace("\\", "/").strip("/")
        parts = value.split("/")
        if not value or any(part in ("", ".", "..") for part in parts) or "\x00" in value:
            raise ValueError("invalid asset path")
        return "/".join(parts)

    def exists(self, key):
        try:
            key = self._key(key)
        except ValueError:
            return False
        if self.zip:
            return key in self.entries
        return bool(self.directory and (self.directory / key).is_file())

    def read_bytes(self, key):
        key = self._key(key)
        if self.zip:
            return self.zip.read(self.entries[key])
        return (self.directory / key).read_bytes()

    def read_json(self, key):
        return json.loads(self.read_bytes(key).decode("utf-8-sig"))

    def serve(self, handler, path, *, prefix, root_prefix="", cache_s=604800, no_cache=False):
        if not path.startswith(prefix):
            return False
        if not self.zip:
            root = self.directory / root_prefix if root_prefix else self.directory
            return er_radar.m3d_serve(handler, path, prefix=prefix, root=root,
                                      cache_s=cache_s, no_cache=no_cache)
        try:
            rel = self._key(path[len(prefix):] or "index.html")
            key = self._key(root_prefix + "/" + rel) if root_prefix else rel
        except ValueError:
            handler.send_response(403)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        requested = self.entries.get(key)
        body = requested
        encoded_gzip = False
        if body is None:
            body = self.entries.get(key + ".gz")
            encoded_gzip = body is not None
        elif "gzip" in (handler.headers.get("Accept-Encoding") or "").lower():
            sidecar = self.entries.get(key + ".gz")
            if sidecar is not None:
                body = sidecar
                encoded_gzip = True
        if body is None:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        etag = '"%08x-%x%s"' % (body.CRC, body.file_size, "-gz" if encoded_gzip else "")
        if handler.headers.get("If-None-Match") == etag:
            handler.send_response(304)
            handler.send_header("ETag", etag)
            if encoded_gzip:
                handler.send_header("Vary", "Accept-Encoding")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        suffix = Path(key).suffix.lower()
        handler.send_response(200)
        handler.send_header("Content-Type", er_radar.M3D_MIME.get(suffix, "application/octet-stream"))
        handler.send_header("Content-Length", str(body.file_size))
        handler.send_header("ETag", etag)
        if "?v=" in (handler.path or ""):
            handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif no_cache:
            handler.send_header("Cache-Control", "no-cache")
        else:
            handler.send_header("Cache-Control", "public, max-age=%d" % cache_s)
        if encoded_gzip:
            handler.send_header("Content-Encoding", "gzip")
            handler.send_header("Vary", "Accept-Encoding")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        if handler.command == "HEAD":
            return True
        try:
            with self.zip.open(body, "r") as source:
                while True:
                    chunk = source.read(262144)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except (ConnectionError, BrokenPipeError, OSError):
            handler.close_connection = True
        return True


ASSET_STORE = AssetStore(directory=ASSETS)
MODEL_STORE = AssetStore(directory=ASSETS / "m3d")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded(value), encoding="utf-8")
    temp.replace(path)


class ReplayClock:
    current = 0.0
    def time(self):
        return self.current


class PlaintextLoader(flowlive.IncrementalFlowLoader):
    def __init__(self, identity):
        super().__init__(identity, s2c_table=None, auto_s2c=False, c2s_self=True, request_prefix=False)
        self.enable_c2s_self(1)
        self.pending_packets = deque()

    def poll(self, max_frames=None):
        added = 0
        for _ in range(min(len(self.pending_packets), max_frames or 256)):
            seq, direction, payload, timestamp = self.pending_packets.popleft()
            added += self.consume_packet(seq, direction, payload, timestamp)
        return added

    def exhausted(self):
        return not self.pending_packets


class Flow:
    def __init__(self, output, number, identity):
        self.number, self.identity = number, identity
        self.loader = PlaintextLoader(output / "flows" / str(number))
        self.view = liveview.LiveView(output, self.loader.flow_dir, loader=self.loader)
        self.packets = Counter()
        self.last_received = time.monotonic()
        self.last_timestamp = 0.0
        self.latest_sequence = 0

    def summary(self):
        return {"flow": str(self.number), "local": self.identity[1], "remote": self.identity[2],
                "epoch": self.identity[3], "packets": dict(self.packets),
                "logicalBunches": self.loader.n_lb, "packetErrors": self.loader.packet_errors,
                "selfPoseSamples": self.loader.pose_samples, "playerRecords": len(self.view.players),
                "movementSlots": len(self.view.slots), "mraChannel": self.view.mra_ch,
                "movementSamples": sum(len(track) for track in self.view.slots.values()),
                "handshakeOk": self.loader.hs.ok, "handshakeFull": self.loader.hs.full,
                "welcome": self.loader.hs.welcome, "semanticErrors": self.view.decode_error_count()}


class Engine:
    def __init__(self, output, replay=False, idle_seconds=15, cache_only=False):
        self.output, self.replay, self.idle_seconds = Path(output), replay, idle_seconds
        self.cache_only = cache_only
        self.timeline = None
        self.transport = {}
        self.journal_read_bytes = 0
        self.last_disk_write = 0.0
        self.rate_events = deque()
        self.last_pose_keys = {}
        self.last_pose_seen = {}
        self.last_identity = None
        self.last_identity_write = 0.0
        self.output.mkdir(parents=True, exist_ok=True)
        self.clock = ReplayClock() if replay else None
        flowlive.time = self.clock or time
        liveview.time = self.clock or time
        self.flows = {}
        self.retired = deque(maxlen=64) if cache_only else []
        self.epochs = {}
        self.session = 0
        self.flow_counter = 0
        self.sequence = None
        self.selected = None
        self.pinned = None
        self.ended = False
        self.fatal_error = None
        self.stats = Counter()
        self.last_state = self.empty("waiting")
        self.last_nonempty = None
        self.lock = threading.RLock()
        self.state_sequence = 0
        self.states = None if cache_only else (self.output / "states.jsonl").open("x", encoding="utf-8", buffering=1)
        self.errors = (self.output / "parser-errors.jsonl").open("w" if cache_only else "x", encoding="utf-8", buffering=1)
        if cache_only:
            for name in ("snapshot-final.json", "parser-result.json"):
                (self.output / name).unlink(missing_ok=True)
            save(self.output / "state-latest.json", self.last_state)
            self.cache_identity(force=True)

    def cache_identity(self, force=False):
        if not self.cache_only or (not force and time.monotonic() - self.last_identity_write < 1):
            return
        self.last_identity_write = time.monotonic()
        player_fields = ("name", "player_id", "bot", "actor_guid", "owner_guid", "channel", "uin",
                         "team", "camp", "inner_team", "hero_id", "expert_id", "spawn_seq")
        link_fields = ("owner", "ps_guid", "spawn_seq", "cls", "ai_key", "hero", "move_handle", "move_handle_seq")
        flows = []
        for f in self.flows.values():
            flows.append({"identity": f.identity,
                          "players": {str(k): {field: v[field] for field in player_fields if field in v}
                                      for k, v in f.view.players.items()},
                          "characters": {str(k): {field: v[field] for field in link_fields if field in v}
                                         for k, v in f.view.links.items()},
                          "playerChannels": dict(f.view.ps_ch_actor),
                          "characterChannels": dict(f.view.ch_actor),
                          "slotClaims": dict(f.view.claim)})
        identity = {"schema": "gateway-identity-cache/v5", "session": self.session,
                    "restoreSupported": False, "flows": flows}
        serialized = encoded(identity)
        if serialized != self.last_identity:
            save(self.output / "identity-cache.json", identity)
            self.last_identity = serialized

    def empty(self, status):
        return {"schema": "gateway-state/v5", "self": None, "entities": [], "names": [],
                "live_active": False, "status": status, "cursor": {"seq": 0, "live": not self.replay}}

    def retire(self, key):
        flow = self.flows.pop(key)
        while not flow.loader.exhausted():
            flow.view.poll(max_frames=256)
        self.retired.append(flow.summary())
        if self.selected == key:
            self.selected = None
        if self.pinned == key:
            self.pinned = None

    def reset(self):
        for key in list(self.flows):
            self.retire(key)
        self.epochs.clear()
        self.last_state = self.empty("waiting")
        self.cache_identity(force=True)

    def accept(self, event):
        with self.lock:
            kind = event.get("type")
            if kind == "session_status":
                self.transport = event.get("data") or {}
            data = event.get("data") or {}
            if kind == "parser_fault":
                self.fatal_error = str(data.get("error", "Input failed"))
                self.flush(force=True)
                return
            if kind == "parser_input_complete":
                self.flush(force=True)
                self.ended = True
                return
            if kind == "session_started":
                self.reset()
                self.session += 1
                self.sequence = None
                self.ended = False
            sequence = event.get("sequence")
            if sequence is not None:
                if not isinstance(sequence, int) or sequence < 0:
                    raise ValueError("Invalid event sequence")
                if self.sequence is not None and sequence <= self.sequence:
                    self.stats["duplicateOrOutOfOrder"] += 1
                    return
                if self.sequence is not None and sequence != self.sequence + 1:
                    self.stats["eventGaps"] += sequence - self.sequence - 1
                    self.reset()
                self.sequence = sequence
            self.stats["inputEvents"] += 1
            if kind == "session_stopped":
                self.flush(force=True)
                self.ended = True
                return
            if kind == "udp_binding_reset":
                for key in list(self.flows):
                    if key[1] + ">" + key[2] == data.get("flow"):
                        self.retire(key)
                return
            if kind != "udp_plaintext":
                return
            if sequence is None:
                raise ValueError("Plaintext packet is missing its sequence")
            direction = {"up": "request", "down": "response"}[data["direction"]]
            timestamp = float(data["timestampMs"]) / 1000
            if not math.isfinite(timestamp) or timestamp <= 0:
                raise ValueError("Invalid packet timestamp")
            payload = base64.b64decode(data["payloadBase64"], validate=True)
            if not 1 <= len(payload) <= 65535 or len(payload) != data["payloadLength"]:
                raise ValueError("Invalid plaintext packet length")
            local, remote, epoch = data["local"], data["remote"], data["epoch"]
            if not isinstance(local, str) or not isinstance(remote, str) or not isinstance(epoch, int) or epoch < 1:
                raise ValueError("Invalid flow identity")
            pair = (local, remote)
            if epoch < self.epochs.get(pair, 0):
                self.stats["retiredEpochPackets"] += 1
                return
            if epoch > self.epochs.get(pair, 0):
                for key in list(self.flows):
                    if key[1:3] == pair:
                        self.retire(key)
                self.epochs[pair] = epoch
            if self.clock:
                self.clock.current = timestamp
            identity = (self.session, local, remote, epoch)
            if identity not in self.flows:
                if len(self.flows) >= 64:
                    raise ValueError("活动连接超出 64，保留归档并报告错误")
                self.flow_counter += 1
                self.flows[identity] = Flow(self.output, self.flow_counter, identity)
            flow = self.flows[identity]
            flow.loader.pending_packets.append((sequence, direction, payload, timestamp))
            flow.packets[direction] += 1
            flow.last_received = time.monotonic()
            flow.last_timestamp, flow.latest_sequence = timestamp, sequence
            self.stats["plaintextPackets"] += 1
            self.stats["upPackets" if direction == "request" else "downPackets"] += 1
            self.rate_events.append((time.monotonic(), "up" if direction == "request" else "down", 1))
            if len(flow.loader.pending_packets) >= 256:
                flow.view.poll(max_frames=256)

    def flush(self, force=False):
        with self.lock:
            for flow in self.flows.values():
                while not flow.loader.exhausted():
                    flow.view.poll(max_frames=256)
            eligible = [key for key, f in self.flows.items() if f.loader.hs.ok]
            if self.pinned in eligible:
                self.selected = self.pinned
            elif eligible:
                self.selected = max(eligible, key=lambda k: self.flows[k].latest_sequence)
            else:
                self.selected = None
            flow = self.flows.get(self.selected)
            if self.fatal_error:
                state = self.empty("error")
                state["error"] = self.fatal_error
            elif flow is None:
                state = self.empty("stopped" if self.ended else "waiting")
            elif not self.replay and (self.ended or time.monotonic() - flow.last_received > self.idle_seconds):
                state = self.empty("stopped" if self.ended else "idle")
            else:
                state = flow.view.snapshot()
                # Trails remain available in the parser; avoid repeating growing history in every event.
                for entity in state["entities"]:
                    entity.pop("trail", None)
                state.update(schema="gateway-state/v5", status="replay" if self.replay else "live",
                             live_active=not self.replay, session=self.session, epoch=flow.identity[3],
                             local=flow.identity[1], remote=flow.identity[2], timestampMs=int(flow.last_timestamp * 1000))
                pose = flow.loader.self_pose
                pose_age = (self.clock.time() if self.clock else time.time()) - flow.loader.self_pose_t
                if pose and 0 <= pose_age <= 0.25:
                    state.update(self_pitch=round(pose["pitch"], 1), self_aim_yaw=round(pose["yaw"], 1),
                                 self_aim_age_ms=round(pose_age * 1000), self_aim_source="upstream")
                    if pose_age <= 1:
                        state.update(self_up_loc=pose["loc"], self_up_ts=pose["ts"])
                state["cursor"]["live"] = not self.replay
            now = time.monotonic()
            if flow is not None:
                for entity in state.get("entities", []):
                    if entity.get("kind") == "loot":
                        entity["identity_status"] = ("missing_item_id" if not entity.get("item_id") else
                            "name_not_mapped" if entity.get("name") in (None, "", "物资") else "identified")
                        entity["metadata_status"] = "available" if entity.get("price") is not None else "missing"
                    if entity.get("kind") not in ("player", "mate", "unknown"):
                        continue
                    slot = entity.get("slot")
                    track = flow.view.slots.get(slot) or []
                    key = (flow.identity, entity.get("key"))
                    if track:
                        seq = track[-1][0]
                        prev = self.last_pose_keys.get(key, -1)
                        count = sum(1 for row in reversed(track) if row[0] > prev)
                        if count:
                            self.last_pose_keys[key] = seq
                            self.last_pose_seen[key] = now
                            self.rate_events.append((now, "characters", count))
                            if not entity.get("is_bot"):
                                self.rate_events.append((now, "human", count))
                        entity["pose_seq"] = seq
                        entity["pose_timestamp_ms"] = int(track[-1][1] * 1000)
                        entity["pose_age_ms"] = round((now - self.last_pose_seen.get(key, now)) * 1000)
                pose_key = (flow.identity, "self")
                if flow.loader.pose_samples != self.last_pose_keys.get(pose_key, 0):
                    count = flow.loader.pose_samples - self.last_pose_keys.get(pose_key, 0)
                    self.last_pose_keys[pose_key] = flow.loader.pose_samples
                    self.last_pose_seen[pose_key] = now
                    self.rate_events.append((now, "self", max(0, count)))
            if self.timeline:
                replay_state = self.timeline.status()
                state["replay"] = replay_state
                state["cursor"] = dict(seq=replay_state["position"], lo=0, hi=replay_state["duration"],
                                       t_lo=0, t_hi=replay_state["duration"], live=False)
            if state != self.last_state or force:
                self.last_state = state
                if state.get("self") is not None or state.get("entities"):
                    self.last_nonempty = state
                self.state_sequence += 1
                event = {"schema": "gateway-semantic/v5", "type": "state", "sequence": self.state_sequence,
                         "timestampMs": int(time.time() * 1000), "data": state}
                if self.states is not None:
                    self.states.write(encoded(event) + "\n")
                if force or now - self.last_disk_write >= 0.5:
                    save(self.output / "state-latest.json", state)
                    self.last_disk_write = now
                    save(self.output / "status-latest.json", self.status())
            self.cache_identity(force=force)

    def error(self, message):
        with self.lock:
            self.stats["inputErrors"] += 1
            if self.cache_only and self.errors.tell() >= 1024 * 1024:
                self.errors.seek(0)
                self.errors.truncate()
            self.errors.write(encoded({"sequence": self.sequence, "error": str(message)[:8192]}) + "\n")
            self.reset()

    def api_state(self):
        with self.lock:
            return encoded(self.last_state).encode("utf-8")

    def api_flows(self):
        with self.lock:
            top = [{"flow": str(f.number), "n_ps": len(f.view.players)} for f in self.flows.values()]
            selected = self.flows.get(self.selected)
            return {"runs": [{"run": self.output.name, "top": top}],
                    "current": [self.output.name, str(selected.number)] if selected else None}

    def api_select(self, run, flow):
        with self.lock:
            for key, item in self.flows.items():
                if run == self.output.name and str(item.number) == flow and item.loader.hs.ok:
                    self.pinned = key
                    return {"ok": True}
            return {"ok": False, "error": "Flow is not active"}

    def restart_replay(self):
        with self.lock:
            self.flows.clear()
            self.retired.clear()
            self.epochs.clear()
            self.sequence = self.selected = self.pinned = None
            self.session = self.flow_counter = 0
            self.stats.clear()
            self.rate_events.clear()
            self.last_pose_keys.clear()
            self.last_pose_seen.clear()
            self.ended = False
            self.fatal_error = None
            self.last_nonempty = None
            self.last_state = self.empty("seeking")

    def api_ctrl(self, query):
        if self.timeline:
            try: return self.timeline.control(query)
            except (ValueError, TypeError) as exc: return {"ok": False, "error": str(exc)}
        return {"ok": False, "live": not self.replay, "error": "This view follows the input stream"}

    def status(self):
        with self.lock:
            now = time.monotonic()
            while self.rate_events and self.rate_events[0][0] < now - 2:
                self.rate_events.popleft()
            rates = Counter()
            for _, kind, count in self.rate_events:
                rates[kind] += count / 2
            entities = self.last_state.get("entities", [])
            counts = Counter(e.get("kind", "unknown") for e in entities)
            counts["human"] = sum(e.get("kind") in ("player", "mate") and not e.get("is_bot") for e in entities)
            counts["bot"] = sum(e.get("kind") == "ai" or bool(e.get("is_bot")) for e in entities)
            counts["characters"] = sum(e.get("kind") in ("player", "mate", "unknown") for e in entities)
            counts["charactersIncludingSelf"] = counts["characters"] + int(self.last_state.get("self") is not None)
            counts["confirmedBots"] = sum(e.get("kind") in ("player", "mate", "unknown") and bool(e.get("is_bot")) for e in entities)
            # Compatibility alias for older local consumers. is_bot is already multi-field confirmed.
            counts["suspectedBots"] = counts["confirmedBots"]
            missing = sum(e.get("kind") == "loot" and not e.get("item_id") for e in entities)
            return {"schema": "gateway-parser-status/v5", "stats": dict(self.stats),
                    "counts": dict(counts), "unknownLoot": missing, "rates": dict(rates),
                    "transport": self.transport, "journalReadBytes": self.journal_read_bytes,
                    "replay": self.timeline.status() if self.timeline else None,
                    "flows": list(self.retired) + [f.summary() for f in self.flows.values()],
                    "states": self.state_sequence, "ended": self.ended, "error": self.fatal_error}

    def close(self):
        self.flush(force=True)
        save(self.output / "snapshot-final.json", self.last_nonempty or self.last_state)
        save(self.output / "parser-result.json", self.status())
        if self.states is not None:
            self.states.close()
        self.errors.close()


class Handler(er_radar.Handler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/resources/"):
            ASSET_STORE.serve(self, path, prefix="/resources/")
            return
        if path.startswith("/ui/"):
            er_radar.m3d_serve(self, path, prefix="/ui/", root=ROOT / "web/ui")
            return
        if path.startswith("/m3d/") and (path.endswith(".glb") or path.endswith("manifest.json")):
            MODEL_STORE.serve(self, path, prefix="/m3d/")
        elif path == "/api/status":
            self._json(self.app.status())
        elif path.startswith("/vendor/"):
            er_radar.m3d_serve(self, path, prefix="/vendor/", root=ROOT / "web/vendor")
        else:
            super().do_GET()


def prepare_page():
    maps = MODEL_STORE.read_json("manifest.json").get("maps", {}) if MODEL_STORE.exists("manifest.json") else {}
    terrain_exists = any(MODEL_STORE.exists(item.get("file", "missing")) or
                         MODEL_STORE.exists(item.get("file", "missing") + ".gz")
                         for item in maps.values())
    if not terrain_exists:
        er_radar.PAGE = er_radar.PAGE.replace('<meta name="tq-no3d" content="0">',
                                               '<meta name="tq-no3d" content="1">')


def main():
    global ASSETS, ASSET_STORE, MODEL_STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--port", type=int, default=17891)
    ap.add_argument("--source", type=Path)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--cache-only", action="store_true")
    asset_group = ap.add_mutually_exclusive_group()
    asset_group.add_argument("--assets", type=Path)
    asset_group.add_argument("--asset-pak", type=Path)
    ap.add_argument("--models", type=Path)
    ap.add_argument("--journal", type=Path)
    ap.add_argument("--timeline", type=Path)
    ap.add_argument("--parent-pid", type=int)
    args = ap.parse_args()
    if args.assets:
        ASSETS = args.assets
        ASSET_STORE = AssetStore(directory=args.assets)
    elif args.asset_pak:
        ASSET_STORE = AssetStore(archive=args.asset_pak)
    if args.models:
        MODEL_STORE = AssetStore(directory=args.models)
    engine = Engine(args.output, replay=args.replay or bool(args.timeline), cache_only=args.cache_only)
    stop = threading.Event()
    if args.timeline:
        engine.timeline = Timeline(engine, args.timeline, stop)
    prepare_page()
    # ER's inherited routes refer to its base Handler class by name.
    Handler.app = er_radar.Handler.app = engine
    map_info = er_radar.load_map_info()
    for item in map_info.get("maps", []):
        item["tileUrl"] = "/resources/maps2d/" + item["key"] + "/{z}_{x}_{y}.jpg"
    Handler.map_info = er_radar.Handler.map_info = map_info
    server = er_radar.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if args.parent_pid and os.name == "nt":
        def watch_parent():
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, args.parent_pid)
            if handle:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
                kernel.CloseHandle(handle)
            os._exit(3)
        threading.Thread(target=watch_parent, daemon=True).start()
    print(encoded({"type": "parser_ready", "port": server.server_port, "schema": "gateway-state/v5"}), flush=True)
    if args.timeline:
        worker = threading.Thread(target=engine.timeline.run, daemon=True)
        worker.start()
        try:
            # stdin EOF is the parent's clean shutdown signal.
            for _ in sys.stdin.buffer:
                pass
        finally:
            stop.set(); worker.join(timeout=30)
            engine.close(); server.shutdown(); server.server_close()
            print(encoded({"type": "parser_completed", "result": engine.status()}), flush=True)
        return
    inbox = queue.Queue(maxsize=4096)
    def read_input():
        source = None
        try:
            if args.journal:
                def parent_eof():
                    for _ in sys.stdin.buffer:
                        pass
                    stop.set()
                threading.Thread(target=parent_eof, daemon=True).start()
                source = args.journal.open("rb")
                pending = b""
                while True:
                    chunk = source.readline(MAX_LINE + 1)
                    if chunk:
                        pending += chunk
                        if len(pending) > MAX_LINE:
                            raise ValueError("Journal line exceeds limit")
                        if pending.endswith(b"\n"):
                            inbox.put(pending)
                            engine.journal_read_bytes = source.tell()
                            pending = b""
                        continue
                    if stop.is_set():
                        if pending:
                            raise ValueError("Incomplete final journal record")
                        break
                    stop.wait(0.005)
                return
            source = args.source.open("rb") if args.source else sys.stdin.buffer
            while True:
                line = source.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    inbox.put(ValueError("JSONL line exceeds limit"))
                    break
                inbox.put(line)
        except Exception as exc:
            inbox.put(exc)
        finally:
            if (args.source or args.journal) and source is not None:
                source.close()
            inbox.put(None)
    threading.Thread(target=read_input, daemon=True).start()
    last_flush = time.monotonic()
    try:
        done = False
        while not done:
            try:
                line = inbox.get(timeout=0.015)
            except queue.Empty:
                line = b""
            for _ in range(256):
                if line is None:
                    done = True
                    break
                if isinstance(line, Exception):
                    raise line
                if line.strip():
                    try:
                        event = json.loads(line.decode("utf-8-sig"))
                        engine.accept(event)
                        if event.get("type") == "parser_input_complete":
                            print(encoded({"type": "parser_input_drained"}), flush=True)
                    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                        engine.error(exc)
                if _ == 255:
                    break
                try:
                    line = inbox.get_nowait()
                except queue.Empty:
                    break
            if time.monotonic() - last_flush >= 1 / 30 or done:
                engine.flush()
                last_flush = time.monotonic()
        engine.close()
        print(encoded({"type": "parser_completed", "result": engine.status()}), flush=True)
    except Exception as exc:
        engine.fatal_error = str(exc)
        engine.close()
        raise
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
