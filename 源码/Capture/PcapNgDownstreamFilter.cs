using System.Buffers.Binary;
using System.Net;

namespace GatewayCapture.Capture;

internal sealed record PcapNgFilterResult(
    long InputPackets,
    long SessionPackets,
    long SessionBytes,
    long DownstreamPackets,
    long DownstreamBytes,
    long FullIpPackets = 0,
    long FullIpBytes = 0,
    long TcpPackets = 0,
    long OtherIpPackets = 0,
    long FragmentPackets = 0,
    long TruncatedPackets = 0);

internal static class PcapNgDownstreamFilter
{
    private const uint SectionHeaderBlock = 0x0A0D0D0A;
    private const uint InterfaceDescriptionBlock = 0x00000001;
    private const uint EnhancedPacketBlock = 0x00000006;
    private const ushort LinkTypeEthernet = 1;
    private const ushort LinkTypeRaw = 101;
    private const ushort LinkTypeLinuxCooked = 113;
    private const ushort LinkTypeLinuxCookedV2 = 276;

    public static PcapNgFilterResult FilterTargetUdpSession(
        string inputPath, string sessionOutputPath, string downstreamOutputPath,
        IPAddress targetAddress) => FilterTargetSession(
            [inputPath], sessionOutputPath, downstreamOutputPath, targetAddress);

    // Each input remains a separate PCAPNG section, preserving timestamps and interface IDs.
    public static PcapNgFilterResult FilterTargetSession(
        IEnumerable<string> inputPaths, string sessionOutputPath, string downstreamOutputPath,
        IPAddress targetAddress, string? fullIpOutputPath = null)
    {
        if (targetAddress.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
            throw new ArgumentException("目标地址必须为 IPv4。", nameof(targetAddress));
        var inputs = inputPaths.Select(Path.GetFullPath).ToArray();
        var outputs = new[] { sessionOutputPath, downstreamOutputPath, fullIpOutputPath }
            .Where(p => p is not null).Select(p => Path.GetFullPath(p!)).ToArray();
        if (outputs.Distinct(StringComparer.OrdinalIgnoreCase).Count() != outputs.Length ||
            outputs.Any(p => inputs.Contains(p, StringComparer.OrdinalIgnoreCase)))
            throw new ArgumentException("输入与输出路径必须互不相同。");
        foreach (var path in outputs) Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var targetBytes = targetAddress.GetAddressBytes();
        long inputPackets = 0, sessionPackets = 0, sessionBytes = 0;
        long downstreamPackets = 0, downstreamBytes = 0, fullPackets = 0, fullBytes = 0;
        long tcp = 0, other = 0, fragments = 0, truncated = 0;
        using var sessionOutput = File.Create(sessionOutputPath);
        using var downstreamOutput = File.Create(downstreamOutputPath);
        using var fullOutput = fullIpOutputPath is null ? null : File.Create(fullIpOutputPath);
        foreach (var inputPath in inputs)
        {
            using var input = File.OpenRead(inputPath);
            var linkTypes = new List<ushort>();
            bool sectionSeen = false;
            while (TryReadBlock(input, out var block))
            {
                var blockType = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(0, 4));
                if (blockType == SectionHeaderBlock)
                {
                    if (block.Length < 28 || BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(8, 4)) != 0x1A2B3C4D)
                        throw new InvalidDataException("Expected little-endian PktMon PCAPNG section.");
                    sectionSeen = true;
                    linkTypes.Clear();
                }
                else if (!sectionSeen) throw new InvalidDataException("Missing PCAPNG section header.");
                if (blockType == InterfaceDescriptionBlock)
                {
                    if (block.Length < 20) throw new InvalidDataException("Truncated interface block.");
                    linkTypes.Add(BinaryPrimitives.ReadUInt16LittleEndian(block.AsSpan(8, 2)));
                }
                if (blockType != EnhancedPacketBlock)
                {
                    if (blockType is 2 or 3) throw new InvalidDataException("Unsupported legacy packet block; original preserved.");
                    sessionOutput.Write(block);
                    downstreamOutput.Write(block);
                    fullOutput?.Write(block);
                    continue;
                }
                inputPackets++;
                if (block.Length < 32) throw new InvalidDataException("Truncated enhanced packet block.");
                var interfaceId = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(8, 4));
                var capturedLength = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(20, 4));
                var originalLength = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(24, 4));
                if (interfaceId >= linkTypes.Count || capturedLength > block.Length - 32)
                    throw new InvalidDataException("Invalid enhanced packet interface/length.");
                var packet = block.AsSpan(28, checked((int)capturedLength));
                if (!IsTargetSessionIp(packet, linkTypes[(int)interfaceId], targetBytes,
                        out var isDownstream, out var protocol, out var fragmented, out var ipTruncated)) continue;
                fullOutput?.Write(block);
                fullPackets++;
                fullBytes += capturedLength;
                if (fragmented) fragments++;
                if (capturedLength < originalLength || ipTruncated) truncated++;
                if (protocol == 6) tcp++;
                else if (protocol != 17) other++;
                if (protocol != 17) continue;
                sessionOutput.Write(block);
                sessionPackets++;
                sessionBytes += capturedLength;
                if (!isDownstream) continue;
                downstreamOutput.Write(block);
                downstreamPackets++;
                downstreamBytes += capturedLength;
            }
            if (!sectionSeen) throw new InvalidDataException("Empty PCAPNG input.");
        }
        return new(inputPackets, sessionPackets, sessionBytes, downstreamPackets, downstreamBytes,
            fullPackets, fullBytes, tcp, other, fragments, truncated);
    }

    private static bool TryReadBlock(Stream stream, out byte[] block)
    {
        block = Array.Empty<byte>();
        Span<byte> header = stackalloc byte[8];
        var first = stream.Read(header);
        if (first == 0)
        {
            return false;
        }

        if (!ReadExactly(stream, header[first..]))
        {
            throw new InvalidDataException("PCAPNG block header is truncated.");
        }

        var totalLength = BinaryPrimitives.ReadInt32LittleEndian(header[4..]);
        if (totalLength < 12 || totalLength % 4 != 0 || totalLength > 64 * 1024 * 1024)
        {
            throw new InvalidDataException($"Unsupported PCAPNG block length: {totalLength}.");
        }

        block = new byte[totalLength];
        header.CopyTo(block);
        if (!ReadExactly(stream, block.AsSpan(header.Length)))
        {
            throw new InvalidDataException("PCAPNG block is truncated.");
        }

        var trailingLength = BinaryPrimitives.ReadInt32LittleEndian(block.AsSpan(totalLength - 4, 4));
        if (trailingLength != totalLength)
        {
            throw new InvalidDataException("PCAPNG block length validation failed.");
        }

        return true;
    }

    private static bool ReadExactly(Stream stream, Span<byte> buffer)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = stream.Read(buffer[offset..]);
            if (read == 0)
            {
                return false;
            }

            offset += read;
        }

        return true;
    }

    private static bool IsTargetSessionIp(
        ReadOnlySpan<byte> frame,
        ushort linkType,
        byte[] targetAddress,
        out bool isDownstream, out byte protocol, out bool fragmented, out bool truncated)
    {
        isDownstream = false;
        protocol = 0; fragmented = false; truncated = false;
        var ipOffset = linkType switch
        {
            LinkTypeEthernet => GetEthernetOrWifiDirectIpOffset(frame),
            LinkTypeRaw => 0,
            LinkTypeLinuxCooked => GetLinuxCookedIpOffset(frame, 16, 14),
            LinkTypeLinuxCookedV2 => GetLinuxCookedIpOffset(frame, 20, 0),
            _ => -1,
        };

        if (ipOffset < 0 || frame.Length < ipOffset + 20)
        {
            return false;
        }

        var version = frame[ipOffset] >> 4;
        if (version != 4)
        {
            return false;
        }

        var headerLength = (frame[ipOffset] & 0x0F) * 4;
        if (headerLength < 20 || frame.Length < ipOffset + headerLength)
        {
            return false;
        }

        protocol = frame[ipOffset + 9];
        fragmented = (BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(ipOffset + 6, 2)) & 0x3FFF) != 0;
        var totalLength = BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(ipOffset + 2, 2));
        if (totalLength < headerLength) return false;
        truncated = ipOffset + totalLength > frame.Length;

        var isSource = frame.Slice(ipOffset + 12, 4).SequenceEqual(targetAddress);
        isDownstream = frame.Slice(ipOffset + 16, 4).SequenceEqual(targetAddress);
        return isSource || isDownstream;
    }

    private static int GetEthernetIpOffset(ReadOnlySpan<byte> frame)
    {
        if (frame.Length < 14)
        {
            return -1;
        }

        var offset = 14;
        var etherType = BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(12, 2));
        while (etherType is 0x8100 or 0x88A8 or 0x9100)
        {
            if (frame.Length < offset + 4)
            {
                return -1;
            }

            etherType = BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(offset + 2, 2));
            offset += 4;
        }

        return etherType == 0x0800 ? offset : -1;
    }

    private static int GetEthernetOrWifiDirectIpOffset(ReadOnlySpan<byte> frame)
    {
        var ethernetOffset = GetEthernetIpOffset(frame);
        return ethernetOffset >= 0 ? ethernetOffset : GetWifiDirectIpOffset(frame);
    }

    // PktMon labels Wi-Fi Direct miniport frames as Ethernet in PCAPNG, but the
    // frame body is an 802.11 data header followed by LLC/SNAP and an IPv4 packet.
    private static int GetWifiDirectIpOffset(ReadOnlySpan<byte> frame)
    {
        if (frame.Length < 32)
        {
            return -1;
        }

        var frameControl = BinaryPrimitives.ReadUInt16LittleEndian(frame.Slice(0, 2));
        const ushort dataFrameType = 0x0008;
        const ushort toDistributionSystem = 0x0100;
        const ushort fromDistributionSystem = 0x0200;
        const ushort qualityOfServiceSubtype = 0x0080;
        const ushort order = 0x8000;
        if ((frameControl & 0x000C) != dataFrameType)
        {
            return -1;
        }

        var headerLength = (frameControl & (toDistributionSystem | fromDistributionSystem)) ==
                           (toDistributionSystem | fromDistributionSystem)
            ? 30
            : 24;
        if ((frameControl & qualityOfServiceSubtype) != 0)
        {
            headerLength += 2;
        }

        if ((frameControl & order) != 0)
        {
            headerLength += 4;
        }

        const int llcSnapLength = 8;
        if (frame.Length < headerLength + llcSnapLength ||
            frame[headerLength] != 0xAA ||
            frame[headerLength + 1] != 0xAA ||
            frame[headerLength + 2] != 0x03 ||
            frame[headerLength + 3] != 0x00 ||
            frame[headerLength + 4] != 0x00 ||
            frame[headerLength + 5] != 0x00 ||
            BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(headerLength + 6, 2)) != 0x0800)
        {
            return -1;
        }

        return headerLength + llcSnapLength;
    }

    private static int GetLinuxCookedIpOffset(ReadOnlySpan<byte> frame, int headerLength, int etherTypeOffset)
    {
        if (frame.Length < headerLength ||
            BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(etherTypeOffset, 2)) != 0x0800)
        {
            return -1;
        }

        return headerLength;
    }
}
