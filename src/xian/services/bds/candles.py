from __future__ import annotations

from dataclasses import dataclass, replace

DEFAULT_CANDLE_SOURCE = "xian_pairs_v1"


@dataclass(frozen=True, slots=True)
class CandleSourceSpec:
    source: str
    contract: str

    def with_contract(self, contract: str) -> "CandleSourceSpec":
        resolved = contract.strip()
        if not resolved:
            return self
        return replace(self, contract=resolved)

    def normalize_market_id(self, raw_market_id: str | int) -> tuple[str, int]:
        text = str(raw_market_id).strip()
        if not text:
            raise ValueError("market id is required")
        market_int = int(text)
        if market_int <= 0:
            raise ValueError("market id must be positive")
        return str(market_int), market_int


CANDLE_SOURCE_SPECS: dict[str, CandleSourceSpec] = {
    DEFAULT_CANDLE_SOURCE: CandleSourceSpec(
        source=DEFAULT_CANDLE_SOURCE,
        contract="con_pairs",
    )
}


def get_candle_source_spec(source: str | None = None) -> CandleSourceSpec:
    source_id = (source or DEFAULT_CANDLE_SOURCE).strip() or DEFAULT_CANDLE_SOURCE
    try:
        return CANDLE_SOURCE_SPECS[source_id]
    except KeyError as exc:
        raise ValueError(f"unknown candle source: {source_id}") from exc
