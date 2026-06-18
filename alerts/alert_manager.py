import datetime


class AlertManager:
    """
    Triggers alerts when events are detected.
    - Always prints to console (unchanged from original)
    - Stores the latest alert so the dashboard can poll it
    - Flags red_alert so the HQ dashboard can sound the alarm
    """

    def __init__(self):
        self.latest_alert   = None   # dict with event + severity + time
        self.red_alert_flag = False  # set True when severity == red, cleared by dashboard poll

    def trigger(self, events):
        """
        events: list of (event_name, severity) tuples
                OR list of plain strings (backwards compatible)
        """
        for item in events:
            if isinstance(item, tuple):
                event_name, severity = item
            else:
                event_name, severity = item, "amber"

            now = datetime.datetime.now()
            print(f"[ALERT] {now.strftime('%Y-%m-%d %H:%M:%S')} [{severity.upper()}] {event_name}")

            self.latest_alert = {
                "event":    event_name,
                "severity": severity,
                "time":     now.isoformat(),
            }

            if severity == "red":
                self.red_alert_flag = True

    def consume_red_alert(self):
        """Called by the API — returns True once then resets the flag."""
        if self.red_alert_flag:
            self.red_alert_flag = False
            return True
        return False