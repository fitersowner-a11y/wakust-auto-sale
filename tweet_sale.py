"""
tweet_sale.py
セール告知ツイートスクリプト

sale_result.json を読み取り、セール対象記事をXに告知投稿する。
実行タイミング: sale_auto.py 実行直後（GitHub Actions の needs: で連携）
"""

import json
import logging
import sys
from pathlib import Path

from tweet_utils import (
    HASHTAG_COMMON,
    HASHTAG_SALE,
    add_to_history,
    get_x_client,
    load_history,
    post_tweet,
    save_history,
    was_sale_already_tweeted,
)

logger = logging.getLogger(__name__)

# sale_result.json のパス（wakust-auto-sale リポジトリからアーティファクトで受け取る想定）
# GitHub Actions では actions/download-artifact で同ディレクトリに配置する
SALE_RESULT_FILE = Path(__file__).parent / "sale_result.json"


# ─────────────────────────────────────────
# テンプレート生成
# ─────────────────────────────────────────
def build_sale_tweet(article: dict) -> str:
    title = article.get("title", "")
    title = mask_title(title) 
    """
    セール告知ツイートのテキストを生成する

    article には以下のキーが必要:
        title       : 記事タイトル（セール文言なし）
        discount    : "50%OFF" or "90%OFF"
        sale_price  : セール後の価格（int, 円）
        url         : 記事URL
        sale_start  : セール開始日（YYYY-MM-DD）
        sale_end    : セール終了日（YYYY-MM-DD）
    """
    title = article.get("title", "")
    discount = article.get("discount", "")
    sale_price = article.get("sale_price", "")
    url = article.get("url", "")
    sale_start = article.get("sale_start", "")
    sale_end = article.get("sale_end", "")

    # 日付フォーマット（YYYY-MM-DD → M/D）
    def fmt_date(d: str) -> str:
        if not d:
            return d
        parts = d.split("-")
        if len(parts) == 3:
            return f"{int(parts[1])}/{int(parts[2])}"
        return d

    tweet = (
        f"🔥今週のセール🔥\n\n"
        f"「{title}」が{discount}で登場！\n"
        f"💰 セール価格：{sale_price}円\n"
        f"📅 期間：{fmt_date(sale_start)}〜{fmt_date(sale_end)}\n\n"
        f"👉 {url}\n\n"
        f"{HASHTAG_COMMON} {HASHTAG_SALE}"
    )
    return tweet


# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────
def main(dry_run: bool = False) -> None:
    logger.info("=== セール告知ツイート開始 ===")

    # sale_result.json 読み込み
    if not SALE_RESULT_FILE.exists():
        logger.error(f"{SALE_RESULT_FILE} が見つかりません。セールスクリプトが正常に実行されたか確認してください。")
        sys.exit(1)

    with open(SALE_RESULT_FILE, encoding="utf-8") as f:
        sale_result = json.load(f)

    sale_articles = sale_result.get("sale_articles", [])
    if not sale_articles:
        logger.info("セール対象記事なし。ツイートをスキップします。")
        return

    client = get_x_client() if not dry_run else None
    history = load_history()
    tweeted_count = 0

    for article in sale_articles:
        post_id = str(article.get("post_id", ""))
        title = article.get("title", "")

        # 重複チェック
        if was_sale_already_tweeted(history, post_id):
            logger.info(f"スキップ（投稿済み）: {title}")
            continue

        tweet_text = build_sale_tweet(article)
        logger.info(f"投稿準備:\n{tweet_text}\n{'─'*40}")

        tweet_id = post_tweet(client, tweet_text, dry_run=dry_run)

        if tweet_id:
            add_to_history(history, tweet_id, post_id, "sale", title)
            tweeted_count += 1
        else:
            logger.error(f"投稿失敗: {title}")

    save_history(history)
    logger.info(f"=== セール告知ツイート完了: {tweeted_count}件投稿 ===")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
