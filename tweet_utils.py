"""
tweet_utils.py
X（Twitter）投稿共通ユーティリティ
- OAuth 1.0a 認証
- ツイート投稿（280文字対応）
- 投稿履歴管理（tweet_history.json）
- タイトルマスキング（Xルール対策）
"""

import os
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tweepy

# ─────────────────────────────────────────
# 定数
# ─────────────────────────────────────────
JST = timezone(timedelta(hours=9))
HISTORY_FILE = Path(__file__).parent / "tweet_history.json"
MAX_TWEET_LENGTH = 280

# ハッシュタグ
HASHTAG_COMMON = "#ワクスト"
HASHTAG_SALE = "#セール #割引"
HASHTAG_NEW = "#新着"

# カテゴリ→ハッシュタグのマッピング（リポスト用）
CATEGORY_HASHTAGS = {
    "東京都": "#東京",
    "神奈川県": "#神奈川",
    "埼玉県": "#埼玉",
    "千葉県": "#千葉",
    "新宿": "#新宿",
    "池袋": "#池袋",
    "多摩": "#多摩",
    "ノウハウ(ネット)": "#ノウハウ",
    "ノウハウ(リアル)": "#ノウハウ",
}

# ─────────────────────────────────────────
# タイトルマスキング（Xルール対策）
# ─────────────────────────────────────────
# NGワードリスト（部分一致で伏せ字化）
# 性的表現・過激な表現をマスキング
MASK_WORDS = [
    # 直接的な性的表現
    "本番", "NN", "生中", "中出し", "ドクドク", "ぶちまけ",
    "射精", "発射", "精子", "ザーメン", "フェラ", "パイズリ",
    "素股", "手コキ", "乳首", "おっぱい", "巨乳", "爆乳",
    "Eカップ", "Fカップ", "Gカップ", "Hカップ", "Iカップ", "Jカップ",
    "Dカップ",
    # 身体・行為の過激表現
    "鼠蹊部", "CKB", "BK", "半BK", "全BK",
    "密着", "跨", "挿入", "腰グラインド",
    "理性崩壊", "理性がぶっ壊", "理性を奪",
    "欲求不満", "変態", "痴女", "淫乱",
    # メンエス特有のNG表現
    "抜き", "ヌキ", "抜いて", "イかせ", "イった", "イキ",
    "エロい", "エロ", "テロ級にエロ",
    "ムチャクチャやった", "ぶっ壊", "襲いたく",
    "セクシー", "妖艶",
]

# 置換パターン（正規表現用）
# カップサイズは特別処理（例: "Hカップ" → "○カップ"）
CUP_PATTERN = re.compile(r'[A-K]カップ')


def mask_title(title: str) -> str:
    """
    タイトルからセンシティブな表現を伏せ字に置換する。
    X(Twitter)のルール違反を防止するためのフィルター。

    例:
        "【秋葉原/爆乳】20歳Eカップの..." → "【秋葉原】20歳○カップの..."
        "本番可能セラピスト攻略ガイド" → "○○可能セラピスト攻略ガイド"
    """
    masked = title

    # カップサイズの伏せ字化（"Hカップ" → "○カップ"）
    masked = CUP_PATTERN.sub('○カップ', masked)

    # NGワードの伏せ字化（長い順に処理して部分一致の問題を防ぐ）
    sorted_words = sorted(MASK_WORDS, key=len, reverse=True)
    for word in sorted_words:
        if word in masked:
            # 単語の長さに応じて○の数を調整（最低2文字）
            replacement = "○" * max(2, len(word))
            masked = masked.replace(word, replacement)

    # 連続する○を整理（○○○○○ → ○○）
    masked = re.sub(r'○{3,}', '○○', masked)

    # 空のカッコを除去（マスキングで中身が空になった場合）
    masked = re.sub(r'[【\[（\(][○\s]*[】\]）\)]', '', masked)
    masked = re.sub(r'/○○/', '/', masked)

    # 先頭末尾の不要な記号を整理
    masked = masked.strip('/ ')

    return masked


# ─────────────────────────────────────────
# ロギング設定
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# X API 認証
# ─────────────────────────────────────────
def get_x_client() -> tweepy.Client:
    """OAuth 1.0a で tweepy.Client を返す"""
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=True,
    )
    return client


# ─────────────────────────────────────────
# ツイート投稿（リトライ付き）
# ─────────────────────────────────────────
def post_tweet(client: tweepy.Client, text: str, dry_run: bool = False) -> str | None:
    """
    ツイートを投稿する。
    - 280文字超過時はタイトル部分を省略して調整
    - exponential backoff でリトライ（最大3回）
    - dry_run=True の場合は実際に投稿せずログ出力のみ

    Returns:
        tweet_id (str) or None
    """
    text = _truncate_tweet(text)

    if dry_run:
        logger.info(f"[DRY RUN] ツイート内容:\n{text}")
        return "dry_run_id"

    for attempt in range(3):
        try:
            response = client.create_tweet(text=text)
            tweet_id = str(response.data["id"])
            logger.info(f"ツイート投稿成功: {tweet_id}")
            return tweet_id
        except tweepy.errors.TooManyRequests:
            wait = 60 * (2 ** attempt)
            logger.warning(f"レートリミット。{wait}秒後にリトライ（{attempt + 1}/3）")
            time.sleep(wait)
        except tweepy.errors.TweepyException as e:
            logger.error(f"ツイート投稿失敗: {e}")
            return None

    logger.error("ツイート投稿: リトライ上限に達しました")
    return None


def _truncate_tweet(text: str) -> str:
    """280文字を超える場合、タイトル行を省略して収める"""
    if len(text) <= MAX_TWEET_LENGTH:
        return text

    lines = text.split("\n")
    # タイトル行（「」で囲まれた行）を短縮
    for i, line in enumerate(lines):
        if line.startswith("「") and line.endswith("」"):
            title = line[1:-1]
            max_title_len = MAX_TWEET_LENGTH - (len(text) - len(line)) - 5
            if max_title_len > 10:
                lines[i] = "「" + title[:max_title_len] + "…」"
            break

    truncated = "\n".join(lines)
    # それでも超える場合は末尾を削る
    if len(truncated) > MAX_TWEET_LENGTH:
        truncated = truncated[: MAX_TWEET_LENGTH - 1] + "…"
    return truncated


# ─────────────────────────────────────────
# 投稿履歴管理
# ─────────────────────────────────────────
def load_history() -> dict:
    """tweet_history.json を読み込む（なければ空を返す）"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"tweets": []}


def save_history(history: dict) -> None:
    """tweet_history.json を保存する"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    logger.info(f"履歴を保存: {HISTORY_FILE}")


def add_to_history(
    history: dict,
    tweet_id: str,
    post_id: str,
    category: str,
    title: str,
) -> None:
    """履歴にエントリを追加する"""
    history["tweets"].append(
        {
            "tweet_id": tweet_id,
            "post_id": str(post_id),
            "category": category,
            "title": title,
            "tweeted_at": datetime.now(JST).isoformat(),
        }
    )


def was_recently_tweeted(history: dict, post_id: str, min_interval_days: int = 14) -> bool:
    """
    指定記事が min_interval_days 以内に投稿済みかを確認する。
    （リポスト重複防止用）
    """
    cutoff = datetime.now(JST) - timedelta(days=min_interval_days)
    for entry in history["tweets"]:
        if entry.get("post_id") == str(post_id):
            tweeted_at = datetime.fromisoformat(entry["tweeted_at"])
            if tweeted_at > cutoff:
                return True
    return False


def was_sale_already_tweeted(history: dict, post_id: str) -> bool:
    """同じ記事のセール告知を既に投稿済みか確認する"""
    for entry in history["tweets"]:
        if entry.get("post_id") == str(post_id) and entry.get("category") == "sale":
            return True
    return False


def was_new_article_tweeted(history: dict, post_id: str) -> bool:
    """新着記事として既に投稿済みか確認する"""
    for entry in history["tweets"]:
        if entry.get("post_id") == str(post_id) and entry.get("category") == "new_article":
            return True
    return False
