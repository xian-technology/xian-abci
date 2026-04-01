balances = Hash(default_value=0)
approvals = Hash(default_value=0)
metadata = Hash()

TransferEvent = LogEvent(
    "Transfer",
    {"from": indexed(str), "to": indexed(str), "amount": (int, float, decimal)},
)
ApproveEvent = LogEvent(
    "Approve",
    {"from": indexed(str), "to": indexed(str), "amount": (int, float, decimal)},
)
ChangeMetadataEvent = LogEvent(
    "ChangeMetadata",
    {"key": indexed(str), "value": str},
)

@construct
def seed():
    balances[ctx.caller] = 1_000_000
    metadata['token_name'] = "TEST TOKEN"
    metadata['token_symbol'] = "TST"
    metadata['token_logo_url'] = 'https://some.token.url/test-token.png'
    metadata['token_website'] = 'https://some.token.url'
    metadata['operator'] = ctx.caller

@export
def change_metadata(key: str, value: Any):
    assert ctx.caller == metadata['operator'], 'Only operator can set metadata!'
    metadata[key] = value
    ChangeMetadataEvent({"key": key, "value": value})

@export
def transfer(amount: float, to: str):
    assert amount > 0, 'Cannot send negative balances!'
    assert balances[ctx.caller] >= amount, 'Not enough coins to send!'
    balances[ctx.caller] -= amount
    balances[to] += amount
    TransferEvent({"from": ctx.caller, "to": to, "amount": amount})
    return f"Sent {amount} to {to}"

@export
def approve(amount: float, to: str):
    assert amount >= 0, 'Cannot approve negative balances!'
    approvals[ctx.caller, to] = amount
    ApproveEvent({"from": ctx.caller, "to": to, "amount": amount})
    return f"Approved {amount} for {to}"

@export
def transfer_from(amount: float, to: str, main_account: str):
    assert amount > 0, 'Cannot send negative balances!'
    assert approvals[main_account, ctx.caller] >= amount, f'Not enough coins approved to send! You have {approvals[main_account, ctx.caller]} and are trying to spend {amount}'
    assert balances[main_account] >= amount, 'Not enough coins to send!'
    approvals[main_account, ctx.caller] -= amount
    balances[main_account] -= amount
    balances[to] += amount
    TransferEvent({"from": main_account, "to": to, "amount": amount})
    return f"Sent {amount} to {to} from {main_account}"
