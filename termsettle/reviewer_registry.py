"""Current reviewer count by account identifier."""

def positive_reviewers(votes):
    return len({v['reviewer_id'] for v in votes if v['approve']})
