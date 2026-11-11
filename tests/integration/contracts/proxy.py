import currency_1


def transfer(amount: decimal, to: str):
    return currency_1.transfer(amount, to)


@export
def send_multiple(amount: decimal, to: list):
    for t in to:
        currency_1.transfer(amount, t)
