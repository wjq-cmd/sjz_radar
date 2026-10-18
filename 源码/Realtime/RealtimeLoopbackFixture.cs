using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;

namespace GatewayCapture.Realtime;

// Test-only transport: local endpoints exercise the production relay, bank
// broker and JSONL output together. No reference key is accepted by this host.
internal static class RealtimeLoopbackFixture
{
    public static async Task RunAsync(int upstreamPort, string directory)
    {
        Directory.CreateDirectory(directory);
        var consoleGate = new object();
        void Report(string type, object data)
        { lock (consoleGate) Console.WriteLine(JsonSerializer.Serialize(new { type, data })); }
        await using var output = new RealtimeOutput(directory, 0);
        output.Start();
        void Publish(string type, object data)
        {
            output.Publish(type, data);
            if (type != "udp_plaintext") Report(type, data);
        }
        var broker = new UdpKeyBroker(directory, Publish);
        await using var relay = new TcpRelayServer(IPAddress.Loopback, 0,
            _ => new(IPAddress.Loopback, upstreamPort), Publish, broker.Add);
        using var udp = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0));
        udp.Client.ReceiveBufferSize = 4 * 1024 * 1024;
        using var stop = new CancellationTokenSource();
        async Task ReceiveAsync()
        {
            try
            {
                while (!stop.IsCancellationRequested)
                {
                    var received = await udp.ReceiveAsync(stop.Token);
                    var bytes = received.Buffer;
                    // direction(1), timestamp LE(8), local IP/port(6), remote
                    // IP/port(6), original UDP payload. Only loopback accepted.
                    if (!IPAddress.IsLoopback(received.RemoteEndPoint.Address) || bytes.Length < 21 || bytes[0] > 1)
                        throw new InvalidDataException("Loopback fixture datagram format.");
                    broker.Observe(new(bytes[0] == 1,
                        new(new IPAddress(bytes.AsSpan(9, 4)), BinaryPrimitives.ReadUInt16BigEndian(bytes.AsSpan(13))),
                        new(new IPAddress(bytes.AsSpan(15, 4)), BinaryPrimitives.ReadUInt16BigEndian(bytes.AsSpan(19))),
                        bytes[21..], BinaryPrimitives.ReadInt64LittleEndian(bytes.AsSpan(1))));
                }
            }
            catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
        }
        relay.Start();
        var receive = ReceiveAsync();
        Report("listening", new { host = "127.0.0.1", port = relay.Port,
            udpPort = ((IPEndPoint)udp.Client.LocalEndPoint!).Port, outputPort = output.Port });
        try { while (await Console.In.ReadLineAsync() is { } line && line.Trim() != "stop") { } }
        finally { stop.Cancel(); await receive; }
        Report("fixture_result", new { udpObserved = broker.Packets, udpPlainPackets = broker.PlainPackets,
            bankCandidates = broker.CandidateCount, boundFlows = broker.ValidatedFlows, output.ArchiveDropped });
    }
}
