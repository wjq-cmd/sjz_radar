using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GatewayCapture.Realtime;

internal static class RealtimeSelfTest
{
    private static void Check(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
    private static TgcpFrame Frame(bool up, ushort command, byte[] body, System.Numerics.BigInteger? publicValue = null, uint sequence = 1)
    {
        var header = new byte[publicValue.HasValue ? (up ? 206 : 97) : 25];
        TgcpProtocol.Magic.CopyTo(header, 0); BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(6), command);
        header[8] = up ? (byte)0 : (byte)1; BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(9), sequence);
        BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(13), (uint)header.Length);
        BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(17), (uint)body.Length);
        if (publicValue.HasValue) { header[21] = 3; header[23] = 64; TgcpProtocol.PublicBytes(publicValue.Value).CopyTo(header, 24); }
        return new(header, body);
    }
    private static TgcpFrame Parse(byte[] wire)
    { var (h, b) = TgcpProtocol.Lengths(wire); Check(wire.Length == h + b, "frame bounds"); return new(wire[..h], wire[h..]); }
    private static async Task<TgcpFrame> ReadFrame(NetworkStream stream)
    {
        var header = new byte[21]; await stream.ReadExactlyAsync(header);
        var (h, b) = TgcpProtocol.Lengths(header); var remaining = new byte[h - 21 + b]; await stream.ReadExactlyAsync(remaining);
        return new(header.Concat(remaining.AsSpan(0, h - 21).ToArray()).ToArray(), remaining[(h - 21)..]);
    }
    private static byte[] EncryptUdp(byte[] plain, byte[] key)
    {
        var output = plain.ToArray();
        unchecked
        {
            for (int offset = 0; offset + 8 <= output.Length; offset += 8)
            {
                uint a = BinaryPrimitives.ReadUInt32LittleEndian(output.AsSpan(offset)), b = BinaryPrimitives.ReadUInt32LittleEndian(output.AsSpan(offset + 4)), sum = 0;
                int at = offset / 8 % 8 * 16;
                for (int round = 0; round < 32; round++)
                {
                    a += (((b << 4) ^ (b >> 5)) + b) ^ (sum + BinaryPrimitives.ReadUInt32LittleEndian(key.AsSpan(at + (int)(sum & 3) * 4)));
                    sum += 0x9e3779b9;
                    b += (((a << 4) ^ (a >> 5)) + a) ^ (sum + BinaryPrimitives.ReadUInt32LittleEndian(key.AsSpan(at + (int)((sum >> 11) & 3) * 4)));
                }
                BinaryPrimitives.WriteUInt32LittleEndian(output.AsSpan(offset), a); BinaryPrimitives.WriteUInt32LittleEndian(output.AsSpan(offset + 4), b);
            }
        }
        return output;
    }
    public static async Task RunAsync()
    {
        int checks = 0;
        using (var session = new TgcpSession())
        {
            var a = TgcpProtocol.KeyPair(); var b = TgcpProtocol.KeyPair();
            var hello = Frame(true, 0x1001, [], a.Public).Wire();
            for (int i = 0; i < hello.Length; i++) Check(TgcpProtocol.Sniff(hello.AsSpan(0, i)) == 0, "partial hello classification");
            Check(TgcpProtocol.Sniff(hello) == 2, "hello classification");
            byte[] first = [];
            foreach (var value in hello) { var outp = session.Feed(true, [value]); first = first.Concat(outp.ToServer).ToArray(); }
            var serverKey = TgcpProtocol.Derive(Parse(first).Public(0x1001), b.Private);
            byte[] bank = RandomNumberGenerator.GetBytes(128);
            byte[] plain = Encoding.ASCII.GetBytes("CSPlayerJoinMatchNtf:" + Convert.ToBase64String(bank) + ";");
            var response = session.Feed(false, Frame(false, 0x1002, TgcpProtocol.Encrypt(plain, serverKey), b.Public).Wire());
            var serverHello = Parse(response.ToClient); var clientKey = TgcpProtocol.Derive(serverHello.Public(0x1002), a.Private);
            Check(TgcpProtocol.Unpad(TgcpProtocol.DecryptPadded(serverHello.Body, clientKey)).SequenceEqual(plain), "server hello payload");
            Check(response.Observations.Single().Banks.Single().SequenceEqual(bank), "bank extraction");
            for (int size = 0; size < 128; size++)
            {
                var content = RandomNumberGenerator.GetBytes(size);
                foreach (bool up in new[] { true, false })
                {
                    var transformed = session.Feed(up, Frame(up, 0x4013, TgcpProtocol.Encrypt(content, up ? clientKey : serverKey)).Wire());
                    var frame = Parse(up ? transformed.ToServer : transformed.ToClient);
                    Check(TgcpProtocol.Unpad(TgcpProtocol.DecryptPadded(frame.Body, up ? serverKey : clientKey)).SequenceEqual(content), "all padding residues"); checks++;
                }
            }
            session.Finish(true); session.Finish(false);
            for (int size = 1; size <= 144; size++) { var data = RandomNumberGenerator.GetBytes(size); Check(UdpDecoder.Decrypt(EncryptUdp(data, bank), bank).SequenceEqual(data), "UDP bank rotation/tail"); checks++; }
        }
        var directory = Path.Combine(Path.GetTempPath(), "GatewayCaptureV4Tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        try
        {
            var emitted = new List<(string Type, object Data)>();
            var broker = new UdpKeyBroker(directory, (type, data) => emitted.Add((type, data)));
            var bank = RandomNumberGenerator.GetBytes(128);
            broker.Add(1, new(false, 0x4013, 611, [], [bank]));
            var local = new IPEndPoint(IPAddress.Parse("192.168.137.88"), 53000); var remote = new IPEndPoint(IPAddress.Parse("203.0.113.8"), 12345);
            var initial = new byte[33]; initial[8] = 1; initial[^1] = 8;
            var reply = RandomNumberGenerator.GetBytes(25); reply[^1] = 8;
            var expected1 = reply[..4].Concat(Convert.FromHexString("1a420b63632b733b2b1b7a7b5b4b2b030000000008")).ToArray();
            var expected2 = new byte[25]; Convert.FromHexString("050000fc").CopyTo(expected2, 0); expected2[4] = (byte)((reply[4] & 0xf8) | 5); reply[5..].CopyTo(expected2, 5);
            byte[][] payloads = [initial, EncryptUdp(expected1, bank), new byte[8].Concat(reply).ToArray(), EncryptUdp(expected2, bank), new byte[35]];
            for (int i = 0; i < payloads.Length; i++) broker.Observe(new(i % 2 == 0, local, remote, payloads[i], DateTimeOffset.UtcNow.AddHours(2).ToUnixTimeMilliseconds()));
            Check(broker.ValidatedFlows == 1 && emitted.Any(e => e.Type == "udp_bank_bound"), "UDP key binding after a long lobby wait"); checks++;
            var lateEvents = new List<(string Type, object Data)>();
            var late = new UdpKeyBroker(Path.Combine(directory, "late"), (type, data) => lateEvents.Add((type, data)));
            byte[][] envelopes = [Convert.FromBase64String("IGw2swMAAAAADA=="), Convert.FromBase64String("YGxGsw8AAAAADA=="), Convert.FromBase64String("gGxOsx8AAAAADA==")];
            for (int i = 0; i < 200; i++) late.Observe(new(false, local, remote, EncryptUdp(envelopes[i%3],bank), 1000+i));
            Check(late.PendingCount == 200 && late.PlainPackets == 0, "pending overflow preserved before bank"); checks++;
            late.Add(2, new(false, 0x4013, 612, [], [bank]));
            Check(late.ValidatedFlows == 1 && late.PlainPackets == 200 && late.PendingCount == 0, "late bank drains disk and memory without loss"); checks++;
            var timestamps = lateEvents.Where(e=>e.Type=="udp_plaintext").Select(e=>JsonSerializer.SerializeToElement(e.Data).GetProperty("timestampMs").GetInt64()).ToArray();
            Check(timestamps.SequenceEqual(Enumerable.Range(1000,200).Select(i=>(long)i)), "late replay chronological order"); checks++;
            late.Observe(new(false,local,remote,new byte[9],1300));
            Check(lateEvents.Count(e=>e.Type=="udp_raw")==201 && late.InvalidPackets==1, "raw invalid payload also archived"); checks++;
            await using (var output = new RealtimeOutput(directory, 0))
            {
                output.Start(); using var consumer = new TcpClient(); await consumer.ConnectAsync(IPAddress.Loopback, output.Port);
                using var reader = new StreamReader(consumer.GetStream(), Encoding.UTF8);
                var ready = await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(3)); Check(ready!.Contains("stream_ready"), "stream banner");
                output.Publish("test_packet", new { value = 42 });
                var line = await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(3));
                Check(JsonDocument.Parse(line!).RootElement.GetProperty("type").GetString() == "test_packet", "realtime output"); checks++;
            }
            Check(File.ReadAllText(Path.Combine(directory, "realtime.jsonl")).Contains("test_packet"), "archive output");
            await TestSocketAsync(); checks++;
            await using (var router = new HotspotTcpRouter(local.Address, IPAddress.Parse("192.168.137.1"), IPAddress.Parse("255.255.255.0"), 45000, 4, (_, _) => { }, _ => { }))
            {
                foreach (var pair in new[] { (router.ForwardFilter, 1), (router.ReturnFilter, 0), (router.UdpFilter, 1) })
                    Check(WinDivertNative.CompileFilter(pair.Item1, pair.Item2, IntPtr.Zero, 0, out var error, out var offset), "WinDivert filter compilation");
                var parsed = new IpTransport(local.Address, remote.Address, local.Port, remote.Port, 20, 40, 20, 6);
                var route = router.MapForward(parsed, 2, 1234)!;
                Check(route.AliasPort >= 20000 && router.MapForward(parsed, 2, 1234) == route, "SYN retransmission alias reuse");
                Check(router.Resolve(new(local.Address, route.AliasPort))!.Equals(remote), "original endpoint lookup");
                Check(router.MapForward(parsed, 2, 1235)!.AliasPort != route.AliasPort, "new SYN sequence creates new alias");
                var originalDown = new IpTransport(remote.Address, local.Address, remote.Port, local.Port, 20, 40, 20, 6);
                Check(router.IsOriginalDownstream(originalDown) && !router.IsOriginalDownstream(originalDown with { DestinationPort = local.Port + 1 }), "suppress original NAT replies only for owned routes"); checks++;
                var packet = new byte[40]; packet[0] = 0x45; packet[9] = 6; packet[32] = 0x50; BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(2), 40);
                IpTransport.Rewrite(packet, parsed, local.Address, route.AliasPort, IPAddress.Parse("192.168.137.1"), 45000);
                Check(IpTransport.Parse(packet, packet.Length, out var rewritten) && rewritten.SourcePort == route.AliasPort && rewritten.DestinationPort == 45000, "packet rewrite");
                var address = new byte[80]; Check(WinDivertNative.Checksums(packet, (uint)packet.Length, address, 0), "checksum helper"); checks++;
                BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(8), 1u | 1u << 17);
                BinaryPrimitives.WriteUInt32LittleEndian(address.AsSpan(16), 15);
                HotspotTcpRouter.SetInboundInterface(address, 4);
                Check((BinaryPrimitives.ReadUInt32LittleEndian(address.AsSpan(8)) & (0xffu | 1u << 17 | 1u << 18)) == 0 && BinaryPrimitives.ReadUInt32LittleEndian(address.AsSpan(16)) == 4, "forward egress must become hotspot ingress"); checks++;
            }
        }
        finally { Directory.Delete(directory, true); }
        Console.WriteLine(JsonSerializer.Serialize(new { v4SelfTest = "passed", checks, actualHotspotTraffic = false }));
    }
    private static async Task TestSocketAsync()
    {
        var upstream = new TcpListener(IPAddress.Loopback, 0); upstream.Start();
        var errors = new List<string>();
        try
        {
            var serverTask = Task.Run(async () =>
            {
                using var server = await upstream.AcceptTcpClientAsync(); var stream = server.GetStream();
                var clientHello = await ReadFrame(stream); var b = TgcpProtocol.KeyPair(); var key = TgcpProtocol.Derive(clientHello.Public(0x1001), b.Private);
                await stream.WriteAsync(Frame(false, 0x1002, TgcpProtocol.Encrypt("hello"u8.ToArray(), key), b.Public).Wire());
                var frame = await ReadFrame(stream); var plain = TgcpProtocol.Unpad(TgcpProtocol.DecryptPadded(frame.Body, key));
                Check(Encoding.UTF8.GetString(plain) == "socket payload", "server socket plaintext");
                await stream.WriteAsync(Frame(false, 0x4013, TgcpProtocol.Encrypt(plain, key)).Wire()); server.Client.Shutdown(SocketShutdown.Send);
                Check(await stream.ReadAsync(new byte[1]) == 0, "server EOF");
            });
            await using var relay = new TcpRelayServer(IPAddress.Loopback, 0, _ => (IPEndPoint)upstream.LocalEndpoint,
                (type, data) => { if (type == "tcp_error") lock (errors) errors.Add(JsonSerializer.Serialize(data)); }, (_, _) => { });
            relay.Start(); using var client = new TcpClient(); await client.ConnectAsync(IPAddress.Loopback, relay.Port);
            var cs = client.GetStream(); var a = TgcpProtocol.KeyPair(); var request = Frame(true, 0x1001, [], a.Public).Wire();
            foreach (byte value in request) await cs.WriteAsync(new byte[] { value });
            var hello = await ReadFrame(cs); var clientKey = TgcpProtocol.Derive(hello.Public(0x1002), a.Private);
            Check(Encoding.UTF8.GetString(TgcpProtocol.Unpad(TgcpProtocol.DecryptPadded(hello.Body, clientKey))) == "hello", "client hello payload");
            await cs.WriteAsync(Frame(true, 0x4013, TgcpProtocol.Encrypt("socket payload"u8.ToArray(), clientKey)).Wire()); client.Client.Shutdown(SocketShutdown.Send);
            var reply = await ReadFrame(cs); Check(Encoding.UTF8.GetString(TgcpProtocol.Unpad(TgcpProtocol.DecryptPadded(reply.Body, clientKey))) == "socket payload", "client socket plaintext");
            Check(await cs.ReadAsync(new byte[1]) == 0, "client EOF");
            await serverTask.WaitAsync(TimeSpan.FromSeconds(5)); Check(errors.Count == 0, "socket relay errors");
        }
        finally { upstream.Stop(); }
    }
}
