#!/usr/bin/env python3
"""Read ../.env and write a nvs_partition_gen-compatible CSV to stdout or a file."""
import csv, os, sys

def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

def main():
    env_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "../../.env")
    out_path = sys.argv[2] if len(sys.argv) > 2 else None

    env = load_env(env_path)

    required = ["FRAME_WIFI_SSID", "FRAME_WIFI_PASSWORD", "FRAME_SERVER_URL", "FRAME_POLL_SECONDS"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        sys.exit(f"Error: missing or empty in .env: {', '.join(missing)}")

    out = open(out_path, "w", newline="") if out_path else sys.stdout
    w = csv.writer(out)
    w.writerow(["key", "type", "encoding", "value"])
    w.writerow(["frame_cfg",  "namespace", "",       ""])
    w.writerow(["ssid",       "data",      "string", env["FRAME_WIFI_SSID"]])
    w.writerow(["password",   "data",      "string", env["FRAME_WIFI_PASSWORD"]])
    w.writerow(["server_url", "data",      "string", env["FRAME_SERVER_URL"]])
    w.writerow(["poll_sec",   "data",      "u32",    env["FRAME_POLL_SECONDS"]])
    if out_path:
        out.close()

if __name__ == "__main__":
    main()
