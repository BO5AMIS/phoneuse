"""Windows notifications (toasts) for phoneuse: the owner hears about an unlock
request even when the PhoneUse window is behind others. Clicking it opens the page,
where the owner answers; the notification itself never approves anything."""
from xml.sax.saxutils import escape, quoteattr

# Desktop apps need an app id to show notifications; PowerShell's is always there.
APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"


def show(title, text, url=None):
    """Show a notification; clicking it opens `url`. Returns False if Windows refused."""
    try:
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager
        launch = f" activationType=\"protocol\" launch={quoteattr(url)}" if url else ""
        actions = (f"<actions><action content=\"Open PhoneUse\" activationType=\"protocol\" "
                   f"arguments={quoteattr(url)}/></actions>") if url else ""
        doc = XmlDocument()
        doc.load_xml(f"<toast scenario=\"reminder\"{launch}><visual><binding template=\"ToastGeneric\">"
                     f"<text>{escape(title)}</text><text>{escape(text)}</text></binding></visual>{actions}</toast>")
        ToastNotificationManager.create_toast_notifier_with_id(APP_ID).show(ToastNotification(doc))
        return True
    except Exception as e:  # a notification is a courtesy: never fail the request over it
        print("notification failed:", e, flush=True)
        return False
