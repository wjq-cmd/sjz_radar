using System.Net.NetworkInformation;
using System.Security.Principal;
using GatewayCapture.Models;

namespace GatewayCapture.Infrastructure;

internal sealed class SystemProbe
{
    public ProbeReport Collect()
    {
        var adapters = NetworkInterface.GetAllNetworkInterfaces();
        var activeAdapters = adapters
            .Where(adapter => adapter.OperationalStatus == OperationalStatus.Up)
            .Select(adapter => $"{adapter.Name} - {adapter.Description}")
            .ToArray();

        var hasWirelessAdapter = adapters.Any(adapter =>
            adapter.NetworkInterfaceType == NetworkInterfaceType.Wireless80211);
        var hasWifiDirectAdapter = adapters.Any(adapter =>
            adapter.Description.Contains("Wi-Fi Direct", StringComparison.OrdinalIgnoreCase));

        return new ProbeReport(
            IsAdministrator(),
            File.Exists(Path.Combine(Environment.SystemDirectory, "pktmon.exe")),
            hasWirelessAdapter,
            hasWifiDirectAdapter,
            activeAdapters);
    }

    private static bool IsAdministrator()
    {
        using var identity = WindowsIdentity.GetCurrent();
        return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
    }
}
