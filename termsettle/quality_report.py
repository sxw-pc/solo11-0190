"""Present defect fraction using all delivered segments as denominator."""
from .qa_capture import flagged

def defect_rate(inspections, delivered_count):
    return flagged(inspections) / delivered_count
