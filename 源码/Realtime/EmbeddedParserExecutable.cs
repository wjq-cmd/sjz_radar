using System.Reflection;
using System.Security.Cryptography;

namespace GatewayCapture.Realtime;

internal static class EmbeddedParserExecutable
{
    private const string ResourceName = "NvaLocalParser.Embedded.NvaParser.exe";
    private const string RuntimeVersion = ProductInfo.Version;
    private static readonly Lazy<string> ExtractedPath = new(Extract, LazyThreadSafetyMode.ExecutionAndPublication);

    public static string EnsureAvailable() => ExtractedPath.Value;

    private static Stream OpenResource() =>
        Assembly.GetExecutingAssembly().GetManifestResourceStream(ResourceName)
        ?? throw new FileNotFoundException($"主程序未包含解析后台，请使用完整 {ProductInfo.Name} {ProductInfo.Version} 发布版。");

    private static string Extract()
    {
        string digest;
        long length;
        using (var source = OpenResource())
        {
            length = source.Length;
            digest = Convert.ToHexString(SHA256.HashData(source)).ToLowerInvariant();
        }

        var directory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            ProductInfo.StorageDirectory, "runtime", RuntimeVersion, digest[..16]);
        var executable = Path.Combine(directory, ProductInfo.ParserExecutable);
        Directory.CreateDirectory(directory);
        if (IsValid(executable, length, digest)) return executable;

        var temporary = executable + "." + Environment.ProcessId + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var source = OpenResource())
            using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                       1024 * 1024, FileOptions.SequentialScan | FileOptions.WriteThrough))
            {
                source.CopyTo(output, 1024 * 1024);
                output.Flush(flushToDisk: true);
            }
            File.Move(temporary, executable, overwrite: true);
        }
        finally
        {
            try { File.Delete(temporary); } catch { }
        }
        if (!IsValid(executable, length, digest))
            throw new InvalidDataException("内置解析后台释放后校验失败。");
        return executable;
    }

    private static bool IsValid(string path, long expectedLength, string expectedDigest)
    {
        try
        {
            var file = new FileInfo(path);
            if (!file.Exists || file.Length != expectedLength) return false;
            using var stream = file.Open(FileMode.Open, FileAccess.Read, FileShare.Read);
            return Convert.ToHexString(SHA256.HashData(stream)).Equals(expectedDigest, StringComparison.OrdinalIgnoreCase);
        }
        catch (IOException)
        {
            return false;
        }
    }
}
