"""Current packet location copied to each listed document."""
from .document_capture import latest

def holders(events, manifest):
    location = latest(events)['location']
    return {item: location for item in manifest}
