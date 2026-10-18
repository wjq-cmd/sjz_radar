using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace GatewayCapture.Realtime;

// The parser receives the same ordered events as the archive, before capture starts.
// Its pipe is isolated from WinDivert's forwarding threads.
internal sealed class ParserHost : IAsyncDisposable
{
    private readonly Channel<byte[]> _input = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(8192)
        { SingleReader = true, FullMode = BoundedChannelFullMode.Wait });
    private readonly TaskCompletionSource<int> _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _drained = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Process? _process;
    private Task? _pump, _stdout, _stderr;
    private StreamWriter? _log;
    private bool _disposed;
    private long _sent, _dropped;
    private string? _error;
    private JsonElement? _result;
    public static string Executable => EmbeddedParserExecutable.EnsureAvailable();
    public int Port { get; private set; }
    public string Url => $"http://127.0.0.1:{Port}/";
    public string? Error => _error;
    public JsonElement? Result => _result;
    public Task WaitForInputCompleteAsync() => _drained.Task.WaitAsync(TimeSpan.FromMinutes(2));
    public object Snapshot => new { port = Port, url = Port > 0 ? Url : null, sentEvents = Interlocked.Read(ref _sent),
        droppedEvents = Interlocked.Read(ref _dropped), error = Error, exited = _process?.HasExited ?? true };

    public async Task StartAsync(string directory, int port = 17891, bool replay = false, bool cacheOnly = false, string? journal = null, string? timeline = null)
    {
        var executable = Executable;
        var assetPackage = Path.Combine(AppContext.BaseDirectory, ProductInfo.AssetPackage);
        if (!File.Exists(assetPackage)) throw new FileNotFoundException($"缺少 {ProductInfo.AssetPackage}，请使用完整 {ProductInfo.Name} {ProductInfo.Version} 发布目录。", assetPackage);
        var models = Path.Combine(AppContext.BaseDirectory, "models");
        if (!Directory.Exists(models)) throw new DirectoryNotFoundException("缺少主目录下的 models 3D 模型文件夹。");
        Directory.CreateDirectory(directory);
        _log = new StreamWriter(Path.Combine(directory, "parser-worker.log"), false, new UTF8Encoding(false)) { AutoFlush = true };
        var start = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden, RedirectStandardInput = true, RedirectStandardOutput = true,
            RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = AppContext.BaseDirectory };
        foreach (var arg in new[] { "--output", directory, "--port", port.ToString(), "--parent-pid", Environment.ProcessId.ToString() })
            start.ArgumentList.Add(arg);
        if (replay) start.ArgumentList.Add("--replay");
        if (cacheOnly) start.ArgumentList.Add("--cache-only");
        start.ArgumentList.Add("--asset-pak"); start.ArgumentList.Add(ProductInfo.AssetPackage);
        start.ArgumentList.Add("--models"); start.ArgumentList.Add("models");
        if (journal is not null) { start.ArgumentList.Add("--journal"); start.ArgumentList.Add(journal); }
        if (timeline is not null) { start.ArgumentList.Add("--timeline"); start.ArgumentList.Add(timeline); }
        try
        {
            _process = Process.Start(start) ?? throw new IOException("解析后台启动失败。");
            _stdout = ReadOutputAsync();
            _stderr = ReadErrorsAsync();
            _pump = PumpAsync();
            Port = await _ready.Task.WaitAsync(TimeSpan.FromSeconds(45));
        }
        catch { await DisposeAsync(); throw; }
    }

    public void Enqueue(byte[] message)
    {
        if (_disposed || Error is not null) return;
        if (!_input.Writer.TryWrite(message))
        {
            Interlocked.Increment(ref _dropped);
            Fail("解析输入队列已满；请停止当前模式并重启应用，重新建立完整会话。");
            _input.Writer.TryComplete(new IOException(Error));
        }
    }

    public async Task SendReplayAsync(string archive)
    {
        using var reader = new StreamReader(archive, Encoding.UTF8, true);
        while (await reader.ReadLineAsync() is { } line)
        {
            if (Encoding.UTF8.GetByteCount(line) > 2 * 1024 * 1024) throw new InvalidDataException("JSONL 行超出上限。");
            await _input.Writer.WriteAsync(Encoding.UTF8.GetBytes(line + "\n"));
        }
        await _input.Writer.WriteAsync(Encoding.UTF8.GetBytes("{\"type\":\"parser_input_complete\"}\n"));
        await _drained.Task.WaitAsync(TimeSpan.FromMinutes(2));
    }

    private void Fail(string error) => Interlocked.CompareExchange(ref _error, error, null);
    private long _logBytes;
    private void Log(string text)
    {
        lock (_input)
        {
            if (_log is null) return;
            if (_logBytes >= 1024 * 1024) { _log.BaseStream.SetLength(0); _log.BaseStream.Position = 0; _logBytes = 0; }
            var line = text.Length > 8192 ? text[..8192] : text;
            _log.WriteLine(line); _logBytes += Encoding.UTF8.GetByteCount(line) + 2;
        }
    }

    private async Task PumpAsync()
    {
        try
        {
            await foreach (var bytes in _input.Reader.ReadAllAsync())
            {
                await _process!.StandardInput.BaseStream.WriteAsync(bytes).AsTask().WaitAsync(TimeSpan.FromSeconds(15));
                Interlocked.Increment(ref _sent);
            }
        }
        catch (Exception ex)
        {
            Fail(ex.Message);
            _input.Writer.TryComplete(ex);
            try
            {
                var fault = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { type = "parser_fault", data = new { error = Error } }) + "\n");
                await _process!.StandardInput.BaseStream.WriteAsync(fault).AsTask().WaitAsync(TimeSpan.FromSeconds(2));
            }
            catch { }
        }
        finally { try { _process?.StandardInput.Close(); } catch { } }
    }

    private async Task ReadOutputAsync()
    {
        try
        {
            while (await _process!.StandardOutput.ReadLineAsync() is { } line)
            {
                Log(line);
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    var root = doc.RootElement;
                    if (root.GetProperty("type").GetString() == "parser_ready") _ready.TrySetResult(root.GetProperty("port").GetInt32());
                    if (root.GetProperty("type").GetString() == "parser_input_drained") _drained.TrySetResult();
                    if (root.GetProperty("type").GetString() == "parser_completed") _result = root.GetProperty("result").Clone();
                }
                catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException) { }
            }
            if (!_ready.Task.IsCompleted) _ready.TrySetException(new IOException("解析后台在就绪前退出，详见 parser-worker.log。"));
            if (!_drained.Task.IsCompleted) _drained.TrySetException(new IOException("解析输入结束，详见 parser-worker.log。"));
            if (!_disposed) { Fail("解析后台已退出，详见 parser-worker.log。"); _input.Writer.TryComplete(); }
        }
        catch (Exception ex) { Fail(ex.Message); _ready.TrySetException(ex); }
    }

    private async Task ReadErrorsAsync()
    {
        while (await _process!.StandardError.ReadLineAsync() is { } line) Log(line);
    }

    public void OpenBrowser()
    {
        try { Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true }); }
        catch (Exception ex) { Console.WriteLine($"请在浏览器打开 {Url}（自动打开失败：{ex.Message}）"); }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _disposed = true;
        _input.Writer.TryComplete();
        try
        {
            if (_pump is not null) await _pump.WaitAsync(TimeSpan.FromSeconds(30));
            if (_process is not null) await _process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));
        }
        catch (Exception ex)
        {
            Fail(ex.Message);
            if (_process is { HasExited: false }) _process.Kill(entireProcessTree: true);
            if (_process is not null) await _process.WaitForExitAsync();
        }
        finally
        {
            if (_stdout is not null) await _stdout;
            if (_stderr is not null) await _stderr;
            if (_process is not null && _process.ExitCode != 0) Fail($"解析后台退出码 {_process.ExitCode}。");
            _log?.Dispose();
            // Keep the process object for final status inspection after DisposeAsync.
        }
    }
}
