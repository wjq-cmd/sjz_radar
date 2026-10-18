using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Net;
using System.Runtime.InteropServices;

namespace GatewayCapture.Realtime;

/// <summary>
/// 本机模式的路由器：在没有热点、没有第二台设备的情况下，拦截本机游戏
/// 发往 TGCP 服务器的 TCP，改写到本地中继，实现主动中间人。
///
/// 与 HotspotTcpRouter 的区别：
/// - 使用 WinDivert layer 0（本机收发），而不是 layer 1（转发）；
/// - 客户端和网关都是本机 IP，靠别名端口区分流量；
/// - 只在 SYN 上建立路由，已存在的连接原样放行，避免打断其他程序；
/// - 中继自己的上游连接按源端口排除，避免被再次拦截。
/// </summary>
internal sealed class LocalCaptureRouter : IAsyncDisposable
{
    private sealed record Route(int GamePort, IPAddress Server, int ServerPort, int AliasPort)
    {
        public long LastSeen = Environment.TickCount64;
    }

    private readonly IPAddress _localIp;
    private readonly IPAddress? _serverIp;
    private readonly int _serverPort;
    private readonly bool _auto;
    private readonly int _listenerPort;
    private readonly IPAddress _udpServerIp;
    private readonly int _udpServerPort;
    private readonly Action<string, object> _event;
    private readonly Action<UdpPacket> _udp;
    private readonly object _gate = new();
    private readonly Dictionary<(int GamePort, IPAddress Server, int ServerPort), Route> _routes = [];
    private readonly Dictionary<int, Route> _aliases = [];
    private readonly ConcurrentDictionary<int, byte> _excludedUpstream = new();
    private int _nextAlias = 20000;
    private DivertHandle? _forward, _return, _udpHandle;
    private readonly List<Task> _workers = [];
    private volatile bool _stopping;

    public long RedirectedUp, RedirectedDown, UdpObserved, Unmapped, SendErrors, ReceiveErrors;
    public bool Running { get; private set; }

    public LocalCaptureRouter(
        IPAddress localIp,
        IPAddress? serverIp,
        int serverPort,
        int listenerPort,
        Action<string, object> report,
        Action<UdpPacket> udp,
        IPAddress? udpServerIp = null,
        int udpServerPort = 0)
    {
        _localIp = localIp;
        _serverIp = serverIp;
        _serverPort = serverPort;
        _auto = serverIp is null;
        _listenerPort = listenerPort;
        _udpServerIp = udpServerIp ?? serverIp ?? IPAddress.None;
        _udpServerPort = udpServerPort;
        _event = report;
        _udp = udp;
    }

    public string ForwardFilter => _auto
        ? $"outbound and ip and tcp and ip.SrcAddr == {_localIp} and tcp.SrcPort != {_listenerPort}"
        : $"outbound and ip and tcp and ip.SrcAddr == {_localIp} " +
          $"and ip.DstAddr == {_serverIp} and tcp.DstPort == {_serverPort}";

    public string ReturnFilter =>
        $"outbound and ip and tcp and ip.SrcAddr == {_localIp} " +
        $"and tcp.SrcPort == {_listenerPort} and ip.DstAddr == {_localIp}";

    public string UdpFilter => _auto
        ? $"ip and udp and (ip.SrcAddr == {_localIp} or ip.DstAddr == {_localIp})"
        : _udpServerPort > 0
            ? $"ip and udp and ((ip.SrcAddr == {_localIp} and ip.DstAddr == {_udpServerIp} and udp.DstPort == {_udpServerPort}) " +
              $"or (ip.SrcAddr == {_udpServerIp} and udp.SrcPort == {_udpServerPort} and ip.DstAddr == {_localIp}))"
            : $"ip and udp and ((ip.SrcAddr == {_localIp} and ip.DstAddr == {_udpServerIp}) " +
              $"or (ip.SrcAddr == {_udpServerIp} and ip.DstAddr == {_localIp}))";

    /// <summary>中继的上游 socket 绑定后上报本地端口，这里登记排除。</summary>
    public void ExcludeUpstreamPort(int port) => _excludedUpstream[port] = 1;

    private Route? MapForward(int gamePort, IPAddress server, int serverPort, bool syn)
    {
        lock (_gate)
        {
            var key = (gamePort, server, serverPort);
            if (_routes.TryGetValue(key, out var existing))
            {
                existing.LastSeen = Environment.TickCount64;
                return existing;
            }
            // 只对新的 SYN 建立路由；已存在的连接原样放行。
            if (!syn || _routes.Count >= 4096) return null;

            foreach (var pair in _routes
                         .Where(item => Environment.TickCount64 - item.Value.LastSeen > 600000)
                         .ToArray())
            {
                _routes.Remove(pair.Key);
                _aliases.Remove(pair.Value.AliasPort);
            }

            int alias;
            do
            {
                alias = _nextAlias++;
                if (_nextAlias > 59999) _nextAlias = 20000;
            } while (_aliases.ContainsKey(alias));

            var route = new Route(gamePort, server, serverPort, alias);
            _routes[key] = route;
            _aliases[alias] = route;
            return route;
        }
    }

    /// <summary>本机回环、组播、以及和本机同 /24 的地址不代理。</summary>
    private bool IsLocalOrSpecial(IPAddress address)
    {
        if (address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
        {
            return true;
        }
        if (IPAddress.IsLoopback(address))
        {
            return true;
        }
        var bytes = address.GetAddressBytes();
        if (bytes[0] >= 224)
        {
            return true;
        }
        var local = _localIp.GetAddressBytes();
        return bytes[0] == local[0] && bytes[1] == local[1] && bytes[2] == local[2];
    }

    /// <summary>中继用它把 client 的对端（本机:别名端口）映射回真实服务器。</summary>
    public IPEndPoint? Resolve(IPEndPoint endpoint)
    {
        lock (_gate)
        {
            if (!endpoint.Address.Equals(_localIp) ||
                !_aliases.TryGetValue(endpoint.Port, out var route))
            {
                return null;
            }
            route.LastSeen = Environment.TickCount64;
            return new IPEndPoint(route.Server, route.ServerPort);
        }
    }

    public async Task StartAsync()
    {
        try
        {
            _forward = WinDivertNative.OpenChecked(ForwardFilter, 0);
            _return = WinDivertNative.OpenChecked(ReturnFilter, 0);
            _udpHandle = WinDivertNative.OpenChecked(UdpFilter, 0, 5); // SNIFF | RECV_ONLY
            Running = true;
            foreach (int kind in new[] { 0, 1, 2 })
            {
                _workers.Add(Task.Factory.StartNew(
                    () => ReceiveLoop(kind),
                    CancellationToken.None,
                    TaskCreationOptions.LongRunning,
                    TaskScheduler.Default));
            }
            _event("local_router_ready", new
            {
                mode = _auto ? "auto" : "pinned",
                local = _localIp.ToString(),
                server = _serverIp?.ToString() ?? "auto",
                serverPort = _serverPort,
                udpServer = _udpServerIp.ToString(),
                udpServerPort = _udpServerPort,
                listenerPort = _listenerPort,
            });
        }
        catch
        {
            await DisposeAsync();
            throw;
        }
    }

    private void ReceiveLoop(int kind)
    {
        var handle = kind == 0 ? _forward! : kind == 1 ? _return! : _udpHandle!;
        var packet = new byte[65575];
        var address = new byte[80];
        try
        {
            while (WinDivertNative.Receive(handle, packet, (uint)packet.Length, out uint size, address))
            {
                int length = (int)size;
                if (!IpTransport.Parse(packet, length, out var parsed))
                {
                    if (kind != 2) Inject(handle, packet, length, address, false);
                    continue;
                }

                if (kind == 2)
                {
                    if (_stopping) continue;
                    int udpLength = BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(parsed.IpLength + 4));
                    if (udpLength < 8 || parsed.IpLength + udpLength > parsed.TotalLength) continue;

                    bool up = parsed.Source.Equals(_localIp);
                    if (_auto && IsLocalOrSpecial(up ? parsed.Destination : parsed.Source))
                    {
                        continue;
                    }

                    var local = new IPEndPoint(
                        up ? parsed.Source : parsed.Destination,
                        up ? parsed.SourcePort : parsed.DestinationPort);
                    var remote = new IPEndPoint(
                        up ? parsed.Destination : parsed.Source,
                        up ? parsed.DestinationPort : parsed.SourcePort);
                    Interlocked.Increment(ref UdpObserved);
                    try
                    {
                        _udp(new UdpPacket(
                            up, local, remote,
                            packet.AsSpan(parsed.IpLength + 8, udpLength - 8).ToArray(),
                            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()));
                    }
                    catch (Exception ex)
                    {
                        // broker 的流上限等异常不应带走整个 UDP 观察线程。
                        _event("local_udp_callback_error", new { error = ex.Message });
                    }
                }
                else if (kind == 0)
                {
                    // 中继自己的上游连接：原样放行，否则会自环。
                    if (_excludedUpstream.ContainsKey(parsed.SourcePort))
                    {
                        Inject(handle, packet, length, address, false);
                        continue;
                    }

                    if (_auto && IsLocalOrSpecial(parsed.Destination))
                    {
                        Inject(handle, packet, length, address, false);
                        continue;
                    }

                    int flags = packet[parsed.IpLength + 13];
                    bool syn = (flags & 0x02) != 0 && (flags & 0x10) == 0;
                    var route = MapForward(parsed.SourcePort, parsed.Destination, parsed.DestinationPort, syn);
                    if (route is null)
                    {
                        Interlocked.Increment(ref Unmapped);
                        Inject(handle, packet, length, address, false);
                        continue;
                    }

                    IpTransport.Rewrite(packet, parsed, _localIp, route.AliasPort, _localIp, _listenerPort);
                    Inject(handle, packet, length, address, true);
                    Interlocked.Increment(ref RedirectedUp);
                }
                else
                {
                    Route? route;
                    lock (_gate)
                    {
                        _aliases.TryGetValue(parsed.DestinationPort, out route);
                    }
                    if (route is null)
                    {
                        Inject(handle, packet, length, address, false);
                        continue;
                    }

                    IpTransport.Rewrite(packet, parsed, route.Server, route.ServerPort, _localIp, route.GamePort);
                    Inject(handle, packet, length, address, true);
                    Interlocked.Increment(ref RedirectedDown);
                }
            }

            if (!_stopping)
            {
                Interlocked.Increment(ref ReceiveErrors);
                _event("local_packet_receive_error", new { layer = kind, error = Marshal.GetLastWin32Error() });
            }
        }
        catch (Exception ex)
        {
            if (!_stopping)
            {
                Interlocked.Increment(ref ReceiveErrors);
                _event("local_packet_worker_error", new { layer = kind, error = ex.Message });
            }
        }
        finally
        {
            if (!_stopping)
            {
                Running = false;
                _stopping = true;
                foreach (var peer in new[] { _forward, _return, _udpHandle })
                {
                    if (peer is { IsClosed: false, IsInvalid: false })
                    {
                        WinDivertNative.Shutdown(peer, 1);
                    }
                }
            }
        }
    }

    private void Inject(DivertHandle handle, byte[] packet, int length, byte[] address, bool changed)
    {
        if (changed && !WinDivertNative.Checksums(packet, (uint)length, address, 0))
        {
            throw new IOException("计算 IP/TCP 校验和失败。");
        }
        if (!WinDivertNative.Send(handle, packet, (uint)length, out uint sent, address) || sent != length)
        {
            int error = Marshal.GetLastWin32Error();
            Interlocked.Increment(ref SendErrors);
            if (SendErrors <= 5)
            {
                _event("local_packet_send_error", new { error, length });
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _stopping = true;
        Running = false;
        foreach (var handle in new[] { _forward, _return, _udpHandle })
        {
            if (handle is { IsClosed: false, IsInvalid: false })
            {
                WinDivertNative.Shutdown(handle, 1);
            }
        }
        try
        {
            await Task.WhenAll(_workers).WaitAsync(TimeSpan.FromSeconds(5));
        }
        catch (TimeoutException)
        {
        }
        foreach (var handle in new[] { _forward, _return, _udpHandle })
        {
            handle?.Dispose();
        }
    }
}
