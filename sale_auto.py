#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wakust.com 自動セールスクリプト (ヘッドレスブラウザ対応)

【動作概要】
- 毎週1回実行し、各カテゴリーからランダムに3件ずつセール対象を選定
- 売上総額に応じて割引率を決定:
    - 売上 10,000pt 以上 → 半額（50%OFF）
    - 売上 10,000pt 未満 → 90%OFF
- 記事タイトルの先頭にセール文言を追加
- 無料部分の本文先頭にセール告知バナーを追記
- 次回実行時に前回のセール対象を元のタイトル・価格・本文に復元してから、新しいセールを開始

【実行モード】
- start_sale  : 新しいセールを開始（前回セール分は自動復元）
- restore_only: セールを終了し、元に戻すのみ（新規セールなし）

【スケジュール例（GitHub Actions / cron）】
- 毎週月曜 深夜1時に start_sale で実行
  cron: '0 1 * * 1'
"""

import json
import os
import sys
import time
import re
import random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ===== 設定 =====
BASE_URL = "https://wakust.com"
LOGIN_URL = f"{BASE_URL}/login/"
MYPAGE_URL = f"{BASE_URL}/mypage/"
EMAIL = os.environ.get("WAKUST_EMAIL", "fitersowner@gmail.com")
PASSWORD = os.environ.get("WAKUST_PASSWORD", "ryutaro0408")

# 対象カテゴリー（名前: カテゴリーID）
CATEGORIES = {
    "東京都": "4",
    "神奈川県": "1245",
    "埼玉県": "2442",
    "千葉県": "2441",
    "新宿": "24476",
    "池袋": "24474",
}

SALE_TOTAL_COUNT = 2  # セール対象にする記事の合計数（全カテゴリーから合計）
EXCLUDE_RECENT_DAYS = 60  # リリースからこの日数以内の記事はセール対象外
SALE_COOLDOWN_DAYS = 90  # 同じ記事を再セールするまでの最低間隔（日）
PAGE_WAIT = 3000  # ms

# === セール文言の設定 ===

# タイトル先頭に追加する文言
SALE_TITLE_PREFIX = "🔥今週のセール品🔥 "

# セール期間（日数）：実行日から何日間のセールか
SALE_DURATION_DAYS = 7


def make_sale_banner_html(start_date, end_date):
    """セール期間の日付入りバナーHTMLを生成する"""
    start_str = start_date.strftime("%Y年%m月%d日")
    end_str = end_date.strftime("%Y年%m月%d日")
    return (
        '<p style="background:#ff4444;color:#fff;padding:12px 16px;'
        'border-radius:8px;font-size:16pt;font-weight:bold;text-align:center;'
        'margin-bottom:16px;">'
        f'🔥 期間限定セール開催中！（{start_str}〜{end_str}）今だけ特別価格でご提供中です！ 🔥'
        '</p>'
    )

# 売上しきい値（pt）
SALES_THRESHOLD = 10000  # これ以上は半額、未満は90%OFF

# ファイルパス
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "sale_log.txt")
STATE_FILE = os.path.join(SCRIPT_DIR, "sale_state.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "sale_history.json")
RESULT_FILE = os.path.join(SCRIPT_DIR, "sale_result.json")


# ===== ユーティリティ =====

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    sys.stdout.flush()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_sale_state():
    """前回のセール状態を読み込む"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"active_sales": []}


def save_sale_state(state):
    """セール状態を保存する"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_sale_history():
    """セール履歴を読み込む（post_id → 最終セール日のリスト）"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_sale_history(history):
    """セール履歴を保存する"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_cooldown_post_ids(history):
    """クールダウン期間中のpost_idのセットを返す"""
    cooldown_ids = set()
    cutoff = datetime.now() - timedelta(days=SALE_COOLDOWN_DAYS)
    for post_id, dates in history.items():
        # 最新のセール日を確認
        latest = max(dates) if dates else None
        if latest:
            try:
                latest_dt = datetime.strptime(latest, "%Y-%m-%d")
                if latest_dt > cutoff:
                    cooldown_ids.add(post_id)
            except ValueError:
                pass
    return cooldown_ids


def record_sale_history(history, post_ids):
    """今回セールした記事を履歴に追加する"""
    today = datetime.now().strftime("%Y-%m-%d")
    for post_id in post_ids:
        if post_id not in history:
            history[post_id] = []
        history[post_id].append(today)
    return history


def dismiss_age_modal(page):
    """年齢確認モーダルをJavaScriptで閉じる"""
    page.evaluate("""
        () => {
            var modal = document.getElementById('age-verification-modal');
            if (modal) {
                modal.style.display = 'none';
                modal.remove();
            }
            var overlays = document.querySelectorAll('.modal-backdrop, .overlay, [class*="overlay"]');
            overlays.forEach(function(el) { el.remove(); });
            document.body.classList.remove('modal-open');
            document.body.style.overflow = '';
            document.body.style.paddingRight = '';
        }
    """)


# ===== ログイン =====

def ensure_login(page):
    """ログイン状態を確認し、必要ならログインする"""
    log("サイトにアクセスしてログイン状態を確認中...")
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    page_text = page.content()
    if "ryu-1992" in page_text or "mypage" in page.url:
        log("ログイン済みを確認しました")
        return True

    log("ログアウト状態です。ログインします...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    # 年齢確認モーダルを閉じる
    dismiss_age_modal(page)
    page.wait_for_timeout(500)

    # メールアドレス入力
    email_input = page.locator("input[type='email'], input[name*='mail'], input[name*='user']").first
    if email_input.count() == 0:
        email_input = page.locator("input[type='text']").last
    email_input.fill(EMAIL)

    # パスワード入力
    page.locator("input[type='password']").last.fill(PASSWORD)

    # ログインボタンをクリック
    page.evaluate("""
        () => {
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = btns[i].textContent.trim();
                if (t === 'ログイン' || t.includes('ログイン')) {
                    btns[i].click();
                    return 'clicked: ' + t;
                }
            }
            return 'not found';
        }
    """)
    page.wait_for_timeout(5000)

    current_content = page.content()
    if "ryu-1992" in current_content or "mypage" in page.url:
        log("ログイン成功")
        return True
    else:
        log(f"ログイン失敗: {page.url}")
        return False


# ===== 記事一覧取得（売上情報付き）=====

def get_post_list_with_sales(page, cat_id, cat_name):
    """カテゴリーの投稿一覧（販売中のみ）と売上情報を取得する"""
    log(f"カテゴリー「{cat_name}」の記事一覧を取得中...")

    url = f"{MYPAGE_URL}?post_list&sort=date&cat={cat_id}&p_n=0&p_s=1&lmt=100"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    # 各行から post_id, 販売ステータス, 売上情報を取得
    rows_data = page.evaluate("""
        () => {
            var result = [];
            var rows = document.querySelectorAll('table tbody tr');
            rows.forEach(function(tr) {
                // 編集リンクからpost_idを取得
                var editLink = tr.querySelector('a[href*="post_edit="]');
                if (!editLink) return;
                var href = editLink.getAttribute('href');
                var m = href.match(/post_edit=(\\d+)/);
                if (!m) return;
                var postId = m[1];

                // 販売ステータス
                var selects = tr.querySelectorAll('select');
                var saleStatus = null;
                selects.forEach(function(s) {
                    var opts = Array.from(s.options).map(function(o) { return o.text; });
                    if (opts.indexOf('販売中') >= 0 || opts.indexOf('販売停止') >= 0) {
                        if (saleStatus === null) {
                            saleStatus = s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : null;
                        }
                    }
                });

                // 売上情報を td.td_4 から取得
                var salesTd = tr.querySelector('td.td_4');
                var salesAmount = 0;
                var salesCount = 0;
                if (salesTd) {
                    var tdText = salesTd.textContent;
                    // "売上：6,000pt" のようなパターンから数値を取得
                    var salesMatch = tdText.match(/売上[：:]\\s*([\\d,]+)\\s*pt/);
                    if (salesMatch) {
                        salesAmount = parseInt(salesMatch[1].replace(/,/g, ''), 10);
                    }
                    // "販売回数：4" のパターン
                    var countMatch = tdText.match(/販売回数[：:]\\s*(\\d+)/);
                    if (countMatch) {
                        salesCount = parseInt(countMatch[1], 10);
                    }
                }

                // 投稿日時
                var cellText = tr.textContent;
                var dateMatches = cellText.match(/(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2})/g);
                var postDate = dateMatches ? dateMatches[0] : null;

                // 予約かどうか
                var isReserved = cellText.indexOf('予約') >= 0;

                result.push({
                    post_id: postId,
                    sale_status: saleStatus,
                    sales_amount: salesAmount,
                    sales_count: salesCount,
                    post_date: postDate,
                    is_reserved: isReserved
                });
            });
            return result;
        }
    """)

    posts = []
    cutoff_date = datetime.now() - timedelta(days=EXCLUDE_RECENT_DAYS)
    skipped_recent = 0

    for row in rows_data:
        post_id = row.get('post_id')
        sale_status = row.get('sale_status')
        is_reserved = row.get('is_reserved', False)
        post_date_str = row.get('post_date')

        if not post_id:
            continue
        if is_reserved:
            continue
        if sale_status == '販売停止':
            continue

        # 直近2ヶ月以内にリリースされた記事はセール対象外
        if post_date_str:
            try:
                post_date = datetime.strptime(post_date_str, "%Y-%m-%d %H:%M")
                if post_date > cutoff_date:
                    skipped_recent += 1
                    continue
            except ValueError:
                pass  # パースに失敗した場合はスキップせず含める

        posts.append({
            "post_id": post_id,
            "cat_id": cat_id,
            "cat_name": cat_name,
            "sales_amount": row.get('sales_amount', 0),
            "sales_count": row.get('sales_count', 0),
            "post_date": post_date_str,
        })

    log(f"  販売中記事数: {len(posts)}件（直近{EXCLUDE_RECENT_DAYS}日除外: {skipped_recent}件）")
    return posts


# ===== 編集ページ操作 =====

def get_article_details(page, post_id):
    """記事の編集ページから現在のタイトル・価格・無料本文を取得する"""
    edit_url = f"{MYPAGE_URL}?post_edit={post_id}"
    page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)
    dismiss_age_modal(page)
    page.wait_for_timeout(500)

    # 現在のタイトルを取得 (name="edit_title", id="Input")
    current_title = page.evaluate("""
        () => {
            var input = document.querySelector('input[name="edit_title"]');
            if (!input) input = document.getElementById('Input');
            return input ? input.value : null;
        }
    """)

    # 現在の価格を取得 (name="post_price", id="Input2")
    current_price = page.evaluate("""
        () => {
            var input = document.querySelector('input[name="post_price"]');
            if (!input) input = document.getElementById('Input2');
            return input ? parseInt(input.value, 10) || 0 : null;
        }
    """)

    # TinyMCE iframe内の無料本文を取得
    free_body_html = page.evaluate("""
        () => {
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                try {
                    var doc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                    var body = doc.querySelector('body#tinymce, body.mce-content-body');
                    if (body) {
                        return body.innerHTML;
                    }
                } catch(e) {}
            }
            var textarea = document.querySelector('textarea[name*="free"], textarea[name*="body"]');
            return textarea ? textarea.value : null;
        }
    """)

    return {
        "current_title": current_title,
        "current_price": current_price,
        "free_body_html": free_body_html,
    }


def update_article(page, post_id, new_title, new_price, new_free_body_html):
    """記事のタイトル・価格・無料本文を更新して保存する"""
    edit_url = f"{MYPAGE_URL}?post_edit={post_id}"
    page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)
    dismiss_age_modal(page)
    page.wait_for_timeout(500)

    # タイトルを更新 (name="edit_title", id="Input")
    escaped_title = new_title.replace("'", "\\'").replace('"', '\\"')
    title_result = page.evaluate(f"""
        () => {{
            var input = document.querySelector('input[name="edit_title"]');
            if (!input) input = document.getElementById('Input');
            if (!input) return 'TITLE_INPUT_NOT_FOUND';

            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(input, '{escaped_title}');
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));

            return 'TITLE_SET: ' + input.value.substring(0, 50);
        }}
    """)
    log(f"    タイトル更新: {title_result}")

    # 価格を更新 (name="post_price", id="Input2")
    price_result = page.evaluate(f"""
        () => {{
            var input = document.querySelector('input[name="post_price"]');
            if (!input) input = document.getElementById('Input2');
            if (!input) return 'PRICE_INPUT_NOT_FOUND';

            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(input, '{new_price}');
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));

            return 'PRICE_SET: ' + input.value;
        }}
    """)
    log(f"    価格更新: {price_result}")

    # TinyMCE iframe内の無料本文を更新
    escaped_html = new_free_body_html.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
    body_result = page.evaluate(f"""
        () => {{
            var newHtml = `{escaped_html}`;
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {{
                try {{
                    var doc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                    var body = doc.querySelector('body#tinymce, body.mce-content-body');
                    if (body) {{
                        body.innerHTML = newHtml;
                        if (typeof tinymce !== 'undefined' && tinymce.activeEditor) {{
                            tinymce.activeEditor.setContent(newHtml);
                        }}
                        return 'BODY_UPDATED';
                    }}
                }} catch(e) {{}}
            }}
            return 'IFRAME_NOT_FOUND';
        }}
    """)
    log(f"    本文更新: {body_result}")

    page.wait_for_timeout(1000)

    # 投稿確認ボタンをクリック
    page.evaluate("""
        () => {
            var btn = document.getElementById('submit_edit_s');
            if (btn) { btn.click(); return; }
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === '投稿確認') {
                    btns[i].click();
                    return;
                }
            }
        }
    """)
    page.wait_for_timeout(2000)

    # モーダル内の「投稿する」ボタンをクリック
    submit_result = page.evaluate("""
        () => {
            var modals = document.querySelectorAll('.modal, [class*="modal"], [id*="modal"]');
            for (var i = 0; i < modals.length; i++) {
                var modal = modals[i];
                var style = window.getComputedStyle(modal);
                if (style.display !== 'none' && style.visibility !== 'hidden') {
                    var btns = modal.querySelectorAll('button, input[type="submit"]');
                    for (var j = 0; j < btns.length; j++) {
                        var btn = btns[j];
                        var txt = btn.textContent.trim();
                        if (txt === '投稿する' || txt === '投稿' || txt === '確定') {
                            btn.click();
                            return 'CLICKED_IN_MODAL: ' + txt;
                        }
                    }
                }
            }
            var allBtns = document.querySelectorAll('button');
            for (var k = 0; k < allBtns.length; k++) {
                var b = allBtns[k];
                var t = b.textContent.trim();
                if (t === '投稿する') {
                    b.click();
                    return 'CLICKED_GLOBAL: ' + t;
                }
            }
            return 'NOT_FOUND';
        }
    """)
    log(f"    投稿ボタン: {submit_result}")
    page.wait_for_timeout(5000)

    success = (
        title_result != 'TITLE_INPUT_NOT_FOUND'
        and price_result != 'PRICE_INPUT_NOT_FOUND'
        and body_result != 'IFRAME_NOT_FOUND'
    )
    return success


def calculate_sale_price(original_price, sales_amount):
    """売上総額に応じてセール価格を計算する"""
    if sales_amount >= SALES_THRESHOLD:
        # 売上1万pt以上 → 半額（50%OFF）
        sale_price = max(int(original_price * 0.5), 100)
        discount_label = "50%OFF"
    else:
        # 売上1万pt未満 → 90%OFF
        sale_price = max(int(original_price * 0.1), 100)
        discount_label = "90%OFF"

    # 100円単位に丸める
    sale_price = max(round(sale_price / 100) * 100, 100)

    return sale_price, discount_label


# ===== セール復元 =====

def restore_articles(page, state):
    """前回セール対象の記事を元のタイトル・価格・本文に復元する"""
    active_sales = state.get("active_sales", [])
    if not active_sales:
        log("復元対象の記事はありません")
        return []

    log(f"前回セール対象 {len(active_sales)}件 を復元します...")
    restore_results = []

    for sale_info in active_sales:
        post_id = sale_info["post_id"]
        original_title = sale_info["original_title"]
        original_price = sale_info["original_price"]
        original_body = sale_info["original_free_body_html"]
        cat_name = sale_info.get("cat_name", "不明")

        log(f"  記事 ID={post_id}（{cat_name}）を復元中...")
        log(f"    元のタイトル: {original_title[:50]}...")
        log(f"    元の価格: {original_price}円")

        success = update_article(page, post_id, original_title, original_price, original_body)

        if success:
            log(f"    復元成功!")
            restore_results.append({"post_id": post_id, "cat_name": cat_name, "success": True})
        else:
            log(f"    復元失敗")
            restore_results.append({"post_id": post_id, "cat_name": cat_name, "success": False})

        time.sleep(2)

    return restore_results


# ===== セール開始 =====

def start_sale_for_articles(page, selected_posts, sale_start_date, sale_end_date):
    """選定された記事にセールを適用する"""
    new_active_sales = []
    sale_results = []

    # セール期間入りバナーHTMLを生成
    sale_banner_html = make_sale_banner_html(sale_start_date, sale_end_date)

    for post in selected_posts:
        post_id = post["post_id"]
        cat_name = post["cat_name"]
        sales_amount = post["sales_amount"]

        log(f"  記事 ID={post_id}（{cat_name}）のセール設定中...")

        # 現在のタイトル・価格・本文を取得
        details = get_article_details(page, post_id)
        if details["current_price"] is None:
            log(f"    価格の取得に失敗。スキップします。")
            sale_results.append({"post_id": post_id, "cat_name": cat_name, "success": False, "error": "価格取得失敗"})
            continue
        if details["current_title"] is None:
            log(f"    タイトルの取得に失敗。スキップします。")
            sale_results.append({"post_id": post_id, "cat_name": cat_name, "success": False, "error": "タイトル取得失敗"})
            continue

        original_title = details["current_title"]
        original_price = details["current_price"]
        original_body = details["free_body_html"] or ""

        # セール価格を計算
        sale_price, discount_label = calculate_sale_price(original_price, sales_amount)
        log(f"    元タイトル: {original_title[:50]}...")
        log(f"    元価格: {original_price}円 → セール価格: {sale_price}円 ({discount_label})")
        log(f"    売上総額: {sales_amount}pt")

        # タイトルにセール文言を追加（すでに付いていない場合のみ）
        if original_title.startswith(SALE_TITLE_PREFIX):
            new_title = original_title
        else:
            new_title = SALE_TITLE_PREFIX + original_title

        # 本文にセールバナーを追記（先頭に挿入・期間日付入り）
        new_body = sale_banner_html + original_body

        log(f"    新タイトル: {new_title[:60]}...")

        # 記事を更新
        success = update_article(page, post_id, new_title, sale_price, new_body)

        if success:
            log(f"    セール適用成功!")
            new_active_sales.append({
                "post_id": post_id,
                "cat_id": post["cat_id"],
                "cat_name": cat_name,
                "original_title": original_title,
                "original_price": original_price,
                "sale_price": sale_price,
                "discount_label": discount_label,
                "sales_amount": sales_amount,
                "original_free_body_html": original_body,
                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            sale_results.append({
                "post_id": post_id,
                "cat_name": cat_name,
                "success": True,
                "discount": discount_label,
                "original_title": original_title[:50],
            })
        else:
            log(f"    セール適用失敗")
            sale_results.append({"post_id": post_id, "cat_name": cat_name, "success": False, "error": "更新失敗"})

        time.sleep(2)

    return new_active_sales, sale_results


# ===== メイン処理 =====

def main():
    # 実行モードを取得（デフォルト: start_sale）
    mode = sys.argv[1] if len(sys.argv) > 1 else "start_sale"

    log("=" * 60)
    log(f"wakust.com 自動セールスクリプト開始 (モード: {mode})")
    log(f"実行サイクル: 毎週")
    log("=" * 60)

    # 前回のセール状態を読み込む
    state = load_sale_state()
    log(f"前回のセール対象記事数: {len(state.get('active_sales', []))}件")

    with sync_playwright() as p:
        log("ヘッドレスブラウザを起動中...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        restore_results = []
        sale_results = []
        new_active_sales = []

        try:
            # ログイン
            if not ensure_login(page):
                log("ログインに失敗しました。スクリプトを終了します。")
                return

            # ===== STEP 1: 前回セールの復元 =====
            log("")
            log("=" * 40)
            log("STEP 1: 前回セール記事の復元")
            log("=" * 40)
            restore_results = restore_articles(page, state)

            if mode == "restore_only":
                save_sale_state({"active_sales": [], "last_restored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                log("復元完了。セール状態をクリアしました。")

                result_data = {
                    "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": mode,
                    "restore_results": restore_results,
                    "sale_results": [],
                    "active_sale_count": 0,
                }
                with open(RESULT_FILE, "w", encoding="utf-8") as f:
                    json.dump(result_data, f, ensure_ascii=False, indent=2)
                log(f"結果を保存しました: {RESULT_FILE}")
                return

            # ===== STEP 2: セール対象の選定 =====
            log("")
            log("=" * 40)
            log("STEP 2: 新しいセール対象の選定")
            log("=" * 40)

            previous_post_ids = set(s["post_id"] for s in state.get("active_sales", []))

            # セール履歴からクールダウン中のpost_idを取得
            sale_history = load_sale_history()
            cooldown_ids = get_cooldown_post_ids(sale_history)
            if cooldown_ids:
                log(f"クールダウン中（直近{SALE_COOLDOWN_DAYS}日以内にセール済み）: {len(cooldown_ids)}件")

            # 全カテゴリーから候補を集める
            all_eligible = []
            for cat_name, cat_id in CATEGORIES.items():
                log(f"\nカテゴリー「{cat_name}」:")
                posts = get_post_list_with_sales(page, cat_id, cat_name)

                eligible = [p for p in posts
                            if p["post_id"] not in previous_post_ids
                            and p["post_id"] not in cooldown_ids]
                excluded_prev = len([p for p in posts if p["post_id"] in previous_post_ids])
                excluded_cool = len([p for p in posts if p["post_id"] in cooldown_ids and p["post_id"] not in previous_post_ids])
                log(f"  セール候補: {len(eligible)}件（前回除外: {excluded_prev}件, クールダウン除外: {excluded_cool}件）")
                all_eligible.extend(eligible)

            # 重複post_idを除去（複数カテゴリーに同じ記事がある場合）
            seen_ids = set()
            unique_eligible = []
            for p in all_eligible:
                if p["post_id"] not in seen_ids:
                    seen_ids.add(p["post_id"])
                    unique_eligible.append(p)

            log(f"\n全カテゴリー合計の候補: {len(unique_eligible)}件")

            # 合計からランダムに2件選定
            all_selected = random.sample(unique_eligible, min(SALE_TOTAL_COUNT, len(unique_eligible)))
            for s in all_selected:
                log(f"  → 選定: ID={s['post_id']}（{s['cat_name']}）, 売上={s['sales_amount']}pt")

            log(f"\n合計セール対象: {len(all_selected)}件")

            if not all_selected:
                log("セール対象の記事がありません。終了します。")
                save_sale_state({"active_sales": []})
                return

            # ===== STEP 3: セール適用 =====
            log("")
            log("=" * 40)
            log("STEP 3: セール価格・タイトル・文言の適用")
            log("=" * 40)

            sale_start_date = datetime.now()
            sale_end_date = sale_start_date + timedelta(days=SALE_DURATION_DAYS)
            log(f"  セール期間: {sale_start_date.strftime('%Y/%m/%d')} 〜 {sale_end_date.strftime('%Y/%m/%d')}")

            new_active_sales, sale_results = start_sale_for_articles(page, all_selected, sale_start_date, sale_end_date)

            # セール状態を保存
            save_sale_state({
                "active_sales": new_active_sales,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

            # セール履歴を更新（成功した記事のみ記録）
            success_post_ids = [s["post_id"] for s in new_active_sales]
            if success_post_ids:
                sale_history = record_sale_history(sale_history, success_post_ids)
                save_sale_history(sale_history)
                log(f"セール履歴を更新しました（{len(success_post_ids)}件追加）")

        finally:
            context.close()
            browser.close()

    # ===== 結果サマリー =====
    log("")
    log("=" * 60)
    log("処理完了 - 結果サマリー")
    log("=" * 60)

    if restore_results:
        log(f"\n【復元】")
        for r in restore_results:
            status = "✓" if r["success"] else "✗"
            log(f"  {status} [{r['cat_name']}] post_id={r['post_id']}")

    if mode == "start_sale":
        success_sales = [r for r in sale_results if r["success"]]
        fail_sales = [r for r in sale_results if not r["success"]]
        log(f"\n【新規セール】")
        log(f"成功: {len(success_sales)}件")
        for r in success_sales:
            title_preview = r.get('original_title', '')[:40]
            log(f"  ✓ [{r['cat_name']}] post_id={r['post_id']} ({r['discount']}) {title_preview}")
        log(f"失敗: {len(fail_sales)}件")
        for r in fail_sales:
            log(f"  ✗ [{r['cat_name']}] post_id={r['post_id']} - {r.get('error')}")

    result_data = {
        "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "restore_results": restore_results,
        "sale_results": sale_results,
        "active_sale_count": len(new_active_sales),
    }
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
    log(f"\n結果を保存しました: {RESULT_FILE}")


if __name__ == "__main__":
    main()
