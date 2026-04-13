#!/usr/bin/env python3
"""
Trump Truth Social → 日本語翻訳 → Bluesky投稿 ボット
trumpstruth.org のRSSフィードを監視し、新規投稿を翻訳してBlueskyに投稿する
GitHub Actions版：シークレットは環境変数から取得
"""

import feedparser
import requests
import json
import os
import re
import ssl
import certifi
import time
import urllib.request
from google import genai
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

MEDIA_ARCHIVE_DOMAIN = "truth-archive.us-iad-1.linodeobjects.com"
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov')

# --- 環境変数からシークレット取得 ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]

# --- Bluesky ---
BSKY_API = "https://bsky.social/xrpc"

RSS_URL = "https://www.trumpstruth.org/feed"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_FILE = os.path.join(SCRIPT_DIR, "trump_processed.json")

JST = timezone(timedelta(hours=9))
BSKY_MAX_LENGTH = 300


def log(msg):
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')
    print(f"[{now}] {msg}")


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return json.load(f)
    return []


def save_processed(processed):
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(processed[-500:], f)


def grapheme_len(text):
    return len(text)


def split_for_posts(text, source_url):
    """テキストをBluesky投稿用に分割する。最後の投稿にリンクを付加"""
    suffix = f"\n{source_url}"
    suffix_len = len(suffix)
    max_len = BSKY_MAX_LENGTH

    if grapheme_len(text) + suffix_len <= max_len:
        return [text + suffix]

    chunks = []
    remaining = text
    while remaining:
        if grapheme_len(remaining) <= max_len:
            chunks.append(remaining)
            break

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

    if grapheme_len(chunks[-1]) + suffix_len <= max_len:
        chunks[-1] += suffix
    else:
        chunks.append(source_url)

    return chunks


def translate_with_gemini(text):
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "以下はトランプ大統領のTruth Social投稿です。日本語に翻訳してください。\n"
        "ルール：\n"
        "- 自然な日本語にする\n"
        "- 投稿のトーンや強調（大文字表現など）を維持する\n"
        "- 翻訳のみを出力し、解説や注釈は不要\n\n"
        f"【原文】\n{text}"
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        error_str = str(e)
        log(f"Gemini API エラー: {error_str}")
        if "403" in error_str or "429" in error_str:
            return "RATE_LIMITED"
        return None


def bsky_login():
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        timeout=30
    )
    resp.raise_for_status()
    session = resp.json()
    did = session['did']
    token = session['accessJwt']

    # 動画アップロードに必要なPDS DIDを取得
    pds_did = None
    try:
        desc_resp = requests.get(
            f"{BSKY_API}/com.atproto.repo.describeRepo",
            params={'repo': did},
            timeout=30
        )
        desc_resp.raise_for_status()
        for svc in desc_resp.json().get('didDoc', {}).get('service', []):
            if svc.get('id') == '#atproto_pds':
                pds_url = svc['serviceEndpoint']
                pds_host = pds_url.replace('https://', '').rstrip('/')
                pds_did = f"did:web:{pds_host}"
                break
    except Exception as e:
        log(f"PDS DID取得エラー（動画アップロード不可）: {e}")

    return did, token, pds_did


def fetch_media(page_url):
    """trumpstruth.orgのページからメディアURL一覧を取得する"""
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(page_url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        response = urllib.request.urlopen(req, context=ctx, timeout=30)
        html = response.read()
        soup = BeautifulSoup(html, 'html.parser')

        images = []
        videos = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if MEDIA_ARCHIVE_DOMAIN not in href:
                continue
            href_lower = href.lower()
            if any(href_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
                # alt テキストを取得（img タグから）
                img_tag = a_tag.find('img')
                alt = img_tag.get('alt', '') if img_tag else ''
                images.append({'url': href, 'alt': alt})
            elif any(href_lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
                videos.append({'url': href})

        return {'images': images[:4], 'videos': videos[:1]}
    except Exception as e:
        log(f"メディア取得エラー: {e}")
        return {'images': [], 'videos': []}


def upload_image_blob(image_url, token):
    """画像をダウンロードしてBlueskyにアップロードする"""
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(image_url, headers={
            'User-Agent': 'Mozilla/5.0'
        })
        response = urllib.request.urlopen(req, context=ctx, timeout=60)
        img_data = response.read()
        content_type = response.headers.get('Content-Type', 'image/jpeg')

        # 1MB超の場合はスキップ
        if len(img_data) > 1_000_000:
            log(f"画像サイズ超過（{len(img_data)} bytes）、スキップ: {image_url}")
            return None

        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.uploadBlob",
            data=img_data,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': content_type
            },
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()['blob']
    except Exception as e:
        log(f"画像アップロードエラー: {e}")
        return None


def upload_video_blob(video_url, did, pds_did, token):
    """動画をダウンロードしてBlueskyにアップロードする"""
    try:
        # 動画ダウンロード
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(video_url, headers={
            'User-Agent': 'Mozilla/5.0'
        })
        response = urllib.request.urlopen(req, context=ctx, timeout=120)
        vid_data = response.read()

        # 50MB超の場合はスキップ
        if len(vid_data) > 50_000_000:
            log(f"動画サイズ超過（{len(vid_data)} bytes）、スキップ: {video_url}")
            return None

        # サービス認証トークン取得
        auth_resp = requests.get(
            f"{BSKY_API}/com.atproto.server.getServiceAuth",
            params={
                'aud': pds_did,
                'lxm': 'com.atproto.repo.uploadBlob',
                'exp': int(time.time()) + 60 * 30,
            },
            headers={'Authorization': f'Bearer {token}'},
            timeout=30
        )
        auth_resp.raise_for_status()
        service_token = auth_resp.json()['token']

        # 動画アップロード
        upload_resp = requests.post(
            "https://video.bsky.app/xrpc/app.bsky.video.uploadVideo",
            data=vid_data,
            params={'did': did, 'name': 'video.mp4'},
            headers={
                'Authorization': f'Bearer {service_token}',
                'Content-Type': 'video/mp4',
            },
            timeout=300
        )
        if upload_resp.status_code != 200:
            log(f"動画アップロード失敗: {upload_resp.status_code} {upload_resp.text}")
            return None

        job_result = upload_resp.json()
        job_id = job_result.get('jobId')

        if job_id:
            # 処理完了を待つ
            for attempt in range(60):
                time.sleep(3)
                status_resp = requests.get(
                    "https://video.bsky.app/xrpc/app.bsky.video.getJobStatus",
                    params={'jobId': job_id},
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=30
                )
                status = status_resp.json()
                state = status.get('jobStatus', {}).get('state', 'unknown')
                if state == 'JOB_STATE_COMPLETED':
                    return status['jobStatus']['blob']
                elif state == 'JOB_STATE_FAILED':
                    log(f"動画処理失敗: {status}")
                    return None
            log("動画処理タイムアウト")
            return None
        else:
            return job_result.get('blob')

    except Exception as e:
        log(f"動画アップロードエラー: {e}")
        return None


def build_media_embed(media, did, pds_did, token):
    """メディア情報からBluesky embed辞書を生成する"""
    # 動画が優先（1投稿に動画は1つのみ）
    if media['videos']:
        if not pds_did:
            log("PDS DIDが取得できなかったため動画スキップ")
        else:
            video = media['videos'][0]
            log(f"動画アップロード中: {video['url']}")
            blob = upload_video_blob(video['url'], did, pds_did, token)
            if blob:
                return {'$type': 'app.bsky.embed.video', 'video': blob}

    # 画像（最大4枚）
    if media['images']:
        uploaded = []
        for img in media['images']:
            log(f"画像アップロード中: {img['url']}")
            blob = upload_image_blob(img['url'], token)
            if blob:
                uploaded.append({'alt': img.get('alt', ''), 'image': blob})
        if uploaded:
            return {'$type': 'app.bsky.embed.images', 'images': uploaded}

    return None


def fetch_post_text(page_url):
    """RSS descriptionが空の場合、ページからテキストを取得する"""
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(page_url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        response = urllib.request.urlopen(req, context=ctx, timeout=30)
        html = response.read()
        soup = BeautifulSoup(html, 'html.parser')

        # 投稿本文を取得
        content_div = soup.find('div', class_='status__content')
        if content_div:
            text = content_div.get_text(separator='\n').strip()
            if text:
                return text

        # 本文がない場合、添付テキスト（動画トランスクリプト等）を取得
        for att in soup.find_all('div', class_='status-details-attachment__text'):
            text = att.get_text(separator='\n').strip()
            if text:
                return text

        return ''
    except Exception as e:
        log(f"ページテキスト取得エラー: {e}")
        return ''


def detect_url_facets(text):
    """テキスト内のURLを検出してBluesky facets（リッチテキスト）を生成する"""
    facets = []
    text_bytes = text.encode('utf-8')
    for m in re.finditer(r'https?://[^\s\u3000）)」』】>]+', text):
        url = m.group()
        # バイト位置を計算（Blueskyはバイトオフセットを使用）
        byte_start = len(text[:m.start()].encode('utf-8'))
        byte_end = byte_start + len(url.encode('utf-8'))
        facets.append({
            'index': {'byteStart': byte_start, 'byteEnd': byte_end},
            'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': url}]
        })
    return facets


def post_to_bluesky(chunks, did, token, embed=None):
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

        # メディアは最初の投稿にのみ添付
        if i == 0 and embed:
            record['embed'] = embed

        facets = detect_url_facets(chunk)
        if facets:
            record['facets'] = facets

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
            timeout=30
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

    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(RSS_URL, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    })
    try:
        response = urllib.request.urlopen(req, context=ctx)
        feed = feedparser.parse(response.read())
    except Exception as e:
        log(f"RSSフィード取得エラー: {e}")
        return

    if not feed.entries:
        log("RSSフィードのエントリなし")
        return

    log(f"RSSフィード取得成功: {len(feed.entries)}件")

    processed = load_processed()
    new_posts = []

    for entry in reversed(feed.entries):
        post_id = entry.get('id') or entry.get('link', '')
        if post_id in processed:
            continue

        content = entry.get('description') or entry.get('summary', '')
        soup = BeautifulSoup(content, 'html.parser')
        text = soup.get_text(separator='\n').strip()

        # RSS descriptionが空の場合、ページ本体からテキストを取得
        if not text:
            page_url = entry.get('link', '')
            if page_url:
                log(f"RSS空のためページ取得: {page_url}")
                text = fetch_post_text(page_url)

        if not text:
            processed.append(post_id)
            continue

        new_posts.append({
            'id': post_id,
            'text': text,
            'link': entry.get('link', ''),
            'published': entry.get('published', '')
        })

    if not new_posts:
        log("新規投稿なし")
        save_processed(processed)
        return

    log(f"新規投稿: {len(new_posts)}件")

    try:
        did, token, pds_did = bsky_login()
        log(f"Blueskyログイン成功 (DID: {did})")
    except Exception as e:
        log(f"Blueskyログインエラー: {e}")
        return

    for post in new_posts:
        log(f"翻訳中: {post['text'][:80]}...")

        translation = translate_with_gemini(post['text'])
        if translation == "RATE_LIMITED":
            log("Gemini APIレート制限、残りの投稿は次回処理")
            save_processed(processed)
            return
        if not translation:
            log("翻訳失敗、スキップ")
            processed.append(post['id'])
            save_processed(processed)
            continue

        chunks = split_for_posts(translation, post['link'])

        # メディア取得・アップロード
        embed = None
        if post['link']:
            media = fetch_media(post['link'])
            if media['images'] or media['videos']:
                log(f"メディア検出: 画像{len(media['images'])}枚, 動画{len(media['videos'])}本")
                embed = build_media_embed(media, did, pds_did, token)

        log(f"Bluesky投稿中 ({len(chunks)}ポスト): {translation[:80]}...")

        try:
            post_uri = post_to_bluesky(chunks, did, token, embed=embed)
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
