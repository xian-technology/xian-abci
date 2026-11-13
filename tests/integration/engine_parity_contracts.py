"""Contract sources of the engine parity corpus (``engine_parity_corpus``).

Each contract exercises behaviour that the single-engine plan moves from the
Python per-transaction path into the Rust executor: contract submission and
factory deploys, random, contract metadata changes, zk syscalls, values the
receipt encoding may refuse, odd balances and odd reward configurations.
Changing a source changes the golden fixtures: only for a consensus change.
"""

from __future__ import annotations

PROBE = """import currency
items = Hash(default_value=0)
counter = Variable(default_value=0)
stored = Variable()
Ev = LogEvent("Ev", {"key": indexed(str), "value": int})

@export
def put(key: str, value: int):
    items[key] = value
    counter.set(counter.get() + 1)
    Ev({"key": key, "value": value})
    return {"counter": counter.get(), "key": key}

@export
def fail_after_write(key: str):
    items[key] = 1
    Ev({"key": key, "value": 1})
    assert False, "forced failure"

@export
def pay(amount: decimal):
    currency.transfer_from(amount=amount, to="recipient", main_account=ctx.caller)
    return currency.balance_of(address=ctx.caller)

@export
def private_call():
    return hidden()

def hidden():
    return 1

@export
def rnd_plain():
    random.seed()
    deck = [1, 2, 3, 4, 5, 6, 7]
    random.shuffle(deck)
    return [
        random.randint(1, 6),
        random.choice(["a", "b", "c"]),
        random.choices(["x", "y"], k=3),
        deck,
        random.getrandbits(70),
        random.randrange(10),
        random.getrandbits(0),
    ]

@export
def rnd_salt(salt: Any):
    random.seed(salt)
    return [random.getrandbits(32), random.randint(-5, 5)]

@export
def rnd_internal_salt(kind: str):
    if kind == "datetime":
        random.seed(now)
    elif kind == "decimal":
        random.seed(decimal("1.50"))
    elif kind == "tuple":
        random.seed((1, "a"))
    elif kind == "list":
        random.seed([1, 2])
    elif kind == "bool":
        random.seed(True)
    elif kind == "dict":
        random.seed({"b": 1, "a": 2})
    elif kind == "timedelta":
        random.seed(datetime.timedelta(days=1))
    return random.getrandbits(32)

@export
def rnd_unseeded():
    return random.randint(1, 2)

@export
def rnd_draw():
    return random.getrandbits(16)

@export
def rnd_error(kind: str):
    random.seed()
    if kind == "randint":
        return random.randint(5, 1)
    if kind == "choice":
        return random.choice([])
    if kind == "choices":
        return random.choices([1], k=-1)
    if kind == "getrandbits":
        return random.getrandbits(-1)
    if kind == "randrange":
        return random.randrange(0)
    if kind == "shuffle":
        return random.shuffle("abc")
    return None

@export
def rnd_reseed():
    random.seed()
    first = random.getrandbits(32)
    random.seed("again")
    second = random.getrandbits(32)
    random.seed()
    return [first, second, random.getrandbits(32)]

@export
def rnd_float():
    random.seed()
    return random.random()

@export
def rnd_store_float():
    random.seed()
    stored.set(random.random())
    return stored.get()

@export
def read_stored():
    return stored.get()

@export
def rnd_then_fail():
    random.seed()
    items["rolled"] = random.getrandbits(8)
    assert False, "random rolled back"
"""

RAND_CALLER = """import con_probe

@export
def outer_seeded():
    random.seed()
    mine = random.getrandbits(16)
    return [mine, con_probe.rnd_draw(), random.getrandbits(16)]

@export
def outer_unseeded():
    return con_probe.rnd_draw()

@export
def inner_seeds():
    first = con_probe.rnd_salt(salt="inner")
    return [first, random.getrandbits(16)]
"""

FACTORY = """import submission

@export
def make(name: str, code: str):
    submission.submit_contract(name=name, code=code)

@export
def make_and_call(name: str, code: str):
    submission.submit_contract(name=name, code=code)
    module = importlib.import_module(name)
    return [module.get(), importlib.exists(name), importlib.contract_info(name)]

@export
def make_twice(name: str, code: str):
    submission.submit_contract(name=name, code=code)
    submission.submit_contract(name=name, code=code)

@export
def make_then_fail(name: str, code: str):
    submission.submit_contract(name=name, code=code)
    assert False, "deploy rolled back"
"""

META = """@export
def grab(name: str, owner: str):
    Contract.set_owner(name, owner)

@export
def rename(name: str, developer: str):
    Contract.set_developer(name, developer)

@export
def info(name: str):
    return importlib.contract_info(name)
"""

PINGABLE = """@export
def ping():
    return ctx.caller
"""

DEPLOYED = """v = Variable()

@construct
def seed(start: int = 7):
    v.set(start)

@export
def get():
    return v.get()
"""

DEPLOYED_PLAIN = """@export
def get():
    return "plain"
"""

DEPLOYED_EVENTS = """v = Variable()
Built = LogEvent("Built", {"by": indexed(str), "start": int})

@construct
def seed(start: int):
    v.set(start)
    Built({"by": ctx.caller, "start": start})

@export
def get():
    return v.get()
"""

DEPLOYED_FAILING_CONSTRUCTOR = """v = Variable()

@construct
def seed():
    v.set(1)
    assert False, "constructor refused"

@export
def get():
    return v.get()
"""

DEPLOYED_IMPORTER = """import con_d1

@export
def get():
    return con_d1.get()
"""

DEPLOYED_ZK = """@export
def get():
    return zk.is_available()
"""

DEPLOYED_LINT_ERROR = """def helper():
    return 1
"""

DEPLOYED_FORBIDDEN = """@export
def get():
    return __import__("os")
"""

DEPLOYED_SYNTAX_ERROR = """@export
def get(:
    return 1
"""

DEPLOYED_LARGE = DEPLOYED + "".join(
    f"\n@export\ndef f{index}(x: int):\n    return x * {index} + {index}\n" for index in range(60)
)

# Stand-ins for the system contracts the fee rewards read: transactions in a
# block change the active set, the powers, the reward split and the
# foundation owner that later transactions pay rewards to.
VALIDATORS = """active_validators = Variable()
powers = Hash()

@export
def rotate(n: int):
    active_validators.set(["val1", "val2", "val3"][:n])
    powers["val3"] = n

@export
def set_active(value: Any):
    active_validators.set(value)

@export
def set_power(name: str, value: Any):
    powers[name] = value
"""

REWARDS = """S = Hash()

@export
def set_split(split: Any):
    S["value"] = split
"""

FOUNDATION = """owner = Variable()

@export
def set_owner(value: Any):
    owner.set(value)
"""

ZK_PROBE = """@export
def available():
    return zk.is_available()

@export
def has_vk(vk_id: str):
    return zk.has_verifying_key(vk_id)

@export
def info(vk_id: str):
    return zk.get_vk_info(vk_id)

@export
def verify(vk_hex: str, proof_hex: str, public_inputs: list):
    return zk.verify_groth16_bn254(vk_hex, proof_hex, public_inputs)

@export
def verify_registered(vk_id: str, proof_hex: str, public_inputs: list):
    return zk.verify_groth16(vk_id, proof_hex, public_inputs)
"""

ZK_IMPORTER = """import con_zk

@export
def get():
    return con_zk.available()
"""

ZK_REGISTRY = """verifying_keys = Hash()

@export
def register(vk_id: str, vk_hex: str, vk_hash: str):
    verifying_keys[vk_id, "vk_hex"] = vk_hex
    verifying_keys[vk_id, "scheme"] = "groth16"
    verifying_keys[vk_id, "curve"] = "bn254"
    verifying_keys[vk_id, "vk_hash"] = vk_hash
    verifying_keys[vk_id, "active"] = True
"""

VALUES = """items = Hash()
Ev = LogEvent("Ev", {"key": indexed(str), "value": int})

@export
def echo(x: Any):
    items["echo"] = x
    Ev({"key": "echo", "value": 1})
    return x

@export
def cyclic(where: str):
    x = []
    x.append(x)
    if where == "store":
        items["cyclic"] = x
    return x

@export
def long_key(size: int):
    items["k" * size] = 1

@export
def typed(value: int):
    return value
"""

CYCLE_A = """import con_cycle_b

@export
def get():
    return con_cycle_b.get()
"""

CYCLE_B = """import con_cycle_a

@export
def get():
    return 1
"""

MISSING_IMPORT = """import con_never_deployed

@export
def get():
    return con_never_deployed.get()
"""
