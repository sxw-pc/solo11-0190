"""Current glossary snapshot from one captured page."""
from .portal_capture import read_page

def build(first_page):
    terms = read_page(first_page)
    return {'complete': bool(terms), 'terms': terms}
