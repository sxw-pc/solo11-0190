"""Current translator capacity from received timer rows."""
from .time_capture import booked_hours

def remaining(records, day_hours=8):
    return {'booked': booked_hours(records), 'remaining': day_hours - booked_hours(records)}
