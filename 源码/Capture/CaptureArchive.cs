using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using GatewayCapture.Infrastructure;
using GatewayCapture.Models;

namespace GatewayCapture.Capture;

internal sealed record CaptureArchiveState(
    string TargetAddress, int ComponentId, string AdapterName, bool UdpOnly,
    int SegmentMiB, DateTimeOffset StartedAt,
    string Status = "prepared", DateTimeOffset? StoppedAt = null,
    string? Error = null, CaptureResult? Result = null,
    string? FilterName = null, int? OwnerPid = null, long? OwnerStartTicks = null)
{
    public string Schema => "gateway-capture/v3";
    public string Scope => UdpOnly ? "target-ipv4-udp-bidirectional" : "target-ipv4-all-protocols-bidirectional";
    public string LogMode => "multi-file";
    public bool PreserveOriginals => true;
}

internal static class CaptureArchive
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    public static async Task SaveStateAsync(string directory, CaptureArchiveState state, CancellationToken ct = default)
    {
        var path = Path.Combine(directory, "capture-state.json");
        var temporary = path + ".tmp";
        await File.WriteAllTextAsync(temporary, JsonSerializer.Serialize(state, JsonOptions), ct);
        File.Move(temporary, path, true);
    }

    internal static string[] TraceFiles(string directory) => Directory.GetFiles(Path.Combine(directory, "raw"), "*.etl")
        .OrderBy(p => Regex.Replace(Path.GetFileName(p), @"\d+", m => m.Value.PadLeft(20, '0')), StringComparer.OrdinalIgnoreCase)
        .ToArray();

    public static async Task<CaptureResult> RecoverAsync(string directory)
    {
        directory = Path.GetFullPath(directory);
        var state = JsonSerializer.Deserialize<CaptureArchiveState>(
            await File.ReadAllTextAsync(Path.Combine(directory, "capture-state.json")))
            ?? throw new InvalidDataException("缺少会话元数据。");
        // Never export a growing ETL, and never stop an unrelated capture.
        var status = await ProcessRunner.RunAsync(Path.Combine(Environment.SystemDirectory, "pktmon.exe"), ["status"]);
        var text = status.StandardOutput + status.StandardError;
        if (!status.Succeeded || !(text.Contains("not running", StringComparison.OrdinalIgnoreCase) || text.Contains("没有运行")))
            throw new InvalidOperationException("PktMon 尚在运行或状态未知；请先在采集窗口停止记录，再恢复导出。");
        return await ExportAsync(directory, state);
    }

    public static async Task<CaptureResult> ExportAsync(string directory, CaptureArchiveState state, CancellationToken ct = default)
    {
        try
        {
            await SaveStateAsync(directory, state with { Status = "exporting", Error = null }, ct);
            var etls = TraceFiles(directory);
            if (etls.Length == 0) throw new FileNotFoundException("raw 目录中没有 ETL 分段。");
            var pcaps = new List<string>();
            foreach (var etl in etls)
            {
                Console.WriteLine($"转换原始分段 {pcaps.Count + 1}/{etls.Length}: {Path.GetFileName(etl)}");
                var pcap = Path.ChangeExtension(etl, ".pcapng");
                var temporary = pcap + ".converting";
                var converted = await ProcessRunner.RunAsync(Path.Combine(Environment.SystemDirectory, "pktmon.exe"),
                    ["etl2pcap", etl, "--out", temporary, "--component-id", state.ComponentId.ToString()], ct);
                await File.WriteAllTextAsync(etl + ".conversion.txt", converted.StandardOutput + converted.StandardError, ct);
                if (!converted.Succeeded) throw new IOException($"分段转换失败: {Path.GetFileName(etl)}；详情见 .conversion.txt。");
                File.Move(temporary, pcap, true);
                pcaps.Add(pcap);
            }
            var full = Path.Combine(directory, "session.fullip.pcapng");
            var udp = Path.Combine(directory, "session.udp.pcapng");
            var down = Path.Combine(directory, "session.downstream.pcapng");
            var counts = PcapNgDownstreamFilter.FilterTargetSession(pcaps,
                udp + ".tmp", down + ".tmp", IPAddress.Parse(state.TargetAddress), full + ".tmp");
            File.Move(full + ".tmp", full, true);
            File.Move(udp + ".tmp", udp, true);
            File.Move(down + ".tmp", down, true);
            var message = counts.FullIpPackets == 0 ? "导出完成，但目标包数为 0；请检查设备地址与采集位置。" :
                $"导出完成：目标 IPv4 {counts.FullIpPackets} 包，TCP {counts.TcpPackets}，UDP {counts.SessionPackets}，其他 {counts.OtherIpPackets}。";
            var result = new CaptureResult(true, message, down, udp, Path.Combine(directory, "capture-state.json"),
                counts.InputPackets, counts.SessionPackets, counts.SessionBytes, counts.DownstreamPackets,
                counts.DownstreamBytes, full, counts.FullIpPackets, counts.TcpPackets, counts.OtherIpPackets, counts.TruncatedPackets);
            await File.WriteAllTextAsync(Path.Combine(directory, "packet-counts.json"), JsonSerializer.Serialize(counts, JsonOptions), ct);
            await SaveStateAsync(directory, state with { Status = "complete", Error = null, Result = result }, ct);
            return result;
        }
        catch (Exception ex)
        {
            await SaveStateAsync(directory, state with { Status = "export_failed", Error = ex.Message }, CancellationToken.None);
            throw new IOException($"导出未完成，原始分段保留在 {Path.Combine(directory, "raw")}。可用菜单 8 恢复。{ex.Message}", ex);
        }
    }
}
