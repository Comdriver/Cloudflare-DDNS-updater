import json
import os
import sys
import time
import logging
from typing import Optional, Dict, Any, List, Tuple

import requests
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

# Cloudflare API
CF_API_BASE = "https://api.cloudflare.com/client/v4"
ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "logs"


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)

def parse_int(value: Optional[str], default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default

def parse_bool(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}

def parse_csv_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]

def setup_logging(log_filename: Optional[str], keep: int, debug: bool) -> logging.Logger:
    logger = logging.getLogger("cf_ddns")
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # always log to terminal
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # optionally log to file
    if log_filename:
        LOG_FILE_PATH = log_filename
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d")
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            LOG_FILE_PATH = LOG_DIR / f"{log_filename}_{timestamp}.log"
            logger.debug("Logging to %s", LOG_FILE_PATH)


            fh = logging.FileHandler(LOG_FILE_PATH, mode="a", encoding="utf-8")
            fh.setFormatter(formatter)
            logger.addHandler(fh)

        except Exception as e:
            logger.error(
                "Failed to set up file logging (%s). Using console only.",
                LOG_FILE_PATH,
            )

    cleanup_old_logs(LOG_DIR, log_filename, keep, logger)
    return logger

def cleanup_old_logs(log_dir: Path, log_filename: Optional[str], keep: int, logger) -> None:
    if keep <= 0 or not log_dir.exists():
        return

    base = Path(log_filename).stem
    files = [
        f for f in log_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() == ".log"
        and f.name.startswith(base + "_")
    ]

    if len(files) <= keep:
        return

    # Sort by modification time, newest first
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    to_delete = files[keep:]

    for f in to_delete:
        try:
            f.unlink()
            logger.debug("Deleted old log: %s", f.name)
        except Exception as e:
            logger.warning("Failed to delete old log %s: %s", f.name, e)

def mask_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "*" * (len(token) - 8) + token[-4:]

def get_external_ip(
    logger: logging.Logger,
    urls: List[str],
    want: str,  # "v4" or "v6"
    retries: int,
    retry_seconds: int,
) -> Optional[str]:
    """
    Tries multiple URLs, multiple rounds.
    Returns the first valid IP found (based on want=v4/v6).
    """
    if want not in ("v4", "v6"):
        raise ValueError("want must be 'v4' or 'v6'")

    for attempt in range(1, max(1, retries) + 1):
        logger.debug("IP lookup (%s) attempt %d/%d", want, attempt, retries)

        for url in urls:
            try:
                logger.debug("Checking %s", url)
                r = requests.get(url, timeout=8)
                r.raise_for_status()

                ct = (r.headers.get("Content-Type") or "").lower()
                ip: Optional[str] = None

                if "json" in ct:
                    data = r.json()
                    ip = data.get("ip") or data.get("ip_addr") or data.get("address")
                    if isinstance(ip, str):
                        ip = ip.strip()
                    else:
                        ip = None
                else:
                    ip = r.text.strip()

                if not ip:
                    logger.warning("No IP found in response from %s", url)
                    continue

                # Very light validation
                if want == "v4":
                    if "." in ip and ":" not in ip:
                        logger.debug("Detected external IPv4: %s (via %s)", ip, url)
                        return ip
                else:
                    if ":" in ip:
                        logger.debug("Detected external IPv6: %s (via %s)", ip, url)
                        return ip

                logger.debug("Response from %s didn't look like %s: %r", url, want, ip)

            except Exception as e:
                logger.warning("Failed checking %s (%s)", url, e)

        if attempt < retries:
            logger.debug("Retrying in %d seconds...", retry_seconds)
            time.sleep(max(0, retry_seconds))

    logger.error("Failed to detect external %s after %d attempts", want, retries)
    return None

def load_state(logger: logging.Logger, state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        logger.warning("State file does not exist yet: %s (first run)", state_path)
        return {}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("State file is not a JSON object. Ignoring: %s", state_path)
            return {}
        return data
    except Exception as e:
        logger.warning("Failed to read/parse state file %s. Ignoring it. Error: %s", state_path, e)
        return {}


def save_state(logger: logging.Logger, state_path: Path, state: Dict[str, Any]) -> None:
    # Atomic write to avoid half-written files if the script is interrupted
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        tmp.replace(state_path)
        logger.debug("State saved: %s", state_path)
    except Exception as e:
        logger.error("Failed to save state file %s: %s", state_path, e)


def decide_updates(
    logger: logging.Logger,
    current_ipv4: Optional[str],
    current_ipv6: Optional[str],
    state: Dict[str, Any],
) -> Tuple[bool, bool]:
    """
    Returns: (should_update_ipv4, should_update_ipv6)
    """
    last_ipv4 = state.get("last_ipv4")
    last_ipv6 = state.get("last_ipv6")

    update_v4 = False
    update_v6 = False

    if current_ipv4:
        if last_ipv4 != current_ipv4:
            logger.info("IPv4 change detected: %s -> %s", last_ipv4, current_ipv4)
            update_v4 = True
        else:
            logger.debug("IPv4 unchanged vs state: %s", current_ipv4)
    else:
        logger.debug("No current IPv4 detected (skipping IPv4 decision).")

    if current_ipv6:
        if last_ipv6 != current_ipv6:
            logger.info("IPv6 change detected: %s -> %s", last_ipv6, current_ipv6)
            update_v6 = True
        else:
            logger.debug("IPv6 unchanged vs state: %s", current_ipv6)
    else:
        logger.debug("No current IPv6 detected (skipping IPv6 decision).")

    return update_v4, update_v6

def apply_new_state(
    state: Dict[str, Any],
    current_ipv4: Optional[str],
    current_ipv6: Optional[str],
    records_a: List[str],
    records_aaaa: List[str],
) -> Dict[str, Any]:
    # Produces the new state dict after a successful DNS update.
    new_state = dict(state)

    if current_ipv4:
        new_state["last_ipv4"] = current_ipv4
    if current_ipv6:
        new_state["last_ipv6"] = current_ipv6
    
    new_state["records"] = {
        "A": sorted(set(records_a)),
        "AAAA": sorted(set(records_aaaa)),
    }

    new_state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return new_state

def cf_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "User-Agent": "cf-ddns/step4",
    }

def cf_list_records(
    logger: logging.Logger,
    api_token: str,
    zone_id: str,
    record_type: str,
    name: str,
) -> List[Dict[str, Any]]:
    # Returns a list of DNS record objects matching type+name (usually 0 or 1, but can be >1).

    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    params = {"type": record_type, "name": name}

    r = requests.get(url, headers=cf_headers(api_token), params=params, timeout=20)
    data = r.json()

    if not data.get("success"):
        logger.error("Cloudflare list_records failed for %s %s: %s", record_type, name, data)
        raise RuntimeError(f"Cloudflare list_records failed for {record_type} {name}")

    return data.get("result", [])

def cf_update_record(
    logger: logging.Logger,
    api_token: str,
    zone_id: str,
    record_id: str,
    record_type: str,
    name: str,
    content: str,
    ttl: int,
    proxied: Optional[bool],
) -> None:

    # Updates one DNS record by ID.
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    payload: Dict[str, Any] = {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": ttl,
    }
    # Preserve proxied if present
    if proxied is not None:
        payload["proxied"] = proxied

    r = requests.put(url, headers=cf_headers(api_token), json=payload, timeout=20)
    data = r.json()

    if not data.get("success"):
        logger.error("Cloudflare update_record failed for %s %s (%s): %s", record_type, name, record_id, data)
        raise RuntimeError(f"Cloudflare update_record failed for {record_type} {name} ({record_id})")

def cf_create_record(
    logger: logging.Logger,
    api_token: str,
    zone_id: str,
    record_type: str,
    name: str,
    content: str,
    ttl: int = 1,
    proxied: Optional[bool] = None,
) -> None:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    payload: Dict[str, Any] = {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": ttl,
    }
    if proxied is not None:
        payload["proxied"] = proxied

    r = requests.post(url, headers=cf_headers(api_token), json=payload, timeout=20)
    data = r.json()

    if not data.get("success"):
        logger.error("Cloudflare create_record failed for %s %s: %s", record_type, name, data)
        raise RuntimeError(f"Cloudflare create_record failed for {record_type} {name}")

@dataclass
class UpdateAction:
    record_id: Optional[str]   # None means "create"
    record_type: str  # "A" or "AAAA"
    name: str
    old_content: Optional[str]
    new_content: str
    ttl: int
    proxied: Optional[bool]

def plan_record_updates(
    logger: logging.Logger,
    api_token: str,
    zone_id: str,
    record_type: str,
    names: List[str],
    new_ip: str,
) -> List[UpdateAction]:
    plan = []

    for name in names:
        results = cf_list_records(logger, api_token, zone_id, record_type, name)

        if not results:
            # No record exists → plan creation
            logger.warning("No %s record for '%s' — will create it.", record_type, name)
            plan.append(
                UpdateAction(
                    record_id=None,
                    record_type=record_type,
                    name=name,
                    old_content=None,
                    new_content=new_ip,
                    ttl=1,
                    proxied=CF_DEFAULT_PROXIED,
                )
            )
            continue

        for rec in results:
            rec_id = rec["id"]
            ttl = rec.get("ttl", 1)
            proxied = rec.get("proxied", None)
            old = rec.get("content", "")

            if old == new_ip:
                logger.debug("No change needed for %s %s (already %s)", record_type, name, new_ip)
                continue

            plan.append(
                UpdateAction(
                    record_id=rec_id,
                    record_type=record_type,
                    name=name,
                    old_content=old,
                    new_content=new_ip,
                    ttl=ttl,
                    proxied=proxied,
                )
            )

    return plan

def apply_update_plan(
    logger: logging.Logger,
    api_token: str,
    zone_id: str,
    plan: List[UpdateAction],
    dry_run: bool,
) -> int:
    # Returns number of successfully applied updates (or planned, in dry-run).
    # Raises on any real-update failure (so we don't write state incorrectly).
    if not plan:
        logger.debug("No Cloudflare updates needed (plan is empty).")
        return 0

    logger.info("Planned Cloudflare updates: %d", len(plan))

    for a in plan:
        if a.record_id is None:
            # CREATE
            if dry_run:
                logger.info(
                    "DRY_RUN: would CREATE %s %s -> %s (ttl=%s proxied=%s)",
                    a.record_type, a.name, a.new_content, a.ttl, a.proxied
                )
            else:
                logger.info(
                    "Creating %s %s -> %s (ttl=%s proxied=%s)",
                    a.record_type, a.name, a.new_content, a.ttl, a.proxied
                )
                cf_create_record(
                    logger=logger,
                    api_token=api_token,
                    zone_id=zone_id,
                    record_type=a.record_type,
                    name=a.name,
                    content=a.new_content,
                    ttl=a.ttl,
                    proxied=a.proxied,
                )
        else:
            # UPDATE
            if dry_run:
                logger.info(
                    "DRY_RUN: would UPDATE %s %s: %s -> %s (ttl=%s proxied=%s id=%s)",
                    a.record_type, a.name, a.old_content, a.new_content,
                    a.ttl, a.proxied, a.record_id
                )
            else:
                logger.info(
                    "Updating %s %s: %s -> %s (ttl=%s proxied=%s id=%s)",
                    a.record_type, a.name, a.old_content, a.new_content,
                    a.ttl, a.proxied, a.record_id
                )
                cf_update_record(
                    logger=logger,
                    api_token=api_token,
                    zone_id=zone_id,
                    record_id=a.record_id,
                    record_type=a.record_type,
                    name=a.name,
                    content=a.new_content,
                    ttl=a.ttl,
                    proxied=a.proxied,
                )

    return len(plan)

#================================================
#
# MAIN
#
#================================================

def main():
    global LOG_DEBUG
    global CF_DEFAULT_PROXIED
    all_plan = []

    load_dotenv()
    errors = False

    DRY_RUN = parse_bool(os.getenv("DRY_RUN"))
    VARIABLES = parse_bool(os.getenv("VARIABLES"))

    # Logging
    LOG_FILENAME = os.getenv("LOG_FILENAME", "").strip() or None
    LOG_KEEP = parse_int(os.getenv("LOG_KEEP"), 0)  # 0 = keep everything
    LOG_DEBUG = parse_bool(os.getenv("LOG_DEBUG"))
    logger = setup_logging(LOG_FILENAME, LOG_KEEP, LOG_DEBUG)
    logger.debug("=======================================================")
    logger.debug("Starting configuration check")

    # Cloudflare 
    CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
    if not CF_API_TOKEN:
        logger.warning("CF_API_TOKEN is missing.")
        errors = True

    CF_ZONE_ID = os.getenv("CF_ZONE_ID", "").strip()
    if not CF_ZONE_ID:
        logger.warning("CF_ZONE_ID is missing.")
        errors = True
    
    CF_DEFAULT_PROXIED = parse_bool(os.getenv("CF_DEFAULT_PROXIED"))

    RECORDS_A = parse_csv_list(os.getenv("RECORDS_A"))
    RECORDS_AAAA = parse_csv_list(os.getenv("RECORDS_AAAA"))
    if not RECORDS_A and not RECORDS_AAAA:
        logger.warning("No DNS records configured (RECORDS_A and RECORDS_AAAA are both empty)")
        errors = True

    CHECK_V4 = parse_bool(os.getenv("CHECK_V4"))
    CHECK_V6 = parse_bool(os.getenv("CHECK_V6"))

    ip_v4_check_urls = parse_csv_list(os.getenv("ip_v4_check_urls")) or [
        "https://api.ipify.org?format=json",
        "https://ifconfig.co/json",
        "https://icanhazip.com",
    ]
    ip_v6_check_urls = parse_csv_list(os.getenv("ip_v6_check_urls")) or [
        "https://api64.ipify.org?format=json",
        "https://ifconfig.co/ip",
        "https://icanhazip.com",
    ]

    STATE_FILENAME = os.getenv("STATE_FILENAME", "").strip() or "cf_ddns_state"

    LOOKUP_RETRY_SECONDS = parse_int(os.getenv("LOOKUP_RETRY_SECONDS"), 3)
    LOOKUP_RETRIES = parse_int(os.getenv("LOOKUP_RETRIES"), 3)

    if errors:
        logger.error("Configuration errors detected. Fix .env and retry.")
        return 2
    
    logger.debug("Configuration check completed successfully")
    
    if VARIABLES:
        logger.info("Resolved configuration:")
        logger.info("CF_API_TOKEN         = %s", mask_token(CF_API_TOKEN))
        logger.info("CF_ZONE_ID           = %s", CF_ZONE_ID)
        logger.info("LOOKUP_RETRY_SECONDS = %s", LOOKUP_RETRY_SECONDS)
        logger.info("LOOKUP_RETRIES       = %s", LOOKUP_RETRIES)
        logger.info("DRY_RUN              = %s", DRY_RUN)
        logger.info("LOG_FILENAME         = %s", LOG_FILENAME)
        logger.info("STATE_FILENAME       = %s", STATE_FILENAME)
        logger.info("CHECK_V4             = %s", CHECK_V4)
        logger.info("CHECK_V6             = %s", CHECK_V6)

        logger.info("RECORDS_A (%d)", len(RECORDS_A))
        for r in RECORDS_A:
            logger.info("  - %s", r)

        logger.info("RECORDS_AAAA (%d)", len(RECORDS_AAAA))
        for r in RECORDS_AAAA:
            logger.info("  - %s", r)

        logger.info("ip_v4_check_urls (%d)", len(ip_v4_check_urls))
        for u in ip_v4_check_urls:
            logger.info("  - %s", u)

        logger.info("ip_v6_check_urls (%d)", len(ip_v6_check_urls))
        for u in ip_v6_check_urls:
            logger.info("  - %s", u)

    if CHECK_V4:
        ip4 = get_external_ip(logger, ip_v4_check_urls, "v4", LOOKUP_RETRIES, LOOKUP_RETRY_SECONDS)
    else:
        ip4 = None
    if CHECK_V6:
        ip6 = get_external_ip(logger, ip_v6_check_urls, "v6", LOOKUP_RETRIES, LOOKUP_RETRY_SECONDS)
    else:
        ip6 = None
    logger.debug("Resolved IPs: ipv4=%s ipv6=%s", ip4, ip6)

    #
    STATE_PATH = LOG_DIR / f"{STATE_FILENAME}.json"
    state = load_state(logger, STATE_PATH)
    known_records = state.get("records", {})
    known_a = set(known_records.get("A", []))
    known_aaaa = set(known_records.get("AAAA", []))
    should_update_v4, should_update_v6 = decide_updates(logger, ip4, ip6, state)
    logger.debug("Decision: update_ipv4=%s update_ipv6=%s", should_update_v4, should_update_v6)

    if ip4 and RECORDS_A:
        to_check = [r for r in RECORDS_A if r not in known_a]
        to_assume_ok = [r for r in RECORDS_A if r in known_a]

        if to_check:
            all_plan.extend(plan_record_updates(logger, CF_API_TOKEN, CF_ZONE_ID, "A", to_check, ip4))
        if should_update_v4:
            all_plan.extend(plan_record_updates(logger, CF_API_TOKEN, CF_ZONE_ID, "A", to_assume_ok, ip4))


    if ip6 and RECORDS_AAAA:
        to_check = [r for r in RECORDS_AAAA if r not in known_aaaa]
        to_assume_ok = [r for r in RECORDS_AAAA if r in known_aaaa]

        if to_check:
            all_plan.extend(plan_record_updates(logger, CF_API_TOKEN, CF_ZONE_ID, "AAAA", to_check, ip6))
        if should_update_v6:
            all_plan.extend(plan_record_updates(logger, CF_API_TOKEN, CF_ZONE_ID, "AAAA", to_assume_ok, ip6))

    
    applied_count = apply_update_plan(logger, CF_API_TOKEN, CF_ZONE_ID, all_plan, dry_run=DRY_RUN)

    if applied_count == 0:
        # No changes needed; do not write state.
        # (State may still be outdated if someone changed manually, but we avoid lying.)
        return 0

    if DRY_RUN:
        logger.info("DRY_RUN complete — %d record(s) would be updated. State not modified.", applied_count)
        return 0

    # Real updates succeeded (if any failed, apply_update_plan would raise)
    logger.info("DNS records updated successfully (%d record(s)).", applied_count)

    # Now update + save state (only after success)
    new_state = apply_new_state(state, ip4, ip6, RECORDS_A, RECORDS_AAAA)
    save_state(logger, STATE_PATH, new_state)    

    return 0

if __name__ == "__main__":
    raise SystemExit(main())