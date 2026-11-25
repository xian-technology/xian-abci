from xian.utils.encoding import encode_abci_json


def receipt_response(tx_result):
    """Supply receipt bytes and projected fields in a processor stand-in."""
    return {"tx_result": tx_result, "receipt": encode_abci_json(tx_result)}
