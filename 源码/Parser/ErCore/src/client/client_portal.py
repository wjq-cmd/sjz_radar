# -*- coding: utf-8 -*-
"""Local control page for the NIC-capture desktop application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from client_release import VERSION
from desktop_modes import (load_settings, save_settings, validate_settings,
                            local_addresses, npcap_available, list_capture_devices)


class RuntimeSession:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.active = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.runtime = "未启动"
        self.settings = load_settings(self.data_dir)
        self.requested = False
        self.generation = 0
        self.active.set()

    def snapshot(self):
        with self.lock:
            return {"runtime": self.runtime, "version": VERSION,
                    "settings": dict(self.settings), "requested": self.requested,
                    "local_addresses": local_addresses(),
                    "npcap_available": npcap_available(),
                    # 镜像模式要选网卡：列表给门户渲染下拉。拿不到（Npcap 未装）就是空表，
                    # 界面据此提示先装 Npcap。
                    "devices": [{"name": n, "desc": d, "ips": list(i)}
                                for n, d, i in list_capture_devices()],
                    "radar_url": "http://127.0.0.1:" + os.environ.get("ER_APP_PORT", "8888")}

    def control(self, body):
        with self.lock:
            action = body.get("action")
            if action == "stop":
                self.requested = False
                self.generation += 1
                return {"ok": True}
            if action != "start":
                return {"ok": False, "error": "无效操作"}
            try:
                settings = validate_settings(body)
                save_settings(self.data_dir, settings)
            except (ValueError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            self.settings = settings
            self.requested = True
            self.generation += 1
            return {"ok": True}

    def close(self):
        self.stop_event.set()
        self.active.clear()


PORTAL_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ER</title><style>
:root{
  --bg:#04050a;--glass:rgba(16,19,27,.72);--glass-solid:rgba(12,14,20,.94);
  --hair:rgba(255,255,255,.09);--hair-2:rgba(255,255,255,.16);
  --txt:#eef1f6;--txt-2:rgba(233,238,247,.62);--txt-3:rgba(233,238,247,.34);
  --accent:#4c8dff;--ally:#37e08a;--warn:#ffcf4d;--danger:#ff5f6d;
  --blur:saturate(1.7) blur(24px);--ease:cubic-bezier(.4,0,.2,1);
  --r-lg:20px;--r-md:14px;--r-sm:10px;
  --shadow:0 18px 48px rgba(0,0,0,.62),0 2px 8px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--txt);
  font:14px/1.7 -apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;padding:22px}
.card{width:100%;max-width:520px;background:var(--glass-solid);
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid var(--hair);border-radius:var(--r-lg);box-shadow:var(--shadow);
  padding:24px 26px}
.head{display:flex;align-items:center;gap:10px;margin-bottom:22px}
.head h1{font-size:19px;margin:0;letter-spacing:.5px;font-weight:680}
.head .sub{color:var(--txt-3);font-size:12px}
.pill{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:12px;
  color:var(--txt-2);border:1px solid var(--hair);border-radius:999px;padding:3px 11px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--txt-3);flex:none}
.pill.ok .dot{background:var(--ally);box-shadow:0 0 8px var(--ally)}
.pill.run .dot{background:var(--accent);box-shadow:0 0 8px var(--accent)}
.lbl{font-size:12px;color:var(--txt-3);margin-bottom:8px}
.sec{margin-bottom:18px}
.seg{display:flex;gap:8px}
.seg button{flex:1;border:1px solid var(--hair);background:transparent;color:var(--txt-2);
  padding:11px 0;border-radius:var(--r-sm);font:inherit;font-size:13px;cursor:pointer;
  transition:background .16s var(--ease),color .16s var(--ease),border-color .16s var(--ease)}
.seg button.on{background:rgba(76,141,255,.16);border-color:rgba(76,141,255,.42);color:#cfe2ff}
.fields{display:grid;grid-template-columns:minmax(0,1fr) 108px;gap:10px;margin-top:12px}
.fields label{min-width:0;display:block}
select,input{width:100%;background:rgba(255,255,255,.04);border:1px solid var(--hair);
  color:var(--txt);padding:10px 12px;border-radius:var(--r-sm);font:inherit;font-size:13px}
/* 下拉展开列表由系统绘制：显式给白底黑字，避免深色主题下白底白字看不清 */
select option{background:#ffffff;color:#111111}
.env{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:12px;color:var(--txt-2)}
.env span{display:flex;align-items:center;gap:6px}
.env i{width:6px;height:6px;border-radius:50%;background:var(--txt-3);flex:none}
.env i.ok{background:var(--ally)}
.env i.no{background:var(--danger)}
.actions{display:flex;gap:10px;margin-top:22px}
.btn{flex:1;border:0;border-radius:var(--r-sm);padding:12px 0;font:inherit;font-size:14px;
  font-weight:600;cursor:pointer;text-align:center;text-decoration:none;
  transition:filter .16s var(--ease),background .16s var(--ease)}
.btn.primary{background:var(--accent);color:#fff}
.btn.primary:hover{filter:brightness(1.12)}
.btn.ghost{background:rgba(255,255,255,.06);color:var(--txt-2);border:1px solid var(--hair)}
.btn.ghost:hover{background:rgba(255,255,255,.1)}
.btn:disabled{opacity:.42;cursor:default;filter:none}
.tip{margin-top:12px;font-size:12px;color:var(--txt-3);min-height:18px}
.tip.err{color:#ffb3a6;background:rgba(255,95,109,.1);border:1px solid rgba(255,95,109,.28);
  border-radius:var(--r-sm);padding:8px 11px;line-height:1.5}
""" + """</style></head><body><main class="card">
<div class="head"><h1>ER</h1><span class="sub">本地雷达</span>
<span class="pill" id="pill"><i class="dot"></i><span id="pillText">未启动</span></span></div>

""" + """
<section class="sec"><div class="lbl">运行模式</div>
<div class="seg" role="radiogroup" aria-label="运行模式">
<button type="button" data-mode="hotspot">热点网卡抓包</button>
<button type="button" data-mode="proxy">SOCKS5 代理</button>
<button type="button" data-mode="mirror">软路由抓包</button>
<button type="button" data-mode="direct">软路由直采</button>
</div>
<div class="fields" id="directFields" hidden>
<label><span class="lbl">监听端口</span><input id="listenPort" type="number" min="1024" max="65535" value="9099"></label>
<label><span class="lbl">本机地址</span><select id="advertiseHost" aria-label="本机地址"></select></label>
<div style="grid-column:1/-1;font-size:12px;color:var(--txt-3);line-height:1.5">
在软路由上执行下面这条命令（它把抓到的包推给本机；按 Ctrl+C 停止）：<br>
<code id="directCmd" style="display:block;margin-top:4px;padding:6px 8px;border-radius:6px;background:rgba(0,0,0,.34);word-break:break-all"></code>
只需要软路由有 <code>tcpdump</code> 和 <code>nc</code>，不用装本项目的任何东西；本机不需要 Npcap。
</div>
</div>
<div class="fields" id="mirrorFields" hidden>
<label><span class="lbl">抓包网卡</span><select id="capDevice" aria-label="抓包网卡"></select></label>
<div style="grid-column:1/-1;font-size:12px;color:var(--txt-3);line-height:1.5">
让软路由把手机的流量镜像到这块网卡，本程序只收包，不改本机任何网络设置（不开热点、不配 NAT、不发 IP）。<br>
手机要打开雷达页，需放行 8888 入站（未提权时按下方提示手动加一次防火墙规则）。
</div>
</div>
<div class="fields" id="proxyFields" hidden>
<label><span class="lbl">监听地址</span><select id="proxyHost" aria-label="监听地址"></select></label>
<label><span class="lbl">端口</span><input id="proxyPort" type="number" min="1024" max="65535" value="10808"></label>
</div>
</section>
<section class="sec env" id="env"></section>
<div class="actions">
<button class="btn primary" id="startRuntime" disabled>启动</button>
<button class="btn ghost" id="stopRuntime" disabled>停止</button>
<a class="btn ghost" id="radar" href="http://127.0.0.1:8888" hidden>打开雷达</a>
</div>
<div class="tip" id="tip" role="status"></div>
""" + """</main><script>
var $ = function(id){ return document.getElementById(id); };
var lastStatus = null;
// 用户手动选过的模式 / 改过的代理地址与端口：选过之后不再被 2 秒一次的状态轮询
// 覆盖回后端的旧值（地址与端口以前没这个守卫，手选完 2 秒就被写回 127.0.0.1）。
var pickedMode = null, pickedHost = false, pickedPort = false, pickedDevice = false,
    pickedAdvertise = false, pickedListenPort = false;
function paintDirectCmd(){
  var ip = $("advertiseHost").value || "127.0.0.1";
  var pt = $("listenPort").value || 9099;
  $("directCmd").textContent =
    "tcpdump -i <手机所在口> -U -w - udp | nc " + ip + " " + pt;
}
function paintMode(mode){
  document.querySelectorAll(".seg button").forEach(function(b){
    b.classList.toggle("on", b.dataset.mode === mode);
  });
  $("proxyFields").hidden = mode !== "proxy";
  $("mirrorFields").hidden = mode !== "mirror";
  $("directFields").hidden = mode !== "direct";
  if (mode === "direct") paintDirectCmd();
}
function renderEnv(s){
  var box = $("env"); box.replaceChildren();
  [["Npcap", !!s.npcap_available]].forEach(function(it){
    var el = document.createElement("span");
    var dot = document.createElement("i");
    dot.className = it[1] ? "ok" : "no";
    el.append(dot, document.createTextNode(it[0] + (it[1] ? "" : " 未就绪")));
    box.appendChild(el);
  });
}
function render(s){
  lastStatus = s;
  $("pillText").textContent = s.runtime || "未启动";
  $("pill").className = "pill" + (s.runtime === "运行中" ? " run" : "");
  if (pickedMode === null) paintMode(s.settings.mode);
  var host = $("proxyHost"), sig = (s.local_addresses || []).join(",");
  if (host.dataset.sig !== sig){
    host.replaceChildren();
    (s.local_addresses || []).forEach(function(ip){
      var o = document.createElement("option"); o.value = ip; o.textContent = ip; host.appendChild(o);
    });
    host.dataset.sig = sig;
  }
  if (!pickedHost) host.value = s.settings.proxy_host;
  if (!pickedPort) $("proxyPort").value = s.settings.proxy_port;
  // 网卡下拉：后端给 (NPF 名, 描述)。value 用 NPF 名（唯一、可直接喂 pcap），
  // 显示描述（人看得懂）。用 sig 判重避免每 2 秒重建下拉把用户的选择冲掉。
  var dev = $("capDevice"), dsig = (s.devices || []).map(function(d){ return d.name; }).join("|");
  if (dev.dataset.sig !== dsig){
    dev.replaceChildren();
    var list = s.devices || [];
    if (!list.length){
      var o0 = document.createElement("option");
      o0.value = ""; o0.textContent = s.npcap_available ? "未发现可用网卡" : "先安装 Npcap";
      dev.appendChild(o0);
    }
    list.forEach(function(d){
      var o = document.createElement("option");
      o.value = d.name;
      o.textContent = d.desc + ((d.ips && d.ips.length) ? "  (" + d.ips.join(", ") + ")" : "");
      dev.appendChild(o);
    });
    dev.dataset.sig = dsig;
  }
  if (!pickedDevice && s.settings.device) dev.value = s.settings.device;
  // 直采：本机地址下拉（给软路由填的地址）+ 生成的命令
  var adv = $("advertiseHost");
  if (adv.dataset.sig !== sig){
    adv.replaceChildren();
    (s.local_addresses || []).forEach(function(ip){
      var o = document.createElement("option"); o.value = ip; o.textContent = ip; adv.appendChild(o);
    });
    adv.dataset.sig = sig;
  }
  if (!pickedAdvertise && s.settings.advertise_host) adv.value = s.settings.advertise_host;
  if (!pickedListenPort && s.settings.listen_port) $("listenPort").value = s.settings.listen_port;
  paintDirectCmd();
  renderEnv(s);
  $("startRuntime").disabled = false;
  $("startRuntime").textContent = s.requested ? "应用并重启" : "启动";
  $("stopRuntime").disabled = !s.requested;
  var radar = $("radar");
  radar.hidden = s.runtime !== "运行中";
  radar.href = s.radar_url || "http://127.0.0.1:8888";
  // 提示行只保留「连不上本地服务」这类连接错误；后端状态里的错误留在控制台和日志
  $("tip").textContent = "";
  $("tip").className = "tip";
}
async function status(){
  try{
    var r = await fetch("/api/status", {cache:"no-store"});
    if (!r.ok) throw Error();
    render(await r.json());
  }catch(e){
    $("tip").textContent = "本地服务未连接";
    $("tip").className = "tip err";
  }
}
document.querySelectorAll(".seg button").forEach(function(b){
  b.onclick = function(){ pickedMode = b.dataset.mode; paintMode(pickedMode); };
});
// 手选地址 / 手改端口后打上标记，等「启动」提交成功再清掉（那时后端已存下我们的选择）
$("proxyHost").addEventListener("change", function(){ pickedHost = true; });
$("proxyPort").addEventListener("input", function(){ pickedPort = true; });
$("capDevice").addEventListener("change", function(){ pickedDevice = true; });
$("advertiseHost").addEventListener("change", function(){
  pickedAdvertise = true; paintDirectCmd(); });
$("listenPort").addEventListener("input", function(){
  pickedListenPort = true; paintDirectCmd(); });
async function control(action){
  var btn = $(action === "start" ? "startRuntime" : "stopRuntime");
  btn.disabled = true;
  var on = document.querySelector(".seg button.on");
  try{
    var r = await fetch("/api/control", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:action, mode:on ? on.dataset.mode : "hotspot",
        proxy_host:$("proxyHost").value, proxy_port:Number($("proxyPort").value),
        device:$("capDevice").value,
        listen_port:Number($("listenPort").value),
        advertise_host:$("advertiseHost").value})});
    var j = await r.json();
    if (!j.ok) throw Error(j.error || "操作失败");
    $("tip").textContent = ""; $("tip").className = "tip";
    if (action === "start") { pickedHost = false; pickedPort = false; pickedDevice = false;
                             pickedAdvertise = false; pickedListenPort = false; }  // 已提交并保存
    await status();
  }catch(e){
    $("tip").textContent = e.message; $("tip").className = "tip err";
  }finally{
    if (lastStatus){
      $("startRuntime").disabled = false;
      $("stopRuntime").disabled = !lastStatus.requested;
    }
  }
}
$("startRuntime").onclick = function(){ control("start"); };
$("stopRuntime").onclick = function(){ control("stop"); };
status(); setInterval(status, 2000);
""" + "</script></body></html>"


def start_portal(session, port=6677):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, value, status=200, html=False):
            body = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self.send(PORTAL_PAGE, html=True)
            elif path == "/api/status":
                self.send(session.snapshot())
            else:
                self.send({"ok": False}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/api/control":
                return self.send({"ok": False}, 404)
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{server.server_port}", f"http://localhost:{server.server_port}"}
            if self.headers.get_content_type() != "application/json":
                return self.send({"ok": False, "error": "请求格式错误"}, 400)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 4096:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError()
                if origin and origin not in allowed:
                    return self.send({"ok": False, "error": "请求来源错误"}, 403)
                result = session.control(body)
                self.send(result, 200 if result.get("ok") else 400)
            except (ValueError, TypeError):
                self.send({"ok": False, "error": "请求格式错误"}, 400)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="client-portal", daemon=True).start()
    return server
