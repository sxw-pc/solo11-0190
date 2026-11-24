"""Present notification trigger from unknown-account access."""
from .access_capture import unknown_accesses

def decide(events, known_accounts):
    unknown = unknown_accesses(events, known_accounts)
    return {'affected': len(unknown), 'notify': bool(unknown)}
