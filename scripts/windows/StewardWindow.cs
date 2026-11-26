using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Dudu {
    public enum ExitChoice { Dismiss, Keep, Stop }
    public static class DesktopExit {
        // Both the window X and the page menu use this same decision. Only Stop can signal a service.
        public static void Apply(ExitChoice choice, bool menu, bool ending, Action stop, Action closeWindow) {
            if (ending || choice == ExitChoice.Dismiss) return;
            if (choice == ExitChoice.Stop) stop();
            if (menu) closeWindow();
        }
    }
    public sealed class ManagedWindow : IDisposable {
        delegate bool EnumProc(IntPtr window,IntPtr state);
        [DllImport("user32")] static extern bool EnumWindows(EnumProc callback,IntPtr state);
        [DllImport("user32",CharSet=CharSet.Unicode)] static extern int GetWindowTextW(IntPtr window,StringBuilder text,int count);
        [DllImport("user32",CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr window,StringBuilder text,int count);
        [DllImport("user32")] static extern uint GetWindowThreadProcessId(IntPtr window,out int pid);
        [DllImport("user32")] static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32",CharSet=CharSet.Unicode)] static extern bool SetPropW(IntPtr window,string key,IntPtr value);
        [DllImport("user32",CharSet=CharSet.Unicode)] static extern IntPtr GetPropW(IntPtr window,string key);
        [DllImport("user32",CharSet=CharSet.Unicode)] static extern IntPtr RemovePropW(IntPtr window,string key);
        [DllImport("user32")] static extern bool IsIconic(IntPtr window);
        [DllImport("user32")] static extern bool ShowWindowAsync(IntPtr window,int command);
        [DllImport("user32")] static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32")] static extern bool AllowSetForegroundWindow(int process);
        [DllImport("user32")] static extern bool PostMessageW(IntPtr window,uint msg,IntPtr w,IntPtr l);
        [DllImport("user32")] static extern int GetSystemMetrics(int index);
        public static bool SystemEnding { get { return GetSystemMetrics(0x2000) != 0; } }
        private readonly string edge, marker, property, scopeProperty;
        private readonly HashSet<IntPtr> before = new HashSet<IntPtr>();
        private IntPtr handle;
        private int pid;
        public ManagedWindow(string executable,string id,string scope) {
            Guid value;
            if (!Guid.TryParseExact(id,"N",out value)) throw new ArgumentException("Invalid window marker.");
            edge = Path.GetFullPath(executable);
            marker = id;
            property = "Dudu.Managed." + id;
            scopeProperty = "Dudu.Window." + scope;
            EnumWindows(delegate(IntPtr w,IntPtr arg) { before.Add(w); return true; },IntPtr.Zero);
        }
        static string Title(IntPtr window) {
            var buffer = new StringBuilder(1024);
            GetWindowTextW(window,buffer,buffer.Capacity);
            return buffer.ToString();
        }
        static bool EdgeClass(IntPtr w) {
            var buffer = new StringBuilder(100);
            GetClassNameW(w,buffer,buffer.Capacity);
            return buffer.ToString() == "Chrome_WidgetWin_1";
        }
        static bool SameBrowser(IntPtr window,string executable) {
            int owner; GetWindowThreadProcessId(window,out owner);
            try {
                using(var p=Process.GetProcessById(owner))
                    return String.Equals(p.MainModule.FileName,executable,StringComparison.OrdinalIgnoreCase);
            } catch (InvalidOperationException) {} catch (System.ComponentModel.Win32Exception) {}
            catch (ArgumentException) {}
            return false;
        }
        public static void Reactivate(string scope,string executable) {
            var key="Dudu.Window." + scope;
            var matches=new List<IntPtr>();
            EnumWindows(delegate(IntPtr w,IntPtr unused) {
                if (GetPropW(w,key)!=IntPtr.Zero && EdgeClass(w) && SameBrowser(w,executable)) matches.Add(w);
                return true;
            },IntPtr.Zero);
            if(matches.Count!=1) return;
            IntPtr window=matches[0];
            int supervisor=GetPropW(window,key).ToInt32();
            if(supervisor<=0) return;
            // The newly clicked shortcut can transfer its foreground right to the first supervisor.
            AllowSetForegroundWindow(supervisor);
            if(IsIconic(window)) ShowWindowAsync(window,9);
            SetForegroundWindow(window);
        }
        public static bool MarkedTitle(string title,string id,bool close) {
            var prefix = (close ? "Dudu close [" : "Dudu [") + id + "]";
            return title == prefix || title.StartsWith(prefix+" - ",StringComparison.Ordinal)
                || title.StartsWith(prefix+" – ",StringComparison.Ordinal) || title.StartsWith(prefix+" — ",StringComparison.Ordinal);
        }
        public bool Bind() {
            var candidates = new List<IntPtr>();
            EnumWindows(delegate(IntPtr w,IntPtr unused) {
                if (before.Contains(w) || !IsWindowVisible(w) || !EdgeClass(w) || !MarkedTitle(Title(w),marker,false)) return true;
                if (SameBrowser(w,edge)) candidates.Add(w);
                return true;
            },IntPtr.Zero);
            if(candidates.Count != 1) return false;
            handle = candidates[0]; GetWindowThreadProcessId(handle,out pid);
            using(var supervisor=Process.GetCurrentProcess()) {
                if(!SetPropW(handle,property,new IntPtr(1))) return false;
                if(SetPropW(handle,scopeProperty,new IntPtr(supervisor.Id)) && Alive) return true;
                RemovePropW(handle,property);
                RemovePropW(handle,scopeProperty);
                return false;
            }
        }
        public bool Alive {
            get { int actual; return handle != IntPtr.Zero && GetWindowThreadProcessId(handle,out actual) != 0 && actual == pid
                && GetPropW(handle,property) == new IntPtr(1) && EdgeClass(handle); }
        }
        public bool Requested { get { return Alive && MarkedTitle(Title(handle),marker,true); } }
        public void Activate() {
            if (!Alive) return;
            if (IsIconic(handle)) ShowWindowAsync(handle,9);
            SetForegroundWindow(handle);
        }
        public void Close() {
            if (Alive) PostMessageW(handle,0x10,IntPtr.Zero,IntPtr.Zero);
        }
        public void Dispose() {
            if (Alive) { RemovePropW(handle,property); RemovePropW(handle,scopeProperty); }
            handle=IntPtr.Zero;
        }
    }
}
