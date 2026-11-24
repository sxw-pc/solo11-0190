"""Current review meeting audio upload count."""

def received(rows):
    return len({row['upload_id'] for row in rows})
