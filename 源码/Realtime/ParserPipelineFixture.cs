using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace GatewayCapture.Realtime;

// Offline fixture: validates the production fan-out to archive, TCP client and parser.
internal static class ParserPipelineFixture
{
    public static async Task RunAsync(string source, string directory, bool cacheOnly = false)
    {
        Directory.CreateDirectory(directory);
        await using var parser = new ParserHost();
        await parser.StartAsync(Path.Combine(directory, "semantic"), 0, replay: true, cacheOnly: cacheOnly);
        await using var output = new RealtimeOutput(directory, 0, parser.Enqueue, archive: !cacheOnly);
        output.Start();
        using var client = new TcpClient { NoDelay = true };
        await client.ConnectAsync("127.0.0.1", output.Port);
        using var reader = new StreamReader(client.GetStream(), Encoding.UTF8);
        using (var ready = JsonDocument.Parse((await reader.ReadLineAsync())!))
            if (ready.RootElement.GetProperty("type").GetString() != "stream_ready") throw new IOException("Missing stream greeting.");
        var events = File.ReadAllLines(source).Where(s => !string.IsNullOrWhiteSpace(s)).ToArray();
        var received = new List<string>();
        var collect = Task.Run(async () =>
        {
            for (int i = 0; i < events.Length; i++)
                received.Add(await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(30)) ?? throw new IOException("Early TCP EOF."));
        });
        int sent = 0;
        foreach (var line in events)
        {
            using var document = JsonDocument.Parse(line);
            output.Publish(document.RootElement.GetProperty("type").GetString()!, document.RootElement.GetProperty("data"));
            if (++sent % 64 == 0) await Task.Delay(1);
        }
        parser.Enqueue(Encoding.UTF8.GetBytes("{\"type\":\"parser_input_complete\"}\n"));
        await parser.WaitForInputCompleteAsync();
        await collect;
        using var http = new HttpClient();
        using var status = JsonDocument.Parse(await http.GetStringAsync(parser.Url + "api/status"));
        using var state = JsonDocument.Parse(await http.GetStringAsync(parser.Url + "api/state"));
        int expectedPackets = events.Count(line => { using var doc = JsonDocument.Parse(line); return doc.RootElement.GetProperty("type").GetString() == "udp_plaintext"; });
        if (status.RootElement.GetProperty("stats").GetProperty("plaintextPackets").GetInt32() != expectedPackets)
            throw new IOException("Parser input count mismatch.");
        if (status.RootElement.GetProperty("stats").TryGetProperty("inputErrors", out var errors) && errors.GetInt32() != 0)
            throw new IOException("Parser input errors.");
        foreach (var flow in status.RootElement.GetProperty("flows").EnumerateArray())
            if (flow.GetProperty("packetErrors").GetInt32() != 0 || flow.GetProperty("semanticErrors").GetInt32() != 0)
                throw new IOException("Parser decode errors.");
        int entities = state.RootElement.GetProperty("entities").GetArrayLength();
        await output.DisposeAsync();
        await parser.DisposeAsync();
        if (parser.Error is not null) throw new IOException(parser.Error);
        if (!cacheOnly)
        {
            var archive = File.ReadAllLines(Path.Combine(directory, "realtime.jsonl"));
            if (!archive.SequenceEqual(received)) throw new IOException("TCP stream differs from the archive.");
        }
        else
        {
            if (File.Exists(Path.Combine(directory, "realtime.jsonl")) || File.Exists(Path.Combine(directory, "semantic", "states.jsonl")))
                throw new IOException("Compact mode unexpectedly archived packets or states.");
            for (int i = 0; i < events.Length; i++)
            {
                using var original = JsonDocument.Parse(events[i]); using var actual = JsonDocument.Parse(received[i]);
                if (actual.RootElement.GetProperty("sequence").GetInt32() != i + 1 ||
                    original.RootElement.GetProperty("type").GetString() != actual.RootElement.GetProperty("type").GetString() ||
                    JsonSerializer.Serialize(original.RootElement.GetProperty("data")) != JsonSerializer.Serialize(actual.RootElement.GetProperty("data")))
                    throw new IOException("Live stream payload mismatch.");
            }
        }
        if (output.ArchiveDropped != 0 || output.SlowClients != 0) throw new IOException("Output lost events.");
        var result = new { events = events.Length, plaintextPackets = expectedPackets, entities,
            cacheOnly, archiveMatchesTcp = !cacheOnly, tcpMatchesInput = cacheOnly, parser = parser.Result, output.ArchiveDropped, output.SlowClients };
        await File.WriteAllTextAsync(Path.Combine(directory, "pipeline-result.json"), JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine(JsonSerializer.Serialize(result));
    }
}
