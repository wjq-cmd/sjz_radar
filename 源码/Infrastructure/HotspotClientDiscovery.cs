using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text.Json;

namespace GatewayCapture.Infrastructure;

internal sealed record HotspotClientDevice(
    string Name,
    IPAddress Address,
    string MacAddress,
    string State);

internal static class HotspotClientDiscovery
{
    public static async Task<IReadOnlyList<HotspotClientDevice>> DiscoverAsync(
        CancellationToken cancellationToken = default)
    {
        var hotspot = HotspotPktMonComponentResolver.FindActiveHotspotAdapter();
        var localAddresses = hotspot.Adapter.GetIPProperties().UnicastAddresses
            .Select(item => item.Address)
            .Where(IsIpv4)
            .ToHashSet();

        var powershell = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell\\v1.0\\powershell.exe");
        var script =
            "$ErrorActionPreference='Stop';" +
            "$profile=[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile();" +
            "if($null -eq $profile){throw 'No internet connection profile';}" +
            "$manager=[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile);" +
            "$neighbors=@(Get-NetNeighbor -InterfaceIndex " + hotspot.InterfaceIndex + " -AddressFamily IPv4 -ErrorAction SilentlyContinue);" +
            "$rows=@();foreach($client in @($manager.GetTetheringClients())){" +
            "$mac=[string]$client.MacAddress;" +
            "$ipv4=@($client.HostNames|Where-Object{$_.Type -eq 'Ipv4'}|ForEach-Object{$_.CanonicalName}|Select-Object -First 1);" +
            "if(-not $ipv4){$ipv4=@($neighbors|Where-Object{$_.LinkLayerAddress -eq $mac}|ForEach-Object{$_.IPAddress}|Select-Object -First 1);}" +
            "$name=@($client.HostNames|Where-Object{$_.Type -ne 'Ipv4' -and $_.CanonicalName}|ForEach-Object{$_.CanonicalName}|Select-Object -First 1);" +
            "$rows+=@{address=[string]$ipv4;mac=$mac;name=[string]$name;state='Online'};}" +
            "ConvertTo-Json -Compress -InputObject @($rows);";
        var result = await ProcessRunner.RunAsync(
            powershell,
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            cancellationToken);
        if (result.Succeeded)
        {
            return ParseOnlineClients(result.StandardOutput, hotspot.Address, localAddresses);
        }

        return await DiscoverHistoricalNeighborsAsync(
            hotspot.InterfaceIndex, hotspot.Address, localAddresses, powershell, cancellationToken);
    }

    private static IReadOnlyList<HotspotClientDevice> ParseOnlineClients(
        string json,
        IPAddress hotspotAddress,
        HashSet<IPAddress> localAddresses)
    {
        var devices = new List<HotspotClientDevice>();
        foreach (var row in JsonSerializer.Deserialize<List<TetheringClientRow>>(
                     json,
                     new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? [])
        {
            if (!IPAddress.TryParse(row.Address, out var address) ||
                !IsUsableClientAddress(address, hotspotAddress, localAddresses) ||
                !IsUsableMac(row.Mac))
            {
                continue;
            }

            var name = string.IsNullOrWhiteSpace(row.Name) ? ResolveName(address) : row.Name.Trim();
            devices.Add(new HotspotClientDevice(name, address, row.Mac!.Trim(), "Online"));
        }

        return Normalize(devices);
    }

    private static async Task<IReadOnlyList<HotspotClientDevice>> DiscoverHistoricalNeighborsAsync(
        int interfaceIndex,
        IPAddress hotspotAddress,
        HashSet<IPAddress> localAddresses,
        string powershell,
        CancellationToken cancellationToken)
    {
        var script =
            "$ErrorActionPreference='SilentlyContinue';" +
            "Get-NetNeighbor -InterfaceIndex " + interfaceIndex + " -AddressFamily IPv4|" +
            "Where-Object{$_.IPAddress -and $_.LinkLayerAddress}|" +
            "ForEach-Object{\"$($_.IPAddress)|$($_.LinkLayerAddress)|$($_.State)\"}";
        var result = await ProcessRunner.RunAsync(
            powershell,
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            cancellationToken);
        if (!result.Succeeded && result.StandardOutput.Trim().Length == 0)
        {
            throw new InvalidOperationException($"无法读取热点设备：{DescribeFailure(result)}");
        }

        var devices = new List<HotspotClientDevice>();
        foreach (var line in result.StandardOutput.Split(
                     new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
        {
            var fields = line.Trim().Split('|', 3);
            if (fields.Length < 3 ||
                !IPAddress.TryParse(fields[0].Trim(), out var address) ||
                !IsUsableClientAddress(address, hotspotAddress, localAddresses))
            {
                continue;
            }

            var mac = fields[1].Trim();
            if (!IsUsableMac(mac))
            {
                continue;
            }

            var name = ResolveName(address);
            devices.Add(new HotspotClientDevice(name, address, mac, $"历史邻居 ({fields[2].Trim()})"));
        }

        return Normalize(devices);
    }

    private static IReadOnlyList<HotspotClientDevice> Normalize(IEnumerable<HotspotClientDevice> devices)
    {
        return devices
            .GroupBy(device => device.Address)
            .Select(group => group.First())
            .OrderBy(device => device.Address.GetAddressBytes(), ByteArrayComparer.Instance)
            .ToArray();
    }

    private static bool IsUsableClientAddress(
        IPAddress address,
        IPAddress hotspotAddress,
        HashSet<IPAddress> localAddresses) =>
        IsIpv4(address) && !address.Equals(hotspotAddress) && !localAddresses.Contains(address) &&
        address.GetAddressBytes()[0] < 224;

    private static bool IsUsableMac(string? mac) =>
        !string.IsNullOrWhiteSpace(mac) &&
        !mac.Equals("00-00-00-00-00-00", StringComparison.OrdinalIgnoreCase) &&
        !mac.Equals("FF-FF-FF-FF-FF-FF", StringComparison.OrdinalIgnoreCase);

    private static string ResolveName(IPAddress address)
    {
        try
        {
            var host = Dns.GetHostEntry(address).HostName;
            if (!string.IsNullOrWhiteSpace(host) && !host.Equals(address.ToString(), StringComparison.OrdinalIgnoreCase))
            {
                return host;
            }
        }
        catch (SocketException)
        {
            // Mobile hotspot clients commonly do not publish reverse DNS.
        }

        return "未知设备";
    }

    private static bool IsIpv4(IPAddress address) => address.AddressFamily == AddressFamily.InterNetwork;

    private static string DescribeFailure(ProcessResult result)
    {
        var detail = string.Join(
            Environment.NewLine,
            new[] { result.StandardOutput.Trim(), result.StandardError.Trim() }
                .Where(value => value.Length > 0));
        return detail.Length == 0 ? $"退出码 {result.ExitCode}" : detail;
    }

    private sealed record TetheringClientRow(string? Address, string? Mac, string? Name, string? State);

    private sealed class ByteArrayComparer : IComparer<byte[]>
    {
        public static ByteArrayComparer Instance { get; } = new();

        public int Compare(byte[]? left, byte[]? right)
        {
            if (ReferenceEquals(left, right)) return 0;
            if (left is null) return -1;
            if (right is null) return 1;
            for (var index = 0; index < Math.Min(left.Length, right.Length); index++)
            {
                var comparison = left[index].CompareTo(right[index]);
                if (comparison != 0) return comparison;
            }

            return left.Length.CompareTo(right.Length);
        }
    }
}
