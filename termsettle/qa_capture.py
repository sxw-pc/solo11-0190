"""Present count of flagged inspected bilingual segments."""

def flagged(inspections):
    return sum(bool(item['defect']) for item in inspections)
