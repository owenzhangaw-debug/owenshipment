#!/usr/bin/env python3
"""
Sync script: owenshipment.netlify.app (via JSONBin.io cloud backup) -> Feishu Base
("欧洲端 散单" -> Owen table).

Run manually to test:
    JSONBIN_MASTER_KEY=xxx python3 owenshipment_feishu_sync.py --dry-run

Run for real:
    JSONBIN_MASTER_KEY=xxx python3 owenshipment_feishu_sync.py

Deploy via cron (see DEPLOY.md written alongside this file for step-by-step setup).

Design notes / business rules encoded here (agreed with Owen over several rounds):
  - Container number field (柜号) only accepts real ISO container numbers
    (AAAA1234567). Non-container reference numbers (LCL/parcel tracking refs,
    HBL numbers, etc.) are still written to 柜号, but never guessed as a
    format assumption for matching.
  - One row per container. An order with N containers becomes N Feishu
    records sharing the same NBS工作单号 but each with its own 柜号 and
    per-container dates. The app may store multiple containers for one
    order as a single comma-separated string; this script explodes that
    into one row per container.
  - 订单状态 / 状态: "已完成" if that container's ATA is present, else "进行中".
  - 到站文件获取日期 = App's "At Hub Date".
  - 清关放行日期 = App's "CC Date", falling back to "T1 Date" if CC Date is empty.
  - 清关时效 = (清关放行日期 - 到站文件获取日期) in days.
  - 总推存天数 / 提货时效 = (Pick Up Date - At Hub Date) in days (same value,
    two fields, per Owen's instruction -- flagged as a best-guess mapping).
  - 签收单及账单文件回传日期 = App's "POD Date".
  - 签收单回传时效 = (POD Date - Delivery Date) in days.
  - 是否完成付款: only ever writes "已请求付款" when the app's Payment Status
    is exactly "Requested" (any other value is left alone -- ambiguous).
  - Existing non-empty Feishu fields are NEVER overwritten by this script,
    except 订单状态 and 状态, which are always re-derived from ATA presence
    (Owen asked for this to always reflect the live rule).

This script only ever READS from JSONBin (never pushes local app data back to
the cloud) and only ever calls the local `lark-cli` binary for Feishu writes
(lark-cli manages its own Feishu OAuth token; this script never touches it).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID", "69ae286843b1c97be9c2dece")
JSONBIN_MASTER_KEY = os.environ.get("JSONBIN_MASTER_KEY")
FEISHU_BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "XxTWb1ss4anN9WsMpzScoL6Tnje")
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "tbl58vGY4bfyJXL7")
ORDERS_KEY_IN_BIN = os.environ.get("JSONBIN_ORDERS_KEY", "NO")  # the app's localStorage key for "Orders"
LARK_IDENTITY = os.environ.get("LARK_IDENTITY", "bot")  # "bot" works headless (CI); no interactive login needed

CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fetch_jsonbin_orders():
    import urllib.request
    import urllib.error

    if not JSONBIN_MASTER_KEY:
        log("ERROR: JSONBIN_MASTER_KEY is not set in the environment. Aborting.")
        sys.exit(1)

    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    req = urllib.request.Request(url, headers={
        "X-Master-Key": JSONBIN_MASTER_KEY,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"JSONBin request failed: HTTP {e.code}\n{body}")
        raise
    orders = data.get("record", {}).get(ORDERS_KEY_IN_BIN, [])
    log(f"Fetched {len(orders)} order rows from JSONBin (key='{ORDERS_KEY_IN_BIN}')")
    return orders


def norm_order_key(order_no):
    if not order_no:
        return None
    s = order_no.strip()
    m = re.search(r"([A-Z]{2,10}\d{6,})", s)
    if not m:
        return s.upper().replace(" ", "")
    base = m.group(1)
    rest = s[m.end():]
    m2 = re.search(r"C\d", rest, re.IGNORECASE)
    return base + (m2.group(0).upper() if m2 else "")


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y/%m/%d")
    except ValueError:
        return None


def days_between(a, b):
    da, db = parse_date(a), parse_date(b)
    return (db - da).days if da and db else None


def zero_pad_date(s):
    if not s:
        return s
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", s.strip())
    if not m:
        return s
    y, mo, d = m.groups()
    return f"{int(y):04d}/{int(mo):02d}/{int(d):02d}"


def money_str(receivable, payable, margin):
    parts = []
    if receivable not in (None, ""):
        parts.append(f"Receivable: €{receivable}")
    if payable not in (None, "") and float(payable or 0) != 0:
        parts.append(f"Payable: €{payable}")
    if margin not in (None, ""):
        parts.append(f"Margin: €{margin}")
    return "\n".join(parts) if parts else None


def build_container_fields(order_no_raw, row, shared):
    """Build the Feishu field dict for a single container row."""
    fields = {}
    fields["NBS工作单号"] = order_no_raw
    for k in ("国家", "提货站|港|堆场", "服务种类", "代理信息"):
        if shared.get(k):
            fields[k] = shared[k]
    fields["交接人|监交人"] = shared.get("交接人|监交人") or "Owen"

    container = row.get("Container No.")
    if container:
        fields["柜号"] = container
        fields["柜量"] = 1

    ata = row.get("ATA")
    if ata:
        fields["ATA"] = zero_pad_date(ata)
    delivery = row.get("Delivery Date")
    if delivery:
        fields["派送日期"] = delivery
    at_hub = row.get("At Hub Date")
    if at_hub:
        fields["到站文件获取日期"] = at_hub

    release_date = row.get("CC Date") or row.get("T1 Date")
    if release_date:
        fields["清关放行日期"] = release_date
        cl = days_between(at_hub, release_date)
        if cl is not None:
            fields["清关时效"] = f"{cl} 天"

    pl = days_between(at_hub, row.get("Pick Up Date"))
    if pl is not None:
        fields["总推存天数"] = f"{pl} 天"
        fields["提货时效"] = f"{pl} 天"

    pod = row.get("POD Date")
    if pod:
        fields["签收单及账单文件回传日期"] = pod
    podl = days_between(delivery, pod)
    if podl is not None:
        fields["签收单回传时效"] = f"{podl} 天"

    if row.get("Payment Status") == "Requested":
        fields["是否完成付款"] = "已请求付款"

    ms = money_str(row.get("Receivable"), row.get("Payable"), row.get("Margin"))
    if ms:
        fields["询比价报价"] = ms

    status = "已完成" if ata else "进行中"
    fields["状态"] = status
    fields["订单状态"] = status
    return fields


def lark_cli(args, input_json=None):
    """Run a lark-cli command, optionally piping a JSON payload via a temp file."""
    cmd = ["lark-cli"] + args + ["--as", LARK_IDENTITY]
    tmp_path = None
    try:
        if input_json is not None:
            fd, tmp_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(input_json, f, ensure_ascii=False)
            cmd += ["--json", f"@{tmp_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = result.stdout
        start = out.find("{")
        payload = json.loads(out[start:]) if start != -1 else {"ok": False, "raw": out}
        if not payload.get("ok", True):
            log(f"lark-cli command failed: {' '.join(args)}\n{out}\n{result.stderr}")
        return payload
    finally:
        if tmp_path:
            os.unlink(tmp_path)


def fetch_current_feishu_records():
    fd, tmp_out = tempfile.mkstemp(suffix=".ndjson")
    os.close(fd)
    try:
        lark_cli([
            "base", "+record-list",
            "--base-token", FEISHU_BASE_TOKEN,
            "--table-id", FEISHU_TABLE_ID,
            "--limit", "500",
            "--format", "ndjson",
            "--output", tmp_out,
            "--overwrite",
        ])
        records = []
        with open(tmp_out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
    finally:
        os.unlink(tmp_out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Compute the plan but do not write to Feishu")
    args = ap.parse_args()

    orders = fetch_jsonbin_orders()
    by_order_id = {}
    for o in orders:
        by_order_id.setdefault(o.get("_id"), []).append(o)

    current = fetch_current_feishu_records()
    by_key = {}
    by_container = {}
    for r in current:
        k = norm_order_key(r.get("NBS工作单号"))
        if k:
            by_key.setdefault(k, []).append(r)
        c = r.get("柜号")
        if c and "\n" not in c:
            by_container[c] = r

    to_create = []
    to_update = {}

    for oid, rows in by_order_id.items():
        order_no_raw = rows[0].get("Order No.")
        key = norm_order_key(order_no_raw)
        if not key:
            continue
        existing_for_order = by_key.get(key, [])
        # Build a "shared" template from the first existing record for this
        # order (if any), else derive from the first row's own data.
        shared = existing_for_order[0] if existing_for_order else {
            "国家": rows[0].get("Destination"),
            "提货站|港|堆场": rows[0].get("Hub"),
            "服务种类": rows[0].get("Service Type"),
            "代理信息": rows[0].get("Trucker"),
        }

        for row in rows:
            # The app can store multiple containers for one order as a single
            # comma-separated string (JSONBin) rather than one row per
            # container (unlike the Excel export). Explode that here so we
            # always end up with one container per Feishu record.
            raw_container = row.get("Container No.")
            containers = [c.strip() for c in raw_container.split(",")] if raw_container else [None]
            containers = [c for c in containers if c] or [None]
            used_record_ids_this_order = set()

            for container in containers:
                sub_row = dict(row)
                sub_row["Container No."] = container

                existing_rec = by_container.get(container) if container else None
                if not existing_rec:
                    # look for an existing record of this order that has no
                    # container yet (covers orders created before a container
                    # number was known), and that we have not already claimed
                    # for a different container in this same pass
                    existing_rec = next(
                        (r for r in existing_for_order
                         if not r.get("柜号") and r["record_id"] not in used_record_ids_this_order),
                        None,
                    )
                if existing_rec:
                    used_record_ids_this_order.add(existing_rec["record_id"])

                fresh_fields = build_container_fields(order_no_raw, sub_row, shared)

                if existing_rec:
                    changes = {}
                    for fk, fv in fresh_fields.items():
                        if fk in ("状态", "订单状态"):
                            if existing_rec.get(fk) != fv:
                                changes[fk] = fv
                        elif not existing_rec.get(fk):
                            changes[fk] = fv
                    if changes:
                        to_update[existing_rec["record_id"]] = changes
                else:
                    to_create.append(fresh_fields)

    log(f"Plan: {len(to_update)} records to update, {len(to_create)} new records to create")

    if args.dry_run:
        print(json.dumps({"update_records": to_update}, ensure_ascii=False, indent=1))
        print(json.dumps({"create_records": to_create}, ensure_ascii=False, indent=1))
        return

    if to_update:
        resp = lark_cli(
            ["base", "+record-batch-update", "--base-token", FEISHU_BASE_TOKEN, "--table-id", FEISHU_TABLE_ID],
            input_json={"update_records": to_update},
        )
        log(f"Updated {len(resp.get('data', {}).get('record_id_list', []))} records")

    if to_create:
        resp = lark_cli(
            ["base", "+record-batch-create", "--base-token", FEISHU_BASE_TOKEN, "--table-id", FEISHU_TABLE_ID],
            input_json={"create_records": to_create},
        )
        log(f"Created {len(resp.get('data', {}).get('record_id_list', []))} records")

    log("Done.")


if __name__ == "__main__":
    main()
