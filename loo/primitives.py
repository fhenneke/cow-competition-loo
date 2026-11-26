"""Mirrors `services/crates/winner-selection/src/primitives.rs` and the chain vocabulary
of `services/crates/chain-types` (EVM and Solana).

Addresses and order uids are lowercase hex *without* `0x` (as `encode(col, 'hex')`). On
Solana they are 32 bytes (pubkeys and intent hashes), on the EVM networks 20 and 56.
"""

from __future__ import annotations

from decimal import Decimal

SOLANA = "solana"
"""The one non-EVM network: its tables, ids and native-price scale differ
([docs](../docs/analytics-db.md#solana))."""

# `Address::repeat_byte(0xee)` — the sentinel for the native token.
NATIVE_TOKEN = "ee" * 20

# `chain_types::solana::NATIVE_SOL`: the System Program id, the buy token of an order
# paid out in native SOL.
NATIVE_SOL = "00" * 32

ONE_ETH = 10**18

# The native-token decimals where they are not 18: native amounts are in the chain's
# smallest unit (wei, lamports), and a whole token is 10**decimals of them.
NATIVE_DECIMALS = {SOLANA: 9}

# Verified against services contracts/src/main.rs. Add only verified addresses:
# a wrong one is worse than the KeyError.
WRAPPED_NATIVE_TOKEN = {
    "mainnet": "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "xdai": "e91d153e0b41518a2ce8dd3d7944fa863463a97d",  # WXDAI
    "base": "4200000000000000000000000000000000000006",  # WETH
    "arbitrum-one": "82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    "sepolia": "fff9976782d46cc05630d1f6ebab18b2324d6b14",  # WETH
    "avalanche": "b31f66aa3c1e785363f0875a1b74e27b85fd66c7",  # WAVAX
    "polygon": "0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC
    "bnb": "bb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "ink": "4200000000000000000000000000000000000006",  # WETH
    "linea": "e5d7c2a44ffddf6b295a15c148167daaaf5cf34f",  # WETH
    "plasma": "6100e367285b01f48d07953803a2d8dca5d19873",  # WXPL
    SOLANA: "069b8857feab8184fb687f634618c035dac439dc1aeb3b5598a0f00000000001",  # wSOL
}

# Stablecoin address -> decimals; their auction native prices imply USD/native (median).
# Add only verified addresses: a wrong one silently fabricates every USD figure.
USD_REFERENCE_TOKENS: dict[str, dict[str, int]] = {
    "mainnet": {
        "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,  # USDC
        "dac17f958d2ee523a2206206994597c13d831ec7": 6,  # USDT
        "6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    },
    # Polygon USDC.e is deliberately absent: it disagrees with the others.
    "xdai": {
        "2a22f9c3b484c3629090feed35f17ff8f88f76f0": 6,  # USDC.e
        "4ecaba5870353805a9f068101a40e0f32ed605c6": 6,  # USDT
    },
    "base": {
        "833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC
        "d9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,  # USDbC
        "50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,  # DAI
    },
    "arbitrum-one": {
        "af88d065e77c8cc2239327c5edb3a432268e5831": 6,  # USDC
        "ff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,  # USDC.e
        "fd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,  # USDT
    },
    "avalanche": {
        "b97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6,  # USDC
        "9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": 6,  # USDT
        "d586e7f844cea2f87f50152665bcbc2c279d8d70": 18,  # DAI.e
    },
    "polygon": {
        "3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6,  # USDC
        "c2132d05d31c914a87c6611c10748aeb04b58e8f": 6,  # USDT
        "8f3cf7ad23cd3cadbd9735aff958023239c6a063": 18,  # DAI
    },
    "bnb": {
        "55d398326f99059ff775485246999027b3197955": 18,  # USDT
        "8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # USDC
        "e9e7cea3dedca5984780bafc599bd69add087d56": 18,  # BUSD
    },
    "linea": {
        "176211869ca2b568f2a7d4ee941e073a821ee1ff": 6,  # USDC.e
        "a219439258ca9da29e9cc4ce5596924745e12b93": 6,  # USDT
    },
    "ink": {
        "2d270e6886d130d724215a266106e6832161eaed": 6,  # dollar token
        "70a38b0c90441e991346b7a0cd98c8528dd1c234": 6,  # dollar token
        "99cbf1ff4527675ed3301671105c9f7748fb8a04": 6,  # dollar token
    },
    "plasma": {
        "b8ce59fc3717ada4c02eadf9682a9e934f625ebb": 6,  # USDT0
        "5d3a1ff2b6bab83b63cd9ad0787074081a52ef34": 18,  # dollar token
        "6695c0f8706c5ace3bdf8995073179cca47926dc": 18,  # dollar token
    },
    SOLANA: {
        "c6fa7af3bedbad3a3d65f36aabc97431b1bbe4c2d2f6e0e47ca60203452f5d61": 6,  # USDC
        "ce010e60afedb22717bd63192f54145a3f965a33bb83134e31593825f29c16eb": 6,  # USDT
    },
}

# `crates/configs/src/autopilot/run_loop.rs:11`
MAX_WINNERS = 20

# A directed token pair: (sell_token, buy_token).
Pair = tuple[str, str]


def wrapped_native_token(network: str) -> str:
    try:
        return WRAPPED_NATIVE_TOKEN[network]
    except KeyError:
        raise KeyError(
            f"no verified wrapped native token for network {network!r}; "
            f"add it to WRAPPED_NATIVE_TOKEN after checking the autopilot config"
        ) from None


def usd_reference_tokens(network: str) -> dict[str, int]:
    try:
        return USD_REFERENCE_TOKENS[network]
    except KeyError:
        raise KeyError(
            f"no verified USD reference tokens for network {network!r}; add the "
            f"network's major stablecoins to USD_REFERENCE_TOKENS after verifying "
            f"their addresses and decimals"
        ) from None


def native_decimals(network: str) -> int:
    return NATIVE_DECIMALS.get(network, 18)


def native_price_scale(network: str) -> int:
    """What a network's recorded native prices are multiplied by to reach the 1e18
    denominator `price_in_eth` divides by: 1 on the EVM networks, 1e9 on Solana, whose
    prices are lamports per atom × 1e9 (`NATIVE_PRICE_DENOMINATOR`). Exact, since
    `amount * (price * 1e9) // 1e18 == amount * price // 1e9`, so every native amount
    stays in the chain's own unit."""
    return 10 ** (18 - native_decimals(network))


def usd_per_native(price: int, decimals: int, native: int = 18) -> Decimal:
    """USD per whole native token implied by a stablecoin's native price on the 1e18
    denominator (`native_price_scale`), for a native token of `native` decimals. Display
    only."""
    return Decimal(10) ** (18 + native - decimals) / Decimal(price)


def canonical_token(token: str, wrapped: str) -> str:
    """Map the native-token sentinel to the wrapped token whose pair it trades
    (`ChainTypes::canonical_token`, `primitives.rs:9`). The two sentinels differ in
    width, so neither can be mistaken for a token of the other chain."""
    return wrapped if token in (NATIVE_TOKEN, NATIVE_SOL) else token


_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_to_hex(text: str) -> str:
    """A Solana address as the DB's `encode(col, 'hex')` spells it. Raises `ValueError`
    on a character outside the alphabet."""
    number = 0
    for char in text:
        digit = _BASE58.find(char)
        if digit < 0:
            raise ValueError(f"{text!r} is not base58")
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(text) - len(text.lstrip("1"))  # each leading '1' is a zero byte
    return (b"\0" * leading + body).hex()


def hex_to_base58(address: str) -> str:
    """The canonical Solana rendering of a hex address: the catalogue's and explorers'."""
    raw = bytes.fromhex(address)
    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, digit = divmod(number, 58)
        digits = _BASE58[digit] + digits
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + digits


def price_in_eth(price: int, amount: int) -> int:
    """Convert a token amount to native (`primitives.rs:20`): `amount * price / 1e18`."""
    return amount * price // ONE_ETH


def ceil_div(numerator: int, denominator: int) -> int:
    """`U256Ext::checked_ceil_div`; operands are non-negative."""
    if denominator == 0:
        raise ZeroDivisionError("ceil_div by zero")
    return -(-numerator // denominator)

