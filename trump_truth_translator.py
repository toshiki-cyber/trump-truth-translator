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
import difflib
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


def normalize_urls(text):
    """RSSフィードで途中改行されたURLを修復し、https://のないURLにプロトコルを付加する"""
    # URL内の改行を結合（次行がスペースなし・日本語なしならURL継続とみなす）
    for _ in range(5):
        new = re.sub(
            r'((?:https?://|(?:[a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}/)[^\s\n]*)\n([^\s\n぀-￿]+)',
            r'\1\2',
            text
        )
        if new == text:
            break
        text = new
    # 裸のドメインURL（https://なし）にプロトコルを付加
    text = re.sub(
        r'(?<!\S)((?:[a-zA-Z0-9][-a-zA-Z0-9]*\.)+(?:com|net|org|gov|edu|io|news|social|app|co|jp)/[^\s]*)',
        r'https://\1',
        text
    )
    return text


def has_japanese(text):
    return bool(re.search(r'[぀-ヿ一-鿿]', text))


def text_fingerprint(text):
    """RT @xxx プレフィックスを除いた先頭150文字 — 内容重複チェック用"""
    t = re.sub(r'^RT\s+@\S+\s+', '', text.strip())
    return 'fp:' + t[:150]


def is_similar_to_processed(fp, processed, threshold=0.92):
    """保存済みフィンガープリントと類似度比較（誤字修正再投稿の二重投稿防止）"""
    for p in processed:
        if p.startswith('fp:'):
            ratio = difflib.SequenceMatcher(None, fp, p).ratio()
            if ratio >= threshold:
                return True
    return False


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


def get_ts_post_data(post_id):
    """Truth Social APIから投稿データ全体を取得（reblog情報含む）"""
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
    return resp.json()


def extract_media_from_ts_data(data):
    """Truth Social APIレスポンスからメディアURLを抽出（RT時はreblog側を参照）"""
    source = data.get('reblog') or data
    video_url = None
    image_urls = []
    seen_urls = set()
    for att in source.get('media_attachments', []):
        att_type = att.get('type', '')
        url = att.get('url', '')
        preview_url = att.get('preview_url', '')
        if att_type in ('video', 'gifv') and not video_url:
            video_url = url
        elif att_type == 'image' and url not in seen_urls and len(image_urls) < 4:
            image_urls.append((url, preview_url))
            seen_urls.add(url)
    return video_url, image_urls


def extract_rt_info_from_ts_data(data):
    """RT投稿の場合、(表示名, アカウント名) を返す。RTでなければ (None, None)"""
    reblog = data.get('reblog')
    if not reblog:
        return None, None
    account = reblog.get('account', {})
    display_name = account.get('display_name', '').strip()
    acct = account.get('acct', '').strip()
    return display_name or acct, acct


def parse_rt_body(text):
    """RT投稿のプレフィックス（RT https://... or RT @xxx）を除去して本文のみ返す"""
    m = re.match(r'^RT\s+https?://\S+\s*(.*)', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r'^RT\s+@\S+\s*(.*)', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


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


def upload_image_to_bsky(image_url, did, token, fallback_url=None):
    """画像をダウンロードしてBlueskyにアップロード、blobを返す（direct優先、失敗時proxy、fallback_url対応）"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://truthsocial.com/'
    }
    def _try_download(url):
        try:
            r = requests.get(url, headers=headers, proxies=NO_PROXY, timeout=30)
            r.raise_for_status()
            return r
        except Exception:
            r = requests.get(url, headers=headers, proxies=BSKY_PROXIES, timeout=30)
            r.raise_for_status()
            return r
    try:
        resp = _try_download(image_url)
    except Exception as e:
        if fallback_url:
            resp = _try_download(fallback_url)
        else:
            raise Exception(f"{image_url}: {e}") from e
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


def fetch_ogp(url):
    """URLからOGP情報（タイトル、説明、画像URL）を取得"""
    try:
        resp = requests.get(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            proxies=NO_PROXY,
            verify=certifi.where(),
            timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        def og(prop):
            tag = soup.find('meta', property=prop) or soup.find('meta', attrs={'name': prop})
            return (tag or {}).get('content', '') if tag else ''
        title = og('og:title') or og('twitter:title')
        if not title:
            t = soup.find('title')
            title = t.get_text(strip=True) if t else ''
        description = og('og:description') or og('twitter:description')
        image_url = og('og:image') or og('twitter:image')
        return title[:300], description[:500], image_url
    except Exception as e:
        log(f"OGP取得エラー ({url[:60]}): {e}")
        return '', '', ''


def make_external_embed(url, did, token):
    """外部リンクカードembedを作成"""
    title, description, image_url = fetch_ogp(url)
    external = {
        'uri': url,
        'title': title or url,
        'description': description or ''
    }
    if image_url:
        try:
            thumb_blob = upload_image_to_bsky(image_url, did, token)
            external['thumb'] = thumb_blob
            log(f"リンクカードサムネイル取得成功")
        except Exception as e:
            log(f"リンクカードサムネイル取得失敗（スキップ）: {e}")
    return {'$type': 'app.bsky.embed.external', 'external': external}


def translate_with_claude(text):
    # URLをプレースホルダーに置換して翻訳後に復元
    urls = re.findall(r'https?://\S+', text)
    text_for_translation = text
    for i, url in enumerate(urls):
        text_for_translation = text_for_translation.replace(url, f'[URL_{i}]', 1)

    # プロキシなしのhttpxクライアントを明示（環境変数プロキシの影響を排除）
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        http_client=httpx.Client(proxy=None)
    )
    prompt = (
        "以下はトランプ大統領のTruth Social投稿です。日本語に翻訳してください。\n"
        "ルール：\n"
        "- 自然な日本語にする\n"
        "- 文体は常体（だ・である調）を使う\n"
        "- 投稿のトーンや強調（大文字表現など）を維持する\n"
        "- [URL_0]、[URL_1]などのプレースホルダーはそのまま保持すること\n"
        "- 人名・国名・機関名は日本の主要メディアの表記に従うこと（例: President Xi → 習主席、Xi Jinping → 習近平）\n"
        "- 翻訳のみを出力し、解説や注釈は不要\n\n"
        f"【原文】\n{text_for_translation}"
    )
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            error_str = str(e)
            log(f"Claude API エラー (試行{attempt + 1}/3): {error_str}")
            if "429" in error_str or "overloaded" in error_str.lower():
                return "RATE_LIMITED"
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return None


def restore_urls(translated, urls):
    """翻訳結果にURLプレースホルダーを元のURLに戻す。残ったURLは末尾に追加"""
    result = translated
    for i, url in enumerate(urls):
        result = result.replace(f'[URL_{i}]', url)
    # プレースホルダーが消えたURLを末尾に追加
    for url in urls:
        if url not in result:
            result = result.rstrip() + '\n' + url
    return result


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


def post_to_bluesky(chunks, did, token, image_blobs=None, video_blob=None, external_embed=None):
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

        # 最初の投稿にのみメディアを添付（動画 > リンクカード > 画像の優先順位）
        if i == 0:
            if video_blob:
                record['embed'] = {
                    '$type': 'app.bsky.embed.video',
                    'video': video_blob,
                    'alt': ''
                }
            elif external_embed:
                record['embed'] = external_embed
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
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if href.startswith('http'):
                a.replace_with(href)
        text = normalize_urls(soup.get_text(separator='\n').strip())

        if not text:
            processed.append(post_id)
            continue

        fp = text_fingerprint(text)
        if fp in processed or is_similar_to_processed(fp, processed):
            log(f"重複スキップ（内容重複）: {text[:60]}...")
            processed.append(post_id)
            continue
        processed.append(fp)

        # Truth Social APIからメディア・RT情報取得、失敗時はRSS HTMLにフォールバック
        video_url = None
        image_urls = []
        rt_display_name, rt_acct = None, None
        ts_post_id = get_ts_post_id(entry.get('link', ''))
        if ts_post_id:
            try:
                ts_data = get_ts_post_data(ts_post_id)
                video_url, image_urls = extract_media_from_ts_data(ts_data)
                rt_display_name, rt_acct = extract_rt_info_from_ts_data(ts_data)
                log(f"TS APIメディア: 動画={'あり' if video_url else 'なし'}, 画像{len(image_urls)}枚")
                if rt_display_name:
                    log(f"RT投稿: {rt_display_name} (@{rt_acct})")
            except Exception as e:
                log(f"Truth Social APIエラー（フォールバック）: {e}")
                video_url = extract_video(content)
                if not video_url:
                    status_link = entry.get('link', '')
                    image_urls = [(u, None) for u in (scrape_images_from_page(status_link) if status_link else [])]
                    if not image_urls:
                        image_urls = [(u, None) for u in extract_images(content)]
        else:
            video_url = extract_video(content)
            if not video_url:
                status_link = entry.get('link', '')
                image_urls = [(u, None) for u in (scrape_images_from_page(status_link) if status_link else [])]
                if not image_urls:
                    image_urls = [(u, None) for u in extract_images(content)]

        # URL重複排除（同じ画像が複数回添付されるのを防ぐ）
        seen = set()
        image_urls = [t for t in image_urls if not (t[0] in seen or seen.add(t[0]))]

        # RTの場合はプレフィックスを除いた本文のみを翻訳対象にする
        body_text = parse_rt_body(text) if text.startswith('RT') else text

        new_posts.append({
            'id': post_id,
            'fp': fp,
            'text': body_text,
            'link': entry.get('link', ''),
            'published': entry.get('published', ''),
            'video_url': video_url,
            'image_urls': image_urls,
            'rt_display_name': rt_display_name,
            'rt_acct': rt_acct,
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

        # URLとホワイトスペース（\xa0等Unicode空白含む）を除いた実質的なテキストがあるか確認
        post_urls = re.findall(r'https?://\S+', post['text'])
        meaningful_text = re.sub(r'https?://\S+', '', post['text'])
        # "RT:" などの短いプレフィックスも除外
        meaningful_text = re.sub(r'\bRT:\s*', '', meaningful_text)
        meaningful_text = re.sub(r'[\s\xa0]+', '', meaningful_text)
        if not meaningful_text:
            # テキストなし（画像のみ or URLのみ or RTのみ）→ 翻訳不要
            translation = '\n'.join(post_urls) if post_urls else "【画像投稿】"
            log("テキストなし（画像のみ/URLのみ/RTのみ）のため翻訳スキップ")
        else:
            translation = translate_with_claude(post['text'])
            if translation and translation not in ("RATE_LIMITED",):
                translation = restore_urls(translation, post_urls)
                if not has_japanese(translation):
                    log(f"翻訳結果に日本語なし（LLMエラー応答）、スキップ: {translation[:80]}")
                    processed.append(post['id'])
                    save_processed(processed)
                    continue
        if translation == "RATE_LIMITED":
            log("Claude APIレート制限、残りの投稿は次回処理")
            # 未処理投稿のfpをprocessedから除去（次回リトライさせるため）
            pending_fps = {p['fp'] for p in new_posts if p['id'] not in processed}
            processed = [x for x in processed if x not in pending_fps]
            save_processed(processed)
            return
        if not translation:
            log("翻訳失敗、スキップ")
            processed.append(post['id'])
            save_processed(processed)
            continue
        # Claude拒否メッセージ検出（「翻訳対象がない」系の応答を投稿しない）
        refusal_phrases = [
            "翻訳対象となるテキストが提供されていません",
            "翻訳してほしい",
            "翻訳するテキストが",
            "テキストが提供されていません",
            "申し訳ありません",
            "申し訳ございません",
            "I appreciate your request",
            "I appreciate you wanting to translate",
        ]
        if any(phrase in translation for phrase in refusal_phrases):
            log(f"Claude拒否メッセージを検出、スキップ: {translation[:80]}")
            processed.append(post['id'])
            save_processed(processed)
            continue

        # 動画または画像をアップロード（動画優先）
        video_blob = None
        image_blobs = []
        external_embed = None
        post_link = post.get('link', '')
        if post.get('video_url'):
            try:
                video_blob = upload_video_to_bsky(post['video_url'], did, token)
                log(f"動画アップロード成功: {post['video_url'][:60]}")
            except Exception as e:
                log(f"動画アップロード失敗（スキップ）: {e}")
        else:
            for url_pair in post.get('image_urls', []):
                primary, fallback = url_pair if isinstance(url_pair, tuple) else (url_pair, None)
                try:
                    blob = upload_image_to_bsky(primary, did, token, fallback_url=fallback)
                    image_blobs.append(blob)
                    log(f"画像アップロード成功: {primary[:60]}")
                except Exception as e:
                    log(f"画像アップロード失敗（スキップ）: {e}")

        # 画像も動画もない場合、trumpstruth.orgのリンクカードを添付
        if not video_blob and not image_blobs and post_link:
            try:
                external_embed = make_external_embed(post_link, did, token)
                log(f"リンクカード作成: {post_link}")
            except Exception as e:
                log(f"リンクカード作成失敗（スキップ）: {e}")

        # RT投稿の場合はヘッダーを先頭に付ける
        if post.get('rt_display_name'):
            rt_header = f"🔁 Donald Trump がリポスト\n{post['rt_display_name']} (@{post['rt_acct']})\n\n"
            full_translation = rt_header + translation
        else:
            full_translation = translation

        media_info = "動画あり" if video_blob else f"画像{len(image_blobs)}枚"
        chunks = split_for_posts(full_translation)
        log(f"Bluesky投稿中 ({len(chunks)}ポスト, {media_info}): {full_translation[:80]}...")

        try:
            post_uri = post_to_bluesky(chunks, did, token, image_blobs, video_blob, external_embed)
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
