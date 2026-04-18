#!/usr/bin/env python3
"""
Trump Truth Social → 日本語翻訳 → Bluesky投稿 ボット
trumpstruth.org のRSSフィードを監視し、新規投稿を翻訳してBlueskyに投稿する
"""

import feedparser
import requests
import httpx
import json
import os
import re
import certifi
import time
import anthropic
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

# --- API Keys ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Bluesky ---
BSKY_HANDLE = os.environ.get("BSKY_HANDLE", "trump-ts-jp.bsky.social")
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD", "")
BSKY_API = "https://bsky.social/xrpc"
_proxy_url = os.environ.get("PROXY_URL", "http://localhost:50717" if os.environ.get("GITHUB_ACTIONS") != "true" else "")
BSKY_PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None
# プロキシを使わない接続（trumpstruth.org, bsky.social など）
NO_PROXY = {"http": "", "https": ""}

RSS_URL = "https://www.trumpstruth.org/feed"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_FILE = os.path.join(SCRIPT_DIR, "trump_processed.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "trump_translator.log")

JST = timezone(timedelta(hours=9))
BSKY_MAX_LENGTH = 300  # Blueskyの文字数上限（grapheme単位）


def log(msg):
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')
    line = f"[{now}] {msg}"
    print(line)


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return json.load(f)
    return []


def save_processed(processed):
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(processed[-500:], f)


def grapheme_len(text):
    """Blueskyの文字数カウント（grapheme単位、日本語も1文字=1）"""
    return len(text)


def split_for_posts(text):
    """テキストをBluesky投稿用に分割する"""
    max_len = BSKY_MAX_LENGTH

    if grapheme_len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if grapheme_len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # 分割点を探す（句点、改行、またはlimit）
        best = 0
        for i, ch in enumerate(remaining):
            if i >= max_len:
                break
            if ch in ('。', '\n'):
                best = i + 1
        if best == 0:
            best = min(max_len, len(remaining))

        chunks.append(remaining[:best])
        remaining = remaining[best:].lstrip('\n')

    return chunks


def extract_facets(text):
    """テキスト内のURLをBluesky richtext facetとして返す（byteオフセット）"""
    facets = []
    for m in re.finditer(r'https?://[^\s]+', text):
        url = m.group()
        byte_start = len(text[:m.start()].encode('utf-8'))
        byte_end = len(text[:m.end()].encode('utf-8'))
        facets.append({
            '$type': 'app.bsky.richtext.facet',
            'index': {
                '$type': 'app.bsky.richtext.facet#byteSlice',
                'byteStart': byte_start,
                'byteEnd': byte_end
            },
            'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': url}]
        })
    return facets


def get_ts_post_id(trumpstruth_url):
    """trumpstruth.orgのページからTruth Social投稿IDを取得"""
    try:
        resp = requests.get(
            trumpstruth_url,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            proxies=NO_PROXY,
            verify=certifi.where(),
            timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        link = soup.find('a', class_='status__external-link')
        if link:
            href = link.get('href', '')
            m = re.search(r'/(\d+)$', href)
            if m:
                return m.group(1)
    except Exception as e:
        log(f"trumpstruth.orgページ取得エラー: {e}")
    return None


def get_ts_media(post_id):
    """Truth Social APIからメディア添付を取得（direct優先、失敗時proxy）"""
    url = f'https://truthsocial.com/api/v1/statuses/{post_id}'
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    try:
        resp = requests.get(url, headers=headers, proxies=NO_PROXY, verify=certifi.where(), timeout=30)
        resp.raise_for_status()
    except Exception:
        if not BSKY_PROXIES:
            raise
        resp = requests.get(url, headers=headers, proxies=BSKY_PROXIES, verify=certifi.where(), timeout=30)
        resp.raise_for_status()
    data = resp.json()
    video_url = None
    image_urls = []
    seen_urls = set()
    for att in data.get('media_attachments', []):
        att_type = att.get('type', '')
        url = att.get('url', '')
        if att_type in ('video', 'gifv') and not video_url:
            video_url = url
        elif att_type == 'image' and url not in seen_urls and len(image_urls) < 4:
            image_urls.append(url)
            seen_urls.add(url)
    return video_url, image_urls


def extract_images(html_content):
    """RSS HTMLから画像URLを抽出（最大4枚）"""
    soup = BeautifulSoup(html_content, 'html.parser')
    images = []
    seen_urls = set()
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if src and src.startswith('http') and src not in seen_urls:
            images.append(src)
            seen_urls.add(src)
            if len(images) >= 4:
                break
    return images


def scrape_images_from_page(status_url):
    """trumpstruth.orgのステータスページから投稿画像URLを抽出（最大4枚）"""
    try:
        resp = requests.get(
            status_url,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            proxies=NO_PROXY,
            verify=certifi.where(),
            timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        images = []
        seen_urls = set()
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if not src or not src.startswith('http'):
                continue
            # ロゴ・アバター・サムネイル画像を除外
            if '/logo.svg' in src or '/avatars/' in src or '/small/' in src:
                continue
            if src in seen_urls:
                continue
            images.append(src)
            seen_urls.add(src)
            if len(images) >= 4:
                break
        return images
    except Exception as e:
        log(f"ページスクレイピングエラー: {e}")
        return []


def extract_video(html_content):
    """RSS HTMLから動画URLを抽出（最初の1件）"""
    soup = BeautifulSoup(html_content, 'html.parser')
    for video in soup.find_all('video'):
        src = video.get('src', '')
        if src and src.startswith('http'):
            return src
        source = video.find('source')
        if source:
            src = source.get('src', '')
            if src and src.startswith('http'):
                return src
    return None


def upload_video_to_bsky(video_url, did, token):
    """動画をダウンロードしてBlueskyにアップロード、blobを返す"""
    MAX_SIZE = 50 * 1024 * 1024  # 50MB

    # ファイルサイズ事前確認（direct優先、失敗時はproxy）
    try:
        head = requests.head(video_url, proxies=NO_PROXY, timeout=15, allow_redirects=True)
    except Exception:
        if not BSKY_PROXIES:
            raise
        head = requests.head(video_url, proxies=BSKY_PROXIES, timeout=15, allow_redirects=True)
    size = int(head.headers.get('content-length', 0))
    if size > MAX_SIZE:
        raise ValueError(f"動画サイズ超過: {size / 1024 / 1024:.1f}MB > 50MB")

    try:
        resp = requests.get(video_url, proxies=NO_PROXY, timeout=120)
        resp.raise_for_status()
    except Exception:
        if not BSKY_PROXIES:
            raise
        resp = requests.get(video_url, proxies=BSKY_PROXIES, timeout=120)
        resp.raise_for_status()

    if len(resp.content) > MAX_SIZE:
        raise ValueError(f"動画サイズ超過: {len(resp.content) / 1024 / 1024:.1f}MB > 50MB")

    content_type = resp.headers.get('content-type', 'video/mp4').split(';')[0]
    upload_resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.uploadBlob",
        data=resp.content,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': content_type
        },
        proxies=NO_PROXY,
        timeout=60
    )
    upload_resp.raise_for_status()
    return upload_resp.json()['blob']


def upload_image_to_bsky(image_url, did, token):
    """画像をダウンロードしてBlueskyにアップロード、blobを返す（direct優先、失敗時proxy）"""
    try:
        resp = requests.get(image_url, proxies=NO_PROXY, timeout=30)
        resp.raise_for_status()
    except Exception:
        if not BSKY_PROXIES:
            raise
        resp = requests.get(image_url, proxies=BSKY_PROXIES, timeout=30)
        resp.raise_for_status()
    content_type = resp.headers.get('content-type', 'image/jpeg').split(';')[0]
    upload_resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.uploadBlob",
        data=resp.content,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': content_type
        },
        proxies=NO_PROXY,
        timeout=30
    )
    upload_resp.raise_for_status()
    return upload_resp.json()['blob']


def translate_with_claude(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "以下はトランプ大統領のTruth Social投稿です。日本語に翻訳してください。\n"
        "ルール：\n"
        "- 自然な日本語にする\n"
        "- 投稿のトーンや強調（大文字表現など）を維持する\n"
        "- 翻訳のみを出力し、解説や注釈は不要\n\n"
        f"【原文】\n{text}"
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        error_str = str(e)
        log(f"Claude API エラー: {error_str}")
        if "429" in error_str or "overloaded" in error_str.lower():
            return "RATE_LIMITED"
        return None


def bsky_login():
    """Blueskyにログインしてセッション情報を返す"""
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        proxies=NO_PROXY, timeout=30
    )
    resp.raise_for_status()
    session = resp.json()
    return session['did'], session['accessJwt']


def post_to_bluesky(chunks, did, token, image_blobs=None, video_blob=None):
    """Blueskyに投稿する。複数チャンクの場合はスレッドにする"""
    root_ref = None
    parent_ref = None

    for i, chunk in enumerate(chunks):
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        record = {
            '$type': 'app.bsky.feed.post',
            'text': chunk,
            'createdAt': now,
            'langs': ['ja']
        }

        facets = extract_facets(chunk)
        if facets:
            record['facets'] = facets

        # 最初の投稿にのみメディアを添付（動画優先、なければ画像）
        if i == 0:
            if video_blob:
                record['embed'] = {
                    '$type': 'app.bsky.embed.video',
                    'video': video_blob,
                    'alt': ''
                }
            elif image_blobs:
                record['embed'] = {
                    '$type': 'app.bsky.embed.images',
                    'images': [{'image': blob, 'alt': ''} for blob in image_blobs]
                }

        # スレッド（リプライ）の場合
        if parent_ref is not None:
            record['reply'] = {
                'root': root_ref,
                'parent': parent_ref
            }

        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.createRecord",
            json={
                'repo': did,
                'collection': 'app.bsky.feed.post',
                'record': record
            },
            headers={'Authorization': f'Bearer {token}'},
            proxies=NO_PROXY, timeout=30
        )
        resp.raise_for_status()
        result = resp.json()

        ref = {'uri': result['uri'], 'cid': result['cid']}
        if i == 0:
            root_ref = ref
        parent_ref = ref

        if i < len(chunks) - 1:
            time.sleep(1)

    return result['uri']


def main():
    log("=== Trump翻訳ボット 実行開始 ===")

    # RSS取得（プロキシ環境変数を無視してdirect接続）
    try:
        rss_resp = requests.get(
            RSS_URL,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            proxies=NO_PROXY,
            verify=certifi.where(),
            timeout=30
        )
        rss_resp.raise_for_status()
        feed = feedparser.parse(rss_resp.content)
    except Exception as e:
        log(f"RSSフィード取得エラー: {e}")
        return

    if not feed.entries:
        log("RSSフィードのエントリなし")
        return

    log(f"RSSフィード取得成功: {len(feed.entries)}件")

    processed = load_processed()
    new_posts = []

    for entry in reversed(feed.entries):  # 古い順に処理
        post_id = entry.get('id') or entry.get('link', '')
        if post_id in processed:
            continue

        content = entry.get('description') or entry.get('summary', '')
        if not content:
            processed.append(post_id)
            continue

        soup = BeautifulSoup(content, 'html.parser')
        text = soup.get_text(separator='\n').strip()

        if not text:
            processed.append(post_id)
            continue

        # Truth Social APIからメディア取得（プロキシ経由）、失敗時はRSS HTMLにフォールバック
        video_url = None
        image_urls = []
        ts_post_id = get_ts_post_id(entry.get('link', ''))
        if ts_post_id:
            try:
                video_url, image_urls = get_ts_media(ts_post_id)
                log(f"TS APIメディア: 動画={'あり' if video_url else 'なし'}, 画像{len(image_urls)}枚")
            except Exception as e:
                log(f"Truth Social APIエラー（フォールバック）: {e}")
                video_url = extract_video(content)
                if not video_url:
                    status_link = entry.get('link', '')
                    image_urls = scrape_images_from_page(status_link) if status_link else []
                    if not image_urls:
                        image_urls = extract_images(content)
        else:
            video_url = extract_video(content)
            if not video_url:
                status_link = entry.get('link', '')
                image_urls = scrape_images_from_page(status_link) if status_link else []
                if not image_urls:
                    image_urls = extract_images(content)

        # URL重複排除（同じ画像が複数回添付されるのを防ぐ）
        seen = set()
        image_urls = [u for u in image_urls if not (u in seen or seen.add(u))]

        new_posts.append({
            'id': post_id,
            'text': text,
            'link': entry.get('link', ''),
            'published': entry.get('published', ''),
            'video_url': video_url,
            'image_urls': image_urls
        })

    if not new_posts:
        log("新規投稿なし")
        save_processed(processed)
        return

    log(f"新規投稿: {len(new_posts)}件")

    # Blueskyログイン
    try:
        did, token = bsky_login()
        log(f"Blueskyログイン成功 (DID: {did})")
    except Exception as e:
        log(f"Blueskyログインエラー: {e}")
        return

    for post in new_posts:
        log(f"翻訳中: {post['text'][:80]}...")

        # URLを除いた本文のみをClaudeに渡す（URLがあるとアクセス拒否メッセージを返すため）
        text_body = re.sub(r'https?://\S+', '', post['text']).strip()
        if not text_body:
            translation = ""
            log("本文なし（メディアのみまたはURLのみ）のため翻訳スキップ")
        else:
            translation = translate_with_claude(text_body)
        if translation == "RATE_LIMITED":
            log("Claude APIレート制限、残りの投稿は次回処理")
            save_processed(processed)
            return
        if translation is None:
            log("翻訳失敗、スキップ")
            processed.append(post['id'])
            save_processed(processed)
            continue

        # 動画または画像をアップロード（動画優先）
        video_blob = None
        image_blobs = []
        if post.get('video_url'):
            try:
                video_blob = upload_video_to_bsky(post['video_url'], did, token)
                log(f"動画アップロード成功: {post['video_url'][:60]}")
            except Exception as e:
                log(f"動画アップロード失敗（スキップ）: {e}")
        else:
            for url in post.get('image_urls', []):
                try:
                    blob = upload_image_to_bsky(url, did, token)
                    image_blobs.append(blob)
                    log(f"画像アップロード成功: {url[:60]}")
                except Exception as e:
                    log(f"画像アップロード失敗（スキップ）: {e}")

        media_info = f"動画あり" if video_blob else f"画像{len(image_blobs)}枚"
        chunks = split_for_posts(translation)
        log(f"Bluesky投稿中 ({len(chunks)}ポスト, {media_info}): {translation[:80]}...")

        try:
            post_uri = post_to_bluesky(chunks, did, token, image_blobs, video_blob)
            log(f"投稿成功 (URI: {post_uri})")
        except Exception as e:
            log(f"Bluesky投稿エラー: {e}")

        processed.append(post['id'])
        save_processed(processed)
        time.sleep(3)

    save_processed(processed)
    log("=== 完了 ===")


if __name__ == "__main__":
    main()
