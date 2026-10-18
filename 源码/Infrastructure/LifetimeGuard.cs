using System.Diagnostics;
using System.Text.Json;
using GatewayCapture.Capture;

namespace GatewayCapture.Infrastructure;

// Separate hidden process: survives console X and forced termination of the owner.
// Each guard knows one immutable resource; it never clears a foreign capture.
internal sealed class LifetimeGuard : IDisposable
{
    private readonly Process _process;
    private LifetimeGuard(Process process) => _process = process;

    public static async Task<LifetimeGuard> StartAsync(string kind, string target)
    {
        var start = new ProcessStartInfo(Environment.ProcessPath!)
        {
            UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true
        };
        if (string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath), "dotnet", StringComparison.OrdinalIgnoreCase))
            start.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "Nva本地离线解析.dll"));
        foreach (var arg in new[] { "--cleanup-guardian", Environment.ProcessId.ToString(),
            Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString(), kind, target }) start.ArgumentList.Add(arg);
        var process = Process.Start(start) ?? throw new IOException("退出清理后台启动失败。");
        try
        {
            if (await process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(15)) != "READY")
                throw new IOException("退出清理后台尚未就绪。");
            return new(process);
        }
        catch { if (!process.HasExited) process.Kill(); process.Dispose(); throw; }
    }

    public void Dispose()
    {
        // Caller disarms only after the corresponding resource was removed.
        if (!_process.HasExited) _process.Kill();
        _process.Dispose();
    }

    internal static bool OwnsCapture(string status, string directory)
    {
        var prefix = Path.GetFullPath(Path.Combine(directory, "raw")) + Path.DirectorySeparatorChar;
        return status.Contains(prefix, StringComparison.OrdinalIgnoreCase);
    }

    public static async Task RunAsync(string[] args)
    {
        int parentId = int.Parse(args[1]); long startTicks = long.Parse(args[2]);
        string kind = args[3], target = args[4];
        if (kind is not ("capture" or "firewall")) throw new ArgumentException("Unknown resource");
        if (kind == "firewall" && !System.Text.RegularExpressions.Regex.IsMatch(target, "^NvaLocalParser-v1-[0-9a-f]{32}$"))
            throw new ArgumentException("Invalid rule name");
        Process? parent = null;
        try
        {
            parent = Process.GetProcessById(parentId);
            if (parent.StartTime.ToUniversalTime().Ticks != startTicks) { parent.Dispose(); parent = null; }
        }
        catch (ArgumentException) { }
        Console.WriteLine("READY"); Console.Out.Flush();
        if (parent is not null) { using (parent) await parent.WaitForExitAsync(); }
        if (kind == "firewall")
        {
            await ProcessRunner.RunAsync("netsh.exe", ["advfirewall", "firewall", "delete", "rule", $"name={target}"]);
            return;
        }
        var statePath = Path.Combine(target, "capture-state.json");
        var ownedState = JsonSerializer.Deserialize<CaptureArchiveState>(await File.ReadAllTextAsync(statePath));
        if (ownedState?.OwnerPid != parentId || ownedState.OwnerStartTicks != startTicks || ownedState.FilterName is null) return;
        var executable = Path.Combine(Environment.SystemDirectory, "pktmon.exe");
        var status = await ProcessRunner.RunAsync(executable, ["status"]);
        string statusText = status.StandardOutput + status.StandardError;
        bool inactive = PktMonCaptureService.IsInactive(statusText);
        if (status.Succeeded && !inactive && OwnsCapture(statusText, target))
        {
            var stopped = await ProcessRunner.RunAsync(executable, ["stop"]);
            await File.WriteAllTextAsync(Path.Combine(target, "pktmon-guardian-stop.txt"), stopped.StandardOutput + stopped.StandardError);
            if (!stopped.Succeeded) return;
            inactive = true;
            var path = Path.Combine(target, "capture-state.json");
            var state = JsonSerializer.Deserialize<CaptureArchiveState>(await File.ReadAllTextAsync(path));
            if (state is not null) await CaptureArchive.SaveStateAsync(target, state with
                { Status = "stopped", StoppedAt = DateTimeOffset.Now, Error = "窗口已关闭；原始 ETL 已保留，可用菜单 8 导出。" });
        }
        // No active capture: a filter alone is harmless, but release ours after verifying it is alone.
        if (status.Succeeded && inactive) await PktMonCaptureService.RemoveOwnedFilterAsync(CancellationToken.None, ownedState.FilterName);
    }
}
