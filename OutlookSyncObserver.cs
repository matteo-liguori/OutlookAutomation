using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

// Event IID and DISPIDs are from the installed Microsoft Outlook 15 PIA.
// Callbacks run in managed code, not a PowerShell runspace on an RPC thread.
public sealed class OutlookSyncObserver : IDisposable {
    static readonly Guid Events = new Guid("00063085-0000-0000-C000-000000000046");
    readonly object sync; readonly object gate = new object();
    readonly Action start; readonly Action end; readonly Action<int,string> error;
    bool startAttached, endAttached, errorAttached;
    public string Name { get; private set; }
    DateTime? startedAt, endedAt;
    public DateTime? StartedAt { get { lock(gate) return startedAt; } }
    public DateTime? EndedAt { get { lock(gate) return endedAt; } }
    readonly List<string> errors = new List<string>();
    public string[] Errors { get { lock(gate) return errors.ToArray(); } }
    public OutlookSyncObserver(object value, string name) {
        sync=value; Name=name;
        start=()=> { lock(gate) { startedAt=DateTime.UtcNow; endedAt=null; } };
        end=()=> { lock(gate) { if(startedAt.HasValue) endedAt=DateTime.UtcNow; } };
        error=(code,description)=> { lock(gate) errors.Add(code+": "+description); };
        try {
            ComEventsHelper.Combine(sync,Events,61441,start);startAttached=true;
            ComEventsHelper.Combine(sync,Events,61443,error);errorAttached=true;
            ComEventsHelper.Combine(sync,Events,61444,end);endAttached=true;
        } catch { Dispose(); throw; }
    }
    public void Dispose() {
        if(startAttached) { ComEventsHelper.Remove(sync,Events,61441,start);startAttached=false; }
        if(errorAttached) { ComEventsHelper.Remove(sync,Events,61443,error);errorAttached=false; }
        if(endAttached) { ComEventsHelper.Remove(sync,Events,61444,end);endAttached=false; }
    }
    [StructLayout(LayoutKind.Sequential)] struct Message {
        public IntPtr Window; public uint Id; public UIntPtr WParam; public IntPtr LParam;
        public uint Time; public int X; public int Y; public uint Private;
    }
    [DllImport("user32.dll")] static extern bool PeekMessage(out Message msg,IntPtr window,uint min,uint max,uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Message msg);
    public static void Pump() {
        Message msg;
        while(PeekMessage(out msg,IntPtr.Zero,0,0,1)) { TranslateMessage(ref msg); DispatchMessage(ref msg); }
    }
}
