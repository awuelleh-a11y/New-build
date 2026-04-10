"""
Verify email addresses in an agents CSV using a local Reacher instance.

Reacher is an open-source SMTP-based email verifier (https://reacher.email).
Run it locally with: docker run -d --name reacher -p 8080:8080 reacherhq/backend:latest

Reachability verdicts:
  safe    — mailbox confirmed valid
  risky   — uncertain (greylisting, catch-all, or role account) — kept by default
  invalid — bad syntax, no MX, or SMTP rejection — filtered out
  unknown — SMTP timeout / server refused connection — kept by default

Usage:
    python3 verify_emails.py --input datasets/agents_Kempner_TX.csv
    python3 verify_emails.py --input datasets/agents_Kempner_TX.csv --drop-risky
    python3 verify_emails.py --input datasets/agents_Kempner_TX.csv --reacher http://localhost:8080
"""

import argparse, csv, json, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import URLError

REACHER_URL = "http://localhost:8080"
DEFAULT_WORKERS = 10   # parallel SMTP checks; keep low to avoid being flagged as spam
RETRY_ATTEMPTS  = 2


def check_email(email: str, reacher_url: str) -> dict:
    """Call Reacher /v0/check_email and return the parsed JSON response."""
    url  = f"{reacher_url}/v0/check_email"
    body = json.dumps({"to_email": email}).encode()
    req  = Request(url, data=body, headers={"Content-Type": "application/json"})

    for attempt in range(RETRY_ATTEMPTS):
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(2)
    return {"input": email, "is_reachable": "unknown", "error": "timeout"}


def is_valid(result: dict, drop_risky: bool) -> bool:
    verdict = result.get("is_reachable", "unknown")
    if verdict == "invalid":
        return False
    if drop_risky and verdict == "risky":
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Verify agent emails via local Reacher SMTP checker")
    parser.add_argument("--input",       required=True,  help="Input agents CSV path")
    parser.add_argument("--output",      default="",     help="Output CSV path (default: overwrite input)")
    parser.add_argument("--reacher",     default=REACHER_URL, help=f"Reacher base URL (default: {REACHER_URL})")
    parser.add_argument("--workers",     type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    parser.add_argument("--drop-risky",  action="store_true", help="Also drop 'risky' emails (catch-all / role accounts)")
    parser.add_argument("--dry-run",     action="store_true", help="Show stats without writing output")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output) if args.output else input_path

    if not input_path.exists():
        print(f"Error: file not found: {input_path}")
        sys.exit(1)

    # Test Reacher connectivity
    try:
        check_email("ping@example.com", args.reacher)
    except Exception as exc:
        print(f"Error: cannot reach Reacher at {args.reacher} — {exc}")
        print("Start it with: docker run -d --name reacher -p 8080:8080 reacherhq/backend:latest")
        sys.exit(1)

    with open(input_path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    # Add verification result column if not present
    if "email_status" not in fieldnames:
        fieldnames = list(fieldnames) + ["email_status"]

    # Separate rows with/without emails
    with_email    = [(i, r) for i, r in enumerate(rows) if r.get("email", "").strip()]
    without_email = [(i, r) for i, r in enumerate(rows) if not r.get("email", "").strip()]

    print(f"\n{'='*60}")
    print(f"  Email verification via Reacher")
    print(f"  Input  : {input_path.name}")
    print(f"  Total  : {len(rows)} agents")
    print(f"  To check: {len(with_email)} (with email) + {len(without_email)} (no email, kept as-is)")
    print(f"  Workers: {args.workers}")
    print(f"  Drop risky: {args.drop_risky}")
    print(f"{'='*60}\n")

    results: dict[int, dict] = {}  # row_index → reacher result

    done = 0
    def run_check(idx_row):
        idx, row = idx_row
        email = row["email"].strip()
        result = check_email(email, args.reacher)
        return idx, result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_check, ir): ir for ir in with_email}
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result
            done += 1
            verdict = result.get("is_reachable", "unknown")
            email   = result.get("input", "")
            if done % 50 == 0 or done == len(with_email):
                print(f"  [{done}/{len(with_email)}] last: {email} → {verdict}")

    # Tally
    counts = {"safe": 0, "risky": 0, "invalid": 0, "unknown": 0}
    kept, dropped = [], []

    for i, row in enumerate(rows):
        if i in results:
            verdict = results[i].get("is_reachable", "unknown")
            counts[verdict] = counts.get(verdict, 0) + 1
            row = dict(row)
            row["email_status"] = verdict
            if is_valid(results[i], args.drop_risky):
                kept.append(row)
            else:
                dropped.append(row)
        else:
            # No email — keep, mark as no_email
            row = dict(row)
            row["email_status"] = "no_email"
            kept.append(row)

    print(f"\n{'='*60}")
    print(f"  Results:")
    print(f"    safe    : {counts.get('safe',0):>5}  ✓ kept")
    print(f"    risky   : {counts.get('risky',0):>5}  {'✗ dropped' if args.drop_risky else '✓ kept'}")
    print(f"    invalid : {counts.get('invalid',0):>5}  ✗ dropped")
    print(f"    unknown : {counts.get('unknown',0):>5}  ✓ kept")
    print(f"    no email: {len(without_email):>5}  ✓ kept")
    print(f"  {'─'*30}")
    print(f"    kept    : {len(kept):>5}")
    print(f"    dropped : {len(dropped):>5}")
    print(f"{'='*60}\n")

    if args.dry_run:
        print("  [dry-run] No file written.")
        return

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)

    print(f"  Saved {len(kept)} verified agents → {output_path}")

    # Save dropped list for reference
    dropped_path = output_path.parent / (output_path.stem + "_invalid_emails.csv")
    if dropped:
        with open(dropped_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(dropped)
        print(f"  Dropped {len(dropped)} invalid → {dropped_path}\n")


if __name__ == "__main__":
    main()
