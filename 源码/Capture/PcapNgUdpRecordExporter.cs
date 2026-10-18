using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;

namespace GatewayCapture.Capture;

internal enum UdpFlowDirection
{
    Both,
    Upstream,
    Downstream,
}

internal sealed record PcapNgRecordExportResult(
    long InputPackets,
    long UdpPackets,
    long MatchingPackets,
    long WrittenRecords,
    long OversizePackets);

internal sealed record UdpFlowSummary(
    IPAddress RemoteAddress,
    ushort RemotePort,
    long UpstreamPackets,
    long DownstreamPackets,
    long UpstreamBytes,
    long DownstreamBytes);

internal static class PcapNgUdpRecordExporter
{
    private const uint SectionHeaderBlock = 0x0A0D0D0A;
    private const uint InterfaceDescriptionBlock = 0x00000001;
    private const uint EnhancedPacketBlock = 0x00000006;
    private const ushort LinkTypeEthernet = 1;
    private const ushort LinkTypeRaw = 101;
    private const ushort LinkTypeLinuxCooked = 113;
    private const ushort LinkTypeLinuxCookedV2 = 276;
    private const int CaptureRecordSize = 0x828;
    private const int CapturePacketOffset = 0x28;
    private const int CapturePacketMaximum = 0x800;

    public static IReadOnlyList<UdpFlowSummary> ListFlows(
        string inputPath,
        IPAddress tabletAddress)
    {
        if (tabletAddress.AddressFamily != AddressFamily.InterNetwork)
        {
            throw new ArgumentException("Flow listing only supports IPv4.");
        }

        var tabletBytes = tabletAddress.GetAddressBytes();
        var linkTypes = new List<ushort>();
        var flows = new Dictionary<(uint Address, ushort Port), long[]>();
        using var input = new FileStream(inputPath, FileMode.Open, FileAccess.Read, FileShare.Read);
        while (TryReadBlock(input, out var block))
        {
            var blockType = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(0, 4));
            if (blockType == SectionHeaderBlock)
            {
                linkTypes.Clear();
                continue;
            }
            if (blockType == InterfaceDescriptionBlock)
            {
                linkTypes.Add(BinaryPrimitives.ReadUInt16LittleEndian(block.AsSpan(8, 2)));
                continue;
            }
            if (blockType != EnhancedPacketBlock || block.Length < 32)
                continue;

            var interfaceId = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(8, 4));
            var capturedLength = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(20, 4));
            const int packetOffset = 28;
            if (interfaceId >= linkTypes.Count || capturedLength > block.Length - packetOffset - 4)
                continue;
            var frame = block.AsSpan(packetOffset, checked((int)capturedLength));
            if (!TryGetUdpPacket(frame, linkTypes[(int)interfaceId], out var udp))
                continue;

            var upstream = udp.SourceAddress.SequenceEqual(tabletBytes);
            var downstream = udp.DestinationAddress.SequenceEqual(tabletBytes);
            if (!upstream && !downstream)
                continue;
            var remoteAddress = upstream ? udp.DestinationAddress : udp.SourceAddress;
            var remotePort = upstream ? udp.DestinationPort : udp.SourcePort;
            var address = BinaryPrimitives.ReadUInt32BigEndian(remoteAddress);
            var key = (address, remotePort);
            if (!flows.TryGetValue(key, out var counters))
                flows[key] = counters = new long[4];
            var payloadBytes = udp.Payload.Length;
            if (upstream)
            {
                counters[0]++;
                counters[2] += payloadBytes;
            }
            else
            {
                counters[1]++;
                counters[3] += payloadBytes;
            }
        }

        return flows.Select(pair => new UdpFlowSummary(
                new IPAddress(pair.Key.Address), pair.Key.Port,
                pair.Value[0], pair.Value[1], pair.Value[2], pair.Value[3]))
            .OrderByDescending(flow => flow.UpstreamPackets + flow.DownstreamPackets)
            .ToArray();
    }

    public static PcapNgRecordExportResult ExportFlow(
        string inputPath,
        string outputPath,
        IPAddress tabletAddress,
        IPAddress remoteAddress,
        ushort remotePort,
        UdpFlowDirection direction)
    {
        if (tabletAddress.AddressFamily != AddressFamily.InterNetwork ||
            remoteAddress.AddressFamily != AddressFamily.InterNetwork)
        {
            throw new ArgumentException("记录导出只支持 IPv4 流。");
        }

        Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
        var tabletBytes = tabletAddress.GetAddressBytes();
        var remoteBytes = remoteAddress.GetAddressBytes();
        var linkTypes = new List<ushort>();
        long inputPackets = 0;
        long udpPackets = 0;
        long matchingPackets = 0;
        long writtenRecords = 0;
        long oversizePackets = 0;
        var recordBuffer = new byte[CaptureRecordSize];

        using var input = new FileStream(inputPath, FileMode.Open, FileAccess.Read, FileShare.Read);
        using var output = new FileStream(outputPath, FileMode.Create, FileAccess.Write, FileShare.None);
        while (TryReadBlock(input, out var block))
        {
            var blockType = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(0, 4));
            if (blockType == SectionHeaderBlock)
            {
                linkTypes.Clear();
                continue;
            }

            if (blockType == InterfaceDescriptionBlock)
            {
                if (block.Length < 20)
                {
                    throw new InvalidDataException("PCAPNG interface block is truncated.");
                }

                linkTypes.Add(BinaryPrimitives.ReadUInt16LittleEndian(block.AsSpan(8, 2)));
                continue;
            }

            if (blockType != EnhancedPacketBlock)
            {
                continue;
            }

            inputPackets++;
            if (block.Length < 32)
            {
                continue;
            }

            var interfaceId = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(8, 4));
            var capturedLength = BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(20, 4));
            const int packetOffset = 28;
            if (interfaceId >= linkTypes.Count || capturedLength > block.Length - packetOffset - 4)
            {
                continue;
            }

            var packet = block.AsSpan(packetOffset, checked((int)capturedLength));
            if (!TryGetUdpPacket(packet, linkTypes[(int)interfaceId], out var udp))
            {
                continue;
            }

            udpPackets++;
            var isUpstream = udp.SourceAddress.SequenceEqual(tabletBytes) &&
                             udp.DestinationAddress.SequenceEqual(remoteBytes) &&
                             udp.DestinationPort == remotePort;
            var isDownstream = udp.SourceAddress.SequenceEqual(remoteBytes) &&
                               udp.DestinationAddress.SequenceEqual(tabletBytes) &&
                               udp.SourcePort == remotePort;
            if (!MatchesDirection(direction, isUpstream, isDownstream))
            {
                continue;
            }

            matchingPackets++;
            if (udp.Payload.Length > CapturePacketMaximum)
            {
                oversizePackets++;
                continue;
            }

            var record = recordBuffer.AsSpan();
            record.Clear();
            var timestamp = (ulong)BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(12, 4)) << 32 |
                            BinaryPrimitives.ReadUInt32LittleEndian(block.AsSpan(16, 4));
            BinaryPrimitives.WriteUInt64LittleEndian(record, timestamp);
            udp.SourceAddress.CopyTo(record.Slice(0x10, 4));
            udp.DestinationAddress.CopyTo(record.Slice(0x14, 4));
            BinaryPrimitives.WriteUInt16LittleEndian(record.Slice(0x18, 2), udp.SourcePort);
            BinaryPrimitives.WriteUInt16LittleEndian(record.Slice(0x1A, 2), udp.DestinationPort);
            BinaryPrimitives.WriteUInt16LittleEndian(record.Slice(0x1C, 2), checked((ushort)udp.Payload.Length));
            udp.Payload.CopyTo(record[CapturePacketOffset..]);
            output.Write(record);
            writtenRecords++;
        }

        return new PcapNgRecordExportResult(
            inputPackets,
            udpPackets,
            matchingPackets,
            writtenRecords,
            oversizePackets);
    }

    private static bool MatchesDirection(UdpFlowDirection direction, bool isUpstream, bool isDownstream)
    {
        return direction switch
        {
            UdpFlowDirection.Both => isUpstream || isDownstream,
            UdpFlowDirection.Upstream => isUpstream,
            UdpFlowDirection.Downstream => isDownstream,
            _ => false,
        };
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
        if (totalLength < 12 || totalLength > 64 * 1024 * 1024)
        {
            throw new InvalidDataException($"Unsupported PCAPNG block length: {totalLength}.");
        }

        block = new byte[totalLength];
        header.CopyTo(block);
        if (!ReadExactly(stream, block.AsSpan(header.Length)))
        {
            throw new InvalidDataException("PCAPNG block is truncated.");
        }

        if (BinaryPrimitives.ReadInt32LittleEndian(block.AsSpan(totalLength - 4, 4)) != totalLength)
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

    private static bool TryGetUdpPacket(ReadOnlySpan<byte> frame, ushort linkType, out UdpPacket packet)
    {
        packet = default;
        var ipOffset = linkType switch
        {
            LinkTypeEthernet => GetEthernetOrWifiDirectIpOffset(frame),
            LinkTypeRaw => 0,
            LinkTypeLinuxCooked => GetLinuxCookedIpOffset(frame, 16, 14),
            LinkTypeLinuxCookedV2 => GetLinuxCookedIpOffset(frame, 20, 0),
            _ => -1,
        };
        if (ipOffset < 0 || frame.Length < ipOffset + 28 || frame[ipOffset] >> 4 != 4)
        {
            return false;
        }

        var headerLength = (frame[ipOffset] & 0x0F) * 4;
        if (headerLength < 20 || frame.Length < ipOffset + headerLength + 8 || frame[ipOffset + 9] != 17)
        {
            return false;
        }

        var udpOffset = ipOffset + headerLength;
        var udpLength = BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(udpOffset + 4, 2));
        if (udpLength < 8 || udpOffset + udpLength > frame.Length)
        {
            return false;
        }

        packet = new UdpPacket(
            frame.Slice(ipOffset + 12, 4),
            frame.Slice(ipOffset + 16, 4),
            BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(udpOffset, 2)),
            BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(udpOffset + 2, 2)),
            frame.Slice(udpOffset + 8, udpLength - 8));
        return true;
    }

    private static int GetEthernetOrWifiDirectIpOffset(ReadOnlySpan<byte> frame)
    {
        var ethernetOffset = GetEthernetIpOffset(frame);
        return ethernetOffset >= 0 ? ethernetOffset : GetWifiDirectIpOffset(frame);
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

    private static int GetWifiDirectIpOffset(ReadOnlySpan<byte> frame)
    {
        if (frame.Length < 32)
        {
            return -1;
        }

        var frameControl = BinaryPrimitives.ReadUInt16LittleEndian(frame.Slice(0, 2));
        if ((frameControl & 0x000C) != 0x0008)
        {
            return -1;
        }

        var headerLength = (frameControl & 0x0300) == 0x0300 ? 30 : 24;
        if ((frameControl & 0x0080) != 0)
        {
            headerLength += 2;
        }

        if ((frameControl & 0x8000) != 0)
        {
            headerLength += 4;
        }

        return frame.Length >= headerLength + 8 &&
               frame[headerLength] == 0xAA &&
               frame[headerLength + 1] == 0xAA &&
               frame[headerLength + 2] == 0x03 &&
               frame[headerLength + 3] == 0x00 &&
               frame[headerLength + 4] == 0x00 &&
               frame[headerLength + 5] == 0x00 &&
               BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(headerLength + 6, 2)) == 0x0800
            ? headerLength + 8
            : -1;
    }

    private static int GetLinuxCookedIpOffset(ReadOnlySpan<byte> frame, int headerLength, int etherTypeOffset)
    {
        return frame.Length >= headerLength &&
               BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(etherTypeOffset, 2)) == 0x0800
            ? headerLength
            : -1;
    }

    private readonly ref struct UdpPacket(
        ReadOnlySpan<byte> sourceAddress,
        ReadOnlySpan<byte> destinationAddress,
        ushort sourcePort,
        ushort destinationPort,
        ReadOnlySpan<byte> payload)
    {
        public ReadOnlySpan<byte> SourceAddress { get; } = sourceAddress;
        public ReadOnlySpan<byte> DestinationAddress { get; } = destinationAddress;
        public ushort SourcePort { get; } = sourcePort;
        public ushort DestinationPort { get; } = destinationPort;
        public ReadOnlySpan<byte> Payload { get; } = payload;
    }
}
