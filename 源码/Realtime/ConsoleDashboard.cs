using System.Net.Http.Json;
using System.Text.Json;

namespace GatewayCapture.Realtime;

internal static class ConsoleDashboard
{
    public static async Task<char> RunCaptureAsync(GatewayCapture.Capture.PktMonCaptureService capture, CancellationToken ct)
    {
        if (!Console.IsOutputRedirected) Console.Clear();
        long previous = 0;
        var previousAt = DateTimeOffset.Now;
        bool details = false;
        while (!ct.IsCancellationRequested)
        {
            var snapshot = capture.Snapshot;
            var files = snapshot.OutputDirectory is not null
                ? new DirectoryInfo(Path.Combine(snapshot.OutputDirectory, "raw")).GetFiles("*.etl") : [];
            long bytes = files.Sum(f => f.Length);
            var now = DateTimeOffset.Now;
            double rate = (bytes-previous)/Math.Max(.01,(now-previousAt).TotalSeconds);
            var lines = new List<string> {
                $"{ProductInfo.Name} {ProductInfo.Version} | 完整离线采集",
                $"设备 {snapshot.TargetAddress} | 用时 {now - (snapshot.StartedAt ?? now):hh\\:mm\\:ss}",
                snapshot.Detail ?? "", "",
                $"ETL 分段 {files.Length} | 已写入 {bytes/1048576d:F1} MiB | 增长 {Math.Max(0,rate)/1048576d:F2} MiB/秒",
                "文件字节含 ETL 元数据；Windows 缓冲可能导致间歇更新。",
                "上下行包数：停止导出后统计。此模式仅采集，Key/明文解析：未启用。",
                "分段持续保留；按 S 停止后自动导出 PCAPNG 和统计。",
                "目录：" + snapshot.OutputDirectory
            };
            if (details) lines.AddRange(files.TakeLast(8).Select(f => $"{f.Name}  {f.Length/1048576d:F1} MiB"));
            lines.Add("[D] 分段详情  [S] 停止并导出  [Q] 导出后退出");
            Render(lines);
            previous = bytes; previousAt = now;
            if (Console.IsInputRedirected) return 's';
            var until = now.AddSeconds(1);
            while (DateTimeOffset.Now < until && !ct.IsCancellationRequested)
            {
                if (Console.KeyAvailable)
                {
                    char key=char.ToLowerInvariant(Console.ReadKey(true).KeyChar);
                    if (key is 's' or 'q') return key;
                    if (key == 'd') { details=!details; Console.Clear(); break; }
                }
                await Task.Delay(25,ct);
            }
        }
        return 'q';
    }
    // No stdin ReadLine task while running: commands are single-key, no extra Enter.
    public static async Task<char> RunAsync(RealtimeSession? live, ParserHost? replay, string directory, CancellationToken ct)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(1) };
        var started = DateTimeOffset.Now;
        bool details = false;
        JsonElement status = default;
        DateTimeOffset? statusAt = null;
        long previousUp = 0, previousDown = 0;
        var lastRate = DateTimeOffset.Now;
        var events = new Queue<string>();
        int oldKeys = 0; long oldPlain = 0;
        if (!Console.IsOutputRedirected) Console.Clear();
        while (!ct.IsCancellationRequested)
        {
            string? url = live?.WebUrl ?? replay?.Url;
            try { if (url is not null) { status = await http.GetFromJsonAsync<JsonElement>(url + "api/status", ct); statusAt = DateTimeOffset.Now; } }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException) { }
            var transport = live is not null ? JsonSerializer.SerializeToElement(live.Snapshot) : Obj(status, "transport");
            var counts = Obj(status, "counts"); var rates = Obj(status, "rates"); var stats = Obj(status, "stats");
            var playback = Obj(status, "replay");
            var flows = Obj(status, "flows");
            long decodeErrors = flows.ValueKind == JsonValueKind.Array ? flows.EnumerateArray().Sum(f => Num(f,"packetErrors")+Num(f,"semanticErrors")) : 0;
            long up = Num(transport, "udpUpPackets"), down = Num(transport, "udpDownPackets");
            double dt = Math.Max(.01, (DateTimeOffset.Now - lastRate).TotalSeconds);
            double upRate = (up - previousUp) / dt, downRate = (down - previousDown) / dt;
            previousUp = up; previousDown = down; lastRate = DateTimeOffset.Now;
            int keys = (int)Num(transport, "bankCandidates"); long plain = Num(transport, "udpPlainPackets");
            if (keys > oldKeys) events.Enqueue($"{DateTime.Now:HH:mm:ss} 发现 Key 候选，正在验证 UDP");
            if (plain > 0 && oldPlain == 0) events.Enqueue($"{DateTime.Now:HH:mm:ss} UDP 验证通过，已开始输出明文");
            oldKeys = keys; oldPlain = plain;
            while (events.Count > 3) events.Dequeue();
            long lag = Math.Max(0, Num(transport, "journalBytes") - Num(status, "journalReadBytes"));
            string error = Str(status, "error");
            string archiveError = Str(transport, "archiveError");
            string parserError = Str(Obj(transport, "parser"), "error");
            if (error.Length == 0 && parserError.Length > 0) error = parserError;
            if (error.Length == 0 && (statusAt is null || (DateTimeOffset.Now-statusAt.Value).TotalSeconds > 3))
                error = "解析状态连接中断，以下解析数值为上次结果";
            if (error.Length == 0 && live is not null && !live.Running) error = "采集线程已停止，请停止保存并检查日志";
            long established = Num(transport, "tgcpEstablished");
            long boundFlows = Num(transport, "udpBoundFlows");
            long plainUp = Num(stats, "upPackets"), plainDown = Num(stats, "downPackets");
            var lines = new List<string>
            {
                $"{ProductInfo.Name} {ProductInfo.Version} | {(live is not null ? "实时解析" : "离线回放 · 人物状态")} | {(DateTimeOffset.Now-started):hh\\:mm\\:ss}",
                $"网页 {url}",
                "",
                "[采集 / 解密]",
                $"↑ 上行 {up:N0} 包 | {upRate:F1} 包/秒     ↓ 下行 {down:N0} 包 | {downRate:F1} 包/秒",
                $"{(established > 0 && keys > 0 && boundFlows > 0 ? "✓" : "…")} 握手 {established} | Key {keys} | UDP 已验证 {boundFlows}",
                $"{(plainUp + plainDown > 0 ? "✓" : "…")} 明文 ↑ {plainUp:N0} / ↓ {plainDown:N0} | 待解密 {Num(transport,"pendingPackets")}",
                "",
                "[人物 / 物资]",
                $"人物 {Num(counts,"characters")}（含自身 {Num(counts,"charactersIncludingSelf")}）| 确认人机 {Num(counts,"confirmedBots")} | AI单位 {Num(counts,"ai")} | 物资 {Num(counts,"loot")}（ID 待识别 {Num(status,"unknownLoot")}）",
                $"死亡盒 {Num(counts,"box")} | 容器 {Num(counts,"container")} | 输入/解析异常 {Num(stats,"inputErrors")}/{decodeErrors} | 序号缺口 {Num(stats,"eventGaps")}",
                "",
                "[更新 / 完整性]",
                $"人物有效坐标样本 {Decimal(rates,"characters"):F1} 次/秒（合计）| 自身姿态 {Decimal(rates,"self"):F1} Hz",
                $"解析输入 {Num(stats,"inputEvents"):N0} 事件 | 积压 {Math.Max(0,Num(transport,"events")-Num(stats,"inputEvents"))} | 归档 {Num(transport,"journalBytes")/1048576d:F1} MiB | 未读取 {lag/1024d:F1} KiB",
                live is null ? $"回放 {Decimal(playback,"position"):F1} / {Decimal(playback,"duration"):F1} 秒 | {Decimal(playback,"speed"):F1}x | {(Bool(playback,"indexing") ? "正在索引" : Bool(playback,"seeking") ? "正在重建关系" : Bool(playback,"paused") ? "暂停" : "播放")}" : "完整事件归档保留；空间不足或写入失败会报告错误。",
                $"状态：{(error.Length > 0 ? "✗ " + error : archiveError.Length > 0 ? "✗ " + archiveError : "✓ 运行中")}",
                ""
            };
            lines.AddRange(events);
            if (details) { lines.Add("目录：" + directory); lines.Add("解析状态：" + status.ToString()); }
            lines.Add(live is not null
                ? "[W] 打开网页  [D] 详情  [S] 停止并重选设备  [Q] 保存并退出"
                : "[W] 网页  [D] 详情  [空格] 回放暂停/继续  [S] 停止保存  [Q] 退出");
            Render(lines);
            if (Console.IsInputRedirected) return 's';
            var until = DateTime.UtcNow.AddMilliseconds(500);
            while (DateTime.UtcNow < until && !ct.IsCancellationRequested)
            {
                if (Console.KeyAvailable)
                {
                    char key = char.ToLowerInvariant(Console.ReadKey(true).KeyChar);
                    if (key is 's' or 'q') return key;
                    if (key == 'd') { details = !details; Console.Clear(); break; }
                    if (key == 'w') { if (live is not null) live.OpenBrowser(); else replay?.OpenBrowser(); }
                    if (key == ' ' && live is null && url is not null)
                        try { await http.GetAsync(url + "api/ctrl?pause=" + (Bool(playback,"paused") ? "0" : "1"), ct); } catch (HttpRequestException) { }
                }
                await Task.Delay(25, ct);
            }
        }
        return 'q';
    }
    private static JsonElement Obj(JsonElement x, string key) => x.ValueKind == JsonValueKind.Object && x.TryGetProperty(key, out var value) ? value : default;
    private static long Num(JsonElement x,string k) => (long)Decimal(x,k);
    private static double Decimal(JsonElement x,string k) { var v=Obj(x,k); return v.ValueKind==JsonValueKind.Number && v.TryGetDouble(out var n)? n:0; }
    private static string Str(JsonElement x,string k) { var v=Obj(x,k); return v.ValueKind==JsonValueKind.String ? v.GetString() ?? "" : ""; }
    private static bool Bool(JsonElement x,string k) => Obj(x,k).ValueKind == JsonValueKind.True;
    private static void Render(List<string> lines)
    {
        if (Console.IsOutputRedirected) { foreach(var line in lines) Console.WriteLine(line); return; }
        try
        {
            int width=Math.Max(20,Console.WindowWidth-1), height=Console.WindowHeight;
            if (lines.Count > height - 1 && height > 2) lines = lines.Take(height-2).Append(lines[^1]).ToList();
            Console.SetCursorPosition(0,0);
            for(int row=0;row<Math.Min(height-1,Math.Max(24,lines.Count));row++)
            {
                var line=row<lines.Count ? lines[row] : "";
                var text=new System.Text.StringBuilder(); int used=0;
                foreach(var c in line){int n=c>255?2:1;if(used+n>width)break;text.Append(char.IsControl(c)?' ':c);used+=n;}
                var previousColor = Console.ForegroundColor;
                Console.ForegroundColor = ColorFor(line);
                Console.Write(text.ToString()+new string(' ',width-used));Console.WriteLine();
                Console.ForegroundColor = previousColor;
            }
        }
        catch (ArgumentOutOfRangeException) { }
        catch (IOException) { }
    }

    private static ConsoleColor ColorFor(string line)
    {
        if (line.StartsWith(ProductInfo.Name, StringComparison.Ordinal)) return ConsoleColor.Cyan;
        if (line.StartsWith("[", StringComparison.Ordinal)) return ConsoleColor.DarkCyan;
        if (line.StartsWith("↑", StringComparison.Ordinal)) return ConsoleColor.Cyan;
        if (line.StartsWith("✓", StringComparison.Ordinal) || line.Contains("状态：✓", StringComparison.Ordinal)) return ConsoleColor.Green;
        if (line.StartsWith("…", StringComparison.Ordinal)) return ConsoleColor.Yellow;
        if (line.Contains("状态：✗", StringComparison.Ordinal)) return ConsoleColor.Red;
        if (line.StartsWith("网页 ", StringComparison.Ordinal) || line.StartsWith("目录：", StringComparison.Ordinal)) return ConsoleColor.DarkGray;
        return ConsoleColor.Gray;
    }
}
