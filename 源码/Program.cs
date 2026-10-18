using System.Buffers.Binary;
using System.Net;
using System.Net.NetworkInformation;
using System.Text;
using GatewayCapture.Capture;
using GatewayCapture.License;
using GatewayCapture.Realtime;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text.Json;

namespace GatewayCapture;

internal static class Program
{
    private static async Task Main(string[] args)
    {
        if (args.Length == 5 && args[0] == "--cleanup-guardian")
        {
            try { await GatewayCapture.Infrastructure.LifetimeGuard.RunAsync(args); }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.ExitCode = 1; }
            return;
        }
        Console.OutputEncoding = Encoding.UTF8;
        Console.InputEncoding = Encoding.UTF8;
        if (args.Length == 3 && args[0] is "--verify-v5-pipeline" or "--verify-v5-cache-pipeline")
        {
            try { await ParserPipelineFixture.RunAsync(args[1], args[2], args[0] == "--verify-v5-cache-pipeline"); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length > 0 && args[0] is "--verify-v5" or "--replay-v5")
        {
            try
            {
                if (args.Length is < 3 or > 4) throw new ArgumentException("用法: --verify-v5|--replay-v5 <realtime.jsonl> <新输出目录> [网页端口]");
                await RunParserReplayAsync(args[1], args[2], args[0] == "--replay-v5", args.Length == 4 ? int.Parse(args[3]) : 0);
            }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length == 3 && args[0] == "--realtime-loopback" && int.TryParse(args[1], out int fixturePort) && fixturePort is > 0 and <= 65535)
        {
            try { await RealtimeLoopbackFixture.RunAsync(fixturePort, args[2]); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length == 2 && args[0] == "--verify-udp-fixture")
        {
            try { Console.WriteLine(JsonSerializer.Serialize(RealtimeFixtureValidator.VerifyUdp(args[1]))); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length > 0 && args[0] == "--relay-loopback")
        {
            await RunLoopbackRelayAsync(args);
            return;
        }
        if (args.Length > 0 && args[0] is "--self-test-v4" or "--self-test-v5")
        {
            try { GatewayCaptureSelfTest.Run(); await RealtimeSelfTest.RunAsync(); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length == 1 && args[0] is "--extract-nva-parser" or "--extract-parser-v5")
        {
            try
            {
                var path = ParserHost.Executable;
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    path,
                    bytes = new FileInfo(path).Length,
                    sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant()
                }));
            }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length == 1 && args[0] == "--license-info")
        {
            try { LicenseService.PrintDiagnostics(); }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length > 0 && args[0] == "--check-realtime-driver")
        {
            try
            {
                using var handle = WinDivertNative.OpenChecked("false", 0, 5);
                Console.WriteLine("WinDivert 驱动打开成功；筛选器 false，不截获任何流量。句柄已关闭。");
            }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length >= 3 && args[0] == "--probe-local")
        {
            try { await RunLocalProbeAsync(args); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length >= 1 && args[0] == "--probe-loopback")
        {
            try { await RunLoopbackProbeAsync(args); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length >= 3 && args[0] == "--local")
        {
            try { await RunLocalAsync(args); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }
        if (args.Length >= 1 && args[0] == "--local-auto")
        {
            try { await RunLocalAutoAsync(args); }
            catch (Exception ex) { Console.Error.WriteLine(ex); Environment.ExitCode = 1; }
            return;
        }

        if (args.Contains("--self-test", StringComparer.OrdinalIgnoreCase))
        {
            GatewayCaptureSelfTest.Run();
            return;
        }

        if (args.Length > 0 && string.Equals(args[0], "--export-open-udp-records", StringComparison.OrdinalIgnoreCase))
        {
            ExportOpenUdpRecords(args);
            return;
        }

        if (args.Length == 3 && string.Equals(args[0], "--list-udp-flows", StringComparison.OrdinalIgnoreCase))
        {
            if (!IPAddress.TryParse(args[2], out var tabletAddress))
            {
                Console.Error.WriteLine("用法: --list-udp-flows <pcapng> <平板IPv4>");
                Environment.ExitCode = 2;
                return;
            }
            foreach (var flow in PcapNgUdpRecordExporter.ListFlows(args[1], tabletAddress).Take(30))
            {
                Console.WriteLine($"{flow.RemoteAddress}:{flow.RemotePort} up={flow.UpstreamPackets}/{flow.UpstreamBytes} down={flow.DownstreamPackets}/{flow.DownstreamBytes}");
            }
            return;
        }

        if (args.Length > 0 && args[0] is "--recover" or "--export-full-ip")
        {
            try
            {
                if (args[0] == "--recover" && args.Length == 2)
                {
                    Console.WriteLine((await CaptureArchive.RecoverAsync(args[1])).Message);
                }
                else if (args[0] == "--export-full-ip" && args.Length == 4)
                {
                    var directory = Path.GetFullPath(args[2]);
                    Directory.CreateDirectory(directory);
                    var full = Path.Combine(directory, "session.fullip.pcapng");
                    var result = PcapNgDownstreamFilter.FilterTargetSession([args[1]],
                        Path.Combine(directory, "session.udp.pcapng"),
                        Path.Combine(directory, "session.downstream.pcapng"), IPAddress.Parse(args[3]), full);
                    var json = System.Text.Json.JsonSerializer.Serialize(result, new System.Text.Json.JsonSerializerOptions { WriteIndented = true });
                    File.WriteAllText(Path.Combine(directory, "packet-counts.json"), json);
                    Console.WriteLine(json);
                }
                else throw new ArgumentException("用法: --recover <会话目录> 或 --export-full-ip <PCAPNG> <输出目录> <设备IPv4>");
            }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.ExitCode = 1; }
            return;
        }

        var lockDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), ProductInfo.StorageDirectory);
        Directory.CreateDirectory(lockDirectory);
        FileStream applicationLock;
        try { applicationLock = new FileStream(Path.Combine(lockDirectory, "interactive.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
        catch (IOException) { Console.WriteLine($"已有 {ProductInfo.Name} 窗口运行，请在原窗口操作或退出后重试。"); return; }
        using (applicationLock)
        {
            // 卡密只保护交互式主流程；上面的开发/自检命令分支已在更早处 return。
            if (!await LicenseService.EnsureActivatedAsync())
            {
                Environment.ExitCode = 3;
                return;
            }

            var application = new GatewayConsoleApplication();
            await application.RunAsync();
        }
    }
    internal static async Task RunParserReplayAsync(string source, string output, bool showBrowser, int port = 0, CancellationToken cancellation = default)
    {
        if (showBrowser) { await RunTimelineAsync(source, output, cancellation); return; }
        source = Path.GetFullPath(source); output = Path.GetFullPath(output);
        if (!File.Exists(source)) throw new FileNotFoundException("未找到 realtime.jsonl。", source);
        if (File.Exists(Path.Combine(output, "states.jsonl"))) throw new IOException("输出目录已有解析结果，请使用新目录。");
        await using var parser = new ParserHost();
        await parser.StartAsync(output, port, replay: true);
        await parser.SendReplayAsync(source);
        Console.WriteLine($"离线回放已载入：{parser.Url}");
        if (showBrowser)
        {
            parser.OpenBrowser();
            Console.WriteLine("按 Enter 关闭回放并保存结果。");
            await Console.In.ReadLineAsync().WaitAsync(cancellation);
        }
        await parser.DisposeAsync();
        if (parser.Error is not null) throw new IOException(parser.Error);
        if (parser.Result is { } result && result.GetProperty("stats").TryGetProperty("inputErrors", out var inputErrors) && inputErrors.GetInt32() != 0)
            throw new InvalidDataException($"输入包含 {inputErrors.GetInt32()} 条错误记录，详见输出目录中的 parser-errors.jsonl。");
        Console.WriteLine(JsonSerializer.Serialize(new { output, result = parser.Result }));
    }
    internal static async Task<char> RunTimelineAsync(string source, string output, CancellationToken cancellation)
    {
        source = Path.GetFullPath(source); output = Path.GetFullPath(output);
        if (!File.Exists(source)) throw new FileNotFoundException("未找到明文事件归档。", source);
        await using var parser = new ParserHost();
        await parser.StartAsync(output, 17891, replay: true, cacheOnly: true, timeline: source);
        parser.OpenBrowser();
        return await ConsoleDashboard.RunAsync(null, parser, output, cancellation);
    }
    private static async Task RunLoopbackRelayAsync(string[] args)
    {
        if (args.Length != 2 || !int.TryParse(args[1], out int port) || port < 1 || port > 65535)
        { Console.Error.WriteLine("用法: --relay-loopback <本机模拟服务器端口>"); Environment.ExitCode = 2; return; }
        var consoleGate = new object();
        void Report(string type, object data) { lock (consoleGate) Console.WriteLine(JsonSerializer.Serialize(new { type, data })); }
        await using var relay = new TcpRelayServer(IPAddress.Loopback, 0, _ => new(IPAddress.Loopback, port), Report,
            (id, observation) =>
            {
                foreach (var bank in observation.Banks) Report("bank_candidate", new { connection = id, fingerprint = Convert.ToHexString(SHA256.HashData(bank)).ToLowerInvariant(), sequence = observation.Sequence });
            });
        relay.Start(); Report("listening", new { host = "127.0.0.1", port = relay.Port });
        while (await Console.In.ReadLineAsync() is { } line && line.Trim() != "stop") { }
    }

    /// <summary>单机模式与交互式主流程共用同一把单实例锁。</summary>
    private static FileStream? TryAcquireApplicationLock()
    {
        var directory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            ProductInfo.StorageDirectory);
        Directory.CreateDirectory(directory);
        try
        {
            return new FileStream(
                Path.Combine(directory, "interactive.lock"),
                FileMode.OpenOrCreate,
                FileAccess.ReadWrite,
                FileShare.None);
        }
        catch (IOException)
        {
            return null;
        }
    }

    private static async Task RunLocalAutoAsync(string[] args)
    {
        IPAddress? localIp = null;
        if (args.Length > 1 && !IPAddress.TryParse(args[1], out localIp))
        {
            throw new ArgumentException("本机IP 格式不正确。用法: --local-auto [本机IP]");
        }
        if (localIp is null)
        {
            using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            socket.Connect(IPAddress.Parse("8.8.8.8"), 53);
            localIp = ((IPEndPoint)socket.LocalEndPoint!).Address;
        }

        using var applicationLock = TryAcquireApplicationLock();
        if (applicationLock is null)
        {
            Console.WriteLine($"已有 {ProductInfo.Name} 窗口运行，请在原窗口操作或退出后重试。");
            return;
        }
        if (!await LicenseService.EnsureActivatedAsync())
        {
            Environment.ExitCode = 3;
            return;
        }

        var cache = Path.Combine(AppContext.BaseDirectory, "captures",
            $"local-auto-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(cache);

        await using var live = new RealtimeSession();
        await live.StartLocalAsync(localIp, null, 0, cache);

        Console.WriteLine($"本机自动模式：本机 {localIp}");
        Console.WriteLine("拦截本机所有新 TCP；TGCP 自动识别，非 TGCP 原样直通。");
        Console.WriteLine($"实时缓存：{cache}");
        Console.WriteLine($"实时网页：{live.WebUrl}");
        Console.WriteLine("代理已就绪。现在启动游戏。");
        live.OpenBrowser();
        await ConsoleDashboard.RunAsync(live, null, cache, CancellationToken.None);
    }

    private static async Task RunLocalAsync(string[] args)
    {
        if (!IPAddress.TryParse(args[1], out var serverIp) ||
            !int.TryParse(args[2], out var serverPort) ||
            serverPort is < 1 or > 65535)
        {
            throw new ArgumentException(
                "用法: --local <服务器IP> <服务器端口> [本机IP] [UDP服务器IP] [UDP端口]\n" +
                "示例: --local 203.0.113.8 3027");
        }

        IPAddress? localIp = null;
        if (args.Length > 3 && !IPAddress.TryParse(args[3], out localIp))
        {
            throw new ArgumentException("本机IP 格式不正确。");
        }
        if (localIp is null)
        {
            using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            socket.Connect(serverIp, serverPort);
            localIp = ((IPEndPoint)socket.LocalEndPoint!).Address;
        }

        IPAddress? udpIp = null;
        if (args.Length > 4 && !IPAddress.TryParse(args[4], out udpIp))
        {
            throw new ArgumentException("UDP服务器IP 格式不正确。");
        }
        int udpPort = 0;
        if (args.Length > 5 && (!int.TryParse(args[5], out udpPort) || udpPort is < 0 or > 65535))
        {
            throw new ArgumentException("UDP端口 格式不正确。");
        }

        using var applicationLock = TryAcquireApplicationLock();
        if (applicationLock is null)
        {
            Console.WriteLine($"已有 {ProductInfo.Name} 窗口运行，请在原窗口操作或退出后重试。");
            return;
        }
        if (!await LicenseService.EnsureActivatedAsync())
        {
            Environment.ExitCode = 3;
            return;
        }

        var cache = Path.Combine(AppContext.BaseDirectory, "captures",
            $"local-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(cache);

        await using var live = new RealtimeSession();
        await live.StartLocalAsync(localIp, serverIp, serverPort, cache,
            udpServerIp: udpIp, udpServerPort: udpPort);

        Console.WriteLine($"本机模式：{localIp} -> {serverIp}:{serverPort}");
        if (udpIp is not null)
        {
            Console.WriteLine($"UDP 观察：{localIp} <-> {udpIp}:{udpPort}");
        }
        Console.WriteLine($"实时缓存：{cache}");
        Console.WriteLine($"实时网页：{live.WebUrl}");
        Console.WriteLine("代理已就绪。现在重启游戏。");
        live.OpenBrowser();
        await ConsoleDashboard.RunAsync(live, null, cache, CancellationToken.None);
    }

    private static async Task RunLocalProbeAsync(string[] args)
    {
        if (!IPAddress.TryParse(args[1], out var remote) ||
            !ushort.TryParse(args[2], out var remotePort))
        {
            throw new ArgumentException("用法: --probe-local <远端IP> <远端端口> [秒数]");
        }

        var seconds = args.Length > 3 && int.TryParse(args[3], out var value)
            ? Math.Clamp(value, 3, 600)
            : 20;

        var filter = $"ip and (tcp or udp) and (ip.SrcAddr == {remote} or ip.DstAddr == {remote})";
        using var handle = WinDivertNative.OpenChecked(filter, 0, 5); // SNIFF | RECV_ONLY
        using var timer = new Timer(
            _ => WinDivertNative.Shutdown(handle, 1),
            null,
            TimeSpan.FromSeconds(seconds),
            Timeout.InfiniteTimeSpan);

        Console.WriteLine($"layer 0 只读探针：{seconds} 秒，远端 {remote}:{remotePort}");
        Console.WriteLine("现在启动游戏并登录。只观察，不修改任何包。");

        int tcp = 0, udp = 0;
        await Task.Run(() =>
        {
            var packet = new byte[65575];
            var address = new byte[80];
            while (WinDivertNative.Receive(handle, packet, (uint)packet.Length, out uint size, address))
            {
                int length = (int)size;
                if (!IpTransport.Parse(packet, length, out var parsed)) continue;
                if (parsed.SourcePort != remotePort && parsed.DestinationPort != remotePort) continue;

                var outbound = (BinaryPrimitives.ReadUInt32LittleEndian(address.AsSpan(8))
                                & (1u << 17)) != 0;
                if (parsed.Protocol == 6) tcp++; else udp++;

                int headerLength = parsed.Protocol == 6
                    ? (packet[parsed.IpLength + 12] >> 4) * 4
                    : 8;
                int payloadLength = Math.Max(
                    0,
                    Math.Min(parsed.TotalLength, length) - parsed.IpLength - headerLength);
                var preview = payloadLength > 0
                    ? Convert.ToHexString(packet.AsSpan(
                        parsed.IpLength + headerLength, Math.Min(payloadLength, 16)))
                    : "";

                Console.WriteLine(
                    $"[{DateTime.Now:HH:mm:ss.fff}] {(outbound ? "OUT" : "IN ")} " +
                    $"{(parsed.Protocol == 6 ? "TCP" : "UDP")} " +
                    $"{parsed.Source}:{parsed.SourcePort} -> " +
                    $"{parsed.Destination}:{parsed.DestinationPort} " +
                    $"payload={payloadLength} {preview}");
            }
        });

        Console.WriteLine($"统计: TCP={tcp} UDP={udp}");
        Console.WriteLine(tcp + udp > 0
            ? "layer 0 能看到本机流量，可以继续做本机重定向。"
            : "没有看到流量：确认远端 IP/端口填对，且游戏确实在运行。");
    }

    private static async Task RunLoopbackProbeAsync(string[] args)
    {
        var seconds = args.Length > 1 && int.TryParse(args[1], out var value)
            ? Math.Clamp(value, 3, 120)
            : 10;

        var listener = new TcpListener(IPAddress.Any, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        using var handle = WinDivertNative.OpenChecked(
            $"tcp and (tcp.SrcPort == {port} or tcp.DstPort == {port})", 0, 5);
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(seconds));

        long seen = 0;
        var receiver = Task.Run(() =>
        {
            var packet = new byte[65575];
            var address = new byte[80];
            try
            {
                while (WinDivertNative.Receive(handle, packet, (uint)packet.Length, out _, address))
                {
                    Interlocked.Increment(ref seen);
                }
            }
            catch (Exception)
            {
            }
        });

        async Task ProbeAsync(IPAddress target, string label)
        {
            var before = Interlocked.Read(ref seen);
            using var client = new TcpClient();
            var accept = listener.AcceptTcpClientAsync(stop.Token).AsTask();
            await client.ConnectAsync(target, port, stop.Token);
            using var server = await accept.WaitAsync(TimeSpan.FromSeconds(5));
            var clientStream = client.GetStream();
            var serverStream = server.GetStream();
            await clientStream.WriteAsync(new byte[] { 0x41, 0x42, 0x43 }, stop.Token);
            var buffer = new byte[3];
            await serverStream.ReadExactlyAsync(buffer, stop.Token);
            await serverStream.WriteAsync(new byte[] { 0x44, 0x45, 0x46 }, stop.Token);
            await clientStream.ReadExactlyAsync(buffer, stop.Token);
            await Task.Delay(300, stop.Token);
            var after = Interlocked.Read(ref seen);
            Console.WriteLine($"{label}: WinDivert 看到 {after - before} 个包");
        }

        Console.WriteLine($"layer 0 回环探针：本地端口 {port}，{seconds} 秒。");

        await ProbeAsync(IPAddress.Loopback, "127.0.0.1");

        var lan = NetworkInterface.GetAllNetworkInterfaces()
            .Where(item => item.OperationalStatus == OperationalStatus.Up)
            .SelectMany(item => item.GetIPProperties().UnicastAddresses)
            .Select(item => item.Address)
            .FirstOrDefault(item => item.AddressFamily == AddressFamily.InterNetwork
                                    && !IPAddress.IsLoopback(item));
        if (lan is not null)
        {
            await ProbeAsync(lan, $"本机网卡 {lan}");
        }
        else
        {
            Console.WriteLine("没有找到非回环 IPv4 地址，跳过 LAN 探针。");
        }

        WinDivertNative.Shutdown(handle, 1);
        try { await receiver; } catch (Exception) { }
        listener.Stop();

        Console.WriteLine("判读：");
        Console.WriteLine("  127.0.0.1 有包 → 本机中继回程可以被 WinDivert 拦截。");
        Console.WriteLine("  LAN IP 有包   → 用本机网卡地址做重定向也可行。");
        Console.WriteLine("  都是 0        → layer 0 看不到本地回环，需要 WFP/TUN 方案。");
    }

    private static void ExportOpenUdpRecords(string[] args)
    {
        if (args.Length is < 6 or > 7 ||
            !IPAddress.TryParse(args[3], out var tabletAddress) ||
            !IPAddress.TryParse(args[4], out var remoteAddress) ||
            !ushort.TryParse(args[5], out var remotePort))
        {
            Console.Error.WriteLine(
                "用法: --export-open-udp-records <pcapng> <records> <平板IPv4> <服务端IPv4> <服务端端口> [downstream|upstream|both]");
            Environment.ExitCode = 2;
            return;
        }

        var direction = args.Length == 7 ? ParseDirection(args[6]) : UdpFlowDirection.Downstream;
        try
        {
            var result = PcapNgUdpRecordExporter.ExportFlow(
                args[1],
                args[2],
                tabletAddress,
                remoteAddress,
                remotePort,
                direction);
            Console.WriteLine($"输入包: {result.InputPackets}");
            Console.WriteLine($"UDP 包: {result.UdpPackets}");
            Console.WriteLine($"匹配流包: {result.MatchingPackets}");
            Console.WriteLine($"已写入 0x828 记录: {result.WrittenRecords}");
            Console.WriteLine($"超出 0x800 payload 上限而跳过: {result.OversizePackets}");
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"记录导出失败: {exception.Message}");
            Environment.ExitCode = 1;
        }
    }

    private static UdpFlowDirection ParseDirection(string value)
    {
        return value.Trim().ToLowerInvariant() switch
        {
            "down" or "downstream" => UdpFlowDirection.Downstream,
            "up" or "upstream" => UdpFlowDirection.Upstream,
            "both" => UdpFlowDirection.Both,
            _ => throw new ArgumentException("方向只能是 downstream、upstream 或 both。"),
        };
    }
}
