using System.Buffers.Binary;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace GatewayCapture.Realtime;

internal sealed record TgcpFrame(byte[] Header, byte[] Body)
{
    public ushort Command => BinaryPrimitives.ReadUInt16BigEndian(Header.AsSpan(6));
    public uint Sequence => BinaryPrimitives.ReadUInt32BigEndian(Header.AsSpan(9));
    public byte[] Wire()
    {
        var result = new byte[Header.Length + Body.Length];
        Header.CopyTo(result, 0);
        BinaryPrimitives.WriteUInt32BigEndian(result.AsSpan(17), (uint)Body.Length);
        Body.CopyTo(result, Header.Length);
        return result;
    }
    public BigInteger Public(ushort command)
    {
        if (Command != command || Header.Length < 88 || !Header.AsSpan(21, 3).SequenceEqual(new byte[] { 3, 0, 64 }))
            throw new InvalidDataException("TGCP hello 格式不匹配。");
        var value = new BigInteger(Header.AsSpan(24, 64), true, true);
        TgcpProtocol.ValidatePublic(value);
        return value;
    }
    public TgcpFrame ReplacePublic(BigInteger value, byte[] body)
    {
        var header = Header.ToArray();
        TgcpProtocol.PublicBytes(value).CopyTo(header, 24);
        return new(header, body);
    }
}

internal static class TgcpProtocol
{
    public static readonly byte[] Magic = Convert.FromHexString("3366000B000C");
    public static readonly byte[] Iv = Enumerable.Range(0, 16).Select(x => (byte)x).ToArray();
    public static readonly BigInteger Prime = new(Convert.FromHexString("97981e0aade0de72e29cd2789193562547bad2591fb7e59c523923dddecebccae49bdcd4b8ec39022b0bd95d4af8fd3cd600919393255c1084c2fd5abb2fede3"), true, true);
    public const int MaxBody = 16 * 1024 * 1024, MaxHeader = 65536;
    private static readonly Regex BankToken = new(@"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{171}=)(?![A-Za-z0-9+/=])", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));

    public static (int Header, int Body) Lengths(ReadOnlySpan<byte> data)
    {
        if (data.Length < 21 || !data[..6].SequenceEqual(Magic)) throw new InvalidDataException("TGCP 帧头错误。");
        var h = BinaryPrimitives.ReadUInt32BigEndian(data[13..]);
        var b = BinaryPrimitives.ReadUInt32BigEndian(data[17..]);
        if (h < 21 || h > MaxHeader || b > MaxBody) throw new InvalidDataException("TGCP 帧长度超出范围。");
        return ((int)h, (int)b);
    }
    public static int Sniff(ReadOnlySpan<byte> prefix)
    {
        var n = Math.Min(prefix.Length, Magic.Length);
        if (!prefix[..n].SequenceEqual(Magic.AsSpan(0, n))) return 1;
        if (prefix.Length < 21) return 0;
        try
        {
            var (h, b) = Lengths(prefix);
            if (BinaryPrimitives.ReadUInt16BigEndian(prefix[6..]) != 0x1001 || h < 88) return 1;
            if (prefix.Length >= 24 && !prefix.Slice(21, 3).SequenceEqual(new byte[] { 3, 0, 64 })) return 1;
            if (prefix.Length < h + b) return 0;
            new TgcpFrame(prefix[..h].ToArray(), prefix.Slice(h, b).ToArray()).Public(0x1001);
            return 2;
        }
        catch (InvalidDataException) { return 1; }
    }
    public static (BigInteger Private, BigInteger Public) KeyPair()
    {
        var raw = RandomNumberGenerator.GetBytes(64);
        raw[0] = (byte)((raw[0] & 0x7f) | 0x40);
        var value = new BigInteger(raw, true, true);
        CryptographicOperations.ZeroMemory(raw);
        return (value, BigInteger.ModPow(2, value, Prime));
    }
    public static void ValidatePublic(BigInteger value)
    {
        if (value <= 1 || value >= Prime - 1) throw new InvalidDataException("DH 公开值越界。");
    }
    public static byte[] PublicBytes(BigInteger value)
    {
        var raw = value.ToByteArray(true, true);
        if (raw.Length > 64) throw new InvalidDataException("DH 公开值超过 64 字节。");
        var result = new byte[64]; raw.CopyTo(result, 64 - raw.Length); return result;
    }
    public static byte[] Derive(BigInteger peer, BigInteger privateValue)
    {
        ValidatePublic(peer);
        var shared = BigInteger.ModPow(peer, privateValue, Prime);
        ValidatePublic(shared);
        var bytes = shared.ToByteArray(true, true);
        try { return MD5.HashData(bytes); }
        finally { CryptographicOperations.ZeroMemory(bytes); }
    }
    public static byte[] Unpad(byte[] padded)
    {
        if (padded.Length < 16) throw new InvalidDataException("TGCP 密文过短。");
        int padding = padded[^1];
        if (padding < 6 || padding > 21 || padding > padded.Length || !padded.AsSpan(padded.Length - 6, 5).SequenceEqual("tsf4g"u8))
            throw new InvalidDataException("TGCP 填充标记校验失败。");
        int length = padded.Length - padding;
        if (padding != (length % 16 >= 11 ? 32 : 16) - length % 16) throw new InvalidDataException("TGCP 填充长度错误。");
        return padded.AsSpan(0, length).ToArray();
    }
    public static byte[] DecryptPadded(byte[] body, byte[] key)
    {
        if (body.Length == 0 || body.Length % 16 != 0) throw new InvalidDataException("TGCP 密文未对齐。");
        using var aes = Aes.Create(); aes.Key = key;
        return aes.DecryptCbc(body, Iv, PaddingMode.None);
    }
    public static byte[] EncryptPadded(byte[] padded, byte[] key)
    {
        using var aes = Aes.Create(); aes.Key = key;
        return aes.EncryptCbc(padded, Iv, PaddingMode.None);
    }
    public static byte[] Encrypt(byte[] plain, byte[] key)
    {
        int padding = (plain.Length % 16 >= 11 ? 32 : 16) - plain.Length % 16;
        var result = new byte[plain.Length + padding]; plain.CopyTo(result, 0);
        "tsf4g"u8.CopyTo(result.AsSpan(result.Length - 6)); result[^1] = (byte)padding;
        return EncryptPadded(result, key);
    }
    public static IEnumerable<byte[]> Banks(byte[] plain)
    {
        foreach (Match match in BankToken.Matches(Encoding.Latin1.GetString(plain)))
        {
            var bank = Convert.FromBase64String(match.Value);
            if (bank.Length == 128 && Convert.ToBase64String(bank) == match.Value) yield return bank;
        }
    }
}

internal sealed class FrameBuffer
{
    private byte[] _bytes = new byte[4096];
    private int _start, _end;
    public int Count => _end - _start;
    public void Append(ReadOnlySpan<byte> data)
    {
        if (Count + data.Length > 2 * (TgcpProtocol.MaxBody + TgcpProtocol.MaxHeader)) throw new InvalidDataException("TGCP 缓冲超限。");
        if (_bytes.Length - _end < data.Length)
        {
            var replacement = new byte[Math.Max(Count + data.Length, Math.Min(_bytes.Length * 2, 2 * (TgcpProtocol.MaxBody + TgcpProtocol.MaxHeader)))];
            _bytes.AsSpan(_start, Count).CopyTo(replacement);
            _end = Count; _start = 0; _bytes = replacement;
        }
        data.CopyTo(_bytes.AsSpan(_end)); _end += data.Length;
    }
    public TgcpFrame? Take()
    {
        if (Count < 21) return null;
        var (h, b) = TgcpProtocol.Lengths(_bytes.AsSpan(_start, Count));
        if (Count < h + b) return null;
        var frame = new TgcpFrame(_bytes.AsSpan(_start, h).ToArray(), _bytes.AsSpan(_start + h, b).ToArray());
        _start += h + b;
        if (_start == _end) _start = _end = 0;
        return frame;
    }
}

internal sealed record TgcpObservation(bool Up, ushort Command, uint Sequence, byte[] Plain, byte[][] Banks);
internal sealed record TgcpOutput(byte[] ToServer, byte[] ToClient, List<TgcpObservation> Observations);

internal sealed class TgcpSession : IDisposable
{
    private BigInteger _x, _y;
    private readonly BigInteger _publicX, _publicY;
    private byte[]? _clientKey, _serverKey;
    private readonly FrameBuffer[] _buffers = [new(), new()];
    private readonly bool[] _finished = new bool[2];
    private readonly HashSet<string> _banks = [];
    public int State { get; private set; }
    public long Frames { get; private set; }
    public TgcpSession()
    {
        (_x, _publicX) = TgcpProtocol.KeyPair(); (_y, _publicY) = TgcpProtocol.KeyPair();
    }
    public TgcpOutput Feed(bool up, byte[] input)
    {
        if (State < 0 || _finished[up ? 0 : 1]) throw new InvalidDataException("TGCP 会话已结束。");
        using var toServer = new MemoryStream(); using var toClient = new MemoryStream();
        var observations = new List<TgcpObservation>();
        _buffers[up ? 0 : 1].Append(input);
        void Drain(bool direction)
        {
            var buffer = _buffers[direction ? 0 : 1];
            while (!(direction && State == 1) && buffer.Take() is { } frame)
            {
                if (frame.Header[8] != (direction ? 0 : 1)) throw new InvalidDataException("TGCP 方向不匹配。");
                byte[] source, target;
                if (State == 0)
                {
                    if (!direction || frame.Body.Length != 0) throw new InvalidDataException("当前配置要求空体 client hello。");
                    _clientKey = TgcpProtocol.Derive(frame.Public(0x1001), _x);
                    toServer.Write(frame.ReplacePublic(_publicY, []).Wire()); State = 1; Frames++; continue;
                }
                if (State == 1)
                {
                    if (direction) throw new InvalidDataException("服务端 hello 尚未到达。");
                    _serverKey = TgcpProtocol.Derive(frame.Public(0x1002), _y);
                    frame = frame.ReplacePublic(_publicX, frame.Body); State = 2; _x = _y = BigInteger.Zero;
                }
                else if (frame.Command is 0x1001 or 0x1002) throw new InvalidDataException("意外的重复 TGCP hello。");
                source = direction ? _clientKey! : _serverKey!;
                target = direction ? _serverKey! : _clientKey!;
                if (frame.Body.Length > 0)
                {
                    var padded = TgcpProtocol.DecryptPadded(frame.Body, source);
                    var plain = TgcpProtocol.Unpad(padded);
                    var candidates = new List<byte[]>();
                    foreach (var bank in TgcpProtocol.Banks(plain))
                    {
                        var fingerprint = Convert.ToHexString(SHA256.HashData(bank)).ToLowerInvariant();
                        if (_banks.Add(fingerprint)) candidates.Add(bank);
                        if (_banks.Count > 1024) throw new InvalidDataException("会话候选密钥数量超限。");
                    }
                    observations.Add(new(direction, frame.Command, frame.Sequence, plain, candidates.ToArray()));
                    frame = frame with { Body = TgcpProtocol.EncryptPadded(padded, target) };
                }
                (direction ? toServer : toClient).Write(frame.Wire()); Frames++;
            }
        }
        try { Drain(up); if (State == 2) Drain(!up); }
        catch { Dispose(); throw; }
        return new(toServer.ToArray(), toClient.ToArray(), observations);
    }
    public void Finish(bool up)
    {
        if (State != 2 || _buffers[up ? 0 : 1].Count != 0) throw new InvalidDataException("TCP 在完整 TGCP 握手/帧前结束。");
        _finished[up ? 0 : 1] = true;
    }
    public void Dispose()
    {
        State = -1; _x = _y = BigInteger.Zero;
        if (_clientKey is not null) CryptographicOperations.ZeroMemory(_clientKey);
        if (_serverKey is not null) CryptographicOperations.ZeroMemory(_serverKey);
    }
}
