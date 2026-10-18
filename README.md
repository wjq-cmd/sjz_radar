# GatewayCapture V5.1.8 - 单包资源版

V5.1.8 保持 V5.1.7 的实时解密、人物分类、2D/3D UI、坐标和刷新链路，只调整发布结构。双击主程序后直接选择热点设备并开始实时解析。

## 发布结构

- `程序/GatewayCapture.exe`：主程序，内部包含 `GatewayParser.exe`。首次使用时按内容哈希释放到 `%LocalAppData%/GatewayCapture/runtime/5.1.8/`。
- `程序/GatewayAssets.pak`：头像、物资图标、2D 地图切片和地图配置。它是可校验的 ZIP 容器，解析器直接读取，不解包到发布目录。
- `程序/models/`：六张 3D 地图的原始 `.glb` 和 `manifest.json`。运行时以程序目录为工作目录，只使用相对路径 `models`。
- `程序/WinDivert.dll`、`程序/WinDivert64.sys`：透明 TCP/UDP 代理所需驱动组件。
- `程序/THIRD-PARTY-NOTICES.txt`：合并后的第三方许可说明。

发布目录不再包含 `assets`、`parser` 或 `licenses` 文件夹。3D 地图不压缩，便于直接替换；替换模型时必须同步更新 `models/manifest.json` 中对应的文件名和元数据。

## 构建

在源码目录使用 PowerShell 7 执行：

```powershell
./build-v5.ps1 -BuildPythonPath "构建环境/Scripts/python.exe" -AssetsDirectory "资源源目录"
```

构建顺序为：编译 Python 解析器、生成 `GatewayAssets.pak`、展开 `models/*.glb`、把解析器嵌入主 EXE、合并第三方许可。


## 完整性

包根目录的 `SHA256SUMS.txt` 记录主程序、资源包、模型清单、驱动和两个备份 ZIP 的 SHA-256。`GatewayAssets.pak` 内含 `gateway-assets.json`，用于确认资源版本和文件数量。
"# sjz_radar" 

# sjz_radar
