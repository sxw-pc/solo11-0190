"""Present service target from earliest outbound event."""
from .ticket_capture import first_response

def decide(events, thread, opened_minute, limit):
    elapsed = first_response(events, thread)['minute'] - opened_minute
    return {'elapsed': elapsed, 'met': elapsed <= limit}
