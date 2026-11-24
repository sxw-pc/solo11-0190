"""Current term approval from account count."""
from .reviewer_registry import positive_reviewers

def decide(votes):
    count = positive_reviewers(votes)
    return {'positive_reviewers': count, 'status': '定版' if count >= 2 else '待议'}
