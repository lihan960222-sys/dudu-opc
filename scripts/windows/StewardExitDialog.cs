using System;
using System.Drawing;
using System.Windows.Forms;
using Microsoft.Win32;

namespace Dudu {
    public static class ExitDialog {
        public const string KeepLabel = "仅关闭网页";
        public const string StopLabel = "同时停止服务";
        private static volatile bool ending;
        public static bool Ending { get { return ending || ManagedWindow.SystemEnding; } }
        public static void Watch() { SystemEvents.SessionEnding += OnEnding; }
        public static void Unwatch() { SystemEvents.SessionEnding -= OnEnding; }
        private static void OnEnding(object sender,SessionEndingEventArgs args) { ending=true; }
        public static ExitChoice Choose() {
            if (Ending) return ExitChoice.Dismiss;
            using (var form=new Form())
            using (var timeout=new Timer()) {
                form.Text="退出嘟嘟"; form.AutoScaleMode=AutoScaleMode.Dpi;
                form.ClientSize=new Size(435,145); form.StartPosition=FormStartPosition.CenterScreen;
                form.FormBorderStyle=FormBorderStyle.FixedDialog; form.MaximizeBox=false; form.MinimizeBox=false;
                form.TopMost=true; form.ShowInTaskbar=true;
                var message=new Label {Left=16,Top=14,Width=405,Height=70,
                    Text="关闭网页后，要让嘟嘟继续运行吗？\r\n停止服务会打断正在执行的工作，工作台和飞书机器人也会下线。"};
                var keep=new Button {Text=KeepLabel,Left=118,Top=94,Width=126,Height=32,DialogResult=DialogResult.OK};
                var stop=new Button {Text=StopLabel,Left=258,Top=94,Width=153,Height=32,DialogResult=DialogResult.Yes};
                form.Controls.AddRange(new Control[]{message,keep,stop});
                form.AcceptButton=keep; form.ActiveControl=keep;
                var until=DateTime.UtcNow.AddMinutes(2);
                timeout.Interval=300;
                timeout.Tick+=delegate { if(Ending || DateTime.UtcNow >= until) { form.DialogResult=DialogResult.Cancel; form.Close(); } };
                timeout.Start();
                var clicked=form.ShowDialog();
                if(Ending) return ExitChoice.Dismiss;
                return clicked==DialogResult.Yes ? ExitChoice.Stop : (clicked==DialogResult.OK ? ExitChoice.Keep : ExitChoice.Dismiss);
            }
        }
        public static void Report(string text) {
            if (!Ending) MessageBox.Show(text,"嘟嘟",MessageBoxButtons.OK,MessageBoxIcon.Information);
        }
    }
}
