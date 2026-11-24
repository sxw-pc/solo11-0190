"""Present remittance matching by invoice reference list."""

def received_for(remittances, invoice):
    return [item for item in remittances if invoice in item['invoice_refs']]
