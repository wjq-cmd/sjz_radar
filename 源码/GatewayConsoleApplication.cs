using System.Net;
using System.Net.Sockets;
using GatewayCapture.Capture;
using GatewayCapture.Infrastructure;
using GatewayCapture.Models;
using GatewayCapture.Realtime;
using System.Text.Json;

namespace GatewayCapture;

internal sealed class GatewayConsoleApplication
{
    private readonly SystemProbe _probe = new();
    private readonly WindowsTetheringController _tethering = new();
    private readonly PktMonCaptureService _capture = new();
    private RealtimeSession? _realtime;
    private bool _exitRequested;
    private bool _restartRequested;
    private readonly CancellationTokenSource _shutdown = new();

    public async Task RunAsync()
    {
        ConsoleCancelEventHandler cancelHandler = (_, e) => { e.Cancel = true; _shutdown.Cancel(); };
        Console.CancelKeyPress += cancelHandler;
        try
        {
            while (!_exitRequested && !_shutdown.IsCancellationRequested)
            {
                PrintBanner();
                var mode = await ChooseModeAsync();
                if (mode == 'q') break;

                if (mode == '2')
                {
                    try { await RunLocalModeAsync(_shutdown.Token); }
                    catch (OperationCanceledException) when (_shutdown.IsCancellationRequested) { throw; }
                    catch (Exception ex) { Console.WriteLine("单机模式失败：" + ex.Message); }
                    if (_exitRequested) break;
                    continue;
                }

                await StartCaptureAsync(true);
                if (_exitRequested || _shutdown.IsCancellationRequested) break;
                if (_restartRequested)
                {
                    _restartRequested = false;
                    continue;
                }
                Console.WriteLine();
                Console.Write("按 Enter 重新选择设备，输入 Q 退出: ");
                if (string.Equals((await ReadLineAsync())?.Trim(), "q", StringComparison.OrdinalIgnoreCase))
                    break;
            }
        }
        catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
        {
            Console.WriteLine("正在停止并保存会话...");
        }
        finally
        {
            Console.CancelKeyPress -= cancelHandler;
            try { if (_realtime is not null) await _realtime.DisposeAsync(); }
            catch (Exception ex) { Console.WriteLine("实时停止信息：" + ex.Message); }
            finally { await _capture.DisposeAsync(); _shutdown.Dispose(); }
        }
    }

    private static void PrintBanner()
    {
        ClearConsole();
        Console.WriteLine("============================================================");
        Console.WriteLine($" {ProductInfo.Name} {ProductInfo.Version} - 实时解析 / 网页 2D + 3D");
        Console.WriteLine(" 支持双机（热点）与单机（本机游戏）两种模式");
        Console.WriteLine("============================================================");
        Console.WriteLine();
    }

    private async Task<char> ChooseModeAsync()
    {
        while (!_shutdown.IsCancellationRequested)
        {
            Console.WriteLine("[1] 双机模式：手机 / 第二台电脑连接本机热点");
            Console.WriteLine("[2] 单机模式：本机运行游戏，自动识别 TGCP");
            Console.WriteLine("[Q] 退出");
            Console.Write("请选择（直接回车 = 单机模式）: ");
            var input = (await ReadLineAsync())?.Trim().ToLowerInvariant();
            if (input is null or "" or "2") return '2';
            if (input == "1") return '1';
            if (input == "q") return 'q';
            Console.WriteLine("选择无效，请重新输入。");
            Console.WriteLine();
        }
        return 'q';
    }

    private async Task RunLocalModeAsync(CancellationToken token)
    {
        IPAddress localIp;
        using (var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
        {
            socket.Connect(IPAddress.Parse("8.8.8.8"), 53);
            localIp = ((IPEndPoint)socket.LocalEndPoint!).Address;
        }

        var cache = Path.Combine(AppContext.BaseDirectory, "captures",
            $"local-auto-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(cache);

        var live = new RealtimeSession();
        try
        {
            await live.StartLocalAsync(localIp, null, 0, cache);
            _realtime = live;
        }
        catch
        {
            await live.DisposeAsync();
            throw;
        }

        Console.WriteLine();
        Console.WriteLine($"单机模式：本机 {localIp}");
        Console.WriteLine("拦截本机所有新 TCP；TGCP 自动识别，非 TGCP 原样直通。");
        Console.WriteLine($"实时缓存：{cache}");
        Console.WriteLine($"实时网页：{live.WebUrl}");
        Console.WriteLine("代理已就绪。现在启动游戏。");
        live.OpenBrowser();

        var command = await ConsoleDashboard.RunAsync(live, null, cache, token);
        await StopCaptureAsync();
        _exitRequested = command == 'q';
    }

    private async Task SettingsAsync()
    {
        Console.WriteLine("[1] 环境检查  [2] 热点状态  [3] 开热点  [4] 关热点  [8] 恢复导出  [0] 返回");
        switch ((await ReadLineAsync())?.Trim())
        {
            case "1": ShowPreflight(); break;
            case "2": await ShowTetheringAsync(); break;
            case "3": await StartHotspotAsync(); break;
            case "4": await StopHotspotAsync(); break;
            case "8": await RecoverCaptureAsync(); break;
        }
        await PauseAsync();
    }

    private void ShowPreflight()
    {
        var report = _probe.Collect();
        Console.WriteLine($"管理员权限: {(report.IsAdministrator ? "是" : "否")}");
        Console.WriteLine($"Windows PktMon: {(report.HasPktMon ? "可用" : "缺失")}");
        Console.WriteLine($"WinDivert 实时组件: {(File.Exists(Path.Combine(AppContext.BaseDirectory, "WinDivert.dll")) && File.Exists(Path.Combine(AppContext.BaseDirectory, "WinDivert64.sys")) ? "文件齐全" : "缺失（模式 2 仍可用）")}");
        Console.WriteLine($"V5 解析后台: {(File.Exists(ParserHost.Executable) ? "文件齐全" : "缺失（模式 2 仍可用）")}");
        Console.WriteLine($"物理无线网卡: {(report.HasWirelessAdapter ? "已发现" : "未发现")}");
        Console.WriteLine($"Wi-Fi Direct 虚拟适配器: {(report.HasWifiDirectAdapter ? "已发现" : "未发现")}");
        Console.WriteLine("当前启用的网络接口:");
        foreach (var adapter in report.ActiveAdapters)
        {
            Console.WriteLine($"  - {adapter}");
        }

        if (!report.IsAdministrator)
        {
            Console.WriteLine("请以管理员身份运行本程序；热点控制和 PktMon 均需要该权限。");
        }
    }

    private async Task ShowTetheringAsync()
    {
        PrintTetheringStatus(await _tethering.GetStatusAsync());
    }

    private async Task StartHotspotAsync()
    {
        PrintTetheringStatus(await _tethering.StartAsync());
    }

    private async Task StopHotspotAsync()
    {
        if (_capture.Snapshot.IsRunning || _realtime is not null)
        {
            Console.WriteLine("请先停止记录，避免关闭热点时丢失尚未导出的会话。");
            return;
        }

        PrintTetheringStatus(await _tethering.StopAsync());
    }

    private async Task StartCaptureAsync(bool realtime)
    {
        if (_capture.Snapshot.IsRunning || _realtime is not null)
        {
            Console.WriteLine("记录已经在运行。");
            return;
        }

        var tethering = await _tethering.GetStatusAsync();
        if (!tethering.IsRunning)
        {
            Console.WriteLine("尚未开启热点。");
            return;
        }

        var devices = await DiscoverHotspotClientsAsync();
        if (devices.Count == 0)
        {
            Console.WriteLine("没有发现热点客户端。请确认平板已连接并产生过网络流量。");
            Console.Write("也可以直接输入平板 IPv4 地址（直接回车取消）: ");
            var fallback = (await ReadLineAsync())?.Trim();
            if (string.IsNullOrWhiteSpace(fallback))
            {
                return;
            }
            if (!IPAddress.TryParse(fallback, out var fallbackAddress) ||
                fallbackAddress.AddressFamily != AddressFamily.InterNetwork)
            {
                Console.WriteLine("请输入有效的 IPv4 地址，例如 192.168.137.2。");
                return;
            }

            await StartCaptureWithAddressAsync(fallbackAddress, realtime);
            return;
        }

        var historicalOnly = devices.All(device => device.State.StartsWith("历史邻居", StringComparison.Ordinal));
        Console.WriteLine(historicalOnly
            ? "热点历史邻居（实时接口不可用，列表中的设备可能已断开）:"
            : "热点当前在线设备:");
        for (var index = 0; index < devices.Count; index++)
        {
            var device = devices[index];
            Console.WriteLine($"[{index + 1}] {device.Name}  {device.Address}  ({device.MacAddress}, {device.State})");
        }

        Console.Write("输入设备编号（也可直接输入 IPv4）: ");
        var selection = (await ReadLineAsync())?.Trim();
        IPAddress? targetAddress = null;
        if (int.TryParse(selection, out var deviceIndex) &&
            deviceIndex >= 1 && deviceIndex <= devices.Count)
        {
            targetAddress = devices[deviceIndex - 1].Address;
        }
        else if (IPAddress.TryParse(selection, out var typedAddress) &&
                 typedAddress.AddressFamily == AddressFamily.InterNetwork)
        {
            targetAddress = typedAddress;
        }

        if (targetAddress is null)
        {
            Console.WriteLine("选择无效，请输入列表中的编号或有效的 IPv4 地址。");
            return;
        }

        await StartCaptureWithAddressAsync(targetAddress, realtime);
    }

    private async Task<IReadOnlyList<HotspotClientDevice>> DiscoverHotspotClientsAsync()
    {
        try
        {
            return await HotspotClientDiscovery.DiscoverAsync();
        }
        catch (Exception exception)
        {
            Console.WriteLine($"枚举热点客户端失败：{exception.Message}");
            return Array.Empty<HotspotClientDevice>();
        }
    }

    private async Task StartCaptureWithAddressAsync(IPAddress targetAddress, bool realtime)
    {
        if (realtime)
        {
            try
            {
                var cache = Path.Combine(AppContext.BaseDirectory, "captures", $"live-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
                Directory.CreateDirectory(cache);
                var live = new RealtimeSession();
                try { await live.StartAsync(targetAddress, cache); _realtime = live; }
                catch { await live.DisposeAsync(); throw; }
                Console.WriteLine($"实时缓存：{cache}（完整明文事件、Key 和待解密记录均保留）");
                Console.WriteLine($"明文端口：{live.Port}；实时网页：{live.WebUrl}");
                live.OpenBrowser();
                Console.WriteLine("代理已就绪。现在重启测试应用。");
                char command = await ConsoleDashboard.RunAsync(live, null, cache, _shutdown.Token);
                await StopCaptureAsync();
                _exitRequested = command == 'q';
                _restartRequested = command == 's';
            }
            catch (Exception ex) { Console.WriteLine("实时启动失败：" + ex.Message); }
            return;
        }
        var defaultDirectory = Path.Combine(AppContext.BaseDirectory, "captures");
        Console.Write($"输出目录（直接回车使用 {defaultDirectory}）: ");
        var outputDirectory = (await ReadLineAsync())?.Trim();
        if (string.IsNullOrWhiteSpace(outputDirectory))
        {
            outputDirectory = defaultDirectory;
        }

        if (!realtime) Console.Write("采集范围：1=全 IPv4（推荐），2=仅 UDP（回车选 1）: ");
        var mode = realtime ? "1" : (await ReadLineAsync())?.Trim();
        if (mode is not (null or "" or "1" or "2"))
        {
            Console.WriteLine("范围选择无效。");
            return;
        }
        var udpOnly = mode == "2";
        Console.WriteLine("全 IPv4 包含 TCP/UDP/ICMP 和 IP 分片；此模式不覆盖 IPv6。");
        var maxMiB = 512;
        if (!realtime)
        {
            Console.Write("每个 ETL 分段大小 MiB（默认 512，满后新建文件，保留早期数据）: ");
            var sizeInput = (await ReadLineAsync())?.Trim();
            maxMiB = int.TryParse(sizeInput, out var parsed) && parsed >= 64 && parsed <= 4096 ? parsed : 512;
        }
        try
        {
            var snapshot = await _capture.StartAsync(
                new CaptureRequest(targetAddress, outputDirectory, maxMiB, udpOnly));
            Console.WriteLine($"已开始记录：目标={snapshot.TargetAddress}，目录={snapshot.OutputDirectory}");
            Console.WriteLine($"采集位置：{snapshot.Detail}");
            Console.WriteLine("记录已启动。现在可打开测试应用并进行登录、匹配和进入对局。");
            char command = await ConsoleDashboard.RunCaptureAsync(_capture, _shutdown.Token);
            await StopCaptureAsync();
            _exitRequested = command == 'q';
        }
        catch (Exception exception)
        {
            Console.WriteLine($"无法开始记录：{exception.Message}");
        }
    }

    private async Task StopCaptureAsync()
    {
        try
        {
            if (_realtime is not null)
            {
                var live = _realtime; _realtime = null;
                try { await live.DisposeAsync(); }
                catch (Exception ex) { Console.WriteLine("实时模式清理信息：" + ex.Message); }
            }
            if (!_capture.Snapshot.IsRunning) { Console.WriteLine("实时模式已停止，当前缓存已保存。"); return; }
            var result = await _capture.StopAsync();
            Console.WriteLine(result.Message);
            if (result.Succeeded)
            {
                Console.WriteLine($"完整 IPv4 PCAPNG: {result.FullIpPcapPath}");
                Console.WriteLine($"双向 UDP PCAPNG: {result.SessionPcapPath}");
                Console.WriteLine($"下行 PCAPNG: {result.PcapPath}");
                Console.WriteLine($"清单:   {result.ManifestPath}");
                Console.WriteLine($"候选包: {result.InputPackets}，会话包: {result.SessionPackets}，字节: {result.SessionBytes}");
                Console.WriteLine($"保留下行 UDP 包: {result.DownstreamPackets}，字节: {result.DownstreamBytes}");
                Console.WriteLine($"截断包: {result.TruncatedPackets}；原始 ETL/PCAPNG 均保留在会话 raw 目录。");
            }
        }
        catch (Exception exception)
        {
            Console.WriteLine($"导出失败：{exception.Message}");
        }
    }

    private async Task RecoverCaptureAsync()
    {
        Console.Write("已停止的会话目录（包含 capture-state.json）: ");
        var directory = (await ReadLineAsync())?.Trim().Trim('"');
        if (string.IsNullOrWhiteSpace(directory)) return;
        try
        {
            var result = await CaptureArchive.RecoverAsync(directory);
            Console.WriteLine(result.Message);
            Console.WriteLine($"完整 IPv4 PCAPNG: {result.FullIpPcapPath}");
        }
        catch (Exception ex) { Console.WriteLine(ex.Message); }
    }

    private async Task ReplayAsync()
    {
        if (_capture.Snapshot.IsRunning || _realtime is not null) { Console.WriteLine("请先停止当前采集，再打开离线回放。"); return; }
        Console.Write("已有 realtime.jsonl 文件路径: ");
        var source = (await ReadLineAsync())?.Trim().Trim('"');
        if (string.IsNullOrWhiteSpace(source)) return;
        var output = Path.Combine(AppContext.BaseDirectory, "replays", $"replay-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}");
        try { _exitRequested = await Program.RunTimelineAsync(source, output, _shutdown.Token) == 'q'; }
        catch (OperationCanceledException) when (_shutdown.IsCancellationRequested) { throw; }
        catch (Exception ex) { Console.WriteLine("回放失败：" + ex.Message); }
    }

    private async Task ShowStatusAsync()
    {
        var tethering = await _tethering.GetStatusAsync();
        var capture = _capture.Snapshot;
        Console.WriteLine($"热点: {(tethering.IsRunning ? "运行中" : "未运行")}，上游={tethering.ProfileName}，状态={tethering.Detail}");
        Console.WriteLine($"记录: {(capture.IsRunning ? "运行中" : "未运行")}，目标={capture.TargetAddress?.ToString() ?? "-"}");
        if (capture.IsRunning)
        {
            Console.WriteLine($"开始时间: {capture.StartedAt:yyyy-MM-dd HH:mm:ss}，输出目录={capture.OutputDirectory}");
        }
        if (_realtime is not null) Console.WriteLine("实时解析: " + JsonSerializer.Serialize(_realtime.Snapshot));
    }

    private async Task StopCaptureIfNeededAsync()
    {
        if (_capture.Snapshot.IsRunning || _realtime is not null)
        {
            Console.WriteLine("正在停止记录并导出文件...");
            await StopCaptureAsync();
        }
    }

    private static void PrintTetheringStatus(TetheringStatus status)
    {
        Console.WriteLine($"上游连接: {status.ProfileName}");
        Console.WriteLine($"移动热点可用: {(status.IsAvailable ? "是" : "否")}");
        Console.WriteLine($"移动热点状态: {(status.IsRunning ? "运行中" : "未运行")}");
        Console.WriteLine($"详情: {status.Detail}");
    }

    private async Task PauseAsync()
    {
        Console.WriteLine();
        Console.Write("按 Enter 返回菜单...");
        await ReadLineAsync();
        ClearConsole();
    }

    private Task<string?> ReadLineAsync() => Task.Run(Console.ReadLine).WaitAsync(_shutdown.Token);

    private static void ClearConsole()
    {
        if (!Console.IsOutputRedirected)
        {
            Console.Clear();
        }
    }
}
