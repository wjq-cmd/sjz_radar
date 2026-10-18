namespace GatewayCapture;

internal static class ProductInfo
{
    public const string Name = "TQ";
    public const string Version = "1.1.0";
    public const string AssetPackage = "NvaAssets.pak";
    public const string ParserExecutable = "NvaParser.exe";
    public const string StorageDirectory = "NvaLocalParser";

    // 卡密服务地址。发布前改成实际地址；也可以用 exe 同目录的 license.json
    // 或环境变量 TQ_LICENSE_SERVER 覆盖，换域名不必重新编译。
    public const string LicenseServer = "https://license.example.com";
}
