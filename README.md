# Cardano Block Checker

Monitors cardano-node logs and verifies minted blocks onchain. When a block is
confirmed on-chain, one Telegram notification is sent with full details:

```
👨‍🌾 New Block!

0️⃣ Block No: 13322799
#️⃣ Hash: 31b06723e7...76e  (linked to cexplorer.io)
🔡 Block Size: 4kB
🔢 TX Count: 3

⛏️ Blocks in Epoch: 2
🗓 Estimated Blocks in Whole Epoch: 0.81
🎁 Luck: 🎉247% performance

🧱 Total Blocks: 529
```

If the pool ID does not match, or the block is not found within 5 minutes,
a warning notification is sent instead.

## How it works

```
cardano-node log
      │
      │  TraceForgedBlock event
      ▼
block_checker.py  (background thread)
      │
      │  polls Koios: block_info, pool_blocks, pool_info, epoch_info
      ▼
https://api.koios.rest
      │
      │  block.pool == config.pool_id ?
      ▼
Telegram: one rich notification  or  "⚠️ Pool Mismatch / Timeout"
```

Koios is a free public Cardano blockchain API — no account or API key required.
Verification starts 20 seconds after the forge event, retries every 20 seconds
for up to 5 minutes.

Luck is calculated as `(blocks_minted_in_epoch / expected_blocks) × 100` where
`expected = (pool_active_stake / network_active_stake) × 21 600`.

## Requirements

- Python 3.8+
- cardano-node with JSON logging enabled and `TraceForge: true` in node config
- Outbound HTTPS access to `api.koios.rest`
- A Telegram bot (created via [@BotFather](https://t.me/BotFather))

## Setup

```bash
# 1. Copy files to your server
cd /opt/cardano/blockChecker

# 2. Create virtualenv and install dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Create config from example
cp config.example.json config.json
nano config.json   # fill in your values

# 4. Test run against the live log (Ctrl+C to stop)
BLOCK_CHECKER_CONFIG=/opt/cardano/blockChecker/config.json venv/bin/python block_checker.py
```

## Testing with a saved log file

By default the script jumps to the **end** of the log file and only reacts to
new events — this is correct for production but means it skips content that is
already in the file.

To replay a saved or historic log fragment, use `--from-start`:

```bash
# Point node_log_path in config.json to your test file, then:
BLOCK_CHECKER_CONFIG=config.json venv/bin/python block_checker.py --from-start
```

The script will process every line from the beginning of the file, send
Telegram messages for any `TraceForgedBlock` events it finds, and then keep
watching for new lines (same as normal operation).

**Block hash key variations across node versions**

Different cardano-node versions write the block hash under different JSON keys:
`blockHash`, `headerHash`, or `block`. All three are detected automatically.

## Configuration

`config.json` (do not commit — it is in .gitignore):

| Key | Description |
|-----|-------------|
| `node_log_path` | Full path to cardano-node JSON log file |
| `pool_id` | Your pool bech32 ID (`pool1...`) — used for onchain verification |
| `telegram_bot_token` | Bot token from @BotFather |
| `telegram_chat_id` | Chat / channel / group ID where notifications are sent |

### Getting your Telegram chat ID

Send any message to your bot, then open in a browser:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```
The `chat.id` value is what you need (negative number for groups/channels).

### Cardano-node JSON logging

Your node config file must write logs in JSON format and have forge tracing on:

```json
"defaultScribes": [["FileSK", "/opt/cardano/cnode/logs/node.json"]],
"setupScribes": [{
    "scKind": "FileSK",
    "scName": "/opt/cardano/cnode/logs/node.json",
    "scFormat": "ScJson"
}],
"TraceForge": true
```

For cardano-node 8.x with the new tracing system, check that `Forge` namespace
is set to at least `Notice` level in your tracing configuration.

## Run as systemd service

```bash
# Copy and edit the service file
sudo cp blockchecker.service /etc/systemd/system/
sudo nano /etc/systemd/system/blockchecker.service
# Set: User, BLOCK_CHECKER_CONFIG path, ExecStart path

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable blockchecker
sudo systemctl start blockchecker

# Check status and live logs
sudo systemctl status blockchecker
sudo journalctl -u blockchecker -f
```

## Troubleshooting

**No notifications at all**
- Check that the node log file path in `config.json` is correct
- Confirm the node is writing JSON logs: `tail -f /path/to/node.json | head -5`
- Make sure the user running block_checker has read access to the log file
- If testing with a saved log file, you must use `--from-start` — without it
  the script skips to the end of the file and sees nothing

**"Block Forged" arrives but no "Block Verified"**
- Koios may be temporarily slow — wait for the 5-minute timeout message
- Check internet connectivity: `curl https://api.koios.rest/api/v1/tip`
- Verify the `pool_id` in config matches exactly the bech32 ID on-chain

**"Pool Mismatch" warning**
- Your `pool_id` in config does not match what Koios reports as slot leader
- Double-check your pool bech32 ID on a block explorer (pool.pm, cexplorer.io)

**Service fails to start**
- Check paths in the service file match where you installed the files
- Check the config file exists and is valid JSON: `python3 -m json.tool config.json`
