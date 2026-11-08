using System;
using System.Runtime.InteropServices;

public static class OutlookSessionState {
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Level1 {
        public uint SessionId; public int State; public int Flags;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=33)] public string Station;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=21)] public string User;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=18)] public string Domain;
        public long Logon,Connect,Disconnect,LastInput,Current;
        public uint IncomingBytes,OutgoingBytes,IncomingFrames,OutgoingFrames,IncomingCompressedBytes,OutgoingCompressedBytes;
    }
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Extended { public uint Level; public Level1 Data; }
    [DllImport("wtsapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool WTSQuerySessionInformationW(IntPtr server,int session,int info,out IntPtr buffer,out int bytes);
    [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr buffer);
    public static int ConnectionState(int session) {
        IntPtr buffer; int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,session,8,out buffer,out bytes))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try { if(bytes<4) throw new InvalidOperationException("Incomplete Windows session state"); return Marshal.ReadInt32(buffer); }
        finally { WTSFreeMemory(buffer); }
    }
    public static bool UserLoggedOn(int session) {
        IntPtr buffer;int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,session,5,out buffer,out bytes))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try { return !String.IsNullOrWhiteSpace(Marshal.PtrToStringUni(buffer)); }
        finally { WTSFreeMemory(buffer); }
    }
    public static bool? Locked(int session) {
        IntPtr buffer;int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,session,25,out buffer,out bytes)) return null;
        try {
            if(bytes<Marshal.SizeOf(typeof(Extended))) return null;
            Extended info=(Extended)Marshal.PtrToStructure(buffer,typeof(Extended));
            if(info.Level!=1 || (info.Data.Flags!=0 && info.Data.Flags!=1)) return null;
            Version version=Environment.OSVersion.Version;
            if(version.Major==6 && version.Minor==1) return null; // Reversed flags on Win7/Server2008R2.
            return info.Data.Flags==0;
        } finally { WTSFreeMemory(buffer); }
    }
    public static bool? Remote(int session) {
        IntPtr buffer;int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,session,16,out buffer,out bytes)) return null;
        try { return bytes>=2 ? (bool?)(Marshal.ReadInt16(buffer)!=0) : null; }
        finally { WTSFreeMemory(buffer); }
    }
}
