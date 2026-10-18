using System.Net;
using System.Text.Json;
using GatewayCapture.Infrastructure;

namespace GatewayCapture.Realtime;

internal sealed class RealtimeSession : IAsyncDisposable
{
    private RealtimeOutput? _output;
    private ParserHost? _parser;
    private UdpKeyBroker? _broker;
    private TcpRelayServer? _relay;
    private HotspotTcpRouter? _router;
    private string? _directory;
    private bool _disposed;
    private readonly CancellationTokenSource _statusStop = new();
    private Task? _statusTask;
    public string? DirectoryPath => _directory;
    public int Port => _output?.Port ?? 0;
    public string? WebUrl => _parser?.Url;
    public void OpenBrowser() => _parser?.OpenBrowser();
    public bool Running => !_disposed && _router?.Running == true;
    private readonly DateTimeOffset _started = DateTimeOffset.Now;
    public object Snapshot => new { running = Running, events = _output?.Events ?? 0, journalBytes = _output?.JournalBytes ?? 0,
        udpUpPackets = _broker?.UpPackets ?? 0, udpDownPackets = _broker?.DownPackets ?? 0,
        udpUpBytes = _broker?.UpBytes ?? 0, udpDownBytes = _broker?.DownBytes ?? 0,
        plainUp = _broker?.PlainUp ?? 0, plainDown = _broker?.PlainDown ?? 0,
        invalidPackets = _broker?.InvalidPackets ?? 0, pendingPackets = _broker?.PendingCount ?? 0, activeTcp = _relay?.ActiveConnections ?? 0, tgcpEstablished = _relay?.Established ?? 0,
        redirectedUp = _router?.RedirectedUp ?? 0, redirectedDown = _router?.RedirectedDown ?? 0, unmappedTcp = _router?.Unmapped ?? 0,
        suppressedOriginalDown = _router?.SuppressedOriginalDown ?? 0,
        bankCandidates = _broker?.CandidateCount ?? 0, udpObserved = _router?.UdpObserved ?? 0, udpPlainPackets = _broker?.PlainPackets ?? 0,
        udpBoundFlows = _broker?.ValidatedFlows ?? 0, outputClients = _output?.ClientCount ?? 0,
        archiveDropped = _output?.ArchiveDropped ?? 0, slowClientsDisconnected = _output?.SlowClients ?? 0,
        archiveError = _output?.ArchiveError, outputListenerError = _output?.ListenerError,
        pendingUdpEvictions = _broker?.PendingEvictions ?? 0, packetSendErrors = _router?.SendErrors ?? 0,
        packetReceiveErrors = _router?.ReceiveErrors ?? 0, parser = _parser?.Snapshot };
    public async Task StartAsync(IPAddress target, string directory, int outputPort = 17890, int webPort = 17891)
    {
        _directory = directory;
        Directory.CreateDirectory(directory);
        File.Delete(Path.Combine(directory, "realtime-result.json"));
        try
        {
            var adapter = HotspotPktMonComponentResolver.FindActiveHotspotAdapter();
            var mask = adapter.Adapter.GetIPProperties().UnicastAddresses.First(u => u.Address.Equals(adapter.Address)).IPv4Mask;
            var targetBytes = target.GetAddressBytes(); var gatewayBytes = adapter.Address.GetAddressBytes(); var maskBytes = mask.GetAddressBytes();
            if (targetBytes.Length != 4 || target.Equals(adapter.Address) || Enumerable.Range(0, 4).Any(i => (targetBytes[i] & maskBytes[i]) != (gatewayBytes[i] & maskBytes[i])))
                throw new ArgumentException("实时设备地址须位于当前热点 IPv4 子网。");
            _output = new(directory, outputPort); _output.Start();
            _parser = new();
            await _parser.StartAsync(Path.Combine(directory, "semantic"), webPort, cacheOnly: true,
                journal: Path.Combine(directory, "realtime.jsonl"));
            _broker = new(directory, _output.Publish);
            _relay = new(adapter.Address, 0, endpoint => _router?.Resolve(endpoint), _output.Publish, _broker.Add);
            _relay.Start();
            _router = new(target, adapter.Address, mask, _relay.Port, adapter.InterfaceIndex, _output.Publish, _broker.Observe);
            var metadata = new { schema = "gateway-session/v5", mode = "realtime-journal", startedAt = _started,
                targetAddress = target.ToString(), gateway = adapter.Address.ToString(), output = $"tcp://127.0.0.1:{Port}",
                framing = "UTF-8 JSONL", payloadEncoding = "base64", tgcpProfile = "mode3-md5-cbc", dhPrivateSource = "fresh-PC-generated", phoneMemoryInput = false,
                web = WebUrl, stateSchema = "gateway-state/v5" };
            await File.WriteAllTextAsync(Path.Combine(directory, "realtime-session.json"), JsonSerializer.Serialize(metadata, new JsonSerializerOptions { WriteIndented = true }));
            _output.Publish("session_started", metadata);
            await _router.StartAsync();
            _statusTask = ReportStatusAsync();
        }
        catch { await DisposeAsync(); throw; }
    }
    private async Task ReportStatusAsync()
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(1));
        try { while (await timer.WaitForNextTickAsync(_statusStop.Token)) _output?.Publish("session_status", Snapshot); }
        catch (OperationCanceledException) when (_statusStop.IsCancellationRequested) { }
    }
    public async ValueTask DisposeAsync()
    {
        if (_disposed) return; _disposed = true;
        var errors = new List<string>();
        _statusStop.Cancel();
        if (_statusTask is not null) try { await _statusTask; } catch (Exception ex) { errors.Add(ex.Message); }
        _statusStop.Dispose();
        if (_relay is not null) try { await _relay.DisposeAsync(); } catch (Exception ex) { errors.Add(ex.Message); }
        if (_router is not null) try { await _router.DisposeAsync(); } catch (Exception ex) { errors.Add(ex.Message); }
        try { _output?.Publish("session_stopped", new { snapshot = Snapshot, errors }); }
        catch (Exception ex) { errors.Add(ex.Message); }
        if (_output is not null) try { await _output.DisposeAsync(); } catch (Exception ex) { errors.Add(ex.Message); }
        if (_parser is not null)
        {
            try { await _parser.DisposeAsync(); } catch (Exception ex) { errors.Add(ex.Message); }
            if (_parser.Error is not null) errors.Add(_parser.Error);
        }
        if (_directory is not null) await File.WriteAllTextAsync(Path.Combine(_directory, "realtime-result.json"),
            JsonSerializer.Serialize(new { startedAt = _started, stoppedAt = DateTimeOffset.Now, snapshot = Snapshot, errors }, new JsonSerializerOptions { WriteIndented = true }));
        if (errors.Count > 0) throw new IOException("实时模式停止时出现错误：" + string.Join("; ", errors));
    }
}
