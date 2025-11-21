from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Callable, Optional

from core import chain as h
import models
import state as _st

class Sequencer:
    def __init__(self, global_state: _st.State) -> None:
        self._state = global_state
        self._logs_by_block: Dict[int, List[dict]] = defaultdict(list)
        self._ready_blocks: set[int] = set()
        self._next_block: int | None = None
        self._on_block: Optional[Callable[[int], None]] = None
    
    def set_on_block(self, fn: Callable[[int], None]) -> None:
        self._on_block = fn

    def add_log(self, raw_log: dict) -> None:
        blk = int(raw_log["blockNumber"], 16) if isinstance(raw_log["blockNumber"], str) else raw_log["blockNumber"]
        self._logs_by_block[blk].append(raw_log)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def note_block(self, blk: int) -> None:
        self._ready_blocks.add(blk)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def _drain(self) -> None:
        while self._next_block is not None and self._next_block in self._ready_blocks:
            logs = self._logs_by_block.pop(self._next_block, [])
            self._ready_blocks.discard(self._next_block)
            self._process_block(self._next_block, logs)
            if self._on_block:
                try:
                    self._on_block(self._next_block)
                except Exception as e:
                    print(f"[SQ] Persist Error: {e!r}")
            self._next_block += 1

    def _process_block(self, blk: int, logs: List[dict]):
        counts = {
            "MC": 0, 
            "TR": 0, 
            
            "VD": 0, 
            "VDP": 0, 
            "VWD": 0,
            "VLOCK": 0, 
            "VUNLOCK": 0, 
            "VCLOSE": 0, 
            
            "SYNC": 0,
            
            "LT": 0, 
            "TC": 0, 
            "MG": 0,
            
            "NFC": 0, 
            "NFB": 0, 
            "NFS": 0, 
            "NFT": 0,
            
            "TF": 0,
        }
        seen = set()

        for log in logs:
            blk_ts = int(log.get("blockTimestamp"), 16)
            txh = log.get("transactionHash")
            li = log.get("logIndex")
            lii = int(li, 16) if isinstance(li, str) else int(li or 0)
            uid = (txh, lii)
            
            if uid in seen:
                continue
            seen.add(uid)

            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if not tag:
                continue
            if tag in counts:
                counts[tag] += 1
            
            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])
            
            print("parsed", tag, parsed)

            if tag == "MC":
                ev = self._to_market_created(parsed)
                self._state.apply_market_created(ev, blk_ts, log["address"].lower())
                
            elif tag == "TR":
                ev = self._to_trade(parsed, blk)
                self._state.apply_trade(ev, blk, blk_ts, log["address"].lower())
                
            elif tag == "VD":
                ev = self._to_vault_deployed(parsed, blk_ts)
                self._state.apply_vault_deployed(blk_ts, ev, log["address"].lower())

            elif tag == "VDP":
                vault_addr = parsed.get("vault", "").lower()
                dep = self._to_vault_deposit(parsed, blk_ts, txh)
                self._state.apply_vault_deposit(vault_addr, dep)

            elif tag == "VWD":
                vault_addr = parsed.get("vault", "").lower()
                wdr = self._to_vault_withdraw(parsed, blk_ts, txh)
                self._state.apply_vault_withdraw(vault_addr, wdr)

            elif tag == "VLOCK":
                vaddr = parsed.get("vault", "").lower()
                if vaddr in self._state.vaults:
                    self._state.vaults[vaddr].locked = True

            elif tag == "VUNLOCK":
                vaddr = parsed.get("vault", "").lower()
                if vaddr in self._state.vaults:
                    self._state.vaults[vaddr].locked = False

            elif tag == "VCLOSE":
                vaddr = parsed.get("vault", "").lower()
                if vaddr in self._state.vaults:
                    self._state.vaults[vaddr].closed = True
                    
            elif tag == "SYNC":
                self._state.apply_amm_sync(blk, parsed, blk_ts)

            elif tag in ("LT", "NFB", "NFS"):
                self._state.apply_launchpad_trade(parsed, blk, blk_ts, log["address"].lower())

            elif tag in ("TC", "NFC"):
                self._state.apply_token_created(blk, parsed, blk_ts, log["address"].lower())

            elif tag in ("MG", "NFT"):
                self._state.apply_migrated(blk, blk_ts, parsed, log["address"].lower())
                
            elif tag == "TF":
                if parsed is not None:
                    self._state.apply_token_transfer(parsed, blk, blk_ts, log["address"].lower())

        print(
            f"[SQ] {blk}: MC {counts['MC']} TR {counts['TR']} "
            f"VD {counts['VD']} VDP {counts['VDP']} VWD {counts['VWD']} "
            f"VLOCK {counts['VLOCK']} VUNLOCK {counts['VUNLOCK']} VCLOSE {counts['VCLOSE']} "
            f"SYNC {counts['SYNC']} TC {counts['TC']} LT {counts['LT']} MG {counts['MG']} "
            f"NFC {counts['NFC']} NFB {counts['NFB']} NFS {counts['NFS']} NFT {counts['NFT']} "
            f"TF {counts['TF']}"
        )
        
    @staticmethod
    def _to_market_created(d: dict) -> models.MarketInfo:
        return models.MarketInfo(
            isCanonical=bool(d.get("isCanonical", False)),
            quoteAsset=d.get("quoteAsset", "").lower(),
            baseAsset=d.get("baseAsset", "").lower(),
            market=d.get("market", "").lower(),
            quoteAddress=d.get("quoteAddress", "").lower(),
            quoteDecimals=int(d.get("quoteDecimals", 0)),
            quoteTicker=d.get("quoteTicker", ""),
            quoteName=d.get("quoteName", ""),
            baseAddress=d.get("baseAddress", "").lower(),
            baseDecimals=int(d.get("baseDecimals", 0)),
            baseTicker=d.get("baseTicker", ""),
            baseName=d.get("baseName", ""),
            marketId=int(d.get("marketId", 0)),
            marketType=int(d.get("marketType", 0)),
            scaleFactor=int(d.get("scaleFactor", 0)),
            tickSize=int(d.get("tickSize", 0)),
            maxPrice=int(d.get("maxPrice", 0)),
            minSize=int(d.get("minSize", 0)),
            takerFee=int(d.get("takerFee", 0)),
            makerRebate=int(d.get("makerRebate", 0)),
        )
        
    @staticmethod
    def _to_trade(d: dict, blk: int) -> models.Trade:
        return models.Trade(
            block_number=blk,
            market=d.get("market", "").lower(),
            user=d.get("user", "").lower(),
            is_buy=bool(d.get("is_buy", False)),
            amount_in=int(d.get("amount_in", 0)),
            amount_out=int(d.get("amount_out", 0)),
            start_price=int(d.get("start_price", 0)),
            end_price=int(d.get("end_price", 0)),
        )

    @staticmethod
    def _to_vault_deployed(d: dict, ts: int) -> models.Vault:
        vaddr = d.get("vault", "").lower()
        quote = d.get("quoteAsset", "").lower()
        base = d.get("baseAsset", "").lower()
        owner = d.get("owner", "").lower()

        metadata = d.get("metadata", {}) or {}
        name = metadata.get("name", "")
        description = metadata.get("description", "")
        social1 = metadata.get("social1", "")
        social2 = metadata.get("social2", "")
        social3 = metadata.get("social3", "")

        return models.Vault(
            vault=vaddr,
            quote=quote,
            base=base,
            market="",
            owner=owner,
            name=name,
            description=description,
            social1=social1,
            social2=social2,
            social3=social3,
            locked=bool(d.get("locked", False)),
            closed=bool(d.get("closed", False)),
            maxShares=int(d.get("maxShares", 0)),
            circulatingShares=0,
            quoteDecimals=int(d.get("quoteDecimals", 0)),
            baseDecimals=int(d.get("baseDecimals", 0)),
            timestamp=ts
        )

    @staticmethod
    def _to_vault_deposit(d: dict, ts: int, txh: str | None) -> models.VaultDeposit:
        return models.VaultDeposit(
            user=d.get("sender", d.get("user", "")).lower(),
            timestamp=int(ts),
            quoteAmount=int(d.get("quoteAmount", d.get("amountQuote", 0))),
            baseAmount=int(d.get("baseAmount", d.get("amountBase", 0))),
            shares=int(d.get("shares", 0)),
            hash=(txh or "").lower(),
        )

    @staticmethod
    def _to_vault_withdraw(d: dict, ts: int, txh: str | None) -> models.VaultWithdraw:
        return models.VaultWithdraw(
            user=d.get("sender", d.get("user", "")).lower(),
            timestamp=int(ts),
            quoteAmount=int(d.get("quoteAmount", d.get("amountQuote", 0))),
            baseAmount=int(d.get("baseAmount", d.get("amountBase", 0))),
            shares=int(d.get("shares", 0)),
            hash=(txh or "").lower(),
        )
        
SEQUENCER = Sequencer(_st.State())