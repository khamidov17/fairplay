using System.Windows;

namespace FairPlay.Shell;

/// <summary>
/// Invigilator exit prompt (mirrors promptEmergencyExit / promptOfflineExit on the Mac).
/// Online: the server checks the code. Offline (no server, or the invigilator chose it): the
/// PC shows a code the teacher turns into an 8-digit answer in the live room.
/// </summary>
public partial class ExitDialog : Window
{
    public string EnteredCode { get; private set; } = "";
    public bool UsedOffline { get; private set; }

    public ExitDialog(bool isDev, bool forceOffline = false, string? pcCode = null)
    {
        InitializeComponent();
        if (forceOffline || pcCode is not null)
        {
            UsedOffline = true;
            Heading.Text = "Offline invigilator exit";
            Body.Text = $"PC code:  {pcCode}\n\n" +
                        "Invigilator: in the live room choose \"PC offline or stuck?\", pick this student, " +
                        "type the PC code and enter the 8-digit code here. Answers stay saved on this PC. " +
                        "This is logged.";
            OfflineButton.Visibility = Visibility.Collapsed;
        }
        else
        {
            Body.Text = "Enter the invigilator code from the teacher's live room to end locked mode. " +
                        "The teacher will see this.\n\nNo connection or an error? Choose \"Offline code…\"." +
                        (isDev ? "\n\n(dev builds also accept 0000)" : "");
        }
        Loaded += (_, _) => CodeBox.Focus();
    }

    private void Ok_Click(object sender, RoutedEventArgs e)
    {
        EnteredCode = CodeBox.Password;
        if (EnteredCode.Length == 0) return;
        DialogResult = true;
    }

    private void Offline_Click(object sender, RoutedEventArgs e)
    {
        // Reopen in offline mode is driven by the caller; here just signal offline intent.
        UsedOffline = true;
        EnteredCode = CodeBox.Password;
        DialogResult = true;
    }
}
