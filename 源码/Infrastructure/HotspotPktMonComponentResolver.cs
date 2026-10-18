using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text.Json;

namespace GatewayCapture.Infrastructure;

internal sealed record HotspotPktMonComponent(
    int ComponentId,
    string AdapterName,
    int InterfaceIndex,
    IPAddress GatewayAddress);

internal static class HotspotPktMonComponentResolver
{
    public static async Task<HotspotPktMonComponent> ResolveAsync(
        CancellationToken cancellationToken = default)
    {
        var adapter = FindActiveHotspotAdapter();
        var listResult = await ProcessRunner.RunAsync(
            Path.Combine(Environment.SystemDirectory, "pktmon.exe"),
            ["list", "--json"],
            cancellationToken);
        if (!listResult.Succeeded)
        {
            throw new InvalidOperationException(
                $"无法查询热点网络组件：{DescribeFailure(listResult)}");
        }

        using var document = JsonDocument.Parse(listResult.StandardOutput);
        var componentId = FindPktMonComponentId(document.RootElement, adapter.InterfaceIndex);
        if (componentId is null)
        {
            throw new InvalidOperationException(
                $"未在 PktMon 中找到热点网卡组件（ifIndex={adapter.InterfaceIndex}）。");
        }

        return new HotspotPktMonComponent(
            componentId.Value,
            adapter.Adapter.Name,
            adapter.InterfaceIndex,
            adapter.Address);
    }

    internal static (NetworkInterface Adapter, IPAddress Address, int InterfaceIndex) FindActiveHotspotAdapter()
    {
        var candidates = new List<(NetworkInterface Adapter, IPAddress Address, int InterfaceIndex)>();
        foreach (var adapter in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (adapter.OperationalStatus != OperationalStatus.Up ||
                !adapter.Description.Contains("Wi-Fi Direct", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var properties = adapter.GetIPProperties();
            var address = properties.UnicastAddresses
                .Select(item => item.Address)
                .FirstOrDefault(IsPrivateIpv4Address);
            if (address is null)
            {
                continue;
            }

            candidates.Add((adapter, address, properties.GetIPv4Properties().Index));
        }

        var selected = candidates
            .OrderByDescending(item => item.Address.ToString() == "192.168.137.1")
            .FirstOrDefault();
        if (selected.Adapter is null)
        {
            throw new InvalidOperationException(
                "未找到正在运行且具有 IPv4 地址的 Windows 移动热点 Wi-Fi Direct 网卡。");
        }

        return selected;
    }

    private static int? FindPktMonComponentId(JsonElement root, int interfaceIndex)
    {
        foreach (var group in root.EnumerateArray())
        {
            if (!group.TryGetProperty("Components", out var components))
            {
                continue;
            }

            foreach (var component in components.EnumerateArray())
            {
                if (!component.TryGetProperty("DriverName", out var driverName) ||
                    !string.Equals(driverName.GetString(), "vwifimp.sys", StringComparison.OrdinalIgnoreCase) ||
                    !MatchesInterfaceIndex(component, interfaceIndex) ||
                    !component.TryGetProperty("Id", out var id) ||
                    !id.TryGetInt32(out var componentId))
                {
                    continue;
                }

                return componentId;
            }
        }

        return null;
    }

    private static bool MatchesInterfaceIndex(JsonElement component, int interfaceIndex)
    {
        if (!component.TryGetProperty("Properties", out var properties))
        {
            return false;
        }

        foreach (var property in properties.EnumerateArray())
        {
            if (!property.TryGetProperty("Name", out var name) ||
                !string.Equals(name.GetString(), "ifIndex", StringComparison.OrdinalIgnoreCase) ||
                !property.TryGetProperty("Value", out var value))
            {
                continue;
            }

            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var numericValue))
            {
                return numericValue == interfaceIndex;
            }

            return int.TryParse(value.GetString(), out var textValue) && textValue == interfaceIndex;
        }

        return false;
    }

    private static bool IsPrivateIpv4Address(IPAddress address)
    {
        if (address.AddressFamily != AddressFamily.InterNetwork)
        {
            return false;
        }

        var bytes = address.GetAddressBytes();
        return bytes[0] == 10 ||
               bytes[0] == 192 && bytes[1] == 168 ||
               bytes[0] == 172 && bytes[1] is >= 16 and <= 31;
    }

    private static string DescribeFailure(ProcessResult result)
    {
        var parts = new[] { result.StandardOutput.Trim(), result.StandardError.Trim() }
            .Where(value => value.Length > 0);
        var detail = string.Join(Environment.NewLine, parts);
        return detail.Length == 0 ? $"退出码 {result.ExitCode}" : detail;
    }
}
