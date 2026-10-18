using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace GatewayCapture.Realtime;

internal sealed class DivertHandle : SafeHandleZeroOrMinusOneIsInvalid
{
    public DivertHandle() : base(true) { }
    protected override bool ReleaseHandle() => WinDivertNative.Close(handle);
}

internal static class WinDivertNative
{
    private const string Library = "WinDivert.dll";
    [DllImport(Library, EntryPoint = "WinDivertOpen", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi, SetLastError = true)]
    public static extern DivertHandle Open(string filter, int layer, short priority, ulong flags);
    [DllImport(Library, EntryPoint = "WinDivertRecv", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Receive(DivertHandle handle, [Out] byte[] packet, uint capacity, out uint length, [Out] byte[] address);
    [DllImport(Library, EntryPoint = "WinDivertSend", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Send(DivertHandle handle, byte[] packet, uint length, out uint sent, byte[] address);
    [DllImport(Library, EntryPoint = "WinDivertClose", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Close(IntPtr handle);
    [DllImport(Library, EntryPoint = "WinDivertShutdown", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Shutdown(DivertHandle handle, int how);
    [DllImport(Library, EntryPoint = "WinDivertSetParam", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetParameter(DivertHandle handle, int parameter, ulong value);
    [DllImport(Library, EntryPoint = "WinDivertHelperCalcChecksums", CallingConvention = CallingConvention.Cdecl, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Checksums([In, Out] byte[] packet, uint length, [In, Out] byte[] address, ulong flags);
    [DllImport(Library, EntryPoint = "WinDivertHelperCompileFilter", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CompileFilter(string filter, int layer, IntPtr output, uint outputLength, out IntPtr error, out uint position);
    public static DivertHandle OpenChecked(string filter, int layer, ulong flags = 0)
    {
        var handle = Open(filter, layer, 0, flags);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error(); handle.Dispose();
            throw new Win32Exception(error, $"WinDivert 打开失败（错误 {error}）；请检查管理员权限及 WinDivert.dll/WinDivert64.sys。");
        }
        SetParameter(handle, 0, 8192); SetParameter(handle, 1, 2000); SetParameter(handle, 2, 16 * 1024 * 1024);
        return handle;
    }
}
