# Cloudflare Dynamic DNS Updater

A small Python script that keeps your Cloudflare DNS records in sync with your current external IP address.
It supports IPv4 and IPv6, multiple records, dry-run mode, logging, and automatic record creation.

## Features

- Detects external IPv4 and/or IPv6
- Updates A and AAAA records in Cloudflare
- Can create missing DNS records automatically
- Supports multiple records per IP type
- Dry-run mode (no real changes)
- Daily log file with automatic cleanup
- Remembers last known IP and created records

## Requirements
- Python 3.9+  
- Packages:
  - `requests`
  - `python-dotenv`

### Install dependencies:

```
pip install -r requirements.txt
```

## Setup

1. Clone or copy the script
2. Create a .env file in the project root:

```
CF_API_TOKEN=your_token_here
CF_ZONE_ID=your_zone_id_here

CF_DEFAULT_PROXIED=1

LOOKUP_RETRIES=3
LOOKUP_RETRY_SECONDS=3

RECORDS_A=example.com,home.example.com
RECORDS_AAAA=example.com

CHECK_V4=1
CHECK_V6=1

ip_v4_check_urls=https://api.ipify.org?format=json,https://ifconfig.co/json,https://icanhazip.com
ip_v6_check_urls=https://api64.ipify.org?format=json,https://ifconfig.co/ip,https://icanhazip.com

LOG_FILENAME=cf_ddns
LOG_KEEP=10
LOG_DEBUG=0
STATE_FILENAME=cf_ddns_state

DRY_RUN=1
VARIABLES=0
```

### Get Cloudflare credentials:

Create an API token at https://dash.cloudflare.com/profile/api-tokens
Find your Zone ID in Cloudflare dashboard

## Configuration Reference

### Cloudflare
|Variable|Description|
| ------ | ------ |
|CF_API_TOKEN|Cloudflare API token|
|CF_ZONE_ID|Cloudflare zone ID|
|CF_DEFAULT_PROXIED|1 = new records are proxied by default|

### DNS Records
|Variable|Description|
| ------ | ------ |
|RECORDS_A|Comma-separated domain and subdomains for IPv4|
|RECORDS_AAAA|Comma-separated domain and subdomains for IPv6|
|CHECK_V4|1 = check IPv4|
|CHECK_V6|1 = check IPv6|

### IP Detection
|Variable|Description|
| ------ | ------ |
|LOOKUP_RETRIES|How many times to retry the IP check|
|LOOKUP_RETRY_SECONDS|Delay between retries|
|ip_v4_check_urls|Custom IPv4 check URLs|
|ip_v6_check_urls|Custom IPv6 check URLs|

### Logging & State
|Variable|Description|
| ------ | ------ |
|LOG_FILENAME|Base name for daily log file, no lof if empty|
|LOG_KEEP|How many log files to keep|
|LOG_DEBUG|1 = verbose, 0 = quiet|
|STATE_FILENAME|Name of state file|

### Debug
|Variable|Description|
| ------ | ------ |
|DRY_RUN|1 = no real changes|
|VARIABLES|1 = print resolved config|

## How It Works

1. Detects external IP
2. Loads state file
3. Checks which DNS records already exist
4. Plans only necessary actions:
  - Update changed records
  - Create missing records
5. If DRY_RUN=1 → only logs what would happen.
If real run → updates Cloudflare and saves state

## Running

python src/main.py

For first test:
DRY_RUN=1
Then switch to:
DRY_RUN=0

## Typical Use Case

Run every 10 minutes via cron / task scheduler

Quiet when nothing changes

Only logs when:
 - IP changes
 - New record is added
 - Error happens

## Safety Notes

Always test with DRY_RUN=1 first

Make sure your API token has only DNS edit permissions