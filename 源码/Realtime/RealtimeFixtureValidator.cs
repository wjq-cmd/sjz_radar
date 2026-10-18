using System.Net;
using System.Security.Cryptography;
using System.Text.Json;

namespace GatewayCapture.Realtime;

internal static class RealtimeFixtureValidator
{
    public static object VerifyUdp(string path)
    {
        using var reader = File.OpenText(path);
        using var first = JsonDocument.Parse(reader.ReadLine() ?? throw new InvalidDataException("Empty fixture"));
        byte[] bank = Convert.FromBase64String(first.RootElement.GetProperty("bank").GetString()!);
        string folder = Path.Combine(Path.GetTempPath(), "GatewayCaptureUdpFixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        long total = 0, valid = 0, emitted = 0;
        var expected = new Dictionary<string, int>(); var actual = new Dictionary<string, int>();
        void Count(Dictionary<string, int> map, string key) { map[key] = map.GetValueOrDefault(key) + 1; }
        try
        {
            var broker = new UdpKeyBroker(folder, (type, data) =>
            {
                if (type != "udp_plaintext") return;
                using var doc = JsonDocument.Parse(JsonSerializer.Serialize(data));
                var e = doc.RootElement;
                var plain = Convert.FromBase64String(e.GetProperty("payloadBase64").GetString()!);
                Count(actual, e.GetProperty("direction").GetString() + ":" + Convert.ToHexString(SHA256.HashData(plain)).ToLowerInvariant()); emitted++;
            });
            broker.Add(1, new(false, 0x4013, 611, [], [bank]));
            while (reader.ReadLine() is { } line)
            {
                using var doc = JsonDocument.Parse(line); var e = doc.RootElement;
                bool up = e.GetProperty("up").GetBoolean();
                var payload = Convert.FromBase64String(e.GetProperty("payload").GetString()!);
                var plain = up ? payload[8..] : UdpDecoder.Decrypt(payload, bank);
                string digest = Convert.ToHexString(SHA256.HashData(plain)).ToLowerInvariant();
                if (digest != e.GetProperty("expectedPlainSha256").GetString()) throw new InvalidDataException($"UDP 明文不匹配: {total}");
                bool envelope = UdpDecoder.ValidEnvelope(plain);
                if (envelope != e.GetProperty("validEnvelope").GetBoolean()) throw new InvalidDataException($"UDP envelope 不匹配: {total}");
                Count(expected, (up ? "up:" : "down:") + digest);
                broker.Observe(new(up, IPEndPoint.Parse(e.GetProperty("local").GetString()!), IPEndPoint.Parse(e.GetProperty("remote").GetString()!), payload, e.GetProperty("timestampMs").GetInt64()));
                total++; if (envelope) valid++;
            }
            if (total != emitted || expected.Count != actual.Count || expected.Any(p => actual.GetValueOrDefault(p.Key) != p.Value)) throw new InvalidDataException($"UDP broker 输出不完整: expected={total}, emitted={emitted}");
            return new { passed = true, total, validEnvelopes = valid, brokerEmitted = emitted, boundFlows = broker.ValidatedFlows, referenceFixture = true, liveNetwork = false };
        }
        finally { Directory.Delete(folder, true); }
    }
}
