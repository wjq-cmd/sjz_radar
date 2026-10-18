using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace GatewayCapture.Realtime;

internal sealed class RealtimeOutput : IAsyncDisposable
{
    private readonly TcpListener _listener;
    private readonly CancellationTokenSource _stop = new();
    private readonly ConcurrentDictionary<long, (TcpClient Client, Channel<byte[]> Queue, Task Task)> _clients = new();
    private readonly FileStream? _file;
    private Task? _acceptTask;
    private long _sequence, _clientId;
    private int _disposed;
    private readonly object _publishGate = new();
    private readonly Action<byte[]>? _parser;
    public long ArchiveDropped = 0, SlowClients;
    private long _journalBytes;
    public string? ArchiveError { get; private set; }
    public string? ListenerError { get; private set; }
    public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;
    public int ClientCount => _clients.Count;
    public long Events => Interlocked.Read(ref _sequence);
    public long JournalBytes => Interlocked.Read(ref _journalBytes);
    public RealtimeOutput(string directory, int port, Action<byte[]>? parser = null, bool archive = true)
    {
        _parser = parser;
        _listener = new(IPAddress.Loopback, port);
        if (archive) _file = new FileStream(Path.Combine(directory, "realtime.jsonl"), FileMode.CreateNew, FileAccess.Write, FileShare.Read, 65536, true);

    }
    public void Start() { _listener.Start(16); _acceptTask = AcceptAsync(); }
    public void Publish(string type, object data)
    {
        lock (_publishGate)
        {
        long sequence = Interlocked.Increment(ref _sequence);
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { schema = "gateway-realtime/v4", sequence, timestampMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), type, data }) + "\n");
        if (_file is not null)
        {
            try { _file.Write(bytes); _file.Flush(); Interlocked.Add(ref _journalBytes, bytes.Length); }
            catch (Exception ex) { ArchiveError = ex.Message; throw; }
        }
        _parser?.Invoke(bytes);
        foreach (var pair in _clients.Values)
            if (!pair.Queue.Writer.TryWrite(bytes)) { Interlocked.Increment(ref SlowClients); pair.Queue.Writer.TryComplete(); pair.Client.Dispose(); }
        }
    }
    private async Task AcceptAsync()
    {
        try
        {
            while (!_stop.IsCancellationRequested)
            {
                var client = await _listener.AcceptTcpClientAsync(_stop.Token);
                if (_clients.Count >= 16) { client.Dispose(); continue; }
                client.NoDelay = true;
                long id = Interlocked.Increment(ref _clientId);
                var queue = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(512) { FullMode = BoundedChannelFullMode.Wait, SingleReader = true });
                var task = SendAsync(client, queue);
                queue.Writer.TryWrite(Encoding.UTF8.GetBytes("{\"schema\":\"gateway-realtime/v4\",\"type\":\"stream_ready\",\"data\":{\"encoding\":\"jsonl-utf8\",\"payloadEncoding\":\"base64\",\"replay\":false}}\n"));
                _clients[id] = (client, queue, task);
                _ = task.ContinueWith(completed => { _clients.TryRemove(id, out var removed); }, TaskScheduler.Default);
            }
        }
        catch (OperationCanceledException) { }
        catch (ObjectDisposedException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ListenerError = ex.Message; }
    }
    private async Task SendAsync(TcpClient client, Channel<byte[]> queue)
    {
        using (client)
        {
            try
            {
                await foreach (var bytes in queue.Reader.ReadAllAsync(_stop.Token))
                    await client.GetStream().WriteAsync(bytes, _stop.Token).AsTask().WaitAsync(TimeSpan.FromSeconds(5), _stop.Token);
            }
            catch (Exception) { }
        }
    }
    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _listener.Stop(); _stop.Cancel();
        foreach (var pair in _clients.Values) { pair.Queue.Writer.TryComplete(); pair.Client.Dispose(); }
        if (_acceptTask is not null) try { await _acceptTask; } catch { }
        await Task.WhenAll(_clients.Values.Select(c => c.Task));
        if (_file is not null) await _file.DisposeAsync();
        _stop.Dispose();
    }
}
