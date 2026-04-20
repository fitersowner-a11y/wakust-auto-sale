"""
tweet_new_article.py
新着記事検知 & ツイートスクリプト

ワクストのマイページをスクレイピングし、当日公開された記事を検知してXに投稿する。
実行タイミング: 毎日 08:00〜10:00 JST（GitHub Actions cron）
"""

import logging
import re
import sys
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright, Page

from tweet_utils import (
    HASHTAG_COMMON,
    HASHTAG_NEW,
    JST,
    add_to_history,
    get_x_client,
    load_history,
    post_tweet,
    save_history,
    was_new_article_tweeted,
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

# スクレイピング対象カテゴリ
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
def build_new_article_tweet(title: str, url: str) -> str:
    tweet = (
        f"🆕 新着記事が公開されました！\n\n"
        f"「{title}」\n\n"
        f"👉 {url}\n\n"
        f"{HASHTAG_COMMON} {HASHTAG_NEW}"
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
# 当日公開記事の取得
# ─────────────────────────────────────────
def fetch_todays_articles(page: Page, today_str: str) -> list[dict]:
    """
    全記事一覧から当日（today_str: YYYY-MM-DD）公開の記事を返す

    Returns:
        [{"post_id": str, "title": str, "url": str, "category": str}, ...]
    """
    results = []
    seen_ids = set()

    # 全カテゴリ（cat=0）で全件取得、表示件数を大きめに設定
    url = f"{MYPAGE_URL}?post_list=&sort=date&cat=0&p_n=0&p_s=0&s_t=&lmt=50"
    logger.info(f"記事一覧取得: {url}")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    _dismiss_modal(page)

    try:
        page.wait_for_selector("table.table tbody tr", timeout=10000)
    except Exception:
        logger.warning("テーブルが見つかりません。記事が0件の可能性があります。")
        return []

    articles = page.evaluate(f"""
        (() => {{
            const rows = Array.from(document.querySelectorAll('table.table tbody tr'));
            const today = "{today_str}";
            const results = [];

            for (const row of rows) {{
                // タイトル（td_2クラス）
                const titleEl = row.querySelector('td.td_2');
                if (!titleEl) continue;
                const title = titleEl.textContent.trim();

                // post_id（編集リンクから取得）
                const editLink = row.querySelector('a[href*="post_edit="]');
                if (!editLink) continue;
                const m = editLink.href.match(/post_edit=(\\d+)/);
                if (!m) continue;
                const post_id = m[1];

                // 投稿日時（td_3に含まれるYYYY-MM-DD）
                const td3 = row.querySelector('td.td_3');
                const dateText = td3 ? td3.textContent : row.textContent;
                const dateMatch = dateText.match(/(\\d{{4}}-\\d{{2}}-\\d{{2}})/);
                if (!dateMatch) continue;
                const postDate = dateMatch[1];

                // 予約投稿スキップ
                if (postDate > today) continue;

                // 当日公開のみ
                if (postDate !== today) continue;

                // 販売ステータス確認
                const select = row.querySelector('select');
                if (select && select.value === '販売停止') continue;

                // カテゴリ名
                const catEl = row.querySelector('td[style*="font-size:13px"]');
                const category = catEl ? catEl.textContent.trim() : "";

                // 記事URL
                const articleLink = row.querySelector('a[href*="/post/"]');
                const articleUrl = articleLink ? articleLink.href : "";

                results.push({{ post_id, title, url: articleUrl, category }});
            }}
            return results;
        }})()
    """)

    for a in articles:
        if a["post_id"] not in seen_ids:
            seen_ids.add(a["post_id"])
            results.append(a)

    return results


# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────
def main(dry_run: bool = False) -> None:
    logger.info("=== 新着記事チェック開始 ===")
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    logger.info(f"本日: {today_str}")

    history = load_history()
    client = get_x_client() if not dry_run else None
    tweeted_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(15000)

        try:
            login(page)
            new_articles = fetch_todays_articles(page, today_str)
            logger.info(f"本日公開の記事: {len(new_articles)}件")

            for article in new_articles:
                post_id = article["post_id"]
                title = article["title"]
                url = article.get("url", "")

                # URL が取れなかった場合は記事IDから生成
                if not url:
                    url = f"{BASE_URL}/post/{post_id}"

                # 重複チェック
                if was_new_article_tweeted(history, post_id):
                    logger.info(f"スキップ（投稿済み）: {title}")
                    continue

                tweet_text = build_new_article_tweet(title, url)
                logger.info(f"投稿準備:\n{tweet_text}\n{'─'*40}")

                tweet_id = post_tweet(client, tweet_text, dry_run=dry_run)

                if tweet_id:
                    add_to_history(history, tweet_id, post_id, "new_article", title)
                    tweeted_count += 1
                else:
                    logger.error(f"投稿失敗: {title}")

        finally:
            browser.close()

    save_history(history)
    logger.info(f"=== 新着記事チェック完了: {tweeted_count}件投稿 ===")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
