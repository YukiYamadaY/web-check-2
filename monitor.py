"""
汎用 新着チェッカー（軽量版・ブラウザ不要）

監視URL・絞り込みワード・通知先はすべて GitHub Secrets から読み込む。
ログには URL・商品名を一切出さない。

必要な Secrets:
  TARGET_URLS   … 監視する一覧ページのURL（複数ならカンマ区切り）
  WEBHOOK_URL   … 通知先のWebhook URL
  FILTER_WORDS  … 絞り込みワード（カンマ区切り。空なら全商品）
"""

import json
import os
import random
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ================= 設定 =================
CHECK_INTERVAL_SEC = 8         # 巡回間隔（秒）
MAX_RUN_SEC = 5.5 * 3600       # 1回の実行の最大時間
QUIET_START_HOUR = 15          # 日本時間この時刻から停止
QUIET_END_HOUR = 9             # 日本時間この時刻まで停止
PERSIST_EVERY_SEC = 20 * 60    # 記録ファイルを保存する間隔
MAX_NOTIFY_PER_CHECK = 50     # 1回で通知する最大件数（10件ずつまとめて送信）

TARGET_URLS = [u.strip() for u in os.environ.get("TARGET_URLS", "").split(",") if u.strip()]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
FILTER_WORDS = [w.strip().lower() for w in os.environ.get("FILTER_WORDS", "").split(",") if w.strip()]
SEEN_FILE = Path(__file__).parent / "seen.json"
JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
}
# ========================================


def log(msg: str):
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def in_quiet_hours() -> bool:
    h = datetime.now(JST).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            log("[WARN] 記録ファイルの読み込み失敗")
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def persist_to_repo():
    try:
        subprocess.run(["git", "add", "seen.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "update [skip ci]"],
                       check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
        log("記録を保存")
    except Exception:
        log("[WARN] 記録の保存に失敗")


def base_and_json(url: str):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    return base, f"{base}{u.path.rstrip('/')}/products.json"


def matches(product: dict) -> bool:
    if not FILTER_WORDS:
        return True
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    text = " ".join([product.get("product_type") or ""] + list(tags)).lower()
    return any(w in text for w in FILTER_WORDS)


def is_available(product: dict) -> bool:
    variants = product.get("variants") or []
    return any(v.get("available", True) for v in variants) if variants else True


def fetch_items(session, url: str, debug: bool = False) -> list[dict]:
    base, json_url = base_and_json(url)
    r = session.get(json_url, params={"limit": 250}, headers=HEADERS, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("rate_limited")
    r.raise_for_status()
    products = r.json().get("products", [])

    items = []
    for p in products:
        if not matches(p) or not is_available(p):
            continue
        variants = p.get("variants") or [{}]
        images = p.get("images") or [{}]
        price = variants[0].get("price")
        title = " ".join(x for x in [p.get("vendor") or "", p.get("title") or ""] if x).strip()
        items.append({
            "id": str(p.get("id")),
            "url": f"{base}/products/{p.get('handle')}",
            "title": title or "(タイトル取得不可)",
            "price": price,
            "img": images[0].get("src", ""),
        })

    if debug:
        types = sorted({(p.get("product_type") or "（なし）") for p in products})[:15]
        log(f"取得 {len(products)}件のうち、絞り込み一致 {len(items)}件")
        log(f"商品種別の例: {' / '.join(types)}")
    return items


def make_embed(item: dict) -> dict:
    embed = {
        "title": f"🆕 新着: {item['title'][:110]}",
        "url": item["url"],
        "color": 0x3498DB,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.get("price"):
        try:
            embed["description"] = f"¥{int(float(item['price'])):,}"
        except Exception:
            embed["description"] = str(item["price"])
    if item["img"]:
        embed["image"] = {"url": item["img"]}
    return embed


def notify_batch(items: list[dict]):
    """10件ずつ1メッセージにまとめて送る（Discordの上限が1通10件のため）"""
    if not WEBHOOK_URL:
        log("[WARN] WEBHOOK_URL未設定")
        return
    for i in range(0, len(items), 10):
        payload = {"embeds": [make_embed(it) for it in items[i:i + 10]]}
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code >= 400:
                log(f"[ERROR] 通知失敗（ステータス {r.status_code}）")
        except Exception as e:
            log(f"[ERROR] 通知エラー（{type(e).__name__}）")
        time.sleep(1)


def main():
    if not TARGET_URLS:
        log("[ERROR] TARGET_URLS が未設定です")
        return
    if in_quiet_hours():
        log("停止時間帯なので終了")
        return

    seen = load_seen()
    first_run = len(seen) == 0
    start = last_persist = time.time()
    fail_streak = cycle = 0
    session = requests.Session()

    while True:
        if time.time() - start > MAX_RUN_SEC:
            log("最大実行時間に到達。次の実行に引き継ぎ")
            break
        if in_quiet_hours():
            log("停止時間帯に入ったので終了")
            break

        cycle += 1
        all_items, ok = [], False
        for url in TARGET_URLS:
            try:
                got = fetch_items(session, url, debug=(cycle == 1))
                ok = True
                all_items.extend(got)
            except Exception as e:
                log(f"[WARN] 取得失敗（{type(e).__name__}）")

        if not ok:
            fail_streak += 1
            wait = min(CHECK_INTERVAL_SEC * (2 ** fail_streak), 300)
            log(f"[WARN] 取得失敗（{fail_streak}回連続）。{wait:.0f}秒待機")
            time.sleep(wait)
            continue
        fail_streak = 0

        new_items = [it for it in all_items if it["id"] not in seen]
        if first_run:
            log(f"初回: {len(all_items)}件を登録（通知なし）")
            first_run = False
        elif new_items:
            log(f"新着 {len(new_items)}件を検知・通知")
            notify_batch(new_items[:MAX_NOTIFY_PER_CHECK])
        elif cycle % 50 == 0:
            log(f"異常なし（{len(all_items)}件監視中・{cycle}回目）")

        seen.update(it["id"] for it in all_items)
        save_seen(seen)

        if time.time() - last_persist > PERSIST_EVERY_SEC:
            persist_to_repo()
            last_persist = time.time()

        time.sleep(max(2.0, CHECK_INTERVAL_SEC + random.uniform(-1.5, 1.5)))

    save_seen(seen)
    persist_to_repo()


if __name__ == "__main__":
    main()
