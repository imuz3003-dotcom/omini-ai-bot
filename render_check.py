#!/usr/bin/env python3
"""Render deploy holatini tekshiruvchi qurol (omini AI bot).

Render API kaliti ORQALI:
  RENDER_API_KEY='rnd_xxx' python render_check.py --wait

Kalitni repozitoriyga yozmang — .env'ga ham qo'shmang (u git'da ko'rinmaydi,
lekin bu qurol faqat mahalliy ishlatish uchun).

Chiqish kodlari: 0 = live va kutilgan commit bilan mos; 1 = live, lekin commit
boshqa; 2 = deploy xato/bekor qilindi; 3 = kalit/sozlamada xato.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.render.com/v1"
OK_STATES = {"live"}
BAD_STATES = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}


def api_get(path, key, params=None):
    """Render API'dan GET qiladi; (json, xato_matni) qaytaradi."""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "[]"), None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        hint = {401: "kalit noto'g'ri yoki eskirgan", 403: "kalitda huquq yo'q",
                404: "servis topilmadi", 429: "juda ko'p so'rov — birozdan so'ng qayta urinib ko'ring"}.get(exc.code)
        return None, f"HTTP {exc.code}: {hint or body}"
    except Exception as exc:  # noqa: BLE001 — tarmoq xatolarini matn qilib qaytarish
        return None, f"{type(exc).__name__}: {exc}"


def find_service(key, name):
    """Nomi bo'yicha servisni qidiradi; topilgan service obyekti yoki None."""
    cursor = None
    while True:
        data, err = api_get("/services", key, {"name": name, "limit": 100, "cursor": cursor})
        if err:
            return None, err
        for item in data:
            svc = item.get("service") or item
            if svc.get("name") == name:
                return svc, None
        cursor = next((i.get("cursor") for i in data if i.get("cursor")), None)
        if not cursor or not data:
            return None, f"'{name}' nomli servis topilmadi (kalit to'g'ri, lekin bu boshqa hisobda bo'lishi mumkin)"


def latest_deploys(key, service_id, limit=10):
    data, err = api_get(f"/services/{service_id}/deploys", key, {"limit": limit})
    if err:
        return None, err
    return [item.get("deploy") or item for item in data], None


def local_head():
    """Repositoriyaning HEAD commit'ini qaytaradi (bo'lmasa None)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def commit_sha(deploy):
    commit = deploy.get("commit") or {}
    return (commit.get("id") or "").strip()


def describe(deploy):
    commit = deploy.get("commit") or {}
    message = re.split(r"[.\n]", (commit.get("message") or "").strip())[0][:60]
    return (f"{deploy.get('status','?'):<20} {commit_sha(deploy)[:7]}  "
            f"{deploy.get('startedAt') or deploy.get('createdAt') or '-'}  {message}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render deploy holatini tekshiradi")
    parser.add_argument("--service", default="omini-ai-bot", help="Render servis nomi")
    parser.add_argument("--expect", default=None, help="kutilgan commit (to'liq yoki qisqa SHA)")
    parser.add_argument("--key-env", default="RENDER_API_KEY", help="kalit saqlangan muhit o'zgaruvchisi")
    parser.add_argument("--wait", action="store_true", help="deploy tugaguncha kuting")
    parser.add_argument("--timeout", type=int, default=420, help="--wait uchun maksimal sekund")
    parser.add_argument("--interval", type=int, default=15, help="--wait tekshirish oralig'i")
    parser.add_argument("--json", action="store_true", help="xomat JSON chiqaradi")
    args = parser.parse_args(argv)

    key = os.getenv(args.key_env, "").strip()
    if not key:
        print(f"Kalit topilmadi: {args.key_env} muhit o'zgaruvchisiga yozing (masalan 'rnd_...').", file=sys.stderr)
        return 3

    service, err = find_service(key, args.service)
    if err:
        print(err, file=sys.stderr)
        return 3

    deploys, err = latest_deploys(key, service["id"])
    if err:
        print(err, file=sys.stderr)
        return 3
    if not deploys:
        print("Bu servis uchun deploy tarixi bo'sh.", file=sys.stderr)
        return 3

    expect = (args.expect or local_head() or "").strip()
    deadline = time.time() + args.timeout
    while True:
        current = deploys[0]
        status = (current.get("status") or "").strip()
        sha = commit_sha(current)
        matches = bool(expect) and bool(sha) and (sha.startswith(expect) or expect.startswith(sha))
        finished = status in OK_STATES or status in BAD_STATES

        if args.wait and not finished and time.time() < deadline:
            print(f"Deploy holati: {status} — {args.interval}s dan so'ng qayta tekshiriladi...")
            time.sleep(args.interval)
            deploys, err = latest_deploys(key, service["id"])
            if err:
                print(err, file=sys.stderr)
                return 3
            continue
        break

    if args.json:
        print(json.dumps({"service": {"id": service.get("id"), "name": service.get("name"),
                                      "dashboardUrl": service.get("dashboardUrl")},
                          "deploys": deploys[:5]}, ensure_ascii=False, indent=2))
    else:
        print(f"Servis  : {service.get('name')}  ({service.get('id')})")
        print(f"Dashboard: {service.get('dashboardUrl') or '-'}")
        print("Oxirgi 5 deploy:")
        for deploy in deploys[:5]:
            print("  " + describe(deploy))
        print(f"\nJoriy holat : {status}")
        print(f"Commit      : {sha or '-'}")
        if expect:
            match = bool(sha) and (sha.startswith(expect) or expect.startswith(sha))
            print(f"Kutilgan    : {expect}  ->  {'MOS' if match else 'MOS EMAS'}")
        if status in BAD_STATES:
            print("XATO: deploy muvaffaqiyatsiz — dashboard Logs'da sababini ko'ring.")
        elif status in OK_STATES:
            print("Deploy tugagan: servis 'live'.")
        else:
            print("Deploy hali yakunlanmagan.")

    if status in BAD_STATES:
        return 2
    if status in OK_STATES:
        if expect and not matches:
            return 1
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
