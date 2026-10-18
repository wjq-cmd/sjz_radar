"""Windows capture-mode settings and relay entry adapter."""
import json
import os
from pathlib import Path
import socket
import ctypes
from functools import lru_cache


@lru_cache(maxsize=2)
def _load_capture_library(path):
    return ctypes.WinDLL(path)


def npcap_available():
    if os.name != "nt":
        return False
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    for folder in (system / "Npcap", system):
        try:
            _load_capture_library(str(folder / "wpcap.dll"))
            return True
        except OSError:
            pass
    return False


def local_addresses():
    addresses = {"127.0.0.1"}
    try:
        addresses.update(row[4][0] for row in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in addresses if not ip.startswith("169.254."))


DEFAULT_LISTEN_PORT = 9099       # 软路由直采缺省监听端口


def list_capture_devices():
    """本机可抓包的网卡 [(name, desc, ips)]。Npcap 缺失/加载失败返回 []。

    惰性 import：nic_capture 会连带拉起 light_relay（抓包库），门户进程平时不需要，
    只有"列出网卡"与"校验所选网卡"时才付这个代价。任何异常都吞掉返回 []，让 UI
    能退化成"先装 Npcap"的提示，而不是门户整页报错。
    """
    try:
        import nic_capture
        lib = nic_capture.load_wpcap()
        if lib is None:
            return []
        # 滤掉 WAN Miniport 系列：它们是协议栈的软件接口，pcap 打不开也抓不到东西，
        # 但会占满下拉列表（本机实测 11 张里 3 张是它），让用户挑不到真网卡。
        return [(n, d, i) for n, d, i in nic_capture.list_devices(lib)
                if not d.lower().startswith("wan miniport")]
    except Exception:
        return []


def validate_settings(value):
    mode = value.get("mode", "hotspot")
    if mode not in ("hotspot", "proxy", "mirror", "direct"):
        raise ValueError("Invalid capture mode")
    if mode == "direct":
        # 「软路由直采」：软路由把 `tcpdump -w -` 的 pcap 流推给本机的监听端口。
        # 本机不抓网卡、不需要 Npcap，所以要校验的只有端口 + 给用户看的对外地址。
        port = value.get("listen_port", DEFAULT_LISTEN_PORT)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("监听端口需在 1024-65535")
        if port in (int(os.environ.get("ER_HOME_PORT", 6677)),
                    int(os.environ.get("ER_APP_PORT", 8888)),
                    int(value.get("proxy_port", 10808))):
            raise ValueError("监听端口与本地服务冲突")
        host = value.get("advertise_host")
        if host not in local_addresses():
            # 记住的地址可能因为换网没了：退回一个可用地址。**优先非环回** ——
            # local_addresses() 是排序的，直接取第一个会拿到 127.0.0.1，而软路由
            # 要填的是本机的局域网地址。（这只是显示/生成命令用，实际监听恒为
            # 0.0.0.0，所以不影响能不能收到流。）
            cands = [ip for ip in local_addresses() if not ip.startswith("127.")]
            host = (cands or ["127.0.0.1"])[0]
        return {"mode": "direct", "listen_port": port, "advertise_host": host}
    if mode == "mirror":
        # 「软路由抓包」：PC 只在一张指定网卡上收镜像流量，不碰任何网络配置。
        # 所以这里唯一要校验的就是网卡。
        device = value.get("device")
        if not isinstance(device, str) or not device.strip():
            raise ValueError("软路由模式需要先选择抓包网卡")
        device = device.strip()
        if len(device) > 200:
            raise ValueError("网卡标识过长")
        devs = list_capture_devices()
        # 拿不到列表（Npcap 未装）就不在这里拦，交给抓包进程报明确错误；
        # 拿得到就必须命中——否则用户可能选了个已经拔掉的网卡，启动之后才失败，
        # 而那时错误信息埋在 worker 日志里，很难定位。
        if devs and not any(device == n or device.lower() in n.lower()
                            or device.lower() in d.lower() for n, d, _i in devs):
            raise ValueError("选择的抓包网卡不存在，请重新选择")
        return {"mode": "mirror", "device": device}
    host = value.get("proxy_host", "127.0.0.1")
    if host not in local_addresses():
        raise ValueError("Select an available local IPv4 address")
    port = value.get("proxy_port", 10808)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Proxy port must be between 1024 and 65535")
    if port in (int(os.environ.get("ER_HOME_PORT", 6677)),
                int(os.environ.get("ER_APP_PORT", 8888))):
        raise ValueError("Proxy port conflicts with a local service")
    return {"mode": mode, "proxy_host": host, "proxy_port": port}


def load_settings(data_dir):
    defaults = {"mode": "hotspot", "proxy_host": "127.0.0.1", "proxy_port": 10808}
    try:
        value = json.loads((Path(data_dir) / "desktop_mode.json").read_text("utf-8"))
        # A remembered adapter may be absent after changing Wi-Fi.
        if value.get("proxy_host") not in local_addresses():
            value["proxy_host"] = "127.0.0.1"
        try:
            return validate_settings(value)
        except ValueError:
            if value.get("mode") == "mirror":
                # 记住的镜像网卡没了/不合法：**保留模式**、只把网卡清空，让门户提示
                # 重选。不能把模式悄悄退回 hotspot——那会让用户以为配置生效了，
                # 实际跑的是完全不同的抓包路径。
                return {"mode": "mirror", "device": None,
                        "proxy_host": value.get("proxy_host", "127.0.0.1"),
                        "proxy_port": value.get("proxy_port", 10808)}
            raise
    except (OSError, ValueError, TypeError, AttributeError):
        return defaults


def save_settings(data_dir, settings):
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "desktop_mode.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings), encoding="utf-8")
    tmp.replace(path)


def check_ports(settings):
    sockets = []
    try:
        endpoints = [("0.0.0.0", int(os.environ.get("ER_APP_PORT", 8888)),
                      socket.SOCK_STREAM)]
        if settings["mode"] == "proxy":
            endpoints += [(settings["proxy_host"], settings["proxy_port"], kind)
                          for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM)]
        for host, port, kind in endpoints:
            sock = socket.socket(socket.AF_INET, kind)
            sockets.append(sock)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, port))
    finally:
        for sock in sockets:
            sock.close()


def run_proxy(base):
    # Local stand-alone mode: never inherit remote deployment relay credentials.
    os.environ.pop("RELAY_TOKENS", None)
    import light_relay as relay
    import sys
    settings = validate_settings({
        "mode": "proxy",
        "proxy_host": os.environ["ER_PROXY_HOST"],
        "proxy_port": int(os.environ["ER_PROXY_PORT"]),
    })
    sys.argv = ["light_relay", "--host", settings["proxy_host"],
                "--port", str(settings["proxy_port"]),
                "--public-addr", settings["proxy_host"],
                "--capture-dir", str(Path(base) / "captures"),
                "--flush-interval", str(1 / 60)]
    return relay.main()
