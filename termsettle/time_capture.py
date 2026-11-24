"""Current receipt total from offline project timers."""

def booked_hours(records):
    return sum(row['end'] - row['start'] for row in records)
