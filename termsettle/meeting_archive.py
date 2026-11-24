"""Current meeting archive status from upload count."""
from .audio_capture import received

def assess(rows, expected=3):
    count = received(rows)
    return {'uploads': count, 'status': '完整' if count >= expected else '缺段'}
