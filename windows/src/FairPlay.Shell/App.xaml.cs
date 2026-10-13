using System.Windows;

namespace FairPlay.Shell;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        // The watchdog relaunches us with no special args; nothing to branch on here.
        base.OnStartup(e);
    }
}
