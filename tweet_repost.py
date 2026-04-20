"""
tweet_repost.py
記事リポストツイートスクリプト

wakust-repost-Score のスコアリングロジックを活用し、
注目記事をX（Twitter）で紹介する。
実行タイミング: 週2回ランダムな曜日・時間帯（GitHub Actions）
"""

import logging
import math
import random
import re
import sys
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, Page

from tweet_utils import (
    CATEGORY_HASHTAGS,
    HASHTAG_COMMON,
    JST,
    add_to_history,
    get_x_client,
    load_history,
    post_tweet,
    save_history,
    was_recently_tweeted,
)
import os

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
EMAIL = os.environ.get("WAKUST_EMAIL", "")
PASSWORD = os.environ.get("WAKUST_PASSWORD", "")
BASE_URL = "https://wakust.com"
LOGIN_URL = f"{BASE_URL}/login"
MYPAGE_URL = f"{BASE_URL}/mypage/"

# 1回の実行で何件ツイートするか
TWEET_COUNT = 3

# 重複防止: 最低この日数は同じ記事をツイートしない
MIN_TWEET_INTERVAL_DAYS = 14

# スコアリング重み（合計 1.0）
WEIGHT_PV_WEEKLY   = 0.35
WEIGHT_PV_DAILY    = 0.20
WEIGHT_SALES       = 0.15
WEIGHT_MOON        = 0.10
WEIGHT_FRESHNESS   = 0.20

# カテゴリ設定
CATEGORIES = {
    "東京都": "4",
    "神奈川県": "1245",
    "埼玉県": "2442",
    "千葉県": "2441",
    "新宿": "24476",
    "池袋": "24474",
    "多摩": "24624",
    "ノウハウ(ネット)": "6898",
    "ノウハウ(リアル)": "6897",
}


# ─────────────────────────────────────────
# テンプレート生成
# ─────────────────────────────────────────
def build_repost_tweet(title: str, url: str, category: str) -> str:
    cat_hashtag = CATEGORY_HASHTAGS.get(category, "")
    hashtags = f"{HASHTAG_COMMON} {cat_hashtag}".strip() if cat_hashtag else HASHTAG_COMMON

    tweet = (
        f"📖 おすすめ記事を紹介！\n\n"
        f"「{title}」\n\n"
        f"👉 {url}\n\n"
        f"{hashtags}"
    )
    return tweet


# ─────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────
def login(page: Page) -> None:
    logger.info("ログイン中...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    _dismiss_modal(page)
    page.fill("input[name='login_email']", EMAIL)
    page.fill("input[name='login_password']", PASSWORD)
    _dismiss_modal(page)
    page.click("button.login_submit", force=True)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)
    _dismiss_modal(page)
    logger.info("ログイン完了")


def _dismiss_modal(page: Page) -> None:
    """年齢確認モーダルを閉じる"""
    try:
        page.evaluate("""
            const ageModal = document.getElementById('age-verification-modal');
            if (ageModal) {
                ageModal.style.display = 'none';
                ageModal.style.visibility = 'hidden';
                ageModal.style.pointerEvents = 'none';
            }
            document.querySelectorAll('.modal, [class*="age"], [id*="age"]').forEach(el => {
                el.style.display = 'none';
                el.style.pointerEvents = 'none';
            });
            const overlay = document.querySelector('.modal-backdrop, .overlay');
            if (overlay) overlay.style.display = 'none';
            document.body.classList.remove('modal-open');
            document.body.style.overflow = '';
        """)
    except Exception:
        pass


# ─────────────────────────────────────────
# 記事データ取得
# ─────────────────────────────────────────
def fetch_articles(page: Page) -> list[dict]:
    """全記事データを取得する"""
    all_articles = []
    seen_ids = set()
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # 全カテゴリ一括取得（表示件数多めに設定）
    url = f"{MYPAGE_URL}?post_list=&sort=date&cat=0&p_n=0&p_s=0&s_t=&lmt=100"
    logger.info(f"記事一覧取得: {url}")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    _dismiss_modal(page)

    try:
        page.wait_for_selector("table.table tbody tr", timeout=10000)
    except Exception:
        logger.warning("テーブルが見つかりません")
        return []

    articles = page.evaluate(f"""
        (() => {{
            const rows = Array.from(document.querySelectorAll('table.table tbody tr'));
            const today = "{today_str}";
            const results = [];

            for (const row of rows) {{
                const editLink = row.querySelector('a[href*="post_edit="]');
                if (!editLink) continue;
                const m = editLink.href.match(/post_edit=(\\d+)/);
                if (!m) continue;
                const post_id = m[1];

                // タイトル（td_2クラス）
                const titleEl = row.querySelector('td.td_2');
                const title = titleEl ? titleEl.textContent.trim() : "";

                // 販売ステータス
                const select = row.querySelector('select');
                if (select && select.value === '販売停止') continue;

                // 投稿日（予約投稿スキップ）
                const td3 = row.querySelector('td.td_3');
                const dateText = td3 ? td3.textContent : row.textContent;
                const dateMatch = dateText.match(/(\\d{{4}}-\\d{{2}}-\\d{{2}})/);
                if (dateMatch && dateMatch[1] > today) continue;

                // カテゴリ
                const catEl = row.querySelector('td[style*="font-size:13px"]');
                const category = catEl ? catEl.textContent.trim() : "";

                // PV・売上・ムーン（td_4に含まれる）
                const td4 = row.querySelector('td.td_4');
                const statsText = td4 ? td4.textContent : "";
                const pvDaily  = parseInt((statsText.match(/前日[：:]\s*(\d+)/) || [])[1] || "0");
                const pvWeekly = parseInt((statsText.match(/週[：:]\s*(\d+)/)   || [])[1] || "0");
                const sales    = parseInt((statsText.match(/販売回数[：:]\s*(\d+)/) || [])[1] || "0");
                const moon     = parseInt((statsText.match(/(\d+)/) || [])[1] || "0");

                // 記事URL
                const articleLink = row.querySelector('a[href*="/post/"]');
                const articleUrl = articleLink ? articleLink.href : "";

                results.push({{
                    post_id, title, url: articleUrl, category,
                    pv_daily: pvDaily, pv_weekly: pvWeekly,
                    sales, moon
                }});
            }}
            return results;
        }})()
    """)

    for a in articles:
        if a["post_id"] not in seen_ids:
            seen_ids.add(a["post_id"])
            all_articles.append(a)

    logger.info(f"取得記事数: {len(all_articles)}")
    return all_articles


# ─────────────────────────────────────────
# スコアリング
# ─────────────────────────────────────────
def normalize_log(value: float, max_val: float = 100.0) -> float:
    """対数スケールで 0〜100 に正規化"""
    if value <= 0:
        return 0.0
    return min(math.log10(value + 1) / math.log10(max_val + 1) * 100, 100.0)


def calc_freshness_score(post_id: str, history: dict) -> float:
    """
    最後にツイートした日からの経過日数でスコアを算出。
    14日以上 → 100、未ツイート → 100（30日経過扱い）
    """
    max_days = 14
    for entry in reversed(history["tweets"]):
        if entry.get("post_id") == str(post_id) and entry.get("category") == "repost":
            tweeted_at = datetime.fromisoformat(entry["tweeted_at"])
            elapsed = (datetime.now(JST) - tweeted_at).days
            return min(elapsed / max_days * 100, 100.0)
    return 100.0  # 未ツイート = 高鮮度


def score_article(article: dict, history: dict) -> float:
    """記事のスコアを計算する"""
    s_pv_weekly = normalize_log(article.get("pv_weekly", 0))
    s_pv_daily  = normalize_log(article.get("pv_daily", 0))
    s_sales     = normalize_log(article.get("sales", 0))
    s_moon      = normalize_log(article.get("moon", 0))
    s_freshness = calc_freshness_score(article["post_id"], history)

    return (
        WEIGHT_PV_WEEKLY * s_pv_weekly
        + WEIGHT_PV_DAILY * s_pv_daily
        + WEIGHT_SALES    * s_sales
        + WEIGHT_MOON     * s_moon
        + WEIGHT_FRESHNESS * s_freshness
    )


# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────
def main(dry_run: bool = False) -> None:
    logger.info("=== 記事リポストツイート開始 ===")

    history = load_history()
    client = get_x_client() if not dry_run else None
    tweeted_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(15000)

        try:
            login(page)
            articles = fetch_articles(page)
        finally:
            browser.close()

    if not articles:
        logger.warning("記事が取得できませんでした")
        return

    # スコアリング & ソート
    for a in articles:
        a["score"] = score_article(a, history)

    articles.sort(key=lambda x: x["score"], reverse=True)

    # 重複チェックで除外後、上位から選定
    candidates = [
        a for a in articles
        if not was_recently_tweeted(history, a["post_id"], MIN_TWEET_INTERVAL_DAYS)
    ]

    if not candidates:
        logger.warning("投稿可能な候補がありません（全記事が間隔制限内）")
        return

    selected = candidates[:TWEET_COUNT]

    for article in selected:
        post_id = article["post_id"]
        title = article["title"]
        url = article.get("url") or f"{BASE_URL}/post/{post_id}"
        category = article["category"]

        tweet_text = build_repost_tweet(title, url, category)
        logger.info(f"投稿準備 [スコア:{article['score']:.1f}]:\n{tweet_text}\n{'─'*40}")

        tweet_id = post_tweet(client, tweet_text, dry_run=dry_run)

        if tweet_id:
            add_to_history(history, tweet_id, post_id, "repost", title)
            tweeted_count += 1
        else:
            logger.error(f"投稿失敗: {title}")

    save_history(history)
    logger.info(f"=== 記事リポストツイート完了: {tweeted_count}件投稿 ===")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
