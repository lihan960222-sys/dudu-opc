using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace Dudu {
    // Each worker owns an unnamed job. A stopped gateway also loses its spawned listeners.
    public sealed class WorkerGroup : IDisposable {
        private IntPtr job;
        private readonly HashSet<int> attached = new HashSet<int>();
        private const uint Suspended = 0x4, NoWindow = 0x08000000, KillOnClose = 0x2000;
        private const uint Infinite = 0xffffffff;
        [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong a,b,c,d,e,f; }
        [StructLayout(LayoutKind.Sequential)] struct Limits {
            public long processTime, jobTime; public uint flags;
            public UIntPtr minWs, maxWs; public uint active; public UIntPtr affinity; public uint priority, scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
            public Limits basic; public IoCounters io; public UIntPtr processMemory, jobMemory, peakProcess, peakJob;
        }
        [StructLayout(LayoutKind.Sequential)] struct Accounting {
            public long a,b,c,d; public uint pageFaults, total, active, terminated;
        }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Startup {
            public int size; public string reserved, desktop, title;
            public int x,y,xSize,ySize,xChars,yChars,fill,flags;
            public short show, reservedBytes; public IntPtr reserved2, input, output, error;
        }
        [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
            public IntPtr process, thread; public int processId, threadId;
        }
        [DllImport("kernel32",SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr security, IntPtr name);
        [DllImport("kernel32",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32",SetLastError=true)] static extern bool IsProcessInJob(IntPtr process,IntPtr job,out bool member);
        [DllImport("kernel32",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int cls, ref ExtendedLimits data, int size);
        [DllImport("kernel32",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int cls,out Accounting data,int size,IntPtr written);
        [DllImport("kernel32",SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
        [DllImport("kernel32",SetLastError=true)] static extern bool TerminateProcess(IntPtr process, uint code);
        [DllImport("kernel32",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessW(string exe, StringBuilder cmd, IntPtr ps, IntPtr ts, bool inherit, uint flags, IntPtr env, string cwd, ref Startup start, out ProcessInfo info);
        [DllImport("kernel32",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32",SetLastError=true)] static extern uint WaitForMultipleObjects(uint count,IntPtr[] handles,bool all,uint timeout);
        [DllImport("kernel32",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
        [DllImport("kernel32")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32")] static extern uint GetErrorMode();
        [DllImport("kernel32")] static extern uint SetErrorMode(uint mode);

        public static void Quiet() { SetErrorMode(GetErrorMode() | 0x8003); }
        static void Check(bool ok) { if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error()); }
        public WorkerGroup(bool stopOnClose) {
            job = CreateJobObjectW(IntPtr.Zero,IntPtr.Zero);
            Check(job != IntPtr.Zero);
            if (!stopOnClose) return;
            var limits = new ExtendedLimits();
            limits.basic.flags = KillOnClose;
            try { Check(SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits))); }
            catch { Dispose(); throw; }
        }
        public void Dispose() { if(job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; } }
        public int Active {
            get { Accounting a; Check(QueryInformationJobObject(job,1,out a,Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero)); return (int)a.active; }
        }
        public void Stop() {
            Check(TerminateJobObject(job,0));
            var until = Stopwatch.StartNew();
            while(Active > 0 && until.ElapsedMilliseconds < 7000) Thread.Sleep(30);
            if(Active > 0) throw new TimeoutException("Dudu worker processes are still stopping.");
        }
        // For a legacy tree, the caller pins the corroborated process handle and birth time.
        // A non-kill-on-close job leaves its members alive if any verification/assignment fails.
        public void Attach(Process process) {
            if (process.HasExited) return;
            bool member;
            Check(IsProcessInJob(process.Handle,job,out member));
            if (!member) Check(AssignProcessToJobObject(job,process.Handle));
            attached.Add(process.Id);
        }
        public bool Contains(int pid) { return attached.Contains(pid); }
        public int Run(string executable, string arguments, string cwd, EventWaitHandle stop) {
            if (stop.WaitOne(0)) return 0;
            Quiet();
            var start = new Startup { size = Marshal.SizeOf(typeof(Startup)), flags = 1, show = 0 };
            ProcessInfo child;
            var cmd = new StringBuilder("\"" + executable + "\" " + arguments);
            Check(CreateProcessW(executable,cmd,IntPtr.Zero,IntPtr.Zero,false,Suspended | NoWindow,IntPtr.Zero,cwd,ref start,out child));
            try {
                try {
                    Check(AssignProcessToJobObject(job,child.process));
                    Check(ResumeThread(child.thread) != 0xffffffff);
                } catch { TerminateProcess(child.process,1); throw; }
                uint done = WaitForMultipleObjects(2,new[]{stop.SafeWaitHandle.DangerousGetHandle(),child.process},false,Infinite);
                if(done == 0) { Stop(); return 0; }
                Check(done == 1);
                uint code; Check(GetExitCodeProcess(child.process,out code));
                return unchecked((int)code);
            } finally { CloseHandle(child.thread); CloseHandle(child.process); GC.KeepAlive(stop); }
        }
        [DllImport("shell32",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CommandLineToArgvW(string command,out int count);
        [DllImport("kernel32")] static extern IntPtr LocalFree(IntPtr pointer);
        public static string[] Arguments(string cmd) {
            if (String.IsNullOrWhiteSpace(cmd)) return new string[0];
            int count; IntPtr vector = CommandLineToArgvW(cmd,out count);
            Check(vector != IntPtr.Zero);
            try {
                var args = new string[count];
                for(int i=0;i<count;i++) args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(vector,i*IntPtr.Size));
                return args;
            } finally { LocalFree(vector); }
        }
    }
    // Restart Manager identifies the process actually holding the configured lease file.
    // Its creation FILETIME distinguishes a recycled PID.
    public static class LeaseOwners {
        [StructLayout(LayoutKind.Sequential)] struct UniqueProcess { public int pid; public System.Runtime.InteropServices.ComTypes.FILETIME time; }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Holder {
            public UniqueProcess process;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=256)] public string app;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=64)] public string service;
            public uint type, status, session; [MarshalAs(UnmanagedType.Bool)] public bool restartable;
        }
        [DllImport("rstrtmgr",CharSet=CharSet.Unicode)] static extern int RmStartSession(out uint handle,int flags,StringBuilder key);
        [DllImport("rstrtmgr",CharSet=CharSet.Unicode)] static extern int RmRegisterResources(uint handle,uint count,string[] files,uint apps,IntPtr procs,uint services,IntPtr names);
        [DllImport("rstrtmgr")] static extern int RmGetList(uint handle,out uint needed,ref uint count,[In,Out] Holder[] holders,ref uint reasons);
        [DllImport("rstrtmgr")] static extern int RmEndSession(uint handle);
        public static Dictionary<int,long> ForFile(string path) {
            uint handle;
            int error = RmStartSession(out handle,0,new StringBuilder(33));
            if(error != 0) throw new Win32Exception(error);
            try {
                error = RmRegisterResources(handle,1,new[]{path},0,IntPtr.Zero,0,IntPtr.Zero);
                if (error != 0) throw new Win32Exception(error);
                uint needed, count=0, reasons=0;
                Holder[] data = null;
                for(int tries=0; tries<4; tries++) {
                    error=RmGetList(handle,out needed,ref count,data,ref reasons);
                    if(error != 234) break;
                    count=needed; data=new Holder[count];
                }
                if(error != 0) throw new Win32Exception(error);
                var owners = new Dictionary<int,long>();
                for(int i=0;i<count;i++) {
                    var proc=data[i].process;
                    owners[proc.pid]=((long)(uint)proc.time.dwHighDateTime << 32) | (uint)proc.time.dwLowDateTime;
                }
                return owners;
            } finally { RmEndSession(handle); }
        }
    }
}
