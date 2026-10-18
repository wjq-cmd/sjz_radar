# -*- coding: utf-8 -*-
"""er_radar.py - 网络层地图雷达（确定性，无启发式/无扫描）。

数据流:
  1. 战斗流自识别: 扫描 captures 全部 run，逐 flow 统计 PlayerState(arch=131) 通道数，
     取最大者为该 run 的战斗流（与 er_live.detect_battle_server 同一确定性标准）。
     计数持久化 src/core/radar_detect_cache.json；最新 run 优先、flow 按 mtime 新->旧，
     命中 n_ps>=3 即预锁定出图，后台索引线程扫完全部 run 收敛到最大值；
     周期复扫新 run / 新 flow（实时抓包自动跟随），战斗流增长即全量重建。
  2. 确定性解析（复用 er_live 管线）:
     PlayerState(arch=131) -> 名字/player_id/owner/actor
     角色spawn(arch=105)   -> h14/h18 绑定 + SNA loc + spawn_seq
     MovementReplicationActor(handle=21) -> 每 slot 逐帧 (seq,ts,x,y,z,yaw)
     join: 角色出生点 <-> slot@spawn_seq (<500cm) 最小距离唯一认领;
     15 slots = 14 远程 + 1 self: 唯一未认领 slot 即本地玩家轨迹（封闭性）。
  3. 回放: seq 游标按真实时间映射推进（frames.jsonl ts 拟合 seq/sec），
     速度可调；/api/state 与 tools/web_radar.py 同构（self/entities/world/rel/names）。

Usage: python src/core/er_radar.py [--run RUN] [--port 8888]
"""
import argparse, bisect, json, shutil, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

import numpy as np

import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "src/core"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_DEBUG = os.environ.get("ER_DEBUG") == "1"   # 调试日志总开关（默认关，正式包静默）

from flowload import load_cached, load
import asset_crypto
from blocks import tile
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int
import sna
import er_live as nl
import repmove4 as rm4
import handshake as hs_mod
import state_track as st
import field13 as f13
from weapon_state import weapon_fields
from death_state import DeathStateTracker
from death_box_state import DeathBoxTracker, box_entity
from box_contents import BoxContentsTracker
from liveview import LOOT_NAMES, confirmed_player_bot
from playerstate_protocol import PlayerStateTracker
from liveview import WEAPON_NAMES, loot_display_fields
from liveview import (LiveView, LiveDetector, extract_loot_full,
                      mark_picked_by_standing, LOOT_PICK_INFER, HERO_NAMES)

DETECT_CACHE = ROOT / "src/core" / "radar_detect_cache.json"
# 坐标合法性判据：与 liveview.WORLD 同源（引擎级 sanity，非地图边界）。
# 见 liveview.py 处的完整说明——硬编码地图窗每上新图必失效，2026-08-29 新图
# (Y∈[-226k,-152k]) 曾使整局样本 100% 被丢。此处直接复用，避免两处判据分叉。
from liveview import WORLD, WORLD_MAX  # noqa: E402  (判据单一来源)

# ---------------------------------------------------------------- 3D 静态资源
# web/m3d/ 由 src/core/build_m3d.py 产出：<mapkey>.glb（白模底图）+ r3d.js（3D 引擎）
# + three.module.js + manifest.json。雷达页按需 import("/m3d/r3d.js")；
# 模型 16~99MB，靠 immutable 长缓存做到一张图只真下一次；.gz 边车把线上
# 字节再压 1/3（手机流量）。改 r3d.js 后必须同步 bump PAGE 里的 ?v= 串。
M3D_DIR = ROOT / "web" / "m3d"
UI_DIR = ROOT / "web" / "ui"
# 干员头像：web/avatars/<干员名>.png（由资源包 resources/operators 复制而来）
AVATAR_DIR = ROOT / "web" / "avatars"
# Korr 前端（第三方 UI）：整棵放在 web/korr/，radar.html + static/{css,js,maps,tx}。
# 由 ER_UI 决定首页用哪套：korr（默认）= Korr 雷达页，er = 本文件内置的老页面。
# 老页面始终可从 /er 打开，两套共用同一个 /api/state。
KORR_DIR = ROOT / "web" / "korr"
KORR_UI = False          # 自用包：只有内置页，Korr 前端不在包里
# Korr 3D 视图要的地形三角形汤（web/korr/maps3d/<key>.f32.gz + manifest.json，
# 由 tools/m3d_to_korr3d.py 从 web/m3d 的地形白模导出）。它的页面按
# /api/map/<地图名>/meta 与 /api/map/<地图名> 两份接口取，见 korr3d_map()。
KORR3D_DIR = KORR_DIR / "maps3d"
M3D_MIME = {
    ".glb": "model/gltf-binary",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".bin": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def m3d_serve(handler, path, cache_s=604800, prefix="/m3d/", root=None, no_cache=False):
    """把 /m3d/<file> 交给静态目录。命中（含 404）返回 True，非本前缀返回 False。

    分块写出，避免几十 MB 模型在内存里再拷一份；带 ETag/If-None-Match，
    来回切图不会重复下载。
    同名 .gz 边车（mtime 不旧于本体）+ 客户端接受 gzip 时直接发压缩体，
    GLB 线上字节 -1/3；URL 带 ?v= 内容版本的一律 immutable——GLB 的 ?v=rev
    与 r3d.js 的 ?v=串换内容必换 URL，长缓存才不会挡住更新。
    """
    root = root or M3D_DIR
    if not path.startswith(prefix):
        return False
    rel = unquote(path[len(prefix):])      # 中文文件名（干员头像）需先解码
    if not rel or rel.endswith("/"):
        rel = "index.html"
    try:
        f = (root / rel).resolve()
        f.relative_to(root.resolve())             # 目录穿越防护
    except (ValueError, OSError):
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    suf = f.suffix.lower()
    pre_gz = False
    if not f.is_file():
        # 独立版只带 .gz 模型（体积 1/3）：明文缺位时按 gzip 体直出
        gz_only = f.with_name(f.name + ".gz")
        if gz_only.is_file():
            f = gz_only
            pre_gz = True
        else:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
    st_ = f.stat()
    etag = '"%x-%x"' % (int(st_.st_mtime), st_.st_size)
    body = f
    if not pre_gz and "gzip" in (handler.headers.get("Accept-Encoding") or "").lower():
        gz = f.with_name(f.name + ".gz")
        try:
            gz_st = gz.stat()
            if gz_st.st_mtime >= st_.st_mtime - 1:   # 边车过期回退明文
                etag = '"%x-%x-gz"' % (int(gz_st.st_mtime), gz_st.st_size)
                body = gz
        except OSError:
            pass
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        if body is not f or pre_gz:
            handler.send_header("Vary", "Accept-Encoding")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    handler.send_response(200)
    handler.send_header("Content-Type",
                        M3D_MIME.get(suf, "application/octet-stream"))
    handler.send_header("Content-Length", str(body.stat().st_size))
    handler.send_header("ETag", etag)
    if "?v=" in (handler.path or ""):
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    elif no_cache:
        handler.send_header("Cache-Control", "no-cache")
    else:
        handler.send_header("Cache-Control", "public, max-age=%d" % cache_s)
    if body is not f or pre_gz:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command == "HEAD":
        return True
    try:
        with open(body, "rb") as fh:
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (ConnectionError, BrokenPipeError, OSError):
        handler.close_connection = True
    return True


# ------------------------------------------------- Korr 3D 的地形接口
_K3D_CACHE = {"mtime": None, "maps": {}, "by_name": {}}


def _korr3d_maps():
    """读 web/korr/maps3d/manifest.json（带 mtime 缓存），建 名字→条目 索引。"""
    p = KORR3D_DIR / "manifest.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}, {}
    if _K3D_CACHE["mtime"] != mt:
        try:
            maps = (json.loads(p.read_text(encoding="utf-8")) or {}).get("maps") or {}
        except ValueError:
            maps = {}
        by = {}
        for key, ent in maps.items():
            for alias in (key, ent.get("frontName"), ent.get("name")):
                if alias:
                    by[str(alias)] = ent
                    by[str(alias).lower()] = ent
        _K3D_CACHE.update(mtime=mt, maps=maps, by_name=by)
    return _K3D_CACHE["maps"], _K3D_CACHE["by_name"]


def korr3d_map(handler, path):
    """Korr 3D 视图的两个取图接口（它自己的服务端也是这两条）。

        GET /api/map/<名字>/meta?token=…  → {fileTag,sizeBytes,tris,bboxMin/Max,downloadUrl}
        GET /api/map/<名字>?token=…       → 三角形汤（无索引 float32，36 字节/三角形）

    我们不做它的地图授权（本地部署），token 收下不用；没有对应地形就 404，
    前端会显示"地图未找到"并回落到 2D。返回 True 表示这条请求已处理。
    """
    rest = unquote(path[len("/api/map/"):])
    is_meta = rest.endswith("/meta")
    if is_meta:
        rest = rest[:-len("/meta")]
    rest = rest.strip("/")
    if not rest:
        return False
    maps, by_name = _korr3d_maps()
    ent = by_name.get(rest) or by_name.get(rest.lower())
    if not ent:
        return False
    blob = KORR3D_DIR / ent["file"]
    if not blob.is_file():
        return False
    if is_meta:
        body = json.dumps({
            "canonicalName": ent.get("frontName") or ent.get("name"),
            "cacheKey": ent["key"],
            "fileTag": ent.get("fileTag") or "",
            "format": "f32le-triangles-v1",
            "sizeBytes": int(ent.get("sizeBytes") or blob.stat().st_size),
            "tris": int(ent.get("tris") or 0),
            "bboxMin": ent.get("bboxMin") or [0, 0, 0],
            "bboxMax": ent.get("bboxMax") or [0, 0, 0],
            "downloadUrl": "/api/map/%s" % quote(ent.get("frontName") or ent["key"]),
            "urlExpiresIn": 86400,
        }, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    # 三角形汤：磁盘上存的是 .f32.gz，直接按 gzip 编码发（浏览器 fetch 自动解压，
    # 前端拿到的字节数就是 meta.sizeBytes，进度条也照常走）
    size = blob.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(size))
    handler.send_header("X-File-Tag", ent.get("fileTag") or "")
    handler.send_header("X-Tris", str(int(ent.get("tris") or 0)))
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            with open(blob, "rb") as fh:
                shutil.copyfileobj(fh, handler.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
    return True


# ---------------------------------------------------------------- detection
def resolve_s2c_table(flow_dir):
    """S2C 加密表自解析（s2c_auto，与 live 路径 auto_s2c 同源）。

    0905 起对局下行带 512B 循环表混淆（P[i]=C[i]^C[i-1]^K[i%512]，每条 UDP
    记录独立重置，见 s2c_dec.py），裸读位流是乱码；bare 流（旧对局）返回
    None 原样直读。resolve_flow 自带连接级缓存/盘上状态，重复调用廉价。
    """
    from s2c_auto import resolve_flow
    try:
        dec = resolve_flow(Path(flow_dir))
    except Exception:
        return None
    return dec.table if (dec.ready and dec.table) else None


# ---- 流发现扫描的缓存 ------------------------------------------------------
# 4Hz 的扫描会把 captures 走一遍：每个 flow 读 meta.json、再 stat 两次。实测
# 76 run / 879 flow 时一轮 ~250ms —— 正好吃掉整个扫描节拍，把 60Hz 增量解析挤成
# ~4Hz。三层缓存都按"目录 mtime 变化即失效"实现，语义不变：
#   _META_CACHE    (meta.json 路径, mtime) -> 解析结果
#   _UDP_DIRS      flows/ 目录 mtime -> 该 run 里 proto=udp 的 flow 目录列表
#   _RUN_FRESHEST  flows/ 目录 mtime -> 上一轮算出的"最新 flow 写入时刻"（用于整轮跳过）
_META_CACHE = {}
_UDP_DIRS = {}
_RUN_FRESHEST = {}

LIVE_STALE_SEC = 180.0     # 只认最近还活跃的战斗流（秒）


class Detector:
    """确定性战斗流索引: run -> {flow: n_ps(arch131 通道数)}。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.table = {}
        try:
            if DETECT_CACHE.exists():
                self.table = json.loads(asset_crypto.decrypt_blob(
                    DETECT_CACHE.read_bytes(), asset_crypto.PURPOSE_RUNTIME))
        except Exception:
            self.table = {}

    def save(self):
        with self.lock:
            DETECT_CACHE.write_bytes(asset_crypto.encrypt(
                json.dumps(self.table, ensure_ascii=False).encode("utf-8"),
                asset_crypto.PURPOSE_RUNTIME))

    def known(self, run_name, flow_name):
        with self.lock:
            return self.table.get(run_name, {}).get(flow_name)

    def record(self, run_name, flow_name, n_ps):
        with self.lock:
            self.table.setdefault(run_name, {})[flow_name] = int(n_ps)

    @staticmethod
    def probe_flow(flow_dir):
        """战斗流判定（引擎级，与游戏客户端一致）+ PlayerState 通道数排序值。

        引擎门：UE4 客户端只有在控制通道(ch0)收到 NMT_WELCOME（含 "/Game/"
        地图串）后才把该 UDP 连接当作对局会话并开始复制 Actor。故
            非战斗流（含被拒连接 Failure、半握手 HC）一律返回 0；
            战斗流返回 PlayerState 通道数（路径解析类 GUID，NetGUID 按连接
            分配不能硬编码 131）。
        """
        try:
            hs = hs_mod.scan_control(flow_dir)
            if not hs.ok:
                return 0
            b = load(Path(flow_dir), s2c_table=resolve_s2c_table(flow_dir))
        except Exception:
            return -1
        # sweep 1: 收集全部导出路径 -> 类 GUID 集合
        ps_archs = set()
        for ch in b.values():
            for x in ch:
                if not x.open or not x.hx:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad:
                    continue
                for e in info.get("exports", []):
                    p = e.get("path")
                    if not p:
                        continue
                    tail = p.rsplit(".", 1)[-1]
                    if tail.startswith("Default__"):
                        tail = tail[9:]
                    if tail in ("BP_DFMPlayerState_C", "BP_SOLPlayerState_C"):
                        ps_archs.add(e["guid"])
        if not ps_archs:
            ps_archs = {131}  # 兼容旧数据
        # sweep 2: 每通道首个 open bunch，arch ∈ ps_archs 计数
        n = 0
        for ch in b.values():
            for x in ch:
                if not x.open:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad:
                    continue
                if info.get("arch") in ps_archs:
                    n += 1
                break
        return n

    @staticmethod
    def flows_by_mtime(run_dir, limit=None):
        fds = []
        fdir = Path(run_dir) / "flows"
        if not fdir.is_dir():
            return fds
        for fd in fdir.iterdir():
            fj = fd / "frames.jsonl"
            if fd.is_dir() and fj.exists():
                try:
                    fds.append((fj.stat().st_mtime, fd))
                except OSError:
                    pass
        fds.sort(key=lambda t: -t[0])
        return [fd for _mt, fd in (fds if limit is None else fds[:limit])]

    @staticmethod
    def _meta(flow_dir):
        """读 meta.json；按 (路径, mtime) 缓存 —— 调用方只读，可安全共享。"""
        p = Path(flow_dir) / "meta.json"
        try:
            stamp = p.stat().st_mtime_ns
        except OSError:
            return {}
        key = (str(flow_dir), stamp)
        hit = _META_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            val = {}
        if len(_META_CACHE) > 4096:
            _META_CACHE.clear()
        _META_CACHE[key] = val
        return val

    @staticmethod
    def udp_flows(run_dir, fresh_within=LIVE_STALE_SEC):
        """meta.json 标记 proto=udp 的 flow（UE 复制连接是 UDP；TCP 流按构造
        不可能是战斗连接）。返回 (size, frames_mtime, dir) 列表，排序交给调用方。

        三层优化见文件顶部注释。**整轮 stale 判据必须用 flow 的 frames.jsonl
        mtime，不能用 run 目录 mtime** —— 实测 run mtime 比它下面最新 flow 的
        mtime 早 20 分钟（写 frames 不会更新 run 目录），拿它当判据会误杀正在进行的对局。"""
        fdir = Path(run_dir) / "flows"
        if not fdir.is_dir():
            return []
        try:
            stamp = fdir.stat().st_mtime_ns
        except OSError:
            return []
        key = str(fdir)
        now = time.time()

        # 1) flows/ 目录没变过（没有新增/删除 flow）且上轮最新写入已过期 -> 整轮跳过
        fresh = _RUN_FRESHEST.get(key)
        if fresh is not None and fresh[0] == stamp and fresh_within:
            if now - fresh[1] > fresh_within:
                return []

        # 2) UDP 成员列表按 flows/ mtime 缓存（新增 flow 会改 flows/ mtime，天然失效）
        hit = _UDP_DIRS.get(key)
        if hit is not None and hit[0] == stamp:
            dirs = hit[1]
        else:
            dirs = [fd for fd in fdir.iterdir()
                    if fd.is_dir() and Detector._meta(fd).get("proto") == "udp"]
            if len(_UDP_DIRS) > 256:
                _UDP_DIRS.clear()
            _UDP_DIRS[key] = (stamp, dirs)

        # 3) 新鲜度必须每次重算（不能缓存：正在写的流 mtime 一直在变）
        out = []
        for fd in dirs:
            try:
                sz = (fd / "response.bin").stat().st_size
            except OSError:
                sz = 0
            try:
                mt = (fd / "frames.jsonl").stat().st_mtime
            except OSError:
                mt = 0
            out.append((sz, mt, fd))
        if len(_RUN_FRESHEST) > 256:
            _RUN_FRESHEST.clear()
        _RUN_FRESHEST[key] = (stamp, max((t[1] for t in out), default=0))
        return out

    @classmethod
    def flows_by_size(cls, run_dir, limit=None):
        """探测调度顺序：UDP 流按 response.bin 大->小（战斗流=数 MB 级长流）。
        仅影响探测先后，分类标准仍是 arch131 通道数最大。"""
        items = sorted(cls.udp_flows(run_dir), key=lambda t: -t[0])
        fds = [fd for _sz, _mt, fd in items]
        return fds if limit is None else fds[:limit]

    def battle_flow_of(self, run_name):
        with self.lock:
            tab = self.table.get(run_name) or {}
        best, best_n = None, 0
        for name, n in tab.items():
            if n > best_n:
                best, best_n = name, n
        return best, best_n


# ---------------------------------------------------------------- parsing
def extract_slots_full(b, mra_ch, seq_ts):
    """slot -> [(seq, ts, x, y, z, yaw)]（确定性逐帧，越界点剔除）。"""
    slots = {}
    for x in b.get(mra_ch, []):
        try:
            blk = tile(x.payload, x.pbits, 0)
        except Exception:
            continue
        if not blk:
            continue
        ts = seq_ts(x.seq)
        for k in blk:
            if not k["has_rep"] or k["deleted"] or not k["is_actor"]:
                continue
            r0 = BitReader(x.payload, k["payload_end"]); r0.position = k["payload_start"]
            try:
                if r0.bit():
                    continue
                h, _ = read_dfm_packed_int(r0)
                if h != 21:
                    continue
                fr, _r = rm4.read_frame4(x.payload, r0.position, k["payload_end"])
            except Exception:
                continue
            for i, it in fr["items"].items():
                p = it.get("pos")
                if not p or not WORLD(p):
                    continue
                rot = it.get("rot")
                yaw = rot[1] if rot else None
                slots.setdefault(i, []).append((x.seq, ts, float(p[0]), float(p[1]), float(p[2]), yaw))
    for tr in slots.values():
        tr.sort(key=lambda t: t[0])
    return slots


class BattleView:
    """一个战斗流的完整确定性视图（文件增长时全量重建）。"""

    def __init__(self, run_dir, flow_dir):
        self.run_dir = Path(run_dir); self.flow_dir = Path(flow_dir)
        self.lock = threading.Lock()
        self.frames_bytes = -1
        self.build()

    @staticmethod
    def _load_seq_ts(flow_dir):
        seqs, tss = [], []
        try:
            with (flow_dir / "frames.jsonl").open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("dir") != "response":
                        continue
                    seqs.append(int(rec["seq"])); tss.append(float(rec["ts"]))
        except OSError:
            pass
        order = np.argsort(seqs)
        return np.asarray(seqs)[order], np.asarray(tss)[order]

    def seq_ts(self, seq):
        s, t = self._seq_ts_map
        i = int(np.searchsorted(s, seq))
        if i <= 0: return float(t[0])
        if i >= len(s): return float(t[-1])
        s0, s1 = int(s[i - 1]), int(s[i])
        if s1 <= s0: return float(t[i])
        f = (seq - s0) / float(s1 - s0)
        return float(t[i - 1] + f * (t[i] - t[i - 1]))

    def frames_size(self):
        try:
            return (self.flow_dir / "frames.jsonl").stat().st_size
        except OSError:
            return -1

    def build(self):
        t0 = time.time()
        self._seq_ts_map = self._load_seq_ts(self.flow_dir)
        # 0905 加密对局：回放侧与 live 侧同走 s2c_auto 自解析（bare 流表=None）
        b = load_cached(self.flow_dir, s2c_table=resolve_s2c_table(self.flow_dir))
        archs = nl.resolve_class_archs(b)
        # 露天物资类 GUID：不依赖 resolve_class_archs 是否带 pk 组（服务器旧版
        # 没有），本地按导出路径补解析（Default__ CDO 前缀同款规则）
        pk_archs = set()
        for g, p in nl.build_guid_paths(b).items():
            t = p.rsplit(".", 1)[-1]
            if t.startswith("Default__"):
                t = t[9:]
            if t.startswith("BP_InventoryPickup"):
                pk_archs.add(g)
        from pickup_state import PickupTracker
        from liveview import LOOT_NAMES, LOOT_META
        self.pickup_tracker = PickupTracker(pk_archs, LOOT_NAMES, LOOT_META, keep_history=True)
        self.loot = self.pickup_tracker.records
        players = nl.extract_players(b, ps_archs=archs["ps"])
        # 只有人类角色参与 slot 认领：MRA 是队伍位置广播，世界 AI 不在其中，
        # 其出生点会以 min-dist 抢占队伍槽位。
        links = nl.extract_char_links(b, char_archs=archs["char"])
        from property_health import HealthBatchTracker
        from weapon_state import WeaponTracker
        self.weapon_tracker = WeaponTracker(keep_history=True)
        health_tracker = HealthBatchTracker(keep_history=True)
        self.box_tracker = DeathBoxTracker(keep_history=True)
        self.box_contents = BoxContentsTracker(keep_history=True)
        ps_tracker = PlayerStateTracker()
        for bunch in sorted((x for xs in b.values() for x in xs), key=lambda x: x.seq):
            health_tracker.consume(bunch)
            closing_box = self.box_tracker.channels.get(bunch.ch)
            self.box_tracker.consume(bunch)
            self.box_contents.consume(bunch)
            if not bunch.open and bunch.close and bunch.close_reason in (0, 2, 4) and closing_box is not None:
                self.box_contents.forget(closing_box, bunch.seq)
            ps_tracker.consume(bunch)
            self.weapon_tracker.consume(bunch)
            self.pickup_tracker.consume(bunch)
        self.health_history = health_tracker.resolved_history()
        self.life_tracker = DeathStateTracker(keep_history=True)
        for actor, link in links.items():
            if link.get("spawn_seq") is not None:
                self.life_tracker.spawn(actor, link["spawn_seq"])
        life_events = [(seq, "health", actor, value) for actor, fields in self.health_history.items()
                       for seq, value in fields.get("IsAlive", [])]
        for seq, ps, fields in ps_tracker.events:
            candidates = [(link.get("spawn_seq", 0), actor) for actor, link in links.items()
                          if link.get("ps_guid") == ps and link.get("spawn_seq", 0) <= seq]
            if candidates:
                life_events.append((seq, "ps", max(candidates)[1], fields))
        for seq, source, actor, value in sorted(life_events, key=lambda x: x[0]):
            if source == "health":
                self.life_tracker.observe(actor, value, seq)
            else:
                self.life_tracker.observe_playerstate(actor, value, {k: seq for k in value}, verified=True)
        self.ps_decode_stats = dict(ps_tracker.stats)
        # ps_tracker 解出的 PS 可能不在 extract_players 的结果里（名字不在开局
        # bunch 的 PS —— 实测 115718 局 PS3600/5868 认领了槽却无名）。旧代码
        # `if ps in players` 直接把这类记录丢掉 -> 回放里真人顶着无名/「玩家」。
        # 这里补建条目并映射全部身份字段（与 liveview._merge_playerstate 同口径）。
        for ps, fields in ps_tracker.records.items():
            p = players.get(ps)
            if p is None:
                if not fields.get("PlayerNamePrivate"):
                    continue
                p = players[ps] = {
                    "name": None, "player_id": None, "bot": None,
                    "actor_guid": ps, "owner_guid": None, "channel": None,
                    "uin": None, "team": None, "camp": None, "inner_team": None,
                    "hero_id": None, "expert_id": None, "spawn_seq": None,
                }
            if fields.get("PlayerNamePrivate"):
                p["name"] = fields["PlayerNamePrivate"]
            # 通道号（自机判定用）：ps_tracker.channels 是 channel -> actor，反查。
            # 缺了也不影响（自机选择逐条跳过 channel=None），但补上更准）。
            if p.get("channel") is None:
                for _ch, _a in ps_tracker.channels.items():
                    if _a == ps:
                        p["channel"] = _ch
                        break
            for key, dest in (("TeamID", "team"), ("Camp", "camp"), ("CampID", "camp"),
                              ("PlayerId", "player_id"),
                              ("Uin", "uin"), ("InnerTeamIndex", "inner_team"),
                              ("HeroId", "hero_id"), ("ExpertId", "expert_id")):
                if key in fields:
                    p[dest] = fields[key]
            _ow = fields.get("Owner")
            if isinstance(_ow, dict):
                _ow = _ow.get("guid")
            if _ow is not None:
                p["owner_guid"] = _ow
        self.links = links
        # 角色消失（close_reason ∈ {0,2,3,4}）：死亡/离开更新距离/销毁
        gone = st.extract_gone_actors(b, archs["char"] | archs["ai"])
        mra_ch, mra_n = nl.find_mra_channel(b)
        slots = extract_slots_full(b, mra_ch, self.seq_ts) if mra_ch is not None else {}
        # 拾取推断（wire 沉默补丁）：close r0 只覆盖秒捡，玩家停驻在物资旁的
        # 拾取姿态补判 r1 后的沉默拾取（回放按 picked_seq 时间旅行过滤）
        if slots:
            mark_picked_by_standing(self.loot, slots)
        # 确定性 join: 角色出生点 <-> slot@spawn_seq (<500cm)，最小距离唯一认领
        explicit = nl.explicit_slot_claims(links)
        reserved = nl.observed_move_slots(links)
        claim = {slot: (actor, 0.0) for slot, actor in explicit.items()}
        for actor, lk in links.items():
            if "move_handle" in lk:
                continue
            loc, S = lk.get("loc"), lk.get("spawn_seq")
            if not loc or S is None or not slots:
                continue
            loc = np.array(loc, dtype=float)
            best = None; bd = 1e18
            for slot, tr in slots.items():
                if slot in reserved:
                    continue
                if not tr:
                    continue
                seqs = [t[0] for t in tr]
                # 时间上最近的采样（左右两侧都看，与 er_live 验证过的语义一致）
                j = bisect.bisect_left(seqs, S)
                for jj in (j - 1, j):
                    if 0 <= jj < len(tr):
                        d = float(np.linalg.norm(loc - np.array(tr[jj][2:5])))
                        if d < bd:
                            bd, best = d, slot
            if best is not None and bd < 500.0:
                prev = claim.get(best)
                if prev is None or bd < prev[1]:
                    claim[best] = (actor, bd)
        # 成员槽判定（结构性、确定性）：真实成员轨迹有数百~数千采样，
        # 瞬态幽灵条目（如 slot0 仅 2 点）不构成成员。
        MEMBER_MIN_POINTS = 16
        members = {s for s, tr in slots.items() if len(tr) >= MEMBER_MIN_POINTS}
        # self 三层判定（与 LiveView 同构，1643 局实证权威 > 闭世界）：
        # ① 闭世界：唯一未认领"成员"槽 = 我，仅当其余成员槽全部被"命名"角色
        #    认领（如 15 = 14 远程 + 1 self）。部分对局（中途进入等）不满足。
        # ② field13 小队广播"我"的最新全量帧位置对齐最近成员槽（300cm² 门限，
        #    payload_hit 预过滤——只解含 SELF_WIRE 的全量帧，增量帧无自机标记）。
        # ③ 权威覆盖：本地玩家 PS = 真人里通道号最低者（UE4 握手先复制本连接
        #    自己的 PlayerState，495/733/988 三局一致；pid 最小规则曾标错两局），
        #    反查其 Character 认领的槽直接钉死。
        unclaimed = [s for s in members if s not in claim]
        self_slot = None
        self_rule = None
        named_actors = set()
        for actor, lk in links.items():
            for g, p in players.items():
                if (lk.get("ps_guid") is not None and g == lk["ps_guid"]) or \
                   (lk.get("owner") is not None and p.get("owner_guid") == lk["owner"]):
                    named_actors.add(actor); break
        claimed_actors = {a for a, _d in claim.values()}
        if len(unclaimed) == 1 and claimed_actors and claimed_actors <= named_actors \
                and len(claim) == len(members) - 1:
            self_slot = unclaimed[0]; self_rule = "closed"
        actor2slot = {a: s for s, (a, _d) in claim.items()}
        # 匿名成员槽补名（与 liveview._derive 第三梯队同口径）：PlayerId 与 MRA 槽号
        # 同出一套分配序，但只是**局部**同序——115718 局 10/10 恒为 242（据此补出
        # slot23 -> 祺捣），215035 局会漂移（248/246/245）。只在高一致时才采信，
        # 有漂移就整条放弃（宁留「未知玩家」也不补错名）。
        self.anon_names = {}
        _pidoff = {}
        _pidoff_tot = 0
        for _s2, (_a, _d3) in claim.items():
            _pg2 = (links.get(_a) or {}).get("ps_guid")
            _p2 = players.get(_pg2) if _pg2 is not None else None
            _pid2 = _p2.get("player_id") if _p2 is not None else None
            if _pid2 is None or _p2.get("bot") is not None or _pid2 >= 8000:
                continue
            _pidoff[_pid2 - _s2] = _pidoff.get(_pid2 - _s2, 0) + 1
            _pidoff_tot += 1
        if _pidoff:
            _off_v, _off_n = max(_pidoff.items(), key=lambda kv: kv[1])
            _off_2nd = max((v for k, v in _pidoff.items() if k != _off_v), default=0)
            if (_off_n >= 4 and _off_n >= _pidoff_tot * 0.8
                    and _off_n > _off_2nd * 3):
                _pid_used = set()
                for _s2, (_a, _d3) in claim.items():
                    _pg2 = (links.get(_a) or {}).get("ps_guid")
                    _p2 = players.get(_pg2) if _pg2 is not None else None
                    if _p2 is not None and _p2.get("player_id") is not None:
                        _pid_used.add(_p2["player_id"])
                _pid_free = {}
                for _g3, _p3 in players.items():
                    _pid3 = _p3.get("player_id")
                    if (_pid3 is None or _pid3 in _pid_used or not _p3.get("name")
                            or _p3.get("bot") is not None):
                        continue
                    _pid_free[_pid3] = _g3
                for _s2 in members:
                    if _s2 in claim or _s2 == self_slot:
                        continue
                    _g3 = _pid_free.get(_s2 + _off_v)
                    if _g3 is not None:
                        self.anon_names[_s2] = players[_g3]
        f13_slot = None
        if self_slot is None:
            fseq, fxy = -1, None
            for ch in sorted(b):
                for x in b[ch]:
                    if x.pbits < 64 or not f13.payload_hit(x.payload):
                        continue
                    try:
                        dec = f13.decode(x.payload, x.pbits)
                    except Exception:
                        continue
                    if not dec or not dec["vals64"] or not dec["locs"]:
                        continue
                    if any(v in f13.SELF_WIRE for v in dec["vals64"]) and x.seq >= fseq:
                        si = min(f13.self_index(dec["vals64"]), len(dec["locs"]) - 1)
                        _loc = dec["locs"][si]
                        fseq, fxy = x.seq, (float(_loc[0]), float(_loc[1]))
            if fxy is not None:
                best = None; bd = 1e18
                for slot, tr in slots.items():
                    if len(tr) < MEMBER_MIN_POINTS:
                        continue
                    seqs = [t[0] for t in tr]
                    j = bisect.bisect_left(seqs, fseq)
                    for jj in (j - 1, j):
                        if 0 <= jj < len(tr):
                            t = tr[jj]
                            d2 = (fxy[0] - t[2]) ** 2 + (fxy[1] - t[3]) ** 2
                            if d2 < bd:
                                bd, best = d2, slot
                if best is not None and bd < 90000.0:
                    self_slot = best; self_rule = "f13"
                    f13_slot = best
        _best_ch = None
        self_ps = None
        self_name = None
        for _g, p in players.items():
            if p.get("bot") is not None or not p.get("name") or p.get("channel") is None:
                continue
            if _best_ch is None or p["channel"] < _best_ch:
                _best_ch, self_ps, self_name = p["channel"], _g, p["name"]
        if self_name is None:   # 回退：pid 最小真人（旧规则）
            for _g, p in sorted(players.items(), key=lambda kv: kv[1].get("player_id") or 1 << 30):
                if p.get("bot") is None and p.get("name"):
                    self_name = p["name"]; break
        if self_ps is not None:
            for _a, _lk in links.items():
                if _lk.get("ps_guid") == self_ps and _a in actor2slot:
                    self_slot = actor2slot[_a]
                    self_rule = "ps(f13=%s)" % f13_slot
                    break
        ents = []
        for actor, lk in links.items():
            name = pid = bot = None
            team = camp = hid = ps_hid = None
            player_state = None
            for g, p in players.items():
                if lk.get("ps_guid") is not None and g == lk["ps_guid"]:
                    name, pid, bot = p["name"], p.get("player_id"), p.get("bot")
                    team, camp = p.get("team"), p.get("camp")
                    player_state = p
                    ps_hid = p.get("hero_id"); break
            if name is None and lk.get("owner") is not None:
                for g, p in players.items():
                    if p.get("owner_guid") == lk["owner"]:
                        name, pid, bot = p["name"], p.get("player_id"), p.get("bot")
                        team, camp = p.get("team"), p.get("camp")
                        player_state = p
                        ps_hid = p.get("hero_id"); break
            # hero：0904 起走角色 fashion 组件（extract_char_links 写入
            # links[a]["hero"]），PS h137 兜底旧流量
            hid = lk.get("hero") or ps_hid
            # self 角色标记：权威 ps_guid 反查优先；无真人 PS 时退回旧
            # 「无 loc 且 28 导出」规则（同一角色的两种识别，互为兜底）
            is_self = (self_ps is not None and lk.get("ps_guid") == self_ps) or \
                (self_ps is None and lk.get("loc") is None and lk.get("num_exports") == 28)
            hero_name = HERO_NAMES.get(hid) if hid else None
            is_bot = confirmed_player_bot(player_state, hero_name is not None)
            if is_self:
                kind = "self"
            elif is_bot:
                kind = "ai"   # 队伍 bot（有名字）
            else:
                kind = "player"
            disp = ("%s(self)" % self_name) if is_self else name
            if disp is None and actor2slot.get(actor) is None and lk.get("loc") is None:
                continue  # 无名且无槽位的角色不显示
            g = gone.get(actor)
            # self 永远在场（服务器对本地玩家的 close 是相关性 cull）
            if is_self:
                g = None
            ents.append({
                "actor": actor,
                "name": disp,
                "kind": kind,
                "slot": None if is_self else actor2slot.get(actor),
                "spawn_loc": lk.get("loc"), "spawn_seq": lk.get("spawn_seq"),
                "player_id": pid,
                "team": team, "camp": camp,
                # 与 LiveView ents 同构：前端气泡读 is_bot（人机后缀）与 hero（干员名）
                "is_bot": is_bot,
                "hero": hero_name,
                "hid": hid,
                # 消失（close_reason ∈ {0,2,3,4}）：close_seq 之后回放游标不再显示
                "close_seq": g[0] if g else None,
                "close_reason": g[1] if g else None,
            })
        with self.lock:
            self.players = players
            self.ents = ents
            self.slots = slots
            self.member_slots = members
            self.claim = dict(actor2slot)
            self.self_slot = self_slot
            self.mra_ch = mra_ch; self.mra_n = mra_n
            self.parse_s = round(time.time() - t0, 2)
            self.frames_bytes = self.frames_size()
        print("[view] %s/%s: %d ents, %d named, %d slotted, self_slot=%s rule=%s (%.1fs)" % (
            self.run_dir.name, self.flow_dir.name, len(ents),
            len([e for e in ents if e["name"]]),
            len([e for e in ents if e["slot"] is not None]),
            self_slot, self_rule, time.time() - t0), flush=True)

    def refresh_if_grown(self):
        if self.frames_size() != self.frames_bytes:
            print("[view] flow grown -> rebuild", flush=True)
            self.build()
            return True
        return False


# ---------------------------------------------------------------- playback
class Playback:
    def __init__(self, view):
        self.view = view
        self.speed = 16.0
        self.cursor_seq = None
        self.last_wall = time.time()
        self._sps = None

    def bounds(self):
        v = self.view
        lo = hi = None
        with v.lock:
            for tr in v.slots.values():
                if tr:
                    a, b_ = tr[0][0], tr[-1][0]
                    lo = a if lo is None else min(lo, a)
                    hi = b_ if hi is None else max(hi, b_)
        return lo, hi

    def seq_per_sec(self):
        if self._sps is None:
            s, t = self.view._seq_ts_map
            if len(s) >= 2 and float(t[-1] - t[0]) > 1.0:
                self._sps = float(s[-1] - s[0]) / float(t[-1] - t[0])
            else:
                self._sps = 56.0
        return self._sps

    def advance(self):
        lo, hi = self.bounds()
        if lo is None:
            return
        now = time.time()
        if self.cursor_seq is None or self.cursor_seq < lo or self.cursor_seq >= hi:
            self.cursor_seq = lo
            self.last_wall = now
            return
        if self.speed > 0:
            self.cursor_seq += (now - self.last_wall) * self.speed * self.seq_per_sec()
            if self.cursor_seq > hi:
                self.cursor_seq = hi
        self.last_wall = now

    def snapshot(self):
        self.advance()
        from property_health import health_at_seq, operator_health
        v = self.view
        cur = int(self.cursor_seq or 0)
        out_ents = []
        self_xyz = None; self_yaw = 0.0; self_name = None
        names = []
        lo, hi = self.bounds()

        def _slot_pos(slot, tr):
            """cursor 处的槽位采样（含轨迹尾）；出生前返回 (None,None,None)。"""
            if slot is None or not tr:
                return None, None, None
            seqs = [t[0] for t in tr]
            j = bisect.bisect_right(seqs, cur) - 1
            if j < 0:
                return None, None, None
            rec = tr[j]
            jj = min(bisect.bisect_right(seqs, cur) + 1, len(tr))
            trail = [[p[2], p[3], p[4]] for p in tr[max(0, jj - 160):jj:4]]
            return [rec[2], rec[3], rec[4]], rec[5], trail

        # 先解析 self（所有 rel 都依赖它）
        with v.lock:
            explicit = nl.explicit_slot_claims(v.links, at_seq=cur)
            current_slots = {actor: slot for slot, actor in explicit.items()}
            def actor_slot(e):
                if "move_handle" in v.links.get(e["actor"], {}):
                    return current_slots.get(e["actor"])
                return v.self_slot if e["kind"] == "self" else e["slot"]

            _self_ent = next((e for e in v.ents if e["kind"] == "self"), None)
            if _self_ent is not None:
                self_slot = actor_slot(_self_ent)
                self_xyz, self_yaw0, _trail0 = _slot_pos(
                    self_slot, v.slots.get(self_slot))
                self_yaw = self_yaw0 or 0.0
                self_name = _self_ent["name"]
            for e in v.ents:
                slot = actor_slot(e)
                if e["kind"] == "self":
                    continue
                if e.get("spawn_seq", 0) > cur:
                    continue
                life = v.life_tracker.at(e["actor"], cur)
                if life["dead"]:
                    continue
                link = v.links.get(e["actor"], {})
                assignments = link.get("move_handle_history", [])
                latest = next((p for p in reversed(assignments) if p[0] <= cur), None)
                if latest is not None and latest[1] >= 0 and e["actor"] not in current_slots:
                    continue
                # 角色消失（close_seq 之后）：回放游标越过 close_seq 即不再显示
                cs = e.get("close_seq")
                if cs is not None and cur >= cs and not \
                        (e.get("close_reason") == 3 and explicit.get(slot) == e["actor"]):
                    continue
                pos, yaw, trail = _slot_pos(slot, v.slots.get(slot))
                if pos is None and slot is None and e.get("spawn_loc"):
                    # 有名字但无槽位轨迹（部分对局）：显示出生点
                    sp = e["spawn_loc"]
                    pos = [float(sp[0]), float(sp[1]), float(sp[2])]
                    out_ents.append({
                        "key": "spawn-%s" % e["actor"], "name": e["name"], "kind": e["kind"],
                        "xyz": pos, "world": pos,
                        "rel": [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None,
                        "yaw": None, "slot": None, "ch": e["actor"], "eid": e["actor"], "trail": None,
                        "team": e.get("team"), "camp": e.get("camp"),
                        "is_bot": e.get("is_bot"), "hero": e.get("hero"), "hid": e.get("hid"), "hid": e.get("hid"),
                        "hp": health_at_seq(v.health_history, e["actor"], cur),
                    **weapon_fields(v.weapon_tracker, e["actor"], WEAPON_NAMES, cur),
                    })
                    continue
                if pos is None:
                    continue
                rel = [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None
                out_ents.append({
                    "key": "slot-%s" % slot, "name": e["name"], "kind": e["kind"],
                    "xyz": pos, "world": pos, "rel": rel, "yaw": yaw,
                    "slot": slot, "ch": slot, "eid": e["actor"], "trail": trail,
                    "dead": False,
                    "team": e.get("team"), "camp": e.get("camp"),
                    "is_bot": e.get("is_bot"), "hero": e.get("hero"), "hid": e.get("hid"),
                    "hp": health_at_seq(v.health_history, e["actor"], cur),
                    **weapon_fields(v.weapon_tracker, e["actor"], WEAPON_NAMES, cur),
                    "binding_source": "move_handle" if explicit.get(slot) == e["actor"] else "spawn_proximity",
                })
                if e["name"]:
                    names.append({"ch": slot, "name": e["name"], "kind": e["kind"]})
            # 未绑定角色的成员槽 -> 匿名实体（不猜身份）
            bound = {actor_slot(e) for e in v.ents if actor_slot(e) is not None}
            for s, tr in v.slots.items():
                if s in bound or s == v.self_slot or s not in v.member_slots:
                    continue
                seqs2 = [t[0] for t in tr]
                j = bisect.bisect_right(seqs2, cur) - 1
                if j < 0:
                    continue
                rec = tr[j]
                # 匿名槽补名：BattleView.build 已按「PlayerId ↔ 槽号同序」补齐
                # （见 build 里 anon_names / _pidoff 与 liveview._derive 第三梯队）。
                anon_p = (getattr(v, "anon_names", None) or {}).get(s)
                out_ents.append({
                    "key": "anon-%s" % s,
                    "name": anon_p.get("name") if anon_p else None,
                    "kind": "player" if anon_p else "unknown",
                    "xyz": [rec[2], rec[3], rec[4]],
                    "world": [rec[2], rec[3], rec[4]], "rel": None, "yaw": rec[5],
                    "slot": s, "ch": s, "eid": 0, "trail": None,
                    "dead": False,
                    "team": anon_p.get("team") if anon_p else None,
                    "camp": anon_p.get("camp") if anon_p else None,
                    "is_bot": confirmed_player_bot(
                        anon_p,
                        HERO_NAMES.get((anon_p or {}).get("hero_id")) is not None),
                })
                if anon_p and anon_p.get("name"):
                    names.append({"ch": s, "name": anon_p["name"], "kind": "player"})
            for entity in out_ents:
                if entity.get("eid"):
                    life = v.life_tracker.at(entity["eid"], cur)
                    entity.update(life_state=life["state"], life_status=life["status"],
                                  life_seq=life["seq"], life_source=life["source"])
                    if life["downed"]:
                        entity.update(status="倒地", status_key="down")
            character_positions = {}
            for e in v.ents:
                slot = actor_slot(e)
                pos, _, _ = _slot_pos(slot, v.slots.get(slot))
                if pos is not None:
                    character_positions[e["actor"]] = pos
            names_by_actor = {e["actor"]: e.get("name") for e in v.ents}
            for actor in v.box_tracker.records:
                record = v.box_tracker.at(actor, cur)
                if not record or record["kind"] != "box" or record.get("gone_seq") is not None or record.get("hidden"):
                    continue
                pos = v.box_tracker.world_position(actor, character_positions, cur)
                if pos is None or not WORLD(pos):
                    continue
                owner = record.get("deceased_actor") or record.get("owner_actor")
                seen = set()
                while owner in v.box_tracker.records and owner not in seen:
                    seen.add(owner)
                    carrier = v.box_tracker.at(owner, cur)
                    if not carrier:
                        break
                    owner = carrier.get("deceased_actor") or carrier.get("owner_actor")
                name = names_by_actor.get(owner)
                if not name:
                    name = record.get("player_name")
                    if name and ("LOCTABLE" in name or "/Game/" in name):
                        name = None
                entity = box_entity(actor, record, pos, owner, name, self_xyz)
                entity.update(v.box_contents.describe(actor, LOOT_NAMES, cur))
                if owner is not None:
                    owner_entity = next((e for e in v.ents if e['actor'] == owner), {})
                    if record.get('deceased_actor'):
                        entity['is_bot'] = bool(owner_entity.get('is_bot'))
                        if entity['is_ai'] is None and owner_entity.get('kind') == 'ai':
                            entity['is_ai'] = True
                    owner_life = v.life_tracker.at(owner, cur)
                    entity['owner_dead'] = owner_life['dead'] if owner_life['confirmed'] else None
                out_ents.append(entity)
            # 露天物资（回放）：游标前已 spawn 且未收到协议级消失 close。
            # 停驻只是 pickup_candidate，不能改变物资是否仍存在的判断。
            for la in v.pickup_tracker.records:
                lr = v.pickup_tracker.at(la, cur)
                if lr is None or lr.get("loc") is None:
                    continue
                candidate = v.loot.get(la, {}).get("pickup_candidate_seq")
                if candidate is not None and lr["seq"] <= candidate <= cur and lr.get("gone_seq") is None:
                    lr = dict(lr, pickup_candidate_seq=candidate, pickup_confidence="candidate")
                if lr["seq"] > cur:
                    continue
                gseq = lr.get("gone_seq")
                if gseq is not None and cur >= gseq:
                    continue
                lp = lr["loc"]
                out_ents.append({
                    "key": "loot-%s" % la,
                    "name": lr.get("name") or "物资",
                    "kind": "loot", "xyz": list(lp), "world": list(lp),
                    "rel": [lp[0] - self_xyz[0], lp[1] - self_xyz[1],
                            lp[2] - self_xyz[2]] if self_xyz else None,
                    "yaw": None, "slot": None, "ch": la, "eid": la, "trail": None,
                    **loot_display_fields(lr),
                })
            meta = {"mra_ch": v.mra_ch, "mra_n": v.mra_n, "built_in_s": v.parse_s}
            flow = "%s / %s" % (v.run_dir.name, v.flow_dir.name)
        # 时间标签用真实 ts
        t_lo = t_hi = None
        s_arr, t_arr = v._seq_ts_map
        if len(s_arr):
            t_lo = float(t_arr[0]); t_hi = float(t_arr[-1])
        for entity in out_ents:
            if entity.get('hp'):
                entity['hp'] = operator_health(entity['hp'], entity['kind'] in ('player', 'mate', 'self') or bool(entity.get('hero')))
        return {
            "self": self_xyz, "self_yaw": self_yaw, "self_name": self_name,
            "self_hero": _self_ent.get("hero") if _self_ent else None,
            "self_hp": operator_health(health_at_seq(v.health_history, _self_ent["actor"], cur)) if _self_ent else None,
            "self_life": v.life_tracker.at(_self_ent["actor"], cur) if _self_ent else None,
            "live_active": False, "origin": [0, 0],
            "entities": out_ents, "names": names,
            "flow": flow, "meta": meta,
            "cursor": {"seq": self.cursor_seq, "lo": lo, "hi": hi,
                       "speed": self.speed, "t_lo": t_lo, "t_hi": t_hi},
        }


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    app = None
    map_info = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj):
        # obj may be bytes (pre-encoded /api/state cache hit): send verbatim,
        # skipping the per-tick json.dumps of a ~29KB body.
        if isinstance(obj, (bytes, bytearray)):
            body = bytes(obj)
        else:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        app = Handler.app
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/api/state":
            # 出错时降级应答（以前异常直接冒到 socketserver，浏览器只看到"空响应"，
            # curl: Empty reply，完全无法定位）。traceback 只在 ER_DEBUG=1 时打。
            try:
                self._json(app.api_state())
            except Exception:
                if _DEBUG:
                    import traceback
                    traceback.print_exc()
                self._json({"self": None, "entities": [], "names": [],
                            "status": "error"})
            return
        if path == "/api/map":
            self._json(Handler.map_info or {}); return
        if path == "/api/flows":
            self._json(app.api_flows()); return
        if path == "/api/select":
            self._json(app.api_select(q.get("run", [""])[0], q.get("flow", [""])[0])); return
        if path == "/api/ctrl":
            self._json(app.api_ctrl(q)); return
        if m3d_serve(self, path):
            return
        if m3d_serve(self, path, prefix="/avatars/", root=AVATAR_DIR, no_cache=True):
            return
        # Korr 前端（第三方 UI，见 实例对接.md）：/static/* 与 /korr/* 都从 web/korr 出，
        # 这样它内部写死的 /static/... 路径不用改一行。两条入口都常开，方便两套页面对照。
        if path in ("/er", "/er.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404); self.end_headers()


class RadarApp:
    def __init__(self, fixed_run=None, live=False):
        self.det = Detector()
        self.fixed_run = Path(fixed_run) if fixed_run else None
        self.live = live
        self.view = None
        self.play = None
        self.lock = threading.Lock()
        self.current = None       # (run_name, flow_name)
        self.pinned = None        # live 模式手动固定 (run, flow)
        self.pin_lock = None      # 回放模式固定 (run, flow):--flow / /api/select 后不再自动轮转
        self.live_detectors = {}  # (run_name, flow_name) -> LiveDetector（live 模式探测）
        # 污染自愈（见 _maybe_rebuild）：解码错误累计到阈值就把当前这一局后台整局
        # 重解一遍、原子换掉视图。_rebuild_base 是新视图的误差基线。
        self._rebuild_busy = False
        self._rebuild_last = 0.0
        self._rebuild_base = None
        # /api/state 应答缓存：前端自适应轮询（POLL_FAST=50ms，数据没变退到 300ms），
        # 底层数据多数 tick 未变。
        # key = (底层变化指纹) -> body_obj；未变直接复用，省 snapshot() 重建。
        self._state_cache = {"key": None, "body_obj": None}

    _RUNS_TTL = 2.0

    def _runs_index(self):
        """[(run_dir, mtime)] 新->旧，2 秒内复用 —— 4Hz 扫描下省掉每轮 glob + 几十次 stat。"""
        now = time.time()
        cache = getattr(self, "_runs_cache", None)
        if cache is not None and now - cache[0] < self._RUNS_TTL:
            return cache[1]
        cap = ROOT / "captures"
        pairs = []
        for d in cap.glob("run_*"):
            if not (d / "flows").is_dir():
                continue
            try:
                pairs.append((d, d.stat().st_mtime))
            except OSError:
                continue
        pairs.sort(key=lambda t: -t[1])
        self._runs_cache = (now, pairs)
        return pairs

    def runs_newest_first(self):
        runs = [d for d, _ in self._runs_index()]
        if self.fixed_run is not None:
            runs = [d for d in runs if d.name == self.fixed_run.name]
        return runs

    def _switch(self, run_dir, flow_dir):
        with self.lock:
            if self.current == (run_dir.name, flow_dir.name):
                return
            print("[radar] view -> %s / %s" % (run_dir.name, flow_dir.name), flush=True)
            if self.live:
                # 键必须带 run：抓包每局新建 run_*，而流目录在每局里都从 0,1,2… 重编号，
                # 只用 flow 名会命中上一局的同名流探测器 —— LiveView 会拿到旧局文件的
                # loader，解析的是已停写的旧文件，表现为"锁定了却不刷新/无实体"。
                det = self.live_detectors.get((run_dir.name, flow_dir.name))
                if det is not None:
                    det.loader.enable_c2s_self(1)  # 锁定后才解上行自机位姿；不抽稀（俯仰 6.6Hz，抽稀=4 只剩 1.6Hz）
                self.view = LiveView(run_dir, flow_dir,
                                     loader=det.loader if det else None,
                                     guid_paths=det.guid_paths if det else None)
                self.play = None
            else:
                self.view = BattleView(run_dir, flow_dir)
                self.play = Playback(self.view)
            self.current = (run_dir.name, flow_dir.name)
            self._rebuild_base = None      # 换流：重解基线跟着新视图重置
            # 切流即作废 body 缓存（指纹含流身份，但保险起见立即清空，
            # 避免下一场首 tick 误发上一场 body）
            self._state_cache["key"] = None
            self._state_cache["body_obj"] = None

    # ---- 污染自愈 ----------------------------------------------------------
    # 实录：内容解码器是按"每批新增 LB"跑的，增量消费时批边界可能切在一条跨多 LB
    # 的记录中间 → 解码器看到半条记录（framing_error 涨），而**身份绑定一旦错就
    # 粘住**：自机 PS 绑到同局另一个人、真人槽退化成「未知玩家」，之后自己不会回滚。
    # 同一份字节"整局一次性重解"批边界只有一个，实测 0 错误——所以按症状触发重解。
    REBUILD_ERR_STEP = 6        # 相对基线的错误增量阈值
    REBUILD_MIN_GAP = 25.0      # 同一局两次重解的最小间隔（秒）
    REBUILD_ERR_FLOOR = 3       # 低于这个绝对值当作噪音，不重解

    def _maybe_rebuild(self, v):
        """解码错误涨到阈值 → 后台整局重解并原子替换视图。"""
        if self._rebuild_busy or self._rebuild_base is None:
            try:
                self._rebuild_base = v.decode_error_count()
            except Exception:
                self._rebuild_base = 0
            return
        try:
            err = v.decode_error_count()
        except Exception:
            return
        now = time.time()
        if (err < self.REBUILD_ERR_FLOOR
                or err - self._rebuild_base < self.REBUILD_ERR_STEP
                or now - self._rebuild_last < self.REBUILD_MIN_GAP):
            return
        self._rebuild_busy = True
        self._rebuild_last = now
        run_dir, flow_dir = v.run_dir, v.flow_dir

        def _work():
            try:
                fresh = self._clean_view(run_dir, flow_dir)
                with self.lock:
                    if self.view is v:                 # 期间没被切走才替换
                        self.view = fresh
                        self._state_cache["key"] = None
                        self._state_cache["body_obj"] = None
                        self._rebuild_base = fresh.decode_error_count()
                print("[live-flow] 重解 %s/%s 清理脏上下文（解码错误 %d）"
                      % (run_dir.name, flow_dir.name, err), flush=True)
            except Exception as e:
                print("[live-flow] 重解失败:", e, flush=True)
            finally:
                self._rebuild_busy = False

        threading.Thread(target=_work, name="view-rebuild", daemon=True).start()

    def _clean_view(self, run_dir, flow_dir):
        """把这一局从原始字节一次性重解（backlog 口径）→ 干净的视图。"""
        from flowlive import IncrementalFlowLoader
        det = self.live_detectors.get((run_dir.name, flow_dir.name))
        gpaths = dict(getattr(det, "guid_paths", {}) or {}) if det else {}
        loader = IncrementalFlowLoader(flow_dir, auto_s2c=True, c2s_self=True)
        while loader.poll(max_frames=20000):
            pass
        view = LiveView(run_dir, flow_dir, loader=loader, guid_paths=gpaths)
        view.poll(max_frames=1 << 30)
        return view

    # ---- threads
    def worker_prelock(self):
        """按 run 新->旧、run 内 response.bin 大->小（每 run 前 12 个）：
        首个 n_ps>=3 的 flow 立即出图；未命中继续下一个 run。"""
        if self.pin_lock is not None:
            rd = ROOT / "captures" / self.pin_lock[0] / "flows" / self.pin_lock[1]
            if rd.is_dir():
                self._switch(rd.parent.parent, rd)
            return
        for target in self.runs_newest_first()[:8]:
            for fd in self.det.flows_by_size(target, limit=12):
                n = self.det.known(target.name, fd.name)
                if n is None:
                    n = self.det.probe_flow(fd)
                    self.det.record(target.name, fd.name, n)
                    self.det.save()
                print("[detect] %s/%s n_ps=%d" % (target.name, fd.name, n), flush=True)
                if n >= 3:
                    self._switch(target, fd)
                    return
            print("[detect] prelock miss in %s (size order), next run..." % target.name, flush=True)
        print("[detect] prelock exhausted", flush=True)

    def worker_indexer(self):
        """后台全量确定性索引所有 run 的 UDP 流；发现更大 arch131 计数则切换视图。"""
        while True:
            try:
                if self.pin_lock is not None:
                    return  # 回放固定流:禁止自动轮转换图
                for run in self.runs_newest_first():
                    bf, bfn = self.det.battle_flow_of(run.name)
                    items = sorted(self.det.udp_flows(run), key=lambda t: -t[1])  # mtime 新->旧
                    for _sz, _mt, fd in items:
                        n = self.det.known(run.name, fd.name)
                        if n is None:
                            n = self.det.probe_flow(fd)
                            self.det.record(run.name, fd.name, n)
                            self.det.save()
                        if n > bfn:
                            bf, bfn = fd.name, n
                            if self.current and self.current[0] == run.name:
                                cur_run = ROOT / "captures" / self.current[0]
                                self._switch(cur_run, cur_run / "flows" / bf)
                            elif self.current is None and n >= 3:
                                self._switch(run, run / "flows" / bf)
                    if bf and bfn > 0 and self.current is None:
                        self._switch(run, run / "flows" / bf)
            except Exception as e:
                print("[indexer] err:", e, flush=True)
            time.sleep(20)

    def worker_live(self):
        """实时抓包跟随：新 run 预锁 + 当前战斗流增长重建。"""
        while True:
            time.sleep(3)
            try:
                if self.fixed_run is None:
                    runs = self.runs_newest_first()
                    if runs and (self.current is None or runs[0].name != self.current[0]):
                        for fd in self.det.flows_by_size(runs[0], limit=5):
                            n = self.det.known(runs[0].name, fd.name)
                            if n is None:
                                n = self.det.probe_flow(fd)
                                self.det.record(runs[0].name, fd.name, n)
                                self.det.save()
                            if n >= 3:
                                self._switch(runs[0], fd)
                                break
                with self.lock:
                    v = self.view
                if v is not None:
                    v.refresh_if_grown()
            except Exception as e:
                print("[live] err:", e, flush=True)

    def worker_live_flow(self):
        """live 模式战斗流识别 —— 引擎级判据，与游戏客户端完全一致。

        UE4 客户端只有在对局 UDP 连接的控制通道(ch0)上完成握手
            Hello(C) -> Challenge(S) -> Login(C) -> Welcome(S) -> Join(C)
        收到 Welcome（"/Game/..." 地图串）后才把它当作对局会话并开始复制
        Actor。因此 战斗流 ⇔ 握手通过（handshake.Handshake.ok）。

        调度（确定性，非启发式）：
          - 候选按 (run 新->旧, flow mtime 新->旧) 全局排序；客户端同一时刻
            只有一局，全局最新的握手通过流 = 最近一局；
          - 第一道门用 handshake.scan_control（只解 ch0，<=96 帧，毫秒级，
            DNS/语音/被拒连接在门处即死）；通过者才建 LiveDetector 增量解析；
          - 早停锁定后交给 LiveView tail-follow；判定结果带备忘（正判定永久、
            负判定仅在流量静止时缓存，增长中的流每轮重扫直至收敛）；
          - 不用 size 预筛：6901B 的短命战斗流会被 64KB 门漏掉，245KB 的
            被拒连接（Failure 无 Welcome）反而会混进来——握手门两者皆正确。
        """
        RUN_WINDOW = 16            # 每轮回溯的 run 数（新->旧）
        HS_STALE_SEC = 120.0       # 负判定的可缓存条件：流量已静止
        # 扫描节拍：加了缓存后单轮只要几毫秒，所以没锁到流时更积极找新局；已经锁到
        # 流时降频，把时间还给 60Hz 增量解析（原先固定 0.25s，而单轮实测要 ~250ms，
        # 等于把增量解析挤成 ~4Hz）。
        SCAN_SEC_IDLE = 0.15
        SCAN_SEC_LOCKED = 0.5
        TICK_SEC = 1.0 / 60.0      # 当前视图增量解析节拍（本地版 60Hz 定版）
        hs_memo = {}               # flow_key -> bool（引擎门判定备忘）
        last_scan = 0.0
        # 预热 S2C 参考谱（首次 20-30ms）：放后台，别让第一局开局承担这笔开销
        def _prewarm():
            try:
                import s2c_auto
                s2c_auto.reference_spectrum()
            except Exception:
                pass
        threading.Thread(target=_prewarm, name="s2c-prewarm", daemon=True).start()
        while True:
            try:
                now = time.time()
                locked_now = self.pinned is None and self.current is not None
                if now - last_scan >= (SCAN_SEC_LOCKED if locked_now else SCAN_SEC_IDLE):
                    last_scan = now
                    locked = False
                    for run in self.runs_newest_first()[:RUN_WINDOW]:
                        udp = self.det.udp_flows(run)
                        for sz, mt, fd in sorted(udp, key=lambda t: -t[1]):
                            # 只认最近 3 分钟内还在写的流：否则会把旧对局当"当前局"自动播回放
                            if now - mt > LIVE_STALE_SEC:
                                break
                            key = "%s/%s" % (run.name, fd.name)
                            ok = hs_memo.get(key)
                            if ok is None:
                                h = hs_mod.scan_control(fd)
                                ok = h.ok
                                if ok or (now - mt) > HS_STALE_SEC:
                                    hs_memo[key] = ok
                                if ok:
                                    print("[live-flow] handshake %s (%s) rounds=%d" %
                                          (key, h.welcome, h.rounds), flush=True)
                                if not ok:
                                    continue
                            elif not ok:
                                continue
                            # 引擎门通过 -> 建/复用增量探测器并锁定（键含 run，见 _switch）
                            dkey = (run.name, fd.name)
                            d = self.live_detectors.get(dkey)
                            if d is None:
                                d = LiveDetector(fd)
                                self.live_detectors[dkey] = d
                                while len(self.live_detectors) > 32:   # 旧局探测器丢弃
                                    self.live_detectors.pop(next(iter(self.live_detectors)))
                            d.poll(max_frames=4000, early_frames=400)
                            if self.pinned is None and self.current != (run.name, fd.name):
                                self._switch(run, fd)
                                hi = d.hs_info()
                                print("[live-flow] locked %s welcome=%s rounds=%d n_ps=%d" %
                                      (key, hi["welcome"], hi["rounds"], d.poll()), flush=True)
                            locked = True
                            break
                        if locked:
                            break
                # 当前视图增量解析（60Hz：poll 空转时只付一次 stat，追赶期消化新帧）
                with self.lock:
                    v = self.view
                if v is not None and isinstance(v, LiveView):
                    v.poll(max_frames=4000)
                    self._maybe_rebuild(v)
            except Exception as e:
                print("[live-flow] err:", e, flush=True)
            time.sleep(TICK_SEC)

    # ---- api
    def api_state(self):
        with self.lock:
            v, p = self.view, self.play
        if v is None:
            return {"self": None, "entities": [], "names": [], "status": "detecting"}
        if isinstance(v, LiveView):
            return self._api_state_live(v)
        if p is None:
            return {"self": None, "entities": [], "names": [], "status": "detecting"}
        return p.snapshot()

    def _api_state_live(self, v):
        """live 模式 /api/state 应答（预编码 body 缓存）。

        前端 400ms 轮询；底层数据（slot 轨迹/实体表/死盒）在多数 tick 未变——
        指纹一致即重发上一次已编码的 body（byte-for-byte identical），完全跳过
        snapshot() 全量重建（trail 构建/死盒关联/FSM 查询 ~1ms）与 json.dumps
        （29KB ~2ms）。

        指纹 = (最新 seq, slot 采样总数, LB 总数, boxes 数, gone 数, self_slot,
                mra 锁定状态)。trail 轨迹头随 cur=latest seq 单调漂移，故 seq
        是必要成分；其余成分捕获 links/boxes/gone 变化，指纹一致即结果一致。
        """
        with v.lock:
            cur = None
            n_samples = 0
            for tr in v.slots.values():
                if tr:
                    cur = tr[-1][0] if cur is None else max(cur, tr[-1][0])
                    n_samples += len(tr)
        # 自机俯仰/瞄准朝向只有上行（C2S ch3 parse_fgpmove_params）才有，下行
        # 的玩家状态复制里不带。取不到、或超过 10s 没更新就当作没有——前端退回
        # 水平视角，而不是拿一个陈旧角度卡住镜头（真实对局里该消息每秒十几条）。
        pose = getattr(v.loader, "self_pose", None)
        pose_age = time.time() - getattr(v.loader, "self_pose_t", 0.0)
        if pose is not None and pose_age > 10.0:
            pose = None
        # 第一视角的**位置**也走上行：同一条 ch3 位姿消息里的 loc（15Hz）。
        # 下行槽位有效只有 4~6.5Hz，移动时每步跳 0.20m 中位 / 0.63m p90、间隔 p90
        # 198ms —— 第一视角"横向一顿一顿"主要来自它（旋转只有 0.3°/42ms，所以俯仰
        # 看着很顺）。上行 loc 与下行槽位水平差中位 0.0cm，可直接顶替。
        # 门槛比 pitch/yaw 紧得多：这条消息 45ms 一条，超过 1s 没来就是上行停了 ——
        # 那时必须让前端退回下行，否则相机会冻在最后一个上行坐标上。
        up_loc = None
        up_ts = None
        if pose is not None and pose_age <= 1.0 and pose.get("loc"):
            up_loc = [round(float(c), 1) for c in pose["loc"]]
            # 采样**时刻**也要发：前端拿它当插值的时间轴。只发位置的话，前端只能用
            # "收到响应的时刻"，而那个被 50ms 轮询网格量化（数据间隔才 42ms），
            # 插值会把这个量化误差重新编码成速度抖动（实测每帧速度标准差 1.36m/s，
            # 反而比不做插值还差）。带上 pose 自己的时间轴后，采样间隔是精确的。
            try:
                up_ts = round(float(pose["ts"]), 4)
            except (TypeError, ValueError, KeyError):
                up_ts = None
        key = (cur, n_samples, v.n_lb, len(v.boxes), len(v.gone),
               v.self_slot, v.mra_locked, len(v.loot),
               # 时间桶：对局销毁（流静止超 GAME_END_STALE_SEC）后物资从实体
               # 里消失的跃迁必须能穿透缓存——桶每 10s 变一次，跃迁 ≤10s 可见
               int(time.time() // 10),
               # 指纹必须同时带 yaw 与 pitch：只带 pitch 的话"平地转身"（俯仰不变）
               # 不会让指纹变化 -> 直接命中上一份 body -> 第一视角方向滞后。
               round(float(pose["pitch"]), 1) if pose else None,
               round(float(pose["yaw"]), 1) if pose else None,
               # 上行位置同样要进指纹：它每 45ms 变一次，不进指纹就会命中上一份
               # body，前端拿到旧坐标（等于把刚修好的滞后又加回去）。年龄用 1 秒桶，
               # 保证上行停发后 body 会被重建一次、把 self_up_loc 摘掉。
               (up_loc[0], up_loc[1], up_loc[2]) if up_loc else None,
               up_ts,
               min(3, int(pose_age)))
        ck = self._state_cache
        if ck["key"] == key and ck["body_obj"] is not None:
            return ck["body_obj"]
        body = v.snapshot()
        if pose:
            body["self_pitch"] = round(float(pose["pitch"]), 1)
            body["self_aim_yaw"] = round(float(pose["yaw"]), 1)
        if up_loc is not None:
            body["self_up_loc"] = up_loc
            if up_ts is not None:
                body["self_up_ts"] = up_ts
        ck["key"] = key
        ck["body_obj"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return ck["body_obj"]

    def api_flows(self):
        with self.det.lock:
            table = {k: dict(v) for k, v in self.det.table.items()}
        runs = []
        for run in self.runs_newest_first():
            flows = table.get(run.name) or {}
            bf, bfn = None, -1
            for name, n in flows.items():
                if n > bfn:
                    bf, bfn = name, n
            top = sorted([{"flow": k, "n_ps": v} for k, v in flows.items()],
                         key=lambda d: -d["n_ps"])[:20]
            runs.append({"run": run.name, "battle_flow": bf, "battle_n_ps": bfn, "top": top})
        return {"runs": runs, "current": self.current}

    def api_select(self, run, flow):
        rd = ROOT / "captures" / run / "flows" / flow
        if not rd.is_dir():
            return {"ok": False, "error": "no such flow"}
        if self.live:
            # live 模式：手动固定 flow，worker 不再自动抢回
            self.pinned = (run, flow)
            self._switch(rd.parent.parent, rd)
            return {"ok": True, "run": run, "flow": flow, "pinned": True}
        n = self.det.known(run, flow)
        if n is None:
            n = self.det.probe_flow(rd)
            self.det.record(run, flow, n); self.det.save()
        self.pin_lock = (run, flow)
        self._switch(rd.parent.parent, rd)
        return {"ok": True, "run": run, "flow": flow, "n_ps": n}

    def api_ctrl(self, q):
        with self.lock:
            p = self.play
        if p is None:
            return {"ok": False, "live": self.live}
        if "speed" in q:
            try:
                p.speed = max(0.0, float(q["speed"][0]))
            except ValueError:
                pass
        if "pause" in q:
            p.speed = 0.0 if q["pause"][0] == "1" else 16.0
        if "seek" in q:
            lo, hi = p.bounds()
            if lo is not None and hi is not None and hi > lo:
                frac = float(q["seek"][0]) / 1000.0
                p.cursor_seq = lo + frac * (hi - lo)
                p.last_wall = time.time()
        return {"ok": True, "speed": p.speed}


def load_map_info():
    # 多地图目录（web/map_catalog.json，由 tools/build_map_catalog.py 从官方
    # 地图小程序逆向数据 dfmap_data.json 生成）：{default, maps:[{key,name,bj,
    # rotate,width,height,centerX,centerY,minZoom,initZoom,initX,initY,
    # tileUrl,tileSize,maxNativeZoom,bounds,poi}, ...]}
    path = ROOT / "web" / "map_catalog.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 兜底：旧单图 map_markers.json（大坝）
    path = ROOT / "web" / "map_markers.json"
    if not path.exists():
        return {"default": "daba", "maps": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    info = doc.get("info") or {}
    levels = doc.get("levels") or []
    poi = [{"name": p.get("name"), "icon": p.get("icon"), "region": p.get("region"),
            "lat": p.get("lat"), "lng": p.get("lng")} for p in
           (levels[0].get("points") if levels else [])
           if p.get("lat") is not None and p.get("lng") is not None]
    single = {"key": "daba", "name": "零号大坝", "bj": info.get("bj", 128),
              "rotate": 0, "width": info.get("width"), "height": info.get("height"),
              "centerX": info.get("centerX"), "centerY": info.get("centerY"),
              "minZoom": info.get("minZoom", 3), "initZoom": info.get("initZoom", 3),
              "initX": info.get("initX"), "initY": info.get("initY"),
              "tileUrl": info.get("tileUrl"), "tileSize": info.get("tileSize", 256),
              "maxNativeZoom": info.get("maxNativeZoom", 4),
              "bounds": info.get("bounds"), "poi": poi}
    return {"default": "daba", "maps": [single]}


PAGE = (UI_DIR / "index.html").read_text(encoding="utf-8")

# 3D 能力开关：部署目录没有 web/m3d/manifest.json（如 8.136 老服务器）时把
# NO3D 置 1，前端隐藏 3D 入口、set3d 直接返回——同一份代码，行为由资产
# 是否存在决定，不做每台服务器一套 PAGE。
PAGE = PAGE.replace(
    '<meta name="tq-no3d" content="0">',
    '<meta name="tq-no3d" content="%d">' % (0 if (M3D_DIR / "manifest.json").is_file() else 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None, help="固定某个 run 目录名（缺省自动跟最新）")
    ap.add_argument("--flow", default=None, help="固定某个 flow 目录名（回放防轮转换图）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（服务器部署用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--live", action="store_true",
                    help="实时模式：增量解析 light_relay 抓包，tail-follow 最新位置")
    ap.add_argument("--ui", choices=("korr", "er"), default=None,
                    help="首页用哪套前端：korr=Korr 雷达页（缺省），er=内置老页面"
                         "（老页面始终可从 /er 打开）")
    args = ap.parse_args()
    if args.ui:
        global KORR_UI
        KORR_UI = args.ui == "korr"
    app = RadarApp(fixed_run=args.run, live=args.live)
    if args.flow:
        app.pin_lock = (args.run, args.flow)
    Handler.app = app
    Handler.map_info = load_map_info()
    if args.live:
        threading.Thread(target=app.worker_live_flow, daemon=True).start()
    else:
        threading.Thread(target=app.worker_prelock, daemon=True).start()
        threading.Thread(target=app.worker_indexer, daemon=True).start()
        threading.Thread(target=app.worker_live, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[er radar] http://%s:%d (live=%s)" % (args.host, args.port, args.live), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

