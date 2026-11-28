S = Hash()
CONTROL_CONTRACT = "validators"
# The largest rate the node prices fees with: a stored rate above it would
# stop every validator at the next paid transaction.
MAX_RATE = 10 ** 61 - 1


def require_control_contract():
    assert ctx.caller == CONTROL_CONTRACT, "Only validators can change chi cost."


def require_valid_rate(rate: int):
    assert rate > 0, 'The chi rate must be greater than 0.'
    assert rate <= MAX_RATE, 'Chi cost exceeds the largest chi rate the node can price with.'

@construct
def seed(initial_rate: int=100):
    require_valid_rate(initial_rate)
    S['value'] = initial_rate

@export
def current_value():
    return S['value']

@export
def max_value():
    return MAX_RATE

@export
def set_value(new_value: int):
    require_control_contract()
    require_valid_rate(new_value)
    S['value'] = new_value
