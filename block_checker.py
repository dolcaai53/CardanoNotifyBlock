#!/usr/bin/env python3
"""
Cardano Block Checker
Tails the cardano-node JSON log, detects block forge events, then verifies
onchain via Koios API that the block was produced by the configured pool.
Sends Telegram notification only after onchain verification passes.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

KOIOS_API = "https://api.koios.rest/api/v1"
# How long to wait before first Koios check (block needs time to propagate)
VERIFY_INITIAL_WAIT = 20   # seconds
# How long to wait between retries
VERIFY_RETRY_INTERVAL = 20  # seconds
# How many times to retry before giving up (~5 minutes total)
VERIFY_MAX_ATTEMPTS = 15


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram notification sent")
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def find_value(obj, key: str):
    """Recursively find a key anywhere in a nested dict/list structure.
    Needed because cardano-node nests event fields differently across versions."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = find_value(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_value(item, key)
            if result is not None:
                return result
    return None


def fetch_block_by_hash(block_hash: str) -> Optional[dict]:
    """Query Koios for block info by block hash. Returns block dict or None."""
    try:
        resp = requests.post(
            f"{KOIOS_API}/block_info",
            json={"_block_hashes": [block_hash]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        log.warning("Koios block_info query failed: %s", e)
        return None


def fetch_block_by_slot(slot: int) -> Optional[dict]:
    """Fallback: query Koios for block at a given absolute slot. Returns block dict or None."""
    try:
        resp = requests.get(
            f"{KOIOS_API}/blocks",
            params={"abs_slot": f"eq.{slot}", "select": "hash,pool,block_height,epoch_no"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        log.warning("Koios blocks query failed: %s", e)
        return None


def fetch_pool_blocks_in_epoch(pool_id: str, epoch_no: int) -> Optional[int]:
    """Return number of blocks minted by this pool in the given epoch."""
    try:
        resp = requests.get(
            f"{KOIOS_API}/pool_blocks",
            params={"_pool_bech32": pool_id, "_epoch_no": epoch_no},
            timeout=10,
        )
        resp.raise_for_status()
        return len(resp.json())
    except Exception as e:
        log.warning("Koios pool_blocks query failed: %s", e)
        return None


def fetch_pool_info(pool_id: str) -> Optional[dict]:
    """Return pool info dict from Koios (block_count, active_stake, live_stake)."""
    try:
        resp = requests.post(
            f"{KOIOS_API}/pool_info",
            json={"_pool_bech32_ids": [pool_id]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        log.warning("Koios pool_info query failed: %s", e)
        return None


def fetch_epoch_active_stake(epoch_no: int) -> Optional[int]:
    """Return total network active stake (lovelace) for the given epoch."""
    try:
        resp = requests.get(
            f"{KOIOS_API}/epoch_info",
            params={"_epoch_no": epoch_no},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data[0].get("active_stake") if data else None
        return int(raw) if raw else None
    except Exception as e:
        log.warning("Koios epoch_info query failed: %s", e)
        return None


def build_notification(block: dict, block_hash: Optional[str], slot: int,
                        pool_id: str) -> str:
    """
    Compose the single rich notification message from block_info data
    plus supplementary Koios queries (pool_blocks, pool_info, epoch_info).
    Missing data is silently omitted so partial API failures degrade gracefully.
    """
    epoch_no = block.get("epoch_no")
    height = block.get("block_height", "?")
    tx_count = block.get("tx_count", "?")
    block_size_bytes = block.get("block_size")

    # Supplementary queries — all optional
    epoch_blocks = fetch_pool_blocks_in_epoch(pool_id, epoch_no) if epoch_no else None
    pool_info = fetch_pool_info(pool_id)
    lifetime_blocks = pool_info.get("block_count") if pool_info else None
    pool_active_stake_raw = pool_info.get("active_stake") if pool_info else None

    expected = None
    luck = None
    if pool_active_stake_raw and epoch_no:
        network_stake = fetch_epoch_active_stake(epoch_no)
        if network_stake and network_stake > 0:
            sigma = int(pool_active_stake_raw) / network_stake
            # 432 000 slots/epoch × 0.05 active-slot coefficient = 21 600 expected slots
            expected = sigma * 21600
            if epoch_blocks is not None and expected > 0:
                luck = epoch_blocks / expected * 100

    # Hash formatting
    if block_hash and len(block_hash) > 15:
        short_hash = block_hash[:10] + "..." + block_hash[-5:]
    else:
        short_hash = block_hash

    explorer_url = f"https://cexplorer.io/block/{block_hash}" if block_hash else None

    # Block size
    if block_size_bytes is not None:
        size_str = f"{block_size_bytes / 1024:.0f}kB"
    else:
        size_str = "?"

    lines = [
        "👨‍🌾 <b>New Block!</b>",
        "",
        f"0️⃣ Block No: {height}",
    ]

    if short_hash:
        if explorer_url:
            lines.append(f'#️⃣ Hash: <a href="{explorer_url}">{short_hash}</a>')
        else:
            lines.append(f"#️⃣ Hash: <code>{short_hash}</code>")

    lines += [
        f"🔡 Block Size: {size_str}",
        f"🔢 TX Count: {tx_count}",
    ]

    if epoch_blocks is not None or expected is not None:
        lines.append("")
        if epoch_blocks is not None:
            lines.append(f"⛏️ Blocks in Epoch: {epoch_blocks}")
        if expected is not None:
            lines.append(f"🗓 Estimated Blocks in Whole Epoch: {expected:.2f}")
        if luck is not None:
            luck_emoji = "🎉" if luck >= 100 else ""
            lines.append(f"🎁 Luck: {luck_emoji}{luck:.0f}% performance")

    if lifetime_blocks is not None:
        lines.append("")
        lines.append(f"🧱 Total Blocks: {lifetime_blocks}")

    return "\n".join(lines)


def verify_onchain(block_hash: Optional[str], slot: int, block_no: int,
                   pool_id: str, token: str, chat_id: str) -> None:
    """
    Background thread: poll Koios until the block appears onchain,
    verify the slot leader matches our pool_id, then send one combined
    Telegram notification with block details and pool stats.
    """
    log.info("Starting onchain verification — slot=%s hash=%s", slot, block_hash)
    time.sleep(VERIFY_INITIAL_WAIT)

    block = None
    for attempt in range(1, VERIFY_MAX_ATTEMPTS + 1):
        log.info("Koios verification attempt %d/%d for slot=%s", attempt, VERIFY_MAX_ATTEMPTS, slot)

        if block_hash:
            block = fetch_block_by_hash(block_hash)
        if block is None:
            # Hash not available or block not yet indexed — try slot lookup
            block = fetch_block_by_slot(slot)

        if block is not None:
            break
        time.sleep(VERIFY_RETRY_INTERVAL)

    if block is None:
        log.error("Could not verify block onchain after %d attempts — slot=%s", VERIFY_MAX_ATTEMPTS, slot)
        send_telegram(token, chat_id, (
            f"⚠️ <b>Onchain Verification Timeout</b>\n"
            f"Block at slot {slot} not found on Koios after 5 minutes.\n"
            f"Check block explorer manually."
        ))
        return

    onchain_pool = block.get("pool")

    # This should never happen if the node is configured correctly,
    # but we alert so the operator can investigate.
    if onchain_pool != pool_id:
        height = block.get("block_height", block_no)
        log.error("Pool mismatch! expected=%s onchain=%s slot=%s", pool_id, onchain_pool, slot)
        send_telegram(token, chat_id, (
            f"⚠️ <b>Pool Mismatch!</b>\n"
            f"Expected: <code>{pool_id}</code>\n"
            f"Onchain:  <code>{onchain_pool}</code>\n"
            f"Slot: {slot}  |  Block: {height}"
        ))
        return

    log.info("Block VERIFIED onchain — slot=%s pool=%s epoch=%s", slot, pool_id, block.get("epoch_no"))
    msg = build_notification(block, block_hash, slot, pool_id)
    send_telegram(token, chat_id, msg)


def process_line(line: str, token: str, chat_id: str, pool_id: str) -> None:
    """Parse one log line and react to block forge events."""
    if not line:
        return
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return  # node prints non-JSON lines during startup, skip them

    kind = find_value(entry, "kind")
    if not kind:
        return

    if kind == "TraceForgedBlock":
        slot = find_value(entry, "slot")
        block_no = find_value(entry, "blockNo")
        # Field name varies across node versions: "blockHash", "headerHash", or "block"
        # We intentionally skip the generic "hash" key — it appears in many log fields
        block_hash = (find_value(entry, "blockHash")
                      or find_value(entry, "headerHash")
                      or find_value(entry, "block"))

        log.info("Block FORGED — slot=%s blockNo=%s hash=%s", slot, block_no, block_hash)

        if slot is None:
            log.error("TraceForgedBlock event missing slot field — cannot verify onchain")
            return

        # Single notification is sent by the background verification thread after Koios confirms
        # Onchain verification runs in the background so log tailing is not blocked
        t = threading.Thread(
            target=verify_onchain,
            args=(block_hash, slot, block_no, pool_id, token, chat_id),
            daemon=True,
        )
        t.start()


def tail_log(log_path: str, config: dict, from_start: bool = False) -> None:
    """Follow the node log file, reopening it automatically on rotation.
    from_start=True reads existing content first — useful for testing with a static file."""
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]
    pool_id = config["pool_id"]

    log.info("Watching log: %s  pool: %s  from_start=%s", log_path, pool_id, from_start)

    while True:
        try:
            with open(log_path) as f:
                if not from_start:
                    f.seek(0, 2)  # jump to end — we only care about new events
                current_inode = os.stat(log_path).st_ino
                while True:
                    line = f.readline()
                    if line:
                        process_line(line.strip(), token, chat_id, pool_id)
                    else:
                        time.sleep(0.5)
                        # Detect log rotation (file replaced by logrotate)
                        try:
                            if os.stat(log_path).st_ino != current_inode:
                                log.info("Log file rotated — reopening")
                                break
                        except FileNotFoundError:
                            break
        except FileNotFoundError:
            log.warning("Log file not found: %s — retrying in 5s", log_path)
            time.sleep(5)
        except Exception as e:
            log.error("Unexpected error: %s — retrying in 5s", e)
            time.sleep(5)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cardano block forge notifier")
    parser.add_argument("--from-start", action="store_true",
                        help="Read log from the beginning (for testing with a static file)")
    args = parser.parse_args()

    config_path = os.environ.get("BLOCK_CHECKER_CONFIG", "config.json")
    if not Path(config_path).exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = load_config(config_path)
    tail_log(config["node_log_path"], config, from_start=args.from_start)


if __name__ == "__main__":
    main()
