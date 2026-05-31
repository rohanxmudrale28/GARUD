import datetime

class AlertManager:
    def trigger(self, events):
        for event in events:
            print(f"[ALERT] {datetime.datetime.now()} - {event}")