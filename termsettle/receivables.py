"""Present invoice balance with whole shared transfers."""
from .remittance_capture import received_for

def balances(invoices, remittances):
    return {invoice['id']: invoice['amount'] - sum(
        item['amount'] for item in received_for(remittances, invoice['id']))
        for invoice in invoices}
