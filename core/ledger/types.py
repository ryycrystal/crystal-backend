from dataclasses import dataclass, field
from decimal import Decimal

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
USDC = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
AUSD = "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
NATIVE = "native"
QUOTE_ASSETS = frozenset({NATIVE, WMON, LVMON, USDC, AUSD})
MON_FAMILY = frozenset({NATIVE, WMON, LVMON})
USD_FAMILY = frozenset({USDC, AUSD})
USD_DECIMALS = 6
ZERO = "0x0000000000000000000000000000000000000000"
ENTRYPOINT_V06 = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
ENTRYPOINT_V07 = "0x0000000071727de22e5e9d8baf0edac6f37da032"
USEROP_EVENT_TOPIC = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST_WEI = 10**15
WEI = Decimal(10) ** 18

KIND_BUY = "buy"
KIND_SELL = "sell"
KIND_TRANSFER_IN = "transfer_in"
KIND_TRANSFER_OUT = "transfer_out"
KIND_MINT = "mint"
KIND_BURN = "burn"
KIND_AIRDROP = "airdrop"
KIND_SWAP_LEG = "swap_leg"
KIND_LP_ADD = "lp_add"
KIND_LP_REMOVE = "lp_remove"
KIND_VAULT_DEPOSIT = "vault_deposit"
KIND_VAULT_WITHDRAW = "vault_withdraw"
KIND_CUSTODY_DEPOSIT = "custody_deposit"
KIND_CUSTODY_WITHDRAW = "custody_withdraw"
FLOW_KINDS = frozenset(
    {
        KIND_BUY,
        KIND_SELL,
        KIND_TRANSFER_IN,
        KIND_TRANSFER_OUT,
        KIND_MINT,
        KIND_BURN,
        KIND_AIRDROP,
        KIND_SWAP_LEG,
        KIND_LP_ADD,
        KIND_LP_REMOVE,
        KIND_VAULT_DEPOSIT,
        KIND_VAULT_WITHDRAW,
        KIND_CUSTODY_DEPOSIT,
        KIND_CUSTODY_WITHDRAW,
    }
)

BASIS_OBSERVED = "observed"
BASIS_ESTIMATED = "estimated"
BASIS_UNRESOLVED = "unresolved"
BASIS_STATES = frozenset({BASIS_OBSERVED, BASIS_ESTIMATED, BASIS_UNRESOLVED})

SOURCE_TRANSFER_NET = "transfer_net"
SOURCE_VENUE_EVENT = "venue_event"
SOURCE_TRACE = "trace"
SOURCE_RECONCILE = "reconcile"
FLOW_SOURCES = frozenset({SOURCE_TRANSFER_NET, SOURCE_VENUE_EVENT, SOURCE_TRACE, SOURCE_RECONCILE})

KIND_EOA = "eoa"
KIND_EOA_7702 = "eoa_7702"
KIND_WALLET_4337 = "wallet_4337"
KIND_VENUE_POOL = "venue_pool"
KIND_VENUE_ROUTER = "venue_router"
KIND_VENUE_CURVE = "venue_curve"
KIND_VENUE_CUSTODY = "venue_custody"
KIND_TOKEN = "token"
KIND_CONTRACT_UNKNOWN = "contract_unknown"
KIND_ZERO = "zero"
ADDRESS_KINDS = frozenset(
    {
        KIND_EOA,
        KIND_EOA_7702,
        KIND_WALLET_4337,
        KIND_VENUE_POOL,
        KIND_VENUE_ROUTER,
        KIND_VENUE_CURVE,
        KIND_VENUE_CUSTODY,
        KIND_TOKEN,
        KIND_CONTRACT_UNKNOWN,
        KIND_ZERO,
    }
)
WALLET_KINDS = frozenset({KIND_EOA, KIND_EOA_7702, KIND_WALLET_4337, KIND_CONTRACT_UNKNOWN})
VENUE_KINDS = frozenset({KIND_VENUE_POOL, KIND_VENUE_ROUTER, KIND_VENUE_CURVE, KIND_VENUE_CUSTODY})


@dataclass(frozen=True)
class Flow:
    block_number: int
    tx_index: int
    log_index: int
    sub_index: int
    txhash: str
    timestamp: int
    wallet: str
    token: str
    token_delta: int
    quote_asset: str | None
    quote_delta: int | None
    mon_value: Decimal
    usd_value: Decimal
    kind: str
    venue: str | None
    counterparty: str | None
    origin: str | None
    source: str
    basis_state: str
    price_native: Decimal | None
    basis_delta: int = 0
    realized_delta: int = 0


@dataclass(frozen=True)
class PositionRow:
    wallet: str
    token: str
    balance_token: int = 0
    custody_balance: int = 0
    token_bought: int = 0
    token_sold: int = 0
    native_spent: int = 0
    native_received: int = 0
    cost_basis_native: int = 0
    realized_pnl_native: int = 0
    basis_estimated_native: int = 0
    realized_estimated_native: int = 0
    unresolved_tokens: int = 0
    unresolved_proceeds_native: int = 0
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    first_flow_ts: int | None = None
    last_flow_ts: int | None = None
    last_flow_block: int | None = None
    flow_count: int = 0


@dataclass(frozen=True)
class TxMeta:
    txhash: str
    block_number: int | None
    tx_index: int | None
    from_addr: str | None
    to_addr: str | None
    value: int
    selector: str | None


@dataclass(frozen=True)
class TraceResult:
    available: bool
    transfers: list[tuple[str, str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class TokenReg:
    token: str
    source: str
    registered_block: int | None = None
    quote_token: str | None = None
    decimals: int = 18
    active: bool = True


@dataclass(frozen=True)
class VenueEvent:
    tag: str
    log_index: int
    parsed: dict
    address: str


@dataclass(frozen=True)
class TransferLeg:
    log_index: int
    token: str
    from_addr: str
    to_addr: str
    amount: int


@dataclass(frozen=True)
class TxBundle:
    txhash: str
    block_number: int
    tx_index: int
    timestamp: int
    transfers: list[TransferLeg] = field(default_factory=list)
    venue_events: list[VenueEvent] = field(default_factory=list)
    meta: TxMeta | None = None
    trace: TraceResult | None = None
    userop_sender: str | None = None


@dataclass(frozen=True)
class Rates:
    mon_usd: Decimal = Decimal(0)
    lvmon_rate: Decimal = Decimal(1)
    usdc_per_mon: Decimal = Decimal(0)
