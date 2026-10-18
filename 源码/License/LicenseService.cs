using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;

namespace GatewayCapture.License;

/// <summary>
/// 卡密激活 / 启动校验。
///
/// 设计：
/// - 首次运行提示输入卡密，POST /redeem；
/// - 之后每次启动 POST /verify（幂等，不重复核销）；
/// - 服务器不可达时允许 72 小时离线宽限；
/// - 本地授权文件带 HMAC，只能防普通用户手改，不承诺防逆向。
/// </summary>
internal static class LicenseService
{
    private const string SignatureSalt = "tq-license-v1-7f3a9c";
    private static readonly TimeSpan NetworkTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan OfflineGrace = TimeSpan.FromHours(72);
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        WriteIndented = true,
    };

    private static string StorageDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        ProductInfo.StorageDirectory);

    private static string StatePath => Path.Combine(StorageDirectory, "license.json");

    public static async Task<bool> EnsureActivatedAsync()
    {
        if (Environment.GetEnvironmentVariable("TQ_LICENSE_BYPASS") == "1")
        {
            Console.WriteLine("[授权] 已通过 TQ_LICENSE_BYPASS 跳过校验（仅用于开发）。");
            return true;
        }

        var server = ResolveServer();
        if (string.IsNullOrWhiteSpace(server) || server.Contains("example.com", StringComparison.OrdinalIgnoreCase))
        {
            Fail("尚未配置激活服务器。请在 license.json 中填写 server，或联系作者。");
            return false;
        }
        if (!Uri.TryCreate(server, UriKind.Absolute, out var serverUri)
            || (serverUri.Scheme != Uri.UriSchemeHttp && serverUri.Scheme != Uri.UriSchemeHttps))
        {
            Fail("激活服务器地址格式不正确。请在 license.json 中填写完整地址，例如 http://1.2.3.4:28473");
            return false;
        }

        var deviceId = DeviceId();
        // 本地授权只用于离线宽限和卡号比对；每次启动仍然要求输入卡密。
        var state = LoadState(deviceId);
        var cardNo = PromptCardKey();
        if (cardNo is null)
        {
            Fail("激活未完成。");
            return false;
        }

        // 已激活的卡走 /verify（幂等）；未激活的卡回退到 /redeem。
        var response = await CallAsync(server, "/verify",
            new { card_no = cardNo, device_id = deviceId });
        if (response is null)
        {
            if (state is not null &&
                string.Equals(state.CardNo, cardNo, StringComparison.Ordinal))
            {
                return OfflineFallback(state);
            }
            Fail("无法连接激活服务器，请检查网络后重试。");
            return false;
        }

        if (response.Code == 0)
        {
            SaveVerifiedState(cardNo, response, deviceId);
            return CheckExpiry(new LicenseState
            {
                CardNo = cardNo,
                ExpiresAt = response.ExpiresAt,
            });
        }

        if (response.Code == 1002 && response.Msg.Contains("尚未激活", StringComparison.Ordinal))
        {
            var redeem = await CallAsync(server, "/redeem",
                new { card_no = cardNo, device_id = deviceId });
            if (redeem is null)
            {
                Fail("无法连接激活服务器，请检查网络后重试。");
                return false;
            }
            if (redeem.Code == 0)
            {
                SaveVerifiedState(cardNo, redeem, deviceId);
                Console.WriteLine(redeem.ExpiresAt is long exp
                    ? $"[授权] 激活成功，有效期至 {DateTimeOffset.FromUnixTimeSeconds(exp).ToLocalTime():yyyy-MM-dd HH:mm}。"
                    : "[授权] 激活成功，永久有效。");
                return true;
            }
            Fail($"激活失败：{redeem.Msg}");
            return false;
        }

        Fail(response.Msg);
        return false;
    }

    private static void SaveVerifiedState(string cardNo, LicenseResponse response, string deviceId)
    {
        SaveState(new LicenseState
        {
            CardNo = cardNo,
            Product = response.Product ?? "",
            ExpiresAt = response.ExpiresAt,
            LastOkServerTime = response.ServerTime ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            LastOkLocalUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            DeviceId = deviceId,
        }, deviceId);
    }

    /// <summary>每次启动都要求输入卡密。</summary>
    private static string? PromptCardKey()
    {
        Console.WriteLine();
        Console.WriteLine("============================================================");
        Console.WriteLine("  请输入卡密（每次启动都需要验证）");
        Console.WriteLine("  格式：XXXX-XXXX-XXXX-XXXX-XXXX");
        Console.WriteLine("============================================================");

        for (var attempt = 1; attempt <= 3; attempt++)
        {
            Console.Write($"卡密（{attempt}/3）: ");
            var input = Console.ReadLine()?.Trim();
            if (string.IsNullOrEmpty(input))
            {
                Console.WriteLine("卡密不能为空。");
                continue;
            }
            return NormalizeCard(input);
        }
        return null;
    }

    // ---------------------------------------------------------------- 网络

    private static async Task<LicenseResponse?> CallAsync(string server, string path, object payload)
    {
        try
        {
            using var http = new HttpClient { Timeout = NetworkTimeout };
            using var content = new StringContent(
                JsonSerializer.Serialize(payload, JsonOptions), Encoding.UTF8, "application/json");
            using var response = await http.PostAsync(server.TrimEnd('/') + path, content);
            var body = await response.Content.ReadAsStringAsync();

            if (response.StatusCode == HttpStatusCode.TooManyRequests)
            {
                Console.WriteLine("[授权] 请求过于频繁，暂时按离线模式处理。");
                return null;
            }

            return ParseResponse(body);
        }
        catch (Exception ex) when (
            ex is HttpRequestException or TaskCanceledException or IOException
            or UriFormatException or InvalidOperationException or NotSupportedException)
        {
            return null; // 网络不可达 / 地址非法 -> 走离线宽限，不让程序闪退
        }
    }

    private static LicenseResponse? ParseResponse(string body)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var code = root.TryGetProperty("code", out var codeElement) ? codeElement.GetInt32() : -1;
            var msg = root.TryGetProperty("msg", out var msgElement) ? msgElement.GetString() ?? "" : "";
            string? product = null;
            long? expiresAt = null, serverTime = null;

            if (root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Object)
            {
                if (data.TryGetProperty("product", out var p) && p.ValueKind == JsonValueKind.String)
                    product = p.GetString();
                if (data.TryGetProperty("expires_at", out var e) && e.ValueKind == JsonValueKind.Number)
                    expiresAt = e.GetInt64();
                if (data.TryGetProperty("server_time", out var s) && s.ValueKind == JsonValueKind.Number)
                    serverTime = s.GetInt64();
            }

            return new LicenseResponse(code, msg, product, expiresAt, serverTime);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    // ---------------------------------------------------------------- 离线

    private static bool OfflineFallback(LicenseState state)
    {
        if (!DateTime.TryParse(
                state.LastOkLocalUtc, CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind, out var lastOk))
        {
            Fail("本地授权记录损坏，请联网后重试。");
            return false;
        }

        var now = DateTime.UtcNow;
        if (now < lastOk.AddMinutes(-5))
        {
            Fail("系统时间异常（早于上次校验时间），请校准系统时间后重试。");
            return false;
        }

        if (!CheckExpiry(state))
        {
            return false;
        }

        var elapsed = now - lastOk;
        if (elapsed > OfflineGrace)
        {
            Fail("已超过 72 小时未能连接激活服务器，请联网后重试。");
            return false;
        }

        var remaining = OfflineGrace - elapsed;
        Console.WriteLine(
            $"[授权] 无法连接激活服务器，离线模式可用（剩余宽限 {remaining.TotalHours:F0} 小时）。");
        return true;
    }

    private static bool CheckExpiry(LicenseState state)
    {
        if (state.ExpiresAt is not long expiresAt)
        {
            return true; // 永久卡
        }

        if (DateTimeOffset.UtcNow.ToUnixTimeSeconds() < expiresAt)
        {
            return true;
        }

        Fail($"卡密已于 {DateTimeOffset.FromUnixTimeSeconds(expiresAt).ToLocalTime():yyyy-MM-dd HH:mm} 到期。");
        return false;
    }

    // ---------------------------------------------------------------- 本地存储

    private static LicenseState? LoadState(string deviceId)
    {
        try
        {
            if (!File.Exists(StatePath)) return null;
            var state = JsonSerializer.Deserialize<LicenseState>(File.ReadAllText(StatePath), JsonOptions);
            if (state is null || string.IsNullOrWhiteSpace(state.CardNo)) return null;
            if (!string.Equals(state.Sig, ComputeSignature(state, deviceId), StringComparison.Ordinal))
            {
                // 兼容旧版本：旧 device_id 包含完整 OSVersion，Windows 更新或 manifest
                // 变化后当前机器算出的 device_id 会变。只要文件自身签名自洽就先接受，
                // 下一次在线校验成功会写回新的 device_id 和签名（自动迁移）。
                if (string.IsNullOrWhiteSpace(state.DeviceId) ||
                    !string.Equals(state.Sig, ComputeSignature(state, state.DeviceId),
                        StringComparison.Ordinal))
                {
                    Console.WriteLine("[授权] 本地授权文件校验失败，需要重新激活。");
                    return null;
                }
            }
            return state;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static void SaveState(LicenseState state, string deviceId)
    {
        try
        {
            Directory.CreateDirectory(StorageDirectory);
            state.DeviceId = deviceId;
            state.Sig = ComputeSignature(state, deviceId);
            var temporary = StatePath + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(state, JsonOptions), new UTF8Encoding(false));
            File.Move(temporary, StatePath, overwrite: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Console.WriteLine($"[授权] 无法写入本地授权文件：{ex.Message}");
        }
    }

    private static void TryDeleteState()
    {
        try { File.Delete(StatePath); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    private static string ComputeSignature(LicenseState state, string deviceId)
    {
        var payload = string.Join(
            "|",
            state.CardNo,
            state.Product,
            state.ExpiresAt?.ToString(CultureInfo.InvariantCulture) ?? "",
            state.LastOkServerTime.ToString(CultureInfo.InvariantCulture),
            state.LastOkLocalUtc,
            deviceId);
        var key = SHA256.HashData(Encoding.UTF8.GetBytes($"{SignatureSalt}|{deviceId}"));
        return Convert.ToHexString(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(payload))).ToLowerInvariant();
    }

    /// <summary>开发诊断：打印当前设备 ID、本地授权字段和签名校验结果。</summary>
    public static void PrintDiagnostics()
    {
        var deviceId = DeviceId();
        Console.WriteLine("device_id: " + deviceId);
        Console.WriteLine("machine: " + Environment.MachineName);
        try { Console.WriteLine("sid: " + (WindowsIdentity.GetCurrent().User?.Value ?? "")); }
        catch (Exception ex) { Console.WriteLine("sid: <error " + ex.Message + ">"); }
        Console.WriteLine("os: " + Environment.OSVersion.VersionString);
        Console.WriteLine("storage: " + StatePath);
        if (!File.Exists(StatePath))
        {
            Console.WriteLine("license: missing");
            return;
        }
        try
        {
            var state = JsonSerializer.Deserialize<LicenseState>(
                File.ReadAllText(StatePath), JsonOptions);
            if (state is null)
            {
                Console.WriteLine("license: cannot deserialize");
                return;
            }
            Console.WriteLine("card_no: " + state.CardNo);
            Console.WriteLine("stored_device_id: " + state.DeviceId);
            Console.WriteLine("expires_at: " + state.ExpiresAt);
            Console.WriteLine("last_ok_server_time: " + state.LastOkServerTime);
            Console.WriteLine("last_ok_local_utc: " + state.LastOkLocalUtc);
            Console.WriteLine("sig_match: " + string.Equals(
                state.Sig, ComputeSignature(state, deviceId), StringComparison.Ordinal));
            Console.WriteLine("legacy_sig_match: " + string.Equals(
                state.Sig, ComputeSignature(state, state.DeviceId), StringComparison.Ordinal));
        }
        catch (Exception ex)
        {
            Console.WriteLine("read error: " + ex);
        }
    }

    // ---------------------------------------------------------------- 工具

    private static string ResolveServer()
    {
        var fromEnvironment = Environment.GetEnvironmentVariable("TQ_LICENSE_SERVER");
        if (!string.IsNullOrWhiteSpace(fromEnvironment))
        {
            return fromEnvironment.Trim();
        }

        try
        {
            var config = Path.Combine(AppContext.BaseDirectory, "license.json");
            if (File.Exists(config))
            {
                using var document = JsonDocument.Parse(File.ReadAllText(config));
                if (document.RootElement.TryGetProperty("server", out var server)
                    && server.ValueKind == JsonValueKind.String)
                {
                    var value = server.GetString();
                    if (!string.IsNullOrWhiteSpace(value))
                    {
                        return value.Trim();
                    }
                }
            }
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
        }

        return ProductInfo.LicenseServer;
    }

    private static string DeviceId()
    {
        string sid = "";
        try
        {
            sid = WindowsIdentity.GetCurrent().User?.Value ?? "";
        }
        catch (Exception)
        {
            // 极端环境下取不到 SID，退化为机器名 + 主版本号
        }

        // 注意：不要用完整的 Environment.OSVersion.VersionString。
        // 它会受 app.manifest 兼容性声明和 Windows 更新的影响，
        // 同一台机器升级系统后 device_id 会变，导致授权文件失效。
        var machineGuid = MachineGuid();
        var raw = string.IsNullOrEmpty(machineGuid)
            ? $"{Environment.MachineName}|{sid}|{Environment.OSVersion.Version.Major}|{SignatureSalt}"
            : $"{machineGuid}|{sid}|{Environment.MachineName}|{SignatureSalt}";
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(raw))).ToLowerInvariant();
        return digest[..32];
    }

    private static string MachineGuid()
    {
        try
        {
            return Microsoft.Win32.Registry.GetValue(
                @"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography",
                "MachineGuid", null) as string ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }

    private static string NormalizeCard(string value)
    {
        var characters = new string(value.Where(char.IsLetterOrDigit).ToArray()).ToUpperInvariant();
        if (characters.Length <= 4)
        {
            return characters;
        }

        var groups = new List<string>();
        for (var index = 0; index < characters.Length; index += 4)
        {
            groups.Add(characters.Substring(index, Math.Min(4, characters.Length - index)));
        }
        return string.Join("-", groups);
    }

    private static void Fail(string message)
    {
        Console.WriteLine();
        Console.WriteLine($"[授权失败] {message}");
        Console.WriteLine("如需帮助，请联系作者。");
    }

    private sealed record LicenseResponse(
        int Code,
        string Msg,
        string? Product,
        long? ExpiresAt,
        long? ServerTime);
}

internal sealed class LicenseState
{
    public string CardNo { get; set; } = "";
    public string Product { get; set; } = "";
    public long? ExpiresAt { get; set; }
    public long LastOkServerTime { get; set; }
    public string LastOkLocalUtc { get; set; } = "";
    public string DeviceId { get; set; } = "";
    public string Sig { get; set; } = "";
}
