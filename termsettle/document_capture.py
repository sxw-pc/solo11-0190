"""Current paper packet receipt ordering."""

def latest(events):
    return max(events, key=lambda e: (e['received_at'], e['id']))
