using System.Buffers.Binary;
using System.Net;
using System.Security.Cryptography;

namespace GatewayCapture.Realtime;

internal static class UdpDecoder
{
    public static byte[] Decrypt(byte[] input, byte[] bank)
    {
        if (bank.Length != 128) throw new ArgumentException("UDP bank 必须为 128 字节。");
        var result = input.ToArray();
        unchecked
        {
            for (int offset = 0; offset + 8 <= input.Length; offset += 8)
            {
                uint a = BinaryPrimitives.ReadUInt32LittleEndian(input.AsSpan(offset));
                uint b = BinaryPrimitives.ReadUInt32LittleEndian(input.AsSpan(offset + 4));
                uint sum = 0xc6ef3720;
                int at = (offset / 8 % 8) * 16;
                for (int round = 0; round < 32; round++)
                {
                    b -= (((a << 4) ^ (a >> 5)) + a) ^ (sum + BinaryPrimitives.ReadUInt32LittleEndian(bank.AsSpan(at + (int)((sum >> 11) & 3) * 4)));
                    sum -= 0x9e3779b9;
                    a -= (((b << 4) ^ (b >> 5)) + b) ^ (sum + BinaryPrimitives.ReadUInt32LittleEndian(bank.AsSpan(at + (int)(sum & 3) * 4)));
                }
                BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(offset), a);
                BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(offset + 4), b);
            }
        }
        return result;
    }
    private sealed class Bits(byte[] data, int end)
    {
        public int Position = 1;
        public uint? Read(int count)
        {
            if (count < 0 || count > 32 || count > end - Position) return null;
            uint value = 0;
            for (int i = 0; i < count; i++) value |= (uint)((data[(Position + i) >> 3] >> ((Position + i) & 7)) & 1) << i;
            Position += count; return value;
        }
        public uint? Packed()
        {
            uint value = 0;
            for (int i = 0; i < 5; i++)
            {
                var raw = Read(8);
                if (raw is null || i == 4 && (raw.Value & 0xe1) != 0) return null;
                value |= (raw.Value >> 1) << (i * 7);
                if ((raw.Value & 1) == 0) return value;
            }
            return null;
        }
    }
    public static bool ValidEnvelope(byte[] data)
    {
        if (data.Length < 10 || data.Length > 2048 || (data[0] & 1) != 0 || data[^1] == 0) return false;
        int bits = 0; for (int v = data[^1]; v != 0; v >>= 1) bits++;
        int end = (data.Length - 1) * 8 + bits - 2;
        if (end < 0 || ((data[end >> 3] >> (end & 7)) & 1) == 0) return false;
        var reader = new Bits(data, end);
        var words = reader.Read(4); if (words is null) return false;
        reader.Position = 33 + ((int)words.Value + 1) * 32; if (reader.Position > end) return false;
        var marker = reader.Read(1);
        if (marker is null || marker == 1 && reader.Read(8) is null || reader.Read(8) is null) return false;
        int bunches = 0;
        while (reader.Position < end)
        {
            if (end - reader.Position < 20 || bunches >= 128) return false;
            var control = reader.Read(1); if (control is null) return false;
            bool opened = false;
            if (control == 1)
            {
                opened = reader.Read(1) == 1;
                bool closed = reader.Read(1) == 1;
                if (closed && reader.Read(4) is null) return false;
            }
            if (reader.Read(1) is null) return false;
            var reliable = reader.Read(1);
            if (reliable is null || reader.Read(1) is null) return false;
            var channel = reader.Packed();
            if (channel is null || channel > 32767 || reader.Read(1) is null || reader.Read(1) is null) return false;
            var partial = reader.Read(1);
            if (partial is null || reliable == 1 && reader.Read(10) is null || partial == 1 && (reader.Read(1) is null || reader.Read(1) is null)) return false;
            if (reliable == 1 || opened)
            {
                var exported = reader.Read(1); if (exported is null) return false;
                if (exported == 1) { if (reader.Packed() is null) return false; }
                else
                {
                    var length = reader.Read(32); if (length is null) return false;
                    long signed = unchecked((int)length.Value), count = Math.Abs(signed);
                    int width = signed < 0 ? 16 : 8;
                    if (count == 0 || count > 256 || count * width > end - reader.Position) return false;
                    reader.Position += (int)count * width; if (reader.Read(32) is null) return false;
                }
            }
            var size = reader.Read(13); if (size is null || size > end - reader.Position) return false;
            reader.Position += (int)size.Value; bunches++;
        }
        return reader.Position == end;
    }
}

internal sealed record UdpPacket(bool Up, IPEndPoint Local, IPEndPoint Remote, byte[] Payload, long TimestampMs);
internal sealed record BankCandidate(string Fingerprint, byte[] Key, long Connection, uint Sequence, long CreatedMs);

internal sealed class UdpKeyBroker
{
    private sealed class Flow
    {
        public List<UdpPacket> Pending = [];
        public string? Spool;
        public long SpoolCount;
        public BankCandidate? Bank;
        public long LastSeen, Epoch;
        public int Failures;
    }
    private readonly object _gate = new();
    private readonly List<BankCandidate> _candidates = [];
    private readonly Dictionary<string, Flow> _flows = [];
    private readonly Action<string, object> _emit;
    private readonly string? _keyDirectory;
    private readonly string _pendingDirectory;
    public long UpPackets, DownPackets, UpBytes, DownBytes, PlainUp, PlainDown;
    public long PendingCount { get { lock (_gate) return _flows.Values.Sum(f => f.Pending.Count + f.SpoolCount); } }
    private long _epoch;
    public long Packets, PlainPackets, ValidatedFlows, InvalidPackets;
    public long PendingEvictions = 0;
    public int CandidateCount { get { lock (_gate) return _candidates.Count; } }
    public UdpKeyBroker(string directory, Action<string, object> emit, bool persistKeys = true)
    {
        _pendingDirectory = Path.Combine(directory, "pending");
        if (persistKeys) _keyDirectory = Path.Combine(directory, "keys"); if (_keyDirectory is not null) Directory.CreateDirectory(_keyDirectory); _emit = emit;
    }
    public void Add(long connection, TgcpObservation observation)
    {
        lock (_gate)
        {
            foreach (var key in observation.Banks)
            {
                string fingerprint = Convert.ToHexString(SHA256.HashData(key)).ToLowerInvariant();
                if (_candidates.Any(x => x.Fingerprint == fingerprint)) continue;
                var candidate = new BankCandidate(fingerprint, key.ToArray(), connection, observation.Sequence, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                _candidates.Add(candidate); if (_candidates.Count > 64) _candidates.RemoveAt(0);
                if (_keyDirectory is not null) File.WriteAllBytes(Path.Combine(_keyDirectory, fingerprint + ".bin"), candidate.Key);
                _emit("bank_candidate", new { fingerprint, connection, tgcpSequence = observation.Sequence, keyLength = 128 });
            }
            foreach (var flow in _flows.Values.Where(f => f.Bank is null && f.Pending.Count > 0)) TryBind(flow);
        }
    }
    private static string FlowId(UdpPacket p) => $"{p.Local}>{p.Remote}";
    public void Observe(UdpPacket packet)
    {
        lock (_gate)
        {
            // Preserve observed datagrams before validation; rejected data remains recoverable.
            _emit("udp_raw", new { timestampMs = packet.TimestampMs, direction = packet.Up ? "up" : "down",
                local = packet.Local.ToString(), remote = packet.Remote.ToString(),
                payloadLength = packet.Payload.Length, payloadBase64 = Convert.ToBase64String(packet.Payload) });
            Packets++;
            if (packet.Up) { UpPackets++; UpBytes += packet.Payload.Length; } else { DownPackets++; DownBytes += packet.Payload.Length; }
            string id = FlowId(packet);
            if (!_flows.TryGetValue(id, out var flow))
            {
                if (_flows.Count >= 1024) throw new IOException("UDP 连接数量超出当前容量；停止解析，保留已记录数据。");
                _flows[id] = flow = new();
            }
            flow.LastSeen = packet.TimestampMs;
            // A fresh UE connection handshake resets any old binding even if
            // the same endpoint tuple is reused by a later match.
            if (packet.Up && IsInitial(packet.Payload) && (flow.Bank is not null || flow.Pending.Count > 0))
            { flow.Bank = null; flow.Pending.Clear(); flow.Spool = null; flow.SpoolCount = 0; flow.Failures = 0; }
            if (flow.Bank is not null)
            {
                if (EmitPlain(packet, flow)) { flow.Failures = 0; return; }
                if (!packet.Up && ++flow.Failures >= 3)
                {
                    _emit("udp_binding_reset", new { flow = id, reason = "consecutive_invalid_payloads" });
                    flow.Bank = null; flow.Pending.Clear(); flow.Spool = null; flow.SpoolCount = 0; flow.Failures = 0;
                }
                else return;
            }
            flow.Pending.Add(packet);
            if (flow.Pending.Count > 64)
            {
                Directory.CreateDirectory(_pendingDirectory);
                flow.Spool ??= Path.Combine(_pendingDirectory, Guid.NewGuid().ToString("N") + ".jsonl");
                var oldest = flow.Pending[0];
                File.AppendAllText(flow.Spool, System.Text.Json.JsonSerializer.Serialize(new {
                    up = oldest.Up, timestampMs = oldest.TimestampMs, local = oldest.Local.ToString(),
                    remote = oldest.Remote.ToString(), payload = Convert.ToBase64String(oldest.Payload) }) + "\n");
                flow.Pending.RemoveAt(0); flow.SpoolCount++;
            }
            TryBind(flow);
        }
    }
    private static bool IsInitial(byte[] p) => p.Length == 33 && p[8] == 1 && p[^1] == 8 && p.AsSpan(9, 23).IndexOfAnyExcept((byte)0) < 0;
    private static bool HandshakeMatches(IReadOnlyList<UdpPacket> packets, BankCandidate bank)
    {
        for (int i = 0; i + 4 < packets.Count; i++)
        {
            var a = packets.Skip(i).Take(5).ToArray();
            if (!a[0].Up || a[1].Up || !a[2].Up || a[3].Up || !a[4].Up ||
                !IsInitial(a[0].Payload) || a[1].Payload.Length != 25 || a[2].Payload.Length != 33 || a[3].Payload.Length != 25 || a[4].Payload.Length != 35) continue;
            var reply = a[2].Payload[8..];
            var first = reply[..4].Concat(Convert.FromHexString("1a420b63632b733b2b1b7a7b5b4b2b030000000008")).ToArray();
            var second = new byte[25]; Convert.FromHexString("050000fc").CopyTo(second, 0);
            second[4] = (byte)((reply[4] & 0xf8) | 5); reply.AsSpan(5).CopyTo(second.AsSpan(5));
            if (UdpDecoder.Decrypt(a[1].Payload, bank.Key).SequenceEqual(first) && UdpDecoder.Decrypt(a[3].Payload, bank.Key).SequenceEqual(second)) return true;
        }
        return false;
    }
    private void TryBind(Flow flow)
    {
        if (flow.Pending.Count == 0) return;
        var down = flow.Pending.Where(x => !x.Up && x.Payload.Length >= 10).TakeLast(4).ToArray();
        var matching = new List<(BankCandidate Bank, string Method)>();
        // A bank can remain valid while the app spends hours in the lobby.
        // Keep the bounded candidate set for this capture session and let
        // actual UDP validation decide; wall-clock age is not key validity.
        foreach (var bank in _candidates.AsEnumerable().Reverse())
        {
            if (HandshakeMatches(flow.Pending, bank)) matching.Add((bank, "two_handshake_responses"));
            else if (down.Length >= 3 && down.Select(p => Convert.ToHexString(SHA256.HashData(p.Payload))).Distinct().Count() >= 3 && down.All(p => UdpDecoder.ValidEnvelope(UdpDecoder.Decrypt(p.Payload, bank.Key))))
                matching.Add((bank, "at_least_three_distinct_envelopes"));
        }
        if (matching.Count != 1) return;
        flow.Bank = matching[0].Bank; flow.Epoch = ++_epoch; ValidatedFlows++;
        _emit("udp_bank_bound", new { flow = FlowId(flow.Pending[0]), epoch = flow.Epoch, fingerprint = flow.Bank.Fingerprint, method = matching[0].Method });
        if (flow.Spool is not null)
        {
            foreach (var line in File.ReadLines(flow.Spool))
            {
                using var doc = System.Text.Json.JsonDocument.Parse(line); var r = doc.RootElement;
                EmitPlain(new UdpPacket(r.GetProperty("up").GetBoolean(),
                    IPEndPoint.Parse(r.GetProperty("local").GetString()!), IPEndPoint.Parse(r.GetProperty("remote").GetString()!),
                    Convert.FromBase64String(r.GetProperty("payload").GetString()!), r.GetProperty("timestampMs").GetInt64()), flow);
            }
            flow.Spool = null; flow.SpoolCount = 0;
        }
        foreach (var packet in flow.Pending) EmitPlain(packet, flow);
        flow.Pending.Clear();
    }
    private bool EmitPlain(UdpPacket packet, Flow flow)
    {
        byte[] plain = packet.Up ? (packet.Payload.Length >= 8 ? packet.Payload[8..] : []) : UdpDecoder.Decrypt(packet.Payload, flow.Bank!.Key);
        bool valid = UdpDecoder.ValidEnvelope(plain);
        bool control = plain.Length == 25 && (plain[0] & 1) != 0;
        if (!valid && !control) { InvalidPackets++; return false; }
        PlainPackets++; if (packet.Up) PlainUp++; else PlainDown++;
        _emit("udp_plaintext", new { timestampMs = packet.TimestampMs, direction = packet.Up ? "up" : "down", local = packet.Local.ToString(), remote = packet.Remote.ToString(),
            epoch = flow.Epoch, keyFingerprint = flow.Bank!.Fingerprint, format = control ? "ue-handshake" : "ue-envelope", validEnvelope = valid,
            payloadLength = plain.Length, payloadBase64 = Convert.ToBase64String(plain) });
        return true;
    }
}
