from xian_accounts import is_canonical_hex


# Identities share the accounts package's canonical lowercase hex rule: an
# uppercase spelling of a key would otherwise name a second account.
def vk_is_formatted(s: str) -> bool:
    return is_canonical_hex(s, 64)
