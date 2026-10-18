using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading.Channels;

namespace GatewayCapture.Realtime;

internal sealed class TcpRelayServer : IAsyncDisposable
{
    private readonly TcpListener _listener;
    private readonly Func<IPEndPoint, IPEndPoint?> _resolve;
    private readonly Action<string, object> _event;
    private readonly Action<long, TgcpObservation> _observe;
    private readonly CancellationTokenSource _stop = new();
    private readonly ConcurrentDictionary<long, Task> _tasks = new();
    private readonly ConcurrentDictionary<long, TcpClient> _clients = new();
    private Task? _accept;
    private long _nextId;
    public long Established;
    public long Frames;
    public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;
    public int ActiveConnections => _clients.Count;

    public TcpRelayServer(IPAddress bind, int port, Func<IPEndPoint, IPEndPoint?> resolve,
        Action<string, object> report, Action<long, TgcpObservation> observe)
    {
        _listener = new(bind, port); _resolve = resolve; _event = report; _observe = observe;
    }
    public void Start() { _listener.Start(128); _accept = AcceptAsync(); }
    private async Task AcceptAsync()
    {
        try
        {
            while (!_stop.IsCancellationRequested)
            {
                var client = await _listener.AcceptTcpClientAsync(_stop.Token);
                if (_clients.Count >= 128) { client.Dispose(); continue; }
                long id = Interlocked.Increment(ref _nextId);
                _clients[id] = client;
                var task = RunAsync(id, client); _tasks[id] = task;
                _ = task.ContinueWith(completed => { _tasks.TryRemove(id, out var removed); }, TaskScheduler.Default);
            }
        }
        catch (OperationCanceledException) { }
        catch (ObjectDisposedException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { _event("relay_listener_error", new { error = ex.Message }); _stop.Cancel(); }
    }
    private async Task RunAsync(long id, TcpClient client)
    {
        using (client)
        using (var upstream = new TcpClient(AddressFamily.InterNetwork))
        using (var cancel = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token))
        {
            var token = cancel.Token;
            TgcpSession? session = null;
            var workers = new List<Task>();
            var gate = new SemaphoreSlim(1, 1);
            string mode = "connecting";
            long lastActivity = Environment.TickCount64, firstEof = 0, handshakeStart = 0;
            long bytesUp = 0, bytesDown = 0, observations = 0;
            Task<byte[]>? initialUp = null, initialDown = null;
            try
            {
                var remote = (IPEndPoint)client.Client.RemoteEndPoint!;
                var destination = _resolve(remote) ?? throw new InvalidOperationException("TCP 原目标映射已过期。");
                await upstream.ConnectAsync(destination.Address, destination.Port, token).AsTask().WaitAsync(TimeSpan.FromSeconds(5), token);
                client.NoDelay = upstream.NoDelay = true;
                var input = new[] { client.GetStream(), upstream.GetStream() };
                var sockets = new[] { upstream.Client, client.Client };
                var output = new[] { upstream.GetStream(), client.GetStream() };
                var queues = Enumerable.Range(0, 2).Select(_ => Channel.CreateBounded<byte[]>(new BoundedChannelOptions(32)
                    { SingleReader = true, SingleWriter = false, FullMode = BoundedChannelFullMode.Wait })).ToArray();
                async Task<byte[]> Read(int direction)
                {
                    var data = new byte[65536];
                    int n = await input[direction].ReadAsync(data, token);
                    if (n > 0) Interlocked.Exchange(ref lastActivity, Environment.TickCount64);
                    return data.AsSpan(0, n).ToArray();
                }
                async Task Send(int direction)
                {
                    await foreach (var data in queues[direction].Reader.ReadAllAsync(token))
                    {
                        await output[direction].WriteAsync(data, token).AsTask().WaitAsync(TimeSpan.FromSeconds(5), token);
                        Interlocked.Exchange(ref lastActivity, Environment.TickCount64);
                    }
                    try { sockets[direction].Shutdown(SocketShutdown.Send); }
                    catch (SocketException) { }
                }
                async Task Transform(int direction, byte[] data)
                {
                    if (direction == 0) bytesUp += data.Length; else bytesDown += data.Length;
                    await gate.WaitAsync(token);
                    try
                    {
                        if (session is null) { await queues[direction].Writer.WriteAsync(data, token); return; }
                        int before = session.State;
                        var transformed = session.Feed(direction == 0, data);
                        if (before != 2 && session.State == 2)
                        {
                            Interlocked.Increment(ref Established);
                            _event("tgcp_established", new { connection = id, remote = destination.ToString() });
                        }
                        foreach (var item in transformed.Observations) { observations++; _observe(id, item); }
                        if (transformed.ToServer.Length > 0) await queues[0].Writer.WriteAsync(transformed.ToServer, token);
                        if (transformed.ToClient.Length > 0) await queues[1].Writer.WriteAsync(transformed.ToClient, token);
                    }
                    finally { gate.Release(); }
                }
                initialUp = Read(0); initialDown = Read(1);
                using var prefix = new MemoryStream();
                var deadline = Task.Delay(1000, token);
                string reason;
                while (true)
                {
                    var winner = await Task.WhenAny(initialUp, initialDown, deadline);
                    if (winner == deadline) { token.ThrowIfCancellationRequested(); reason = "prefix_timeout"; break; }
                    if (winner == initialDown) { reason = "server_first"; break; }
                    var data = await initialUp;
                    if (data.Length == 0) { reason = "client_eof"; break; }
                    initialUp = null; prefix.Write(data);
                    int sniff = TgcpProtocol.Sniff(prefix.GetBuffer().AsSpan(0, (int)prefix.Length));
                    if (sniff == 1) { reason = "non_tgcp"; break; }
                    if (prefix.Length > 1024 * 1024) { reason = "prefix_limit"; break; }
                    if (sniff == 2) { reason = "tgcp_mode3"; session = new(); handshakeStart = Environment.TickCount64; break; }
                    if (prefix.Length == 1024 * 1024) { reason = "prefix_limit"; break; }
                    initialUp = Read(0);
                }
                mode = session is null ? "passthrough" : "tgcp";
                _event("tcp_classified", new { connection = id, mode, reason, remote = destination.ToString() });
                workers.Add(Send(0)); workers.Add(Send(1));
                if (prefix.Length > 0) await Transform(0, prefix.ToArray());
                async Task Pump(int direction, Task<byte[]>? initial)
                {
                    var data = await (initial ?? Read(direction));
                    while (data.Length != 0) { await Transform(direction, data); data = await Read(direction); }
                    await gate.WaitAsync(token);
                    try { session?.Finish(direction == 0); queues[direction].Writer.TryComplete(); Interlocked.CompareExchange(ref firstEof, Environment.TickCount64, 0); }
                    finally { gate.Release(); }
                }
                workers.Add(Pump(0, initialUp)); workers.Add(Pump(1, initialDown));
                async Task Watchdog()
                {
                    while (true)
                    {
                        await Task.Delay(200, token); long now = Environment.TickCount64;
                        if (session is not null && session.State != 2 && now - handshakeStart > 6000) throw new TimeoutException("TGCP 握手超时。");
                        if (now - Interlocked.Read(ref lastActivity) > 60000) throw new TimeoutException("TCP 空闲超时。");
                        long eof = Interlocked.Read(ref firstEof);
                        if (eof != 0 && now - eof > 5000) throw new TimeoutException("TCP 半关闭超时。");
                    }
                }
                workers.Add(Watchdog());
                var pending = workers.Take(4).ToHashSet(); var watchdog = workers[4];
                while (pending.Count != 0)
                {
                    var completed = await Task.WhenAny(pending.Append(watchdog));
                    await completed; pending.Remove(completed);
                }
                _event("tcp_closed", new { connection = id, mode, frames = session?.Frames ?? 0, observations, bytesUp, bytesDown });
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
            catch (Exception ex) { _event("tcp_error", new { connection = id, mode, error = ex.Message }); }
            finally
            {
                cancel.Cancel(); client.Close(); upstream.Close();
                var all = workers.Concat(new[] { initialUp, initialDown }.OfType<Task>()).ToArray();
                try { await Task.WhenAll(all); } catch { }
                if (session is not null) { Interlocked.Add(ref Frames, session.Frames); session.Dispose(); }
                gate.Dispose(); _clients.TryRemove(id, out _);
            }
        }
    }
    public async ValueTask DisposeAsync()
    {
        _stop.Cancel(); _listener.Stop();
        foreach (var client in _clients.Values) client.Dispose();
        if (_accept is not null) try { await _accept; } catch { }
        await Task.WhenAll(_tasks.Values); _stop.Dispose();
    }
}
