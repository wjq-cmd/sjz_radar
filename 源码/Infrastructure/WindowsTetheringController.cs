using GatewayCapture.Models;

namespace GatewayCapture.Infrastructure;

internal sealed class WindowsTetheringController
{
    public Task<TetheringStatus> GetStatusAsync() => RunAsync("status");

    public Task<TetheringStatus> StartAsync() => RunAsync("start");

    public Task<TetheringStatus> StopAsync() => RunAsync("stop");

    private static async Task<TetheringStatus> RunAsync(string operation)
    {
        var result = await ProcessRunner.RunAsync(
            "powershell.exe",
            ["-NoProfile", "-NonInteractive", "-Command", BuildScript(operation)]);
        if (!result.Succeeded)
        {
            var failureDetail = string.Join(Environment.NewLine,
                new[] { result.StandardOutput.Trim(), result.StandardError.Trim() }
                    .Where(value => value.Length > 0));
            return new TetheringStatus(false, false, string.Empty,
                failureDetail.Length == 0 ? $"Windows 热点命令失败，退出码 {result.ExitCode}。" : failureDetail);
        }

        var values = ParseValues(result.StandardOutput);
        values.TryGetValue("PROFILE", out var profileName);
        values.TryGetValue("CAPABILITY", out var capability);
        values.TryGetValue("STATE", out var state);
        values.TryGetValue("RESULT", out var commandResult);

        var available = string.Equals(capability, "Enabled", StringComparison.OrdinalIgnoreCase);
        var running = string.Equals(state, "On", StringComparison.OrdinalIgnoreCase);
        var detail = string.IsNullOrWhiteSpace(commandResult) ? capability ?? "Unknown" : commandResult;
        return new TetheringStatus(available, running, profileName ?? string.Empty, detail);
    }

    private static Dictionary<string, string> ParseValues(string output)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in output.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = line.IndexOf('=');
            if (separator <= 0)
            {
                continue;
            }

            values[line[..separator].Trim()] = line[(separator + 1)..].Trim();
        }

        return values;
    }

    private static string BuildScript(string operation)
    {
        return $$"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$profile = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]::GetInternetConnectionProfile()
if ($null -eq $profile) {
    'PROFILE='
    'CAPABILITY=NoProfile'
    'STATE=Off'
    'RESULT=NoProfile'
    exit 0
}

$managerType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
$manager = $managerType::CreateFromConnectionProfile($profile)
$capability = $managerType::GetTetheringCapabilityFromConnectionProfile($profile)
$resultStatus = 'NoOperation'

function Wait-TetheringState($tetheringManager, $expectedState) {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($tetheringManager.TetheringOperationalState.ToString() -eq $expectedState) {
            return
        }

        Start-Sleep -Milliseconds 200
    }

    $actualState = $tetheringManager.TetheringOperationalState.ToString()
    throw "移动热点状态未在 30 秒内变为 $expectedState，当前状态: $actualState"
}

if ('{{operation}}' -eq 'start' -and $manager.TetheringOperationalState -ne 'On') {
    $null = $manager.StartTetheringAsync()
    Wait-TetheringState $manager 'On'
    $resultStatus = 'Started'
}

if ('{{operation}}' -eq 'stop' -and $manager.TetheringOperationalState -eq 'On') {
    $null = $manager.StopTetheringAsync()
    Wait-TetheringState $manager 'Off'
    $resultStatus = 'Stopped'
}

'PROFILE=' + $profile.ProfileName
'CAPABILITY=' + $capability.ToString()
'STATE=' + $manager.TetheringOperationalState.ToString()
'RESULT=' + $resultStatus
""";
    }
}
