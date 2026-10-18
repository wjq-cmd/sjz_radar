using System.Net;
using GatewayCapture.Infrastructure;
using GatewayCapture.Models;

namespace GatewayCapture.Capture;

internal sealed class PktMonCaptureService : IAsyncDisposable
{
    private const string FilterName = "NvaLocalParser";
    private readonly SemaphoreSlim _gate = new(1, 1);
    private ActiveCapture? _active;
    private LifetimeGuard? _guard;
    private string _filterName = FilterName;

    public CaptureSnapshot Snapshot => _active is null
        ? new CaptureSnapshot(false, null, null, null, "未记录")
        : new CaptureSnapshot(
            true,
            _active.TargetAddress,
            _active.State.StartedAt,
            _active.OutputDirectory,
            $"正在通过 Windows PktMon 旁路记录（{_active.Component.AdapterName}）");

    public async Task<CaptureSnapshot> StartAsync(CaptureRequest request, CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if (_active is not null)
            {
                throw new InvalidOperationException("已有记录任务在运行。");
            }

            EnsurePktMonExists();
            await EnsurePktMonIsAvailableAsync(cancellationToken);
            if (request.TargetAddress.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
                throw new ArgumentException("目标地址必须为 IPv4。");
            if (request.MaxFileSizeMiB is < 64 or > 4096)
                throw new ArgumentException("每段大小须为 64 至 4096 MiB。");
            var component = await HotspotPktMonComponentResolver.ResolveAsync(cancellationToken);
            var directory = Path.Combine(Path.GetFullPath(request.OutputDirectory),
                $"gateway-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid().ToString("N")[..8]}");
            Directory.CreateDirectory(Path.Combine(directory, "raw"));
            // PktMon truncates long filter names in list output; keep ownership verifiable.
            _filterName = "GC5-" + Guid.NewGuid().ToString("N")[..16];
            var state = new CaptureArchiveState(request.TargetAddress.ToString(), component.ComponentId,
                component.AdapterName, request.UdpOnly, request.MaxFileSizeMiB, DateTimeOffset.Now,
                FilterName: _filterName, OwnerPid: Environment.ProcessId,
                OwnerStartTicks: System.Diagnostics.Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks);
            await CaptureArchive.SaveStateAsync(directory, state, cancellationToken);
            _guard = await LifetimeGuard.StartAsync("capture", directory);
            var filterArguments = new List<string> { "filter", "add", _filterName, "-i", request.TargetAddress.ToString() };
            if (request.UdpOnly) filterArguments.AddRange(["-t", "UDP"]);
            var filterResult = await RunPktMonAsync(filterArguments, cancellationToken);
            if (!filterResult.Succeeded)
            {
                _guard.Dispose(); _guard = null;
                throw new InvalidOperationException(DescribeFailure("添加 PktMon 筛选器失败", filterResult));
            }
            try
            {
                var startResult = await RunPktMonAsync(
                    ["start", "--capture", "--comp", component.ComponentId.ToString(), "--pkt-size", "0",
                     "--file-name", Path.Combine(directory, "raw", "trace.etl"),
                     "--file-size", request.MaxFileSizeMiB.ToString(), "--log-mode", "multi-file"], cancellationToken);
                if (!startResult.Succeeded)
                    throw new InvalidOperationException(DescribeFailure("启动 PktMon 失败", startResult));
            }
            catch
            {
                await RemoveOwnedFilterAsync(CancellationToken.None, _filterName);
                _guard?.Dispose(); _guard = null;
                throw;
            }
            _active = new ActiveCapture(request.TargetAddress, directory, component, state);
            await CaptureArchive.SaveStateAsync(directory, state with { Status = "recording" }, CancellationToken.None);
            return Snapshot;
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<CaptureResult> StopAsync(CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if (_active is null)
            {
                return new CaptureResult(false, "没有正在运行的记录任务。", null, null, null, 0, 0, 0, 0, 0);
            }

            var active = _active;
            var status = await RunPktMonAsync(["status"], cancellationToken);
            if (!status.Succeeded || (!IsInactive(status.StandardOutput + status.StandardError) &&
                !LifetimeGuard.OwnsCapture(status.StandardOutput + status.StandardError, active.OutputDirectory)))
                return new CaptureResult(false, "当前 PktMon 状态与本次会话不符，已保留原始文件。", null, null, null, 0, 0, 0, 0, 0);
            bool alreadyStopped = IsInactive(status.StandardOutput + status.StandardError);
            var stopResult = alreadyStopped ? new ProcessResult(0, "本次记录已停止。", "") : await RunPktMonAsync(["stop"], cancellationToken);
            if (!stopResult.Succeeded)
                return new CaptureResult(false, DescribeFailure("停止 PktMon 失败，可重试菜单 6", stopResult),
                    null, null, null, 0, 0, 0, 0, 0);
            // Keep the active handle until Windows has confirmed stop.
            _active = null;
            await RemoveOwnedFilterAsync(CancellationToken.None, _filterName);
            _guard?.Dispose(); _guard = null;
            try
            {
                await File.WriteAllTextAsync(Path.Combine(active.OutputDirectory, "pktmon-stop.txt"),
                    stopResult.StandardOutput + stopResult.StandardError, CancellationToken.None);
                var stopped = active.State with { Status = "stopped", StoppedAt = DateTimeOffset.Now };
                await CaptureArchive.SaveStateAsync(active.OutputDirectory, stopped, CancellationToken.None);
                return await CaptureArchive.ExportAsync(active.OutputDirectory, stopped, cancellationToken);
            }
            finally
            {
                await RemoveOwnedFilterAsync(CancellationToken.None, _filterName);
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_active is not null)
        {
            await StopAsync();
        }

        _gate.Dispose();
    }

    private static void EnsurePktMonExists()
    {
        if (!File.Exists(Path.Combine(Environment.SystemDirectory, "pktmon.exe")))
        {
            throw new FileNotFoundException("未找到 Windows 内置 pktmon.exe。", "pktmon.exe");
        }
    }

    private async Task EnsurePktMonIsAvailableAsync(CancellationToken cancellationToken)
    {
        var status = await RunPktMonAsync(["status"], cancellationToken);
        var statusText = (status.StandardOutput + status.StandardError).ToLowerInvariant();
        if (!status.Succeeded || (!statusText.Contains("not running") && !statusText.Contains("没有运行")))
        {
            throw new InvalidOperationException("PktMon 已有抓包运行。实时解析可直接选 9；完整采集请先在原抓包窗口选 6/0 停止。当前记录信息：" + Environment.NewLine + status.StandardOutput);
        }

        var filters = await RunPktMonAsync(["filter", "list"], cancellationToken);
        var filterText = filters.StandardOutput + filters.StandardError;
        if (filters.Succeeded && HasNoFilters(filterText))
        {
            return;
        }

        if (filters.Succeeded && HasOnlyOwnedFilter(filterText))
        {
            // PktMon can retain this filter when a previous console is terminated.
            // `filter remove` removes every filter, so only call it after proving ours is alone.
            await RemoveOwnedFilterAsync(cancellationToken);
            var remaining = await RunPktMonAsync(["filter", "list"], cancellationToken);
            if (remaining.Succeeded && HasNoFilters(remaining.StandardOutput + remaining.StandardError))
            {
                return;
            }
        }

        throw new InvalidOperationException("PktMon 已存在其他筛选器，程序不会修改它们。");
    }

    private static bool HasNoFilters(string output)
    {
        var normalized = output.ToLowerInvariant();
        return normalized.Contains("none") || normalized.Contains("无");
    }

    private static bool HasOnlyOwnedFilter(string output, string filterName = FilterName)
    {
        var filterRows = output
            .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
            .Select(line => line.Trim())
            .Where(line => line.Length > 0 && char.IsDigit(line[0]))
            .ToArray();
        if (filterRows.Length != 1)
        {
            return false;
        }

        var fields = filterRows[0].Split([' ', '\t'], StringSplitOptions.RemoveEmptyEntries);
        return fields.Length >= 2 &&
               int.TryParse(fields[0], out _) &&
               string.Equals(fields[1], filterName, StringComparison.OrdinalIgnoreCase);
    }

    private static async Task<ProcessResult> RunPktMonAsync(IEnumerable<string> arguments, CancellationToken cancellationToken)
    {
        return await ProcessRunner.RunAsync(
            Path.Combine(Environment.SystemDirectory, "pktmon.exe"),
            arguments,
            cancellationToken);
    }

    internal static bool IsInactive(string text) => text.Contains("not running", StringComparison.OrdinalIgnoreCase) || text.Contains("没有运行");

    internal static async Task RemoveOwnedFilterAsync(CancellationToken cancellationToken, string filterName = FilterName)
    {
        var filters = await RunPktMonAsync(["filter", "list"], cancellationToken);
        if (filters.Succeeded && HasOnlyOwnedFilter(filters.StandardOutput + filters.StandardError, filterName))
            await RunPktMonAsync(["filter", "remove"], cancellationToken);
    }

    private static string DescribeFailure(string title, ProcessResult result)
    {
        var parts = new[] { result.StandardOutput.Trim(), result.StandardError.Trim() }
            .Where(value => value.Length > 0);
        var detail = string.Join(Environment.NewLine, parts);
        return detail.Length == 0 ? $"{title}，退出码 {result.ExitCode}。" : $"{title}：{detail}";
    }

    private sealed record ActiveCapture(
        IPAddress TargetAddress,
        string OutputDirectory,
        HotspotPktMonComponent Component,
        CaptureArchiveState State);
}
