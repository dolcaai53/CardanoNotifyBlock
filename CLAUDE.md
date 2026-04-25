role: Senior Developer and Linux specialist
Keep code clear, logically simple and add comments where the WHY is not obvious.
Do not make changes just because you want to — only change what is asked.

## Purpose

Notify a Cardano stake pool operator via Telegram when their block producer mints a block,
with onchain verification that the block was produced by their specific pool.

## Architecture

Single Python script (`block_checker.py`) running as a systemd service.

**Detection:** tails the cardano-node JSON log file watching for `TraceForgedBlock` events.
These events are emitted only by this node when it successfully forges a block.

**Verification:** when a forge event is detected, a background thread queries the
Koios public API (`api.koios.rest`) to confirm the block appears on-chain with the
expected pool ID as slot leader. Retries every 20 seconds for up to 5 minutes.

**Notification:** one Telegram message per block, sent after Koios confirms the block.
Contains: block number, hash (linked to cexplorer.io), block size, TX count,
blocks minted in current epoch, estimated blocks for the epoch, luck %, and lifetime block count.
Or a warning if pool mismatches or verification times out.

Luck is computed as: `(blocks_in_epoch / expected) × 100`
where `expected = (pool_active_stake / network_active_stake) × 21 600`
(432 000 slots/epoch × 0.05 active-slot coefficient).

## Key implementation decisions

- Background thread for Koios polling so log tailing is never blocked
- `find_value()` recursive key search handles different cardano-node log formats across versions
- Block lookup by hash first (more precise), falls back to slot number if hash is not in log event
- Inode check on every 0.5s sleep detects log rotation without a fixed timer
- Daemon thread — no cleanup needed if the main process exits

## Deferred: approach B (cncli)

`cncli` (github.com/cardano-community/cncli) was considered as an alternative.
It validates blocks using the pool's VRF key — stronger cryptographic proof.
Revisit if stronger verification is needed. See project memory for details.

## Config keys

| Key | Type | Description |
|-----|------|-------------|
| `node_log_path` | string | Absolute path to cardano-node JSON log |
| `pool_id` | string | Pool bech32 ID (`pool1...`) — verified against Koios response |
| `telegram_bot_token` | string | Telegram bot token from @BotFather |
| `telegram_chat_id` | string | Telegram chat/channel/group ID |

## Files

| File | Purpose |
|------|---------|
| `block_checker.py` | Main script |
| `blockchecker.service` | systemd unit |
| `config.example.json` | Config template (copy to `config.json`) |
| `requirements.txt` | Python deps (`requests` only) |
| `README.md` | Setup and troubleshooting guide |
