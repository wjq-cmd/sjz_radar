using System.Buffers.Binary;
using System.Net;

namespace GatewayCapture.Capture;

internal static class GatewayCaptureSelfTest
{
    public static void Run()
    {
        var directory = Path.Combine(Path.GetTempPath(), $"GatewayCaptureSelfTest-{Guid.NewGuid():N}");
        Directory.CreateDirectory(directory);
        try
        {
            var inputPath = Path.Combine(directory, "input.pcapng");
            var sessionPath = Path.Combine(directory, "session.pcapng");
            var downstreamPath = Path.Combine(directory, "downstream.pcapng");
            var recordsPath = Path.Combine(directory, "downstream.records");
            var tablet = IPAddress.Parse("192.168.137.2");

            using (var stream = new FileStream(inputPath, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                WriteBlock(stream, BuildSectionHeader());
                WriteBlock(stream, BuildEthernetInterface());
                WriteBlock(stream, BuildEnhancedPacket(BuildEthernetIpv4Udp(
                    IPAddress.Parse("203.0.113.10"), tablet, 1)));
                WriteBlock(stream, BuildEnhancedPacket(BuildEthernetIpv4Udp(
                    tablet, IPAddress.Parse("203.0.113.10"), 2)));
                WriteBlock(stream, BuildEnhancedPacket(BuildEthernetIpv4Udp(
                    IPAddress.Parse("198.51.100.2"), IPAddress.Parse("198.51.100.3"), 3)));
                WriteBlock(stream, BuildEnhancedPacket(BuildWifiDirectIpv4Udp(
                    IPAddress.Parse("203.0.113.20"), tablet, 4)));
                WriteBlock(stream, BuildEnhancedPacket(BuildWifiDirectIpv4Udp(
                    tablet, IPAddress.Parse("203.0.113.20"), 5)));
            }

            var recordResult = PcapNgUdpRecordExporter.ExportFlow(
                inputPath,
                recordsPath,
                tablet,
                IPAddress.Parse("203.0.113.20"),
                12345,
                UdpFlowDirection.Downstream);
            if (recordResult.InputPackets != 5 || recordResult.UdpPackets != 5 ||
                recordResult.MatchingPackets != 1 || recordResult.WrittenRecords != 1 ||
                new FileInfo(recordsPath).Length != 0x828)
            {
                throw new InvalidOperationException(
                    $"Unexpected record export result: input={recordResult.InputPackets}, " +
                    $"udp={recordResult.UdpPackets}, matching={recordResult.MatchingPackets}, " +
                    $"written={recordResult.WrittenRecords}.");
            }

            var record = File.ReadAllBytes(recordsPath);
            if (record[0x28] != 4)
            {
                throw new InvalidOperationException("Exported record payload does not match the Wi-Fi Direct packet.");
            }

            var result = PcapNgDownstreamFilter.FilterTargetUdpSession(
                inputPath,
                sessionPath,
                downstreamPath,
                tablet);
            if (result.InputPackets != 5 || result.SessionPackets != 4 ||
                result.DownstreamPackets != 2 || result.DownstreamBytes == 0)
            {
                throw new InvalidOperationException(
                    $"Unexpected filter result: input={result.InputPackets}, " +
                    $"session={result.SessionPackets}, downstream={result.DownstreamPackets}.");
            }

            var mixedPath = Path.Combine(directory, "mixed.pcapng");
            var fullPath = Path.Combine(directory, "full.pcapng");
            var peer = IPAddress.Parse("203.0.113.40");
            var tcpDown = BuildIpv4Packet(peer, tablet, 6, new byte[20]);
            var tcpUp = BuildIpv4Packet(tablet, peer, 6, new byte[20]);
            var wifiTcp = ToWifiDirect(tcpDown);
            var foreignTcp = BuildIpv4Packet(peer, IPAddress.Parse("192.168.137.99"), 6, new byte[20]);
            var icmp = BuildIpv4Packet(peer, tablet, 1, new byte[8]);
            // Non-initial fragment shorter than a UDP header must still survive capture export.
            var fragment = BuildIpv4Packet(peer, tablet, 17, [11, 22, 33, 44]);
            BinaryPrimitives.WriteUInt16BigEndian(fragment.AsSpan(20, 2), 1);
            var truncated = tcpDown[..^4];
            var packets = new[] { tcpDown, tcpUp, wifiTcp, foreignTcp, icmp, fragment, truncated };
            using (var stream = File.Create(mixedPath))
            {
                WriteBlock(stream, BuildSectionHeader());
                WriteBlock(stream, BuildEthernetInterface());
                foreach (var packet in packets) WriteBlock(stream, BuildEnhancedPacket(packet));
            }
            var fullResult = PcapNgDownstreamFilter.FilterTargetSession(
                [inputPath, mixedPath], sessionPath, downstreamPath, tablet, fullPath);
            if (fullResult.InputPackets != 12 || fullResult.FullIpPackets != 10 ||
                fullResult.TcpPackets != 4 || fullResult.SessionPackets != 5 ||
                fullResult.OtherIpPackets != 1 || fullResult.FragmentPackets != 1 ||
                fullResult.TruncatedPackets != 1 || fullResult.DownstreamPackets != 3)
                throw new InvalidOperationException($"Full IPv4 / multi-section counts failed: {fullResult}");
            // Re-filtering proves section/interface resets and IPv4 fragments are retained.
            var again = PcapNgDownstreamFilter.FilterTargetSession([fullPath],
                Path.Combine(directory, "again.udp.pcapng"), Path.Combine(directory, "again.down.pcapng"),
                tablet, Path.Combine(directory, "again.full.pcapng"));
            if (again.InputPackets != 10 || again.TcpPackets != 4 || again.FullIpPackets != 10 ||
                !File.ReadAllBytes(fullPath).SequenceEqual(File.ReadAllBytes(Path.Combine(directory, "again.full.pcapng"))))
                throw new InvalidOperationException("PCAPNG bytes/timestamps changed on full-session re-export.");
            using (var expected = new MemoryStream())
            {
                WriteBlock(expected, BuildSectionHeader());
                WriteBlock(expected, BuildEthernetInterface());
                WriteBlock(expected, BuildEnhancedPacket(BuildEthernetIpv4Udp(IPAddress.Parse("203.0.113.10"), tablet, 1)));
                WriteBlock(expected, BuildEnhancedPacket(BuildEthernetIpv4Udp(tablet, IPAddress.Parse("203.0.113.10"), 2)));
                WriteBlock(expected, BuildEnhancedPacket(BuildWifiDirectIpv4Udp(IPAddress.Parse("203.0.113.20"), tablet, 4)));
                WriteBlock(expected, BuildEnhancedPacket(BuildWifiDirectIpv4Udp(tablet, IPAddress.Parse("203.0.113.20"), 5)));
                WriteBlock(expected, BuildSectionHeader());
                WriteBlock(expected, BuildEthernetInterface());
                foreach (var packet in packets.Where(p => !ReferenceEquals(p, foreignTcp)))
                    WriteBlock(expected, BuildEnhancedPacket(packet));
                if (!expected.ToArray().SequenceEqual(File.ReadAllBytes(fullPath)))
                    throw new InvalidOperationException("Full export did not preserve original target packet blocks exactly.");
            }
            var rejected = false;
            try { PcapNgDownstreamFilter.FilterTargetSession([inputPath], inputPath, downstreamPath, tablet); }
            catch (ArgumentException) { rejected = true; }
            if (!rejected) throw new InvalidOperationException("Input overwrite was accepted.");
            var badPath = Path.Combine(directory, "bad.pcapng");
            File.WriteAllBytes(badPath, File.ReadAllBytes(inputPath)[..^1]);
            rejected = false;
            try { PcapNgDownstreamFilter.FilterTargetSession([badPath], sessionPath, downstreamPath, tablet); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidOperationException("Truncated PCAPNG silently accepted.");
            var raw = Path.Combine(directory, "raw");
            Directory.CreateDirectory(raw);
            foreach (var name in new[] { "trace_10.etl", "trace_2.etl", "trace_1.etl" }) File.WriteAllBytes(Path.Combine(raw, name), []);
            if (!CaptureArchive.TraceFiles(directory).Select(Path.GetFileName).SequenceEqual(new[] { "trace_1.etl", "trace_2.etl", "trace_10.etl" }))
                throw new InvalidOperationException("Segment ordering failed.");
            Console.WriteLine("PASS: UDP regression, TCP both directions, Wi-Fi Direct, non-target exclusion, ICMP, IP fragments,");
            Console.WriteLine("      truncation reporting, multi-section export, byte-exact retention, input protection, corrupt input, segment ordering.");
            Console.WriteLine("PCAPNG target-session and Open UDP record export self-test passed.");
        }
        finally
        {
            try
            {
                Directory.Delete(directory, true);
            }
            catch (IOException)
            {
                // Temporary diagnostics can be cleaned by the operating system later.
            }
        }
    }

    private static byte[] BuildIpv4Packet(IPAddress source, IPAddress destination, byte protocol, byte[] payload)
    {
        var frame = new byte[14 + 20 + payload.Length];
        frame[0] = 0x02; frame[6] = 0x0A;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(12, 2), 0x0800);
        frame[14] = 0x45;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(16, 2), checked((ushort)(20 + payload.Length)));
        frame[22] = 64; frame[23] = protocol;
        source.GetAddressBytes().CopyTo(frame, 26);
        destination.GetAddressBytes().CopyTo(frame, 30);
        payload.CopyTo(frame, 34);
        if (protocol == 6 && payload.Length >= 20) frame[46] = 0x50;
        return frame;
    }

    private static byte[] ToWifiDirect(byte[] ethernet)
    {
        var prefix = BuildWifiDirectIpv4Udp(IPAddress.Loopback, IPAddress.Loopback, 0)[..32];
        return prefix.Concat(ethernet.Skip(14)).ToArray();
    }

    private static byte[] BuildSectionHeader()
    {
        var body = new byte[16];
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(0, 4), 0x1A2B3C4D);
        BinaryPrimitives.WriteUInt16LittleEndian(body.AsSpan(4, 2), 1);
        BinaryPrimitives.WriteUInt16LittleEndian(body.AsSpan(6, 2), 0);
        BinaryPrimitives.WriteUInt64LittleEndian(body.AsSpan(8, 8), ulong.MaxValue);
        return BuildBlock(0x0A0D0D0A, body);
    }

    private static byte[] BuildEthernetInterface()
    {
        var body = new byte[8];
        BinaryPrimitives.WriteUInt16LittleEndian(body.AsSpan(0, 2), 1);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(4, 4), 65_535);
        return BuildBlock(0x00000001, body);
    }

    private static byte[] BuildEnhancedPacket(byte[] packet)
    {
        var paddedLength = (packet.Length + 3) & ~3;
        var body = new byte[20 + paddedLength];
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(0, 4), 0);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(4, 4), 0);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(8, 4), 1);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(12, 4), checked((uint)packet.Length));
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(16, 4), checked((uint)packet.Length));
        packet.CopyTo(body, 20);
        return BuildBlock(0x00000006, body);
    }

    private static byte[] BuildEthernetIpv4Udp(IPAddress source, IPAddress destination, byte marker)
    {
        var frame = new byte[14 + 20 + 8 + 4];
        frame[0] = 0x02;
        frame[6] = 0x0A;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(12, 2), 0x0800);

        const int ipOffset = 14;
        frame[ipOffset] = 0x45;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(ipOffset + 2, 2), 32);
        frame[ipOffset + 8] = 64;
        frame[ipOffset + 9] = 17;
        source.GetAddressBytes().CopyTo(frame, ipOffset + 12);
        destination.GetAddressBytes().CopyTo(frame, ipOffset + 16);

        const int udpOffset = ipOffset + 20;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset, 2), 12345);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset + 2, 2), 23456);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset + 4, 2), 12);
        frame[udpOffset + 8] = marker;
        return frame;
    }

    private static byte[] BuildWifiDirectIpv4Udp(IPAddress source, IPAddress destination, byte marker)
    {
        const int ieee80211HeaderLength = 24;
        const int llcSnapLength = 8;
        var frame = new byte[ieee80211HeaderLength + llcSnapLength + 20 + 8 + 4];
        BinaryPrimitives.WriteUInt16LittleEndian(frame.AsSpan(0, 2), 0x0208);
        frame[4] = 0x02;
        frame[10] = 0x0A;
        frame[16] = 0x12;

        var llcOffset = ieee80211HeaderLength;
        frame[llcOffset] = 0xAA;
        frame[llcOffset + 1] = 0xAA;
        frame[llcOffset + 2] = 0x03;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(llcOffset + 6, 2), 0x0800);

        var ipOffset = llcOffset + llcSnapLength;
        frame[ipOffset] = 0x45;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(ipOffset + 2, 2), 32);
        frame[ipOffset + 8] = 64;
        frame[ipOffset + 9] = 17;
        source.GetAddressBytes().CopyTo(frame, ipOffset + 12);
        destination.GetAddressBytes().CopyTo(frame, ipOffset + 16);

        var udpOffset = ipOffset + 20;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset, 2), 12345);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset + 2, 2), 23456);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(udpOffset + 4, 2), 12);
        frame[udpOffset + 8] = marker;
        return frame;
    }

    private static byte[] BuildBlock(uint type, byte[] body)
    {
        var totalLength = 8 + body.Length + 4;
        var block = new byte[totalLength];
        BinaryPrimitives.WriteUInt32LittleEndian(block.AsSpan(0, 4), type);
        BinaryPrimitives.WriteInt32LittleEndian(block.AsSpan(4, 4), totalLength);
        body.CopyTo(block, 8);
        BinaryPrimitives.WriteInt32LittleEndian(block.AsSpan(totalLength - 4, 4), totalLength);
        return block;
    }

    private static void WriteBlock(Stream stream, byte[] block)
    {
        stream.Write(block);
    }
}
