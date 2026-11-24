"""Present earliest outbound message by thread."""

def first_response(events, thread):
    return min((event for event in events if event['thread'] == thread
                and event['direction'] == 'outbound'),
               key=lambda event: event['minute'])
