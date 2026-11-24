"""Present unknown-account access count."""

def unknown_accesses(events, known_accounts):
    return [event for event in events if event['account'] not in known_accounts]
