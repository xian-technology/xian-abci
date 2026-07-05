from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

DEFAULT_CANDLE_SOURCE = "xian_pairs_v1"


@dataclass(frozen=True, slots=True)
class CandleSourceSpec:
    source: str
    contract: str
    event: str
    market_field: str
    amount0_in_field: str
    amount1_in_field: str
    amount0_out_field: str
    amount1_out_field: str
    market_id_kind: Literal["int", "str"] = "int"

    def with_contract(self, contract: str) -> "CandleSourceSpec":
        resolved = contract.strip()
        if not resolved:
            return self
        return replace(self, contract=resolved)

    def normalize_market_id(self, raw_market_id: str | int) -> tuple[str, int | None]:
        text = str(raw_market_id).strip()
        if not text:
            raise ValueError("market id is required")
        if self.market_id_kind == "int":
            market_int = int(text)
            if market_int <= 0:
                raise ValueError("market id must be positive")
            return str(market_int), market_int
        return text, None


CANDLE_SOURCE_SPECS: dict[str, CandleSourceSpec] = {
    DEFAULT_CANDLE_SOURCE: CandleSourceSpec(
        source=DEFAULT_CANDLE_SOURCE,
        contract="con_pairs",
        event="Swap",
        market_field="pair",
        amount0_in_field="amount0In",
        amount1_in_field="amount1In",
        amount0_out_field="amount0Out",
        amount1_out_field="amount1Out",
        market_id_kind="int",
    )
}


def get_candle_source_spec(source: str | None = None) -> CandleSourceSpec:
    source_id = (source or DEFAULT_CANDLE_SOURCE).strip() or DEFAULT_CANDLE_SOURCE
    try:
        return CANDLE_SOURCE_SPECS[source_id]
    except KeyError as exc:
        raise ValueError(f"unknown candle source: {source_id}") from exc
