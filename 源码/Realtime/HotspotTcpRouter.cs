using System.Buffers.Binary;
using System.Net;
using System.Runtime.InteropServices;
using GatewayCapture.Infrastructure;

namespace GatewayCapture.Realtime;

internal readonly record struct IpTransport(IPAddress Source, IPAddress Destination, int SourcePort, int DestinationPort, int IpLength, int TotalLength, int TransportLength, byte Protocol)
{
    public static bool Parse(byte[] packet, int size, out IpTransport parsed)
    {
        parsed = default;
        if (size < 20 || packet[0] >> 4 != 4) return false;
        int ipLength = (packet[0] & 15) * 4, total = BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(2));
        if (ipLength < 20 || total < ipLength + 8 || total > size || (BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(6)) & 0x3fff) != 0) return false;
        byte protocol = packet[9];
        if (protocol is not (6 or 17)) return false;
        if (protocol == 6 && total < ipLength + 20) return false;
        int length = protocol == 6 ? (packet[ipLength + 12] >> 4) * 4 : 8;
        if (protocol == 6 && (total < ipLength + 20 || length < 20) || total < ipLength + length) return false;
        parsed = new(new IPAddress(packet.AsSpan(12, 4)), new IPAddress(packet.AsSpan(16, 4)),
            BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(ipLength)), BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(ipLength + 2)), ipLength, total, length, protocol);
        return true;
    }
    public static void Rewrite(byte[] packet, IpTransport parsed, IPAddress source, int sourcePort, IPAddress destination, int destinationPort)
    {
        source.GetAddressBytes().CopyTo(packet, 12); destination.GetAddressBytes().CopyTo(packet, 16);
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(parsed.IpLength), (ushort)sourcePort);
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(parsed.IpLength + 2), (ushort)destinationPort);
    }
}

internal sealed class HotspotTcpRouter : IAsyncDisposable
{
    internal sealed record Route(IPEndPoint Client, IPEndPoint Remote, uint SynSequence, int AliasPort)
    { public long LastSeen = Environment.TickCount64; }
    private readonly IPAddress _phone, _gateway;
    private readonly uint _subnetMask;
    private readonly int _listenerPort, _hotspotInterfaceIndex;
    private readonly Action<string, object> _event;
    private readonly Action<UdpPacket> _udp;
    private readonly object _gate = new();
    private readonly Dictionary<string, Route> _flows = [];
    private readonly Dictionary<int, Route> _aliases = [];
    private int _nextAlias = 20000;
    private DivertHandle? _forward, _network, _udpHandle;
    private readonly List<Task> _workers = [];
    private volatile bool _stopping;
    private string? _firewallRule;
    private LifetimeGuard? _guard;
    public long RedirectedUp, RedirectedDown, Unmapped, UdpObserved, SendErrors, ReceiveErrors, SuppressedOriginalDown;
    public bool Running { get; private set; }

    public HotspotTcpRouter(IPAddress phone, IPAddress gateway, IPAddress mask, int listenerPort, int hotspotInterfaceIndex, Action<string, object> report, Action<UdpPacket> udp)
    {
        if (hotspotInterfaceIndex <= 0) throw new ArgumentOutOfRangeException(nameof(hotspotInterfaceIndex));
        _phone = phone; _gateway = gateway; _subnetMask = BinaryPrimitives.ReadUInt32BigEndian(mask.GetAddressBytes());
        _listenerPort = listenerPort; _hotspotInterfaceIndex = hotspotInterfaceIndex; _event = report; _udp = udp;
    }
    internal static void SetInboundInterface(byte[] address, int interfaceIndex)
    {
        uint bits = BinaryPrimitives.ReadUInt32LittleEndian(address.AsSpan(8));
        bits &= ~((uint)0xff | 1u << 17 | 1u << 18);
        BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(8), bits);
        BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(16), checked((uint)interfaceIndex));
        BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(20), 0);
    }
    public string ForwardFilter => $"ip and tcp and ((ip.SrcAddr == {_phone} and ip.DstAddr != {_gateway}) or (ip.DstAddr == {_phone} and ip.SrcAddr != {_gateway}))";
    public string ReturnFilter => $"outbound and ip and tcp and ip.SrcAddr == {_gateway} and tcp.SrcPort == {_listenerPort} and ip.DstAddr == {_phone}";
    public string UdpFilter => $"ip and udp and (ip.SrcAddr == {_phone} or ip.DstAddr == {_phone})";
    private bool IsLocal(IPAddress address) => (BinaryPrimitives.ReadUInt32BigEndian(address.GetAddressBytes()) & _subnetMask) == (BinaryPrimitives.ReadUInt32BigEndian(_gateway.GetAddressBytes()) & _subnetMask);
    private static string Key(IPEndPoint client, IPEndPoint remote) => $"{client}>{remote}";
    internal Route? MapForward(IpTransport parsed, byte flags, uint sequence)
    {
        if (IsLocal(parsed.Destination)) return null;
        lock (_gate)
        {
            var client = new IPEndPoint(parsed.Source, parsed.SourcePort); var remote = new IPEndPoint(parsed.Destination, parsed.DestinationPort);
            string key = Key(client, remote); bool syn = (flags & 0x12) == 0x02;
            if (_flows.TryGetValue(key, out var route) && (!syn || route.SynSequence == sequence)) { route.LastSeen = Environment.TickCount64; return route; }
            if (!syn || _stopping) return null;
            // Retain recent aliases for delayed FIN/RST packets, but expire
            // idle mappings and never reuse a live alias.
            foreach (var old in _aliases.Values.Where(r => Environment.TickCount64 - r.LastSeen > 600000).ToArray())
            {
                _aliases.Remove(old.AliasPort); string oldKey = Key(old.Client, old.Remote);
                if (_flows.TryGetValue(oldKey, out var current) && ReferenceEquals(current, old)) _flows.Remove(oldKey);
            }
            if (_aliases.Count >= 8192) return null;
            int alias;
            do { alias = _nextAlias++; if (_nextAlias > 59999) _nextAlias = 20000; } while (_aliases.ContainsKey(alias));
            route = new(client, remote, sequence, alias); _aliases[alias] = route; _flows[key] = route; return route;
        }
    }
    public IPEndPoint? Resolve(IPEndPoint endpoint)
    {
        lock (_gate)
        {
            if (!endpoint.Address.Equals(_phone) || !_aliases.TryGetValue(endpoint.Port, out var route)) return null;
            route.LastSeen = Environment.TickCount64; return route.Remote;
        }
    }
    internal Route? MapReturn(int alias)
    {
        lock (_gate) { if (!_aliases.TryGetValue(alias, out var route)) return null; route.LastSeen = Environment.TickCount64; return route; }
    }
    internal bool IsOriginalDownstream(IpTransport parsed)
    {
        if (!parsed.Destination.Equals(_phone)) return false;
        lock (_gate)
            return _flows.ContainsKey(Key(new(parsed.Destination, parsed.DestinationPort), new(parsed.Source, parsed.SourcePort)));
    }
    public async Task StartAsync()
    {
        try
        {
            // Firewall rule is limited to this phone, gateway address, and
            // ephemeral listener; it is removed by this instance on stop.
            _firewallRule = "NvaLocalParser-v1-" + Guid.NewGuid().ToString("N");
            _guard = await LifetimeGuard.StartAsync("firewall", _firewallRule);
            var rule = await ProcessRunner.RunAsync("netsh.exe", ["advfirewall", "firewall", "add", "rule", $"name={_firewallRule}", "dir=in", "action=allow", "protocol=TCP", $"localip={_gateway}", $"remoteip={_phone}", $"localport={_listenerPort}", "profile=any"]);
            if (!rule.Succeeded) throw new IOException("添加本次代理的临时防火墙规则失败：" + rule.StandardError + rule.StandardOutput);
            _network = WinDivertNative.OpenChecked(ReturnFilter, 0);
            _udpHandle = WinDivertNative.OpenChecked(UdpFilter, 1, 5);
            _forward = WinDivertNative.OpenChecked(ForwardFilter, 1);
            Running = true;
            foreach (int kind in new[] { 0, 1, 2 }) _workers.Add(Task.Factory.StartNew(() => ReceiveLoop(kind), CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default));
            _event("hotspot_proxy_ready", new { phone = _phone.ToString(), gateway = _gateway.ToString(), hotspotInterfaceIndex = _hotspotInterfaceIndex, listenerPort = _listenerPort, firewallRule = _firewallRule });
        }
        catch { await DisposeAsync(); throw; }
    }
    private void Inject(DivertHandle handle, byte[] packet, int length, byte[] address, bool changed)
    {
        if (changed && !WinDivertNative.Checksums(packet, (uint)length, address, 0)) throw new IOException("计算 IP/TCP 校验和失败。");
        if (!WinDivertNative.Send(handle, packet, (uint)length, out uint sent, address) || sent != length)
        {
            int error = Marshal.GetLastWin32Error(); Interlocked.Increment(ref SendErrors);
            if (SendErrors <= 5) _event("packet_send_error", new { error, length });
        }
    }
    private void ReceiveLoop(int kind)
    {
        var handle = kind == 0 ? _forward! : kind == 1 ? _network! : _udpHandle!;
        var packet = new byte[65575]; var address = new byte[80];
        try
        {
            while (WinDivertNative.Receive(handle, packet, (uint)packet.Length, out uint size, address))
            {
                int length = (int)size;
                if (!IpTransport.Parse(packet, length, out var parsed))
                { if (kind != 2) Inject(handle, packet, length, address, false); continue; }
                if (kind == 2)
                {
                    if (_stopping) continue;
                    int udpLength = BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(parsed.IpLength + 4));
                    if (udpLength < 8 || parsed.IpLength + udpLength > parsed.TotalLength) continue;
                    bool up = parsed.Source.Equals(_phone);
                    var local = new IPEndPoint(up ? parsed.Source : parsed.Destination, up ? parsed.SourcePort : parsed.DestinationPort);
                    var remote = new IPEndPoint(up ? parsed.Destination : parsed.Source, up ? parsed.DestinationPort : parsed.SourcePort);
                    Interlocked.Increment(ref UdpObserved);
                    _udp(new(up, local, remote, packet.AsSpan(parsed.IpLength + 8, udpLength - 8).ToArray(), DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()));
                }
                else if (kind == 0)
                {
                    if (parsed.Destination.Equals(_phone))
                    {
                        // ICS can forward the original pre-NAT SYN despite
                        // the upstream diversion. Consume the translated
                        // original replies only for routes owned by us. The
                        // proxy's local NETWORK replies do not use FORWARD.
                        if (IsOriginalDownstream(parsed)) { Interlocked.Increment(ref SuppressedOriginalDown); continue; }
                        Inject(handle, packet, length, address, false); continue;
                    }
                    var route = MapForward(parsed, packet[parsed.IpLength + 13], BinaryPrimitives.ReadUInt32BigEndian(packet.AsSpan(parsed.IpLength + 4)));
                    if (route is null) { Interlocked.Increment(ref Unmapped); Inject(handle, packet, length, address, false); continue; }
                    IpTransport.Rewrite(packet, parsed, _phone, route.AliasPort, _gateway, _listenerPort);
                    // At FORWARD, IfIdx identifies the egress interface (the
                    // upstream adapter), not where the phone packet arrived.
                    // Inject on the interface owning the local listener IP.
                    SetInboundInterface(address, _hotspotInterfaceIndex);
                    Inject(_network!, packet, length, address, true); Interlocked.Increment(ref RedirectedUp);
                }
                else
                {
                    var route = MapReturn(parsed.DestinationPort);
                    if (route is null) { Inject(handle, packet, length, address, false); continue; }
                    IpTransport.Rewrite(packet, parsed, route.Remote.Address, route.Remote.Port, route.Client.Address, route.Client.Port);
                    uint bits = BinaryPrimitives.ReadUInt32LittleEndian(address.AsSpan(8)); bits |= 1u << 17;
                    BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(8), bits);
                    Inject(handle, packet, length, address, true); Interlocked.Increment(ref RedirectedDown);
                }
            }
            if (!_stopping) { Interlocked.Increment(ref ReceiveErrors); _event("packet_receive_error", new { layer = kind, error = Marshal.GetLastWin32Error() }); }
        }
        catch (Exception ex) { if (!_stopping) { Interlocked.Increment(ref ReceiveErrors); _event("packet_worker_error", new { layer = kind, error = ex.Message }); } }
        finally
        {
            if (!_stopping)
            {
                Running = false; _stopping = true;
                foreach (var peer in new[] { _forward, _network, _udpHandle })
                    if (peer is { IsClosed: false, IsInvalid: false }) WinDivertNative.Shutdown(peer, 1);
            }
        }
    }
    public async ValueTask DisposeAsync()
    {
        _stopping = true; Running = false;
        foreach (var handle in new[] { _forward, _network, _udpHandle })
            if (handle is { IsClosed: false, IsInvalid: false }) WinDivertNative.Shutdown(handle, 1);
        try { await Task.WhenAll(_workers).WaitAsync(TimeSpan.FromSeconds(5)); } catch (TimeoutException) { }
        foreach (var handle in new[] { _forward, _network, _udpHandle }) handle?.Dispose();
        if (_firewallRule is not null)
        {
            var removed = await ProcessRunner.RunAsync("netsh.exe", ["advfirewall", "firewall", "delete", "rule", $"name={_firewallRule}"]);
            if (!removed.Succeeded) _event("firewall_cleanup_error", new { rule = _firewallRule, error = removed.StandardError });
            if (removed.Succeeded) { _guard?.Dispose(); _guard = null; }
            _firewallRule = null;
        }
    }
}
