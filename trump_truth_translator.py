#!/usr/bin/env python3
"""
Trump Truth Social → 日本語翻訳 → Bluesky投稿 ボット
trumpstruth.org のRSSフィードを監視し、新規投稿を翻訳してBlueskyに投稿する
"""

import feedparser
import requests
import httpx
import json
import io
import os
import re
import certifi
import time
import difflib
import hashlib
import anthropic
from enum import Enum
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageOps
from urllib.parse import urlparse, urljoin

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
BSKY_IMAGE_SAFE_SIZE = 950_000  # 公式上限の変動に備え、1MB未満に収める
KNOWN_REEVALUATE_IDS = {
    'https://www.trumpstruth.org/statuses/40727',
    '117074526504264990',
}
TEXT_FALLBACK_POLICY_VERSION = 1
TID_RKEY_POLICY_VERSION = 1
KNOWN_REEVALUATE_CANONICAL = {
    'https://www.trumpstruth.org/statuses/40727': 'truth:117074526504264990',
}


def is_known_reevaluate_id(value):
    text = str(value)
    return text in KNOWN_REEVALUATE_IDS or '117074526504264990' in text


def should_force_known_reevaluation(history, feed_post_id):
    """既知不良投稿を終端状態になるまで旧fingerprint判定から救済する。"""
    if not is_known_reevaluate_id(feed_post_id) or not isinstance(history, dict):
        return False
    posts = history.get('posts', {})
    states = []
    direct = posts.get(feed_post_id)
    if direct:
        states.append(direct)
    truth_id = extract_ts_post_id_from_url(feed_post_id)
    canonical_key = (f'truth:{truth_id}' if truth_id else
                     KNOWN_REEVALUATE_CANONICAL.get(str(feed_post_id)))
    canonical = posts.get(canonical_key) if canonical_key else None
    if canonical and canonical not in states:
        states.append(canonical)
    states.extend(
        state for state in posts.values()
        if state.get('feed_post_id') == feed_post_id and state not in states
    )
    return any(
        state.get('post_status') not in ('POSTED', 'BLOCKED')
        for state in states
    )


def has_feed_alias(history, feed_post_id):
    if not isinstance(history, dict):
        return False
    return any(
        state.get('feed_post_id') == feed_post_id
        for state in history.get('posts', {}).values()
    )


def find_source_state(history, feed_post_id='', status_url='', truth_social_id=None):
    """URL形式が変わっても同じTruth投稿の保存状態を見つける。"""
    if not isinstance(history, dict):
        return None, {}
    posts = history.get('posts', {})
    truth_id = truth_social_id or extract_ts_post_id_from_url(feed_post_id)
    for key, state in posts.items():
        if (
            key == feed_post_id
            or (status_url and state.get('status_url') == status_url)
            or (feed_post_id and state.get('feed_post_id') == feed_post_id)
            or (truth_id and str(state.get('truth_social_id') or '') == str(truth_id))
        ):
            return key, state
    return None, {}


class MediaState(str, Enum):
    """元投稿のメディア確認状態。失敗・不明をNO_MEDIAと区別する。"""

    NO_MEDIA = "NO_MEDIA"
    READY = "READY"
    PENDING = "PENDING"
    INVALID = "INVALID"


class InvalidMediaError(ValueError):
    """取得できたメディア自体が破損している場合のエラー。"""


class PartialThreadError(RuntimeError):
    """スレッドの一部作成後に失敗し、再開情報を保持するエラー。"""

    def __init__(self, message, checkpoint):
        super().__init__(message)
        self.checkpoint = checkpoint


def log(msg):
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')
    line = f"[{now}] {msg}"
    print(line)


def new_processing_history(processed=None):
    """旧形式の処理済みリストを保持した構造化履歴を作る。"""
    migrated = []
    for item in (processed or []):
        if is_known_reevaluate_id(item):
            continue
        migrated.append(item)
        truth_id = extract_ts_post_id_from_url(item) if isinstance(item, str) else None
        canonical = f'truth:{truth_id}' if truth_id else None
        if canonical and canonical not in migrated:
            migrated.append(canonical)
    return {
        'version': 2,
        'processed': migrated,
        'posts': {},
    }


def normalize_processing_history(data):
    """旧リスト形式と現行の構造化形式を共通形式へ変換する。"""
    if isinstance(data, list):
        return new_processing_history(data)
    if not isinstance(data, dict):
        return new_processing_history()
    history = {
        'version': 2,
        'processed': list(data.get('processed', [])),
        'posts': dict(data.get('posts', {})),
    }
    for state in history['posts'].values():
        if (
            state.get('rkey_policy_version', 0) < TID_RKEY_POLICY_VERSION
            and 'Invalid TID string' in str(state.get('failure_reason', ''))
            and 'ttt-' in str(state.get('failure_reason', ''))
        ):
            state['rkey_policy_version'] = TID_RKEY_POLICY_VERSION
            state['retry_count'] = 0
            state.pop('next_retry_at', None)
    return history


def can_fallback_video_to_text(text):
    """動画なしでも投稿する価値がある本文が存在するか判定する。"""
    source = text or ''
    plain = (BeautifulSoup(source, 'html.parser').get_text(separator='\n')
             if '<' in source else source)
    plain = re.sub(r'https?://\S+', '', plain)
    plain = re.sub(r'\bRT:\s*', '', plain)
    return bool(re.sub(r'[\s\xa0]+', '', plain))


def processed_entries(history):
    """テスト中の旧リスト入力にも対応して処理済み一覧を返す。"""
    if isinstance(history, dict):
        return history.setdefault('processed', [])
    return history


def record_post_state(history, post_id, media_state, reason=None, truth_social_id=None, **updates):
    """投稿ごとのメディア状態・失敗理由・再試行回数を記録する。"""
    if not isinstance(history, dict):
        return
    posts = history.setdefault('posts', {})
    previous = posts.get(post_id, {})
    state_value = media_state.value if isinstance(media_state, MediaState) else str(media_state)
    allow_text_fallback = bool(
        updates.get('allow_text_fallback') or previous.get('allow_text_fallback')
    )
    failed = (
        state_value in (MediaState.PENDING.value, MediaState.INVALID.value)
        and not (state_value == MediaState.INVALID.value and allow_text_fallback)
    )
    retry_count = int(previous.get('retry_count', 0)) + (1 if failed else 0)
    state = dict(previous)
    state.update({
        'media_state': state_value,
        'post_status': ('TEXT_FALLBACK_READY'
                        if state_value == MediaState.INVALID.value and allow_text_fallback
                        else 'BLOCKED' if state_value == MediaState.INVALID.value
                        else 'RETRY' if failed else previous.get('post_status', 'READY')),
        'failure_reason': reason if failed else None,
        'retry_count': retry_count,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    })
    state.update({key: value for key, value in updates.items() if value is not None})
    if failed:
        delay_minutes = min(5 * (2 ** max(0, retry_count - 1)), 360)
        state['next_retry_at'] = (
            datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        ).isoformat().replace('+00:00', 'Z')
    else:
        state.pop('next_retry_at', None)
    if truth_social_id or previous.get('truth_social_id'):
        state['truth_social_id'] = truth_social_id or previous['truth_social_id']
    posts[post_id] = state


def record_failure(history, post_id, stage, reason, **updates):
    """メディア状態を変えずに処理段階の失敗を記録する。"""
    if not isinstance(history, dict):
        return
    posts = history.setdefault('posts', {})
    state = dict(posts.get(post_id, {}))
    retry_count = (int(state.get('retry_count', 0)) + 1
                   if state.get('failure_stage') == stage else 1)
    state.update({key: value for key, value in updates.items() if value is not None})
    state['post_status'] = 'POST_VERIFY_PENDING' if stage == 'VERIFY' else 'RETRY'
    state['failure_stage'] = stage
    state['failure_reason'] = reason
    state['retry_count'] = retry_count
    state['updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    delay_minutes = min(5 * (2 ** max(0, retry_count - 1)), 360)
    state['next_retry_at'] = (
        datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    ).isoformat().replace('+00:00', 'Z')
    posts[post_id] = state


def pending_entries_from_history(history, existing_ids):
    """RSS圏外の未解決投稿を保存済みsource情報から再構成する。"""
    if not isinstance(history, dict):
        return []
    entries = []
    for post_id, state in history.get('posts', {}).items():
        status = state.get('post_status')
        if post_id in existing_ids or status in ('POSTED', 'POST_VERIFY_PENDING'):
            continue
        if status == 'BLOCKED':
            updated = state.get('updated_at')
            if not updated or datetime.now(timezone.utc) - datetime.fromisoformat(
                updated.replace('Z', '+00:00')
            ) < timedelta(hours=24):
                continue
        if not state.get('status_url'):
            continue
        entries.append({
            'id': post_id,
            'link': state['status_url'],
            'description': state.get('source_text', ''),
            'published': state.get('published', ''),
            '_blocked_probe': status == 'BLOCKED',
        })
    return entries


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return normalize_processing_history(json.load(f))
    return new_processing_history()


def prepare_history_for_save(processed):
    history = normalize_processing_history(processed)
    # 重複防止履歴は現在のRSS窓を十分超える件数を保持する。
    history['processed'] = history['processed'][-5000:]
    # 未解決状態は捨てず、解決済みだけを最新500件に絞る。
    if len(history['posts']) > 500:
        unresolved = {
            key: value for key, value in history['posts'].items()
            if value.get('post_status') != 'POSTED'
        }
        resolved = sorted(
            (
                (key, value) for key, value in history['posts'].items()
                if value.get('post_status') == 'POSTED'
            ),
            key=lambda item: item[1].get('updated_at', ''),
        )[-500:]
        history['posts'] = {**dict(resolved), **unresolved}
    return history


def save_processed(processed):
    history = prepare_history_for_save(processed)
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def retry_due(history, post_id, now=None, media_identity=None, manual=False):
    """既存の失敗投稿にだけ指数バックオフを適用する。新規投稿は常に対象。"""
    if not isinstance(history, dict) or post_id not in history.get('posts', {}):
        return True
    state = history['posts'][post_id]
    if state.get('post_status') == 'BLOCKED':
        return bool(manual or (media_identity and media_identity != state.get('media_identity')))
    next_retry = state.get('next_retry_at')
    if not next_retry:
        return True
    current = now or datetime.now(timezone.utc)
    return current >= datetime.fromisoformat(next_retry.replace('Z', '+00:00'))


def deterministic_post_rkey(source_id, chunk_index):
    """app.bsky.feed.postが要求する13文字TIDを決定的に生成する。"""
    digest = hashlib.sha256(f'{source_id}:{chunk_index}'.encode()).digest()
    match = re.search(r'(\d{15,20})', str(source_id))
    if match:
        # Truth Social/Mastodonのsnowflake ID上位48bitはミリ秒時刻。
        micros = (int(match.group(1)) >> 16) * 1000 + chunk_index
    else:
        # Truth IDを取得できないテスト・旧履歴向けの安定した過去時刻。
        epoch_2020_us = 1_577_836_800_000_000
        six_years_us = 6 * 365 * 24 * 60 * 60 * 1_000_000
        micros = epoch_2020_us + int.from_bytes(digest[:8], 'big') % six_years_us
    clock_id = int.from_bytes(digest[-2:], 'big') & 0x3ff
    return encode_tid((micros << 10) | clock_id)


TID_ALPHABET = '234567abcdefghijklmnopqrstuvwxyz'


def encode_tid(value):
    chars = []
    for shift in range(60, -1, -5):
        chars.append(TID_ALPHABET[(value >> shift) & 0x1f])
    return ''.join(chars)


def decode_tid(value):
    decoded = 0
    for char in value:
        decoded = (decoded << 5) | TID_ALPHABET.index(char)
    return decoded


def canonical_source_key(status_url, truth_social_id=None):
    truth_id = truth_social_id or extract_ts_post_id_from_url(status_url)
    return f'truth:{truth_id}' if truth_id else f'url:{status_url}'


def confirm_no_media(history, source_key, now=None):
    now = now or datetime.now(timezone.utc)
    state = history.setdefault('posts', {}).setdefault(source_key, {})
    first_seen = state.get('first_seen')
    if not first_seen:
        first_seen = now.isoformat().replace('+00:00', 'Z')
        state['first_seen'] = first_seen
    state['no_media_confirmations'] = int(state.get('no_media_confirmations', 0)) + 1
    elapsed = now - datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
    return state['no_media_confirmations'] >= 3 and elapsed >= timedelta(minutes=15)


def update_source_identity(history, source_key, fingerprint, media_identity):
    state = history.setdefault('posts', {}).setdefault(source_key, {})
    if (
        state.get('source_fingerprint') not in (None, fingerprint)
        or state.get('media_identity') not in (None, media_identity)
    ):
        for key in (
            'translation', 'thread_checkpoint', 'root_uri', 'root_record',
            'root_rkey', 'expected_embed', 'expected_images', 'failure_stage',
            'failure_reason', 'next_retry_at', 'retry_count', 'repair_attempted',
            'repair_attempts', 'created_records',
        ):
            state.pop(key, None)
        state['post_status'] = 'READY'
    state['source_fingerprint'] = fingerprint
    state['media_identity'] = media_identity


def pending_verifications(history):
    if not isinstance(history, dict):
        return []
    return [
        dict(state, source_key=key)
        for key, state in history.get('posts', {}).items()
        if state.get('post_status') == 'POST_VERIFY_PENDING' and state.get('root_uri')
    ]


def record_matches(existing, expected):
    value = existing.get('value', existing)
    for key in ('$type', 'text', 'langs', 'embed', 'reply'):
        if value.get(key) != expected.get(key):
            return False
    return True


def media_only_text(kind):
    """app.bsky.feed.postのtextは空文字でも文字列として有効。"""
    return ''


def complete_post(history, source_key, fingerprint=None):
    entries = processed_entries(history)
    state = (history.setdefault('posts', {}).setdefault(source_key, {})
             if isinstance(history, dict) else {})
    for value in (fingerprint, source_key, state.get('feed_post_id')):
        if value and value not in entries:
            entries.append(value)
    if isinstance(history, dict):
        state['post_status'] = 'POSTED'
        state['failure_reason'] = None
        state.pop('failure_stage', None)
        state.pop('next_retry_at', None)
        state.pop('thread_checkpoint', None)


def begin_repair_attempt(history, source_key, persist):
    """修復通信より先に一度限りの試行チェックポイントを永続化する。"""
    state = history.setdefault('posts', {}).setdefault(source_key, {})
    if state.get('repair_attempted'):
        return False
    state['repair_attempted'] = True
    state['repair_attempts'] = int(state.get('repair_attempts', 0)) + 1
    persist(history)
    return True


def merge_archived_media(video_url, image_urls, archived_video, archived_images):
    if video_url:
        return video_url, image_urls
    if archived_video:
        return archived_video, image_urls
    if archived_images:
        merged = []
        for index, pair in enumerate(image_urls):
            primary, fallback = pair if isinstance(pair, tuple) else (pair, None)
            merged.append((primary, archived_images[index] if index < len(archived_images) else fallback))
        if len(archived_images) >= len(image_urls):
            merged.extend((url, None) for url in archived_images[len(image_urls):])
        return None, merged
    return video_url, image_urls


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


def select_external_card_url(text):
    """投稿本文からTruth Social系を除いた最初の外部URLを返す。"""
    for raw_url in re.findall(r'https?://\S+', text):
        url = raw_url.rstrip('.,;:!?)]}\'"')
        hostname = (urlparse(url).hostname or '').lower()
        if hostname == 'truthsocial.com' or hostname.endswith('.truthsocial.com'):
            continue
        if hostname == 'trumpstruth.org' or hostname.endswith('.trumpstruth.org'):
            continue
        return url
    return None


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


def extract_ts_post_id_from_url(url):
    """Truth Socialの公開URLから投稿IDを取り出す。"""
    hostname = (urlparse(url).hostname or '').lower()
    if hostname != 'truthsocial.com' and not hostname.endswith('.truthsocial.com'):
        return None
    match = re.search(r'/(?:posts|statuses)/(\d+)(?:[/?#]|$)', url)
    return match.group(1) if match else None


def extract_ts_post_id_from_html(html):
    """trumpstruth.orgページ内のTruth SocialステータスURLから投稿IDを取得する。"""
    soup = BeautifulSoup(html, 'html.parser')
    # 現行のミラー用classに加え、Truth Socialのstatus URL形式自体を判定する。
    links = soup.find_all('a', href=True)
    for link in links:
        href = link.get('href', '')
        post_id = extract_ts_post_id_from_url(href)
        if post_id:
            return post_id
        hostname = (urlparse(href).hostname or '').lower()
        if hostname != 'truthsocial.com' and not hostname.endswith('.truthsocial.com'):
            continue
        if 'status__external-link' in link.get('class', []):
            match = re.search(r'/(\d+)(?:[/?#]|$)', href)
            if match:
                return match.group(1)
    return None


def get_ts_post_id(trumpstruth_url):
    """trumpstruth.orgのページからTruth Social投稿IDを取得"""
    direct_post_id = extract_ts_post_id_from_url(trumpstruth_url)
    if direct_post_id:
        return direct_post_id
    try:
        resp = requests.get(
            trumpstruth_url,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            proxies=NO_PROXY,
            verify=certifi.where(),
            timeout=15
        )
        resp.raise_for_status()
        return extract_ts_post_id_from_html(resp.text)
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


def classify_ts_media(data):
    source = data.get('reblog') or data
    attachments = source.get('media_attachments', [])
    if not attachments:
        return {'state': MediaState.NO_MEDIA, 'reason': None}
    for attachment in attachments:
        if attachment.get('type') not in ('video', 'gifv', 'image'):
            return {'state': MediaState.INVALID,
                    'reason': f"未対応メディア: {attachment.get('type', 'unknown')}"}
        if not attachment.get('url'):
            return {'state': MediaState.PENDING, 'reason': 'メディアURL欠落'}
    return {'state': MediaState.READY, 'reason': None}


def canonical_media_identity(truth_id, media):
    names = []
    values = media if isinstance(media, list) else [media]
    for value in values:
        primary = value[0] if isinstance(value, tuple) else value
        if primary:
            names.append(os.path.basename(urlparse(primary).path))
    return f"attachment:{truth_id or 'unknown'}:" + ','.join(sorted(names))


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


def inspect_archived_media_from_page(status_url):
    """ミラーを取得し、保存済み添付を返す。取得失敗は呼び出し元へ通知する。"""
    resp = requests.get(
        status_url,
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
        proxies=NO_PROXY,
        verify=certifi.where(),
        timeout=15
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, 'html.parser')
    video_url = None
    image_urls = []
    seen_urls = set()
    selectors = (
        '.status-attachment__link[href], '
        '.status-details-attachment__media a[href]'
    )
    for link in soup.select(selectors):
        href = link.get('href', '')
        parsed = urlparse(href)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or not parsed.hostname.endswith('.linodeobjects.com')
            or '/attachments/' not in parsed.path
            or href in seen_urls
        ):
            continue
        seen_urls.add(href)
        extension = os.path.splitext(parsed.path.lower())[1]
        if extension in ('.mp4', '.mov', '.webm', '.m4v') and not video_url:
            video_url = href
        elif extension in ('.jpg', '.jpeg', '.png', '.webp', '.gif') and len(image_urls) < 4:
            image_urls.append(href)
    return video_url, image_urls


def scrape_archived_media_from_page(status_url):
    """ミラーが保存した元添付だけを取得し、OGP・リンクカード画像は除外する。"""
    try:
        return inspect_archived_media_from_page(status_url)
    except Exception as e:
        log(f"保存済みメディア取得エラー: {e}")
        return None, []


def prefer_archived_media(status_url, video_url, image_urls):
    """ミラー保存済みメディアがあれば、403になりやすいTruth CDN URLより優先する。"""
    status_host = (urlparse(status_url).hostname or '').lower()
    if status_host not in ('trumpstruth.org', 'www.trumpstruth.org'):
        return video_url, image_urls
    archived_video, archived_images = scrape_archived_media_from_page(status_url)
    if archived_video:
        log("ミラー保存済み動画を使用（不良時はTruth候補へフォールバック）")
        return (archived_video, video_url) if video_url else archived_video, []
    if archived_images:
        original_by_name = {}
        for url_pair in image_urls:
            primary, fallback = url_pair if isinstance(url_pair, tuple) else (url_pair, None)
            for candidate in (primary, fallback):
                if candidate:
                    original_by_name[os.path.basename(urlparse(candidate).path)] = candidate
        # APIが示す期待枚数を保持し、ミラー同期途中の一部画像で置換しない。
        merged_video, merged_images = merge_archived_media(
            video_url, image_urls, archived_video, archived_images
        )
        log(f"ミラー保存済み画像候補を併用: {len(archived_images)}枚")
        return merged_video, merged_images
    return video_url, image_urls


def resolve_post_media(status_url, ts_post_id, html_content=''):
    """API・RSS・ミラーを照合し、メディア有無を明示的な状態で返す。"""
    status_host = (urlparse(status_url).hostname or '').lower()
    is_mirror = status_host in ('trumpstruth.org', 'www.trumpstruth.org')
    video_url = None
    image_urls = []
    rt_display_name, rt_acct = None, None
    api_error = None

    if not ts_post_id:
        return {
            'state': MediaState.PENDING,
            'reason': 'Truth Social投稿IDを取得できず、メディア有無が不明',
            'video_url': None,
            'image_urls': [],
            'rt_display_name': None,
            'rt_acct': None,
        }

    try:
        ts_data = get_ts_post_data(ts_post_id)
        source_data = (ts_data.get('reblog') or ts_data) if isinstance(ts_data, dict) else None
        if (
            not isinstance(source_data, dict)
            or not isinstance(source_data.get('media_attachments'), list)
        ):
            return {
                'state': MediaState.PENDING,
                'reason': 'Truth Social API schema不正: media_attachments欠落',
                'video_url': None, 'image_urls': [],
                'rt_display_name': None, 'rt_acct': None,
            }
        classification = classify_ts_media(ts_data)
        if classification['state'] in (MediaState.PENDING, MediaState.INVALID):
            return {
                'state': classification['state'], 'reason': classification['reason'],
                'video_url': None, 'image_urls': [],
                'rt_display_name': None, 'rt_acct': None,
            }
        video_url, image_urls = extract_media_from_ts_data(ts_data)
        rt_display_name, rt_acct = extract_rt_info_from_ts_data(ts_data)
        log(f"TS APIメディア: 動画={'あり' if video_url else 'なし'}, 画像{len(image_urls)}枚")
        if rt_display_name:
            log(f"RT投稿: {rt_display_name} (@{rt_acct})")
    except Exception as error:
        api_error = error
        log(f"Truth Social APIエラー（フォールバック）: {error}")
        video_url = extract_video(html_content)
        if not video_url:
            image_urls = [(url, None) for url in extract_images(html_content)]

    if video_url or image_urls:
        video_url, image_urls = prefer_archived_media(
            status_url, video_url, image_urls
        )
        return {
            'state': MediaState.READY,
            'reason': None,
            'video_url': video_url,
            'image_urls': image_urls,
            'rt_display_name': rt_display_name,
            'rt_acct': rt_acct,
        }

    # APIが添付0件を返すケースでも、ミラー側に保存済み添付がある実例がある。
    # ミラーページを確認できない限り、メディアなしとは確定しない。
    if is_mirror:
        try:
            archived_video, archived_images = inspect_archived_media_from_page(
                status_url
            )
        except Exception as error:
            return {
                'state': MediaState.PENDING,
                'reason': f'ミラーのメディア確認失敗: {error}',
                'video_url': None,
                'image_urls': [],
                'rt_display_name': rt_display_name,
                'rt_acct': rt_acct,
            }
        if archived_video or archived_images:
            image_pairs = [(url, None) for url in archived_images]
            return {
                'state': MediaState.READY,
                'reason': None,
                'video_url': archived_video,
                'image_urls': image_pairs,
                'rt_display_name': rt_display_name,
                'rt_acct': rt_acct,
            }

    if api_error:
        return {
            'state': MediaState.PENDING,
            'reason': f'Truth Social API取得失敗: {api_error}',
            'video_url': None,
            'image_urls': [],
            'rt_display_name': rt_display_name,
            'rt_acct': rt_acct,
        }

    return {
        'state': MediaState.NO_MEDIA,
        'reason': None,
        'video_url': None,
        'image_urls': [],
        'rt_display_name': rt_display_name,
        'rt_acct': rt_acct,
    }


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


def upload_video_via_bsky_service(video_data, content_type, did, token, pds_audience):
    """Bluesky動画サービスで変換済みの動画blobを取得する"""
    auth_resp = requests.get(
        f"{BSKY_API}/com.atproto.server.getServiceAuth",
        params={
            'aud': pds_audience,
            'lxm': 'com.atproto.repo.uploadBlob',
            'exp': int(time.time()) + 30 * 60,
        },
        headers={'Authorization': f'Bearer {token}'},
        proxies=NO_PROXY,
        timeout=30
    )
    try:
        auth_resp.raise_for_status()
    except requests.HTTPError as error:
        raise RuntimeError(
            f"Bluesky動画認証失敗 (HTTP {auth_resp.status_code}): {auth_resp.text[:500]}"
        ) from error
    service_token = auth_resp.json()['token']
    upload_resp = requests.post(
        f"https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?did={did}&name=truth-social-video.mp4",
        data=video_data,
        headers={'Authorization': f'Bearer {service_token}', 'Content-Type': content_type},
        proxies=NO_PROXY,
        timeout=180
    )
    try:
        job = upload_resp.json()
    except Exception:
        job = {}
    # 公式video serviceは再送時にalready_exists系HTTP応答でも既存blobを
    # 返すことがある。blobがあれば冪等な成功として利用する。
    if job.get('blob'):
        return job['blob']
    try:
        upload_resp.raise_for_status()
    except requests.HTTPError as error:
        raise RuntimeError(
            f"Bluesky動画送信失敗 (HTTP {upload_resp.status_code}): {upload_resp.text[:500]}"
        ) from error
    job_id = job.get('jobId')
    if not job_id:
        raise ValueError(f"動画処理ジョブIDを取得できません: {job}")
    for _ in range(75):
        time.sleep(2)
        status_resp = requests.get(
            f"https://video.bsky.app/xrpc/app.bsky.video.getJobStatus?jobId={job_id}",
            proxies=NO_PROXY,
            timeout=30
        )
        status_resp.raise_for_status()
        status = status_resp.json().get('jobStatus', status_resp.json())
        if status.get('blob'):
            return status['blob']
        if status.get('state') == 'JOB_STATE_FAILED':
            raise ValueError(f"Bluesky動画処理失敗: {status}")
    raise TimeoutError("Bluesky動画処理が150秒以内に完了しませんでした")


def validate_basic_mp4_structure(video_data, content_type, source_url=''):
    """MP4の基本構造（box境界、moov、trak）を検査する。"""
    extension = os.path.splitext(urlparse(source_url).path.lower())[1]
    is_mp4 = content_type in ('video/mp4', 'video/quicktime') or extension in (
        '.mp4', '.mov', '.m4v'
    )
    if not is_mp4:
        if not video_data:
            raise InvalidMediaError("動画データが空")
        return

    offset = 0
    found_moov = False
    found_trak = False
    while offset + 8 <= len(video_data):
        box_size = int.from_bytes(video_data[offset:offset + 4], 'big')
        box_type = video_data[offset + 4:offset + 8]
        header_size = 8
        if box_size == 1:
            if offset + 16 > len(video_data):
                raise InvalidMediaError("MP4の拡張boxヘッダーが切断されている")
            box_size = int.from_bytes(video_data[offset + 8:offset + 16], 'big')
            header_size = 16
        elif box_size == 0:
            box_size = len(video_data) - offset
        if box_size < header_size:
            raise InvalidMediaError("MP4のboxサイズが不正")
        if offset + box_size > len(video_data):
            name = box_type.decode('ascii', errors='replace')
            raise InvalidMediaError(f"MP4の{name} boxが途中で切断されている")
        if box_type == b'moov':
            found_moov = True
            child_offset = offset + header_size
            box_end = offset + box_size
            while child_offset + 8 <= box_end:
                child_size = int.from_bytes(video_data[child_offset:child_offset + 4], 'big')
                child_type = video_data[child_offset + 4:child_offset + 8]
                if child_size < 8 or child_offset + child_size > box_end:
                    raise InvalidMediaError("MP4のmoov内box構造が不正")
                if child_type == b'trak':
                    found_trak = True
                child_offset += child_size
        offset += box_size

    if offset != len(video_data):
        raise InvalidMediaError("MP4末尾のboxデータが切断されている")
    if not found_moov:
        raise InvalidMediaError("MP4にmoov atomがないため再生できない")
    if not found_trak:
        raise InvalidMediaError("MP4のmoovにtrakがないため再生できない")


def validate_video_data(video_data, content_type, source_url=''):
    """後方互換用。MP4の基本構造検証を行う。"""
    return validate_basic_mp4_structure(video_data, content_type, source_url)


def prepare_video_for_bsky(video_url):
    """動画を一度だけ取得し、基本MP4構造検証済みデータを返す。"""
    if isinstance(video_url, tuple):
        errors = []
        for candidate in video_url:
            if not candidate:
                continue
            try:
                return prepare_video_for_bsky(candidate)
            except Exception as error:
                errors.append(str(error))
        raise InvalidMediaError('動画候補がすべて不正: ' + ' / '.join(errors))
    MAX_SIZE = 50 * 1024 * 1024  # 50MB
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://truthsocial.com/'
    }

    # ファイルサイズ事前確認（direct優先、失敗時はproxy）
    try:
        head = requests.head(video_url, headers=headers, proxies=NO_PROXY, timeout=15, allow_redirects=True)
        head.raise_for_status()
    except Exception:
        if not BSKY_PROXIES:
            raise
        head = requests.head(video_url, headers=headers, proxies=BSKY_PROXIES, timeout=15, allow_redirects=True)
        head.raise_for_status()
    size = int(head.headers.get('content-length', 0))
    if size > MAX_SIZE:
        raise ValueError(f"動画サイズ超過: {size / 1024 / 1024:.1f}MB > 50MB")

    try:
        resp = requests.get(video_url, headers=headers, proxies=NO_PROXY, timeout=120)
        resp.raise_for_status()
    except Exception:
        if not BSKY_PROXIES:
            raise
        resp = requests.get(video_url, headers=headers, proxies=BSKY_PROXIES, timeout=120)
        resp.raise_for_status()

    if len(resp.content) > MAX_SIZE:
        raise ValueError(f"動画サイズ超過: {len(resp.content) / 1024 / 1024:.1f}MB > 50MB")

    content_type = resp.headers.get('content-type', 'video/mp4').split(';')[0]
    validate_basic_mp4_structure(resp.content, content_type, video_url)
    return resp.content, content_type


def upload_video_to_bsky(video_url, did, token, pds_audience, prepared_video=None):
    """基本MP4構造検証済み動画をBluesky動画サービスへ送り、blobを返す。"""
    video_data, content_type = prepared_video or prepare_video_for_bsky(video_url)
    # Blueskyの推奨フローで変換完了済みblobだけを返す。失敗時に未変換blobを
    # embedすると「ビデオが見つかりません」になるため、呼び出し元で再試行する。
    return upload_video_via_bsky_service(
        video_data, content_type, did, token, pds_audience
    )


def normalize_image_for_bsky(image_data):
    """画像をBlueskyに安全に添付できるJPEGへ変換し、元の縦横比を返す"""
    with Image.open(io.BytesIO(image_data)) as source:
        source = ImageOps.exif_transpose(source)
        width, height = source.size
        if not width or not height:
            raise ValueError("画像サイズを取得できません")
        aspect_ratio = {'width': width, 'height': height}

        if source.mode in ('RGBA', 'LA') or (source.mode == 'P' and 'transparency' in source.info):
            background = Image.new('RGBA', source.size, 'white')
            background.alpha_composite(source.convert('RGBA'))
            image = background.convert('RGB')
        else:
            image = source.convert('RGB')

        max_dimension = 2048
        if max(image.size) > max_dimension:
            scale = max_dimension / max(image.size)
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.Resampling.LANCZOS
            )

        for _ in range(8):
            for quality in (90, 82, 74, 66, 58, 50):
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=quality, optimize=True)
                if output.tell() <= BSKY_IMAGE_SAFE_SIZE:
                    return output.getvalue(), 'image/jpeg', aspect_ratio
            image = image.resize(
                (max(1, round(image.width * 0.85)), max(1, round(image.height * 0.85))),
                Image.Resampling.LANCZOS
            )
    raise ValueError("画像をBlueskyの容量上限まで圧縮できませんでした")


def upload_image_to_bsky(image_url, did, token, fallback_url=None):
    """画像をダウンロードしてBlueskyにアップロード、blobを返す。"""
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
            if not BSKY_PROXIES:
                raise
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
    image_data, content_type, aspect_ratio = normalize_image_for_bsky(resp.content)
    upload_resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.uploadBlob",
        data=image_data,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': content_type
        },
        proxies=NO_PROXY,
        timeout=30
    )
    upload_resp.raise_for_status()
    return upload_resp.json()['blob'], aspect_ratio


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
            thumb_blob, _ = upload_image_to_bsky(image_url, did, token)
            external['thumb'] = thumb_blob
            log(f"リンクカードサムネイル取得成功")
        except Exception as e:
            log(f"リンクカードサムネイル取得失敗（スキップ）: {e}")
    return {'$type': 'app.bsky.embed.external', 'external': external}


def translate_with_claude(text, has_media=False):
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
    media_context = ""
    if has_media:
        media_context = (
            "\nこの投稿には画像または動画が添付されている。原文がキャプション・見出し・"
            "文の断片なら、説明的な完結文を補わず、断片・キャプション調を維持すること。"
        )
    prompt = (
        "以下はトランプ大統領のTruth Social投稿です。日本語に翻訳してください。\n"
        "ルール：\n"
        "- 自然な日本語にする\n"
        "- 文体は常体（だ・である調）を使う\n"
        "- 投稿のトーンや強調（大文字表現など）を維持する\n"
        "- 原文が示す因果、功績、責任、主体、確信度、評価、強調を弱めず・補わずに保持する\n"
        "- 状況説明への言い換えで、原文の因果関係や誰に功績・責任を帰属させているかを曖昧にしない\n"
        "- [URL_0]、[URL_1]などのプレースホルダーはそのまま保持すること\n"
        "- 人名・国名・機関名は日本の主要メディアの表記に従うこと（例: President Xi → 習主席、Xi Jinping → 習近平）\n"
        "- 翻訳のみを出力し、解説や注釈は不要\n\n"
        f"{media_context}\n\n"
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


def get_pds_audience(session):
    """createSessionのDID文書からユーザーのPDS DIDを取得する。"""
    did_doc = session.get('didDoc') or {}
    for service in did_doc.get('service', []):
        if service.get('id', '').endswith('#atproto_pds'):
            endpoint = service.get('serviceEndpoint', '')
            hostname = urlparse(endpoint).hostname
            if hostname:
                return f'did:web:{hostname}'
    raise ValueError("Blueskyログイン応答からPDSを取得できません")


def bsky_login():
    """Blueskyにログインしてセッション情報を返す"""
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        proxies=NO_PROXY, timeout=30
    )
    resp.raise_for_status()
    session = resp.json()
    return session['did'], session['accessJwt'], get_pds_audience(session)


def mark_post_processed(processed, post):
    """Bluesky投稿に成功した投稿だけを重複判定済みにする。"""
    complete_post(processed, post['id'], post.get('fp'))


def should_retry_media_post(has_source_media, video_blob, image_blobs, expected_images=0):
    """元メディアがあるのにアップロードできなければ、本文だけで確定投稿しない。"""
    if not has_source_media:
        return False
    if video_blob:
        return False
    if expected_images:
        return len(image_blobs) != expected_images
    return not image_blobs


def get_record(did, token, rkey):
    resp = requests.get(
        f"{BSKY_API}/com.atproto.repo.getRecord",
        params={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey},
        headers={'Authorization': f'Bearer {token}'}, proxies=NO_PROXY, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def put_record(did, token, rkey, record, swap_record=None):
    body = {'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey,
            'record': record}
    if swap_record:
        body['swapRecord'] = swap_record
    resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.putRecord",
        json=body,
        headers={'Authorization': f'Bearer {token}'}, proxies=NO_PROXY, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def delete_created_records(did, token, records):
    """この実行で作ったスレッドを返信から逆順に削除し、消失を確認する。"""
    for item in reversed(records):
        rkey = item['rkey']
        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.deleteRecord",
            json={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey},
            headers={'Authorization': f'Bearer {token}'}, proxies=NO_PROXY,
            timeout=30,
        )
        resp.raise_for_status()
        try:
            get_record(did, token, rkey)
        except requests.HTTPError as error:
            response = error.response
            is_not_found = response is not None and response.status_code == 404
            if is_not_found:
                continue
            return False
        except Exception:
            return False
        return False
    return True


def delete_or_mark_alert(state, did, token, records, reason):
    """削除確認失敗も履歴へ残し、verification処理全体は継続する。"""
    try:
        deleted = bool(records) and delete_created_records(did, token, records)
    except Exception as error:
        deleted = False
        reason = f'{reason}; 削除失敗: {error}'
    state['post_status'] = 'BLOCKED'
    state['failure_stage'] = 'VERIFY_DELETE' if deleted else 'ALERT_DELETE_FAILED'
    state['failure_reason'] = reason
    return deleted


def post_to_bluesky(chunks, did, token, image_blobs=None, video_blob=None,
                    external_embed=None, checkpoint=None, source_id='',
                    checkpoint_callback=None):
    """Blueskyに投稿する。複数チャンクの場合はスレッドにする"""
    checkpoint = dict(checkpoint or {})
    root_ref = checkpoint.get('root_ref')
    parent_ref = checkpoint.get('parent_ref')
    start_index = int(checkpoint.get('next_index', 0))
    if start_index >= len(chunks):
        return parent_ref['uri']

    for i in range(start_index, len(chunks)):
        chunk = chunks[i]
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
                    'images': [
                        {'image': blob, 'alt': '', 'aspectRatio': aspect_ratio}
                        for blob, aspect_ratio in image_blobs
                    ]
                }

        # スレッド（リプライ）の場合
        if parent_ref is not None:
            record['reply'] = {
                'root': root_ref,
                'parent': parent_ref
            }

        try:
            rkey = deterministic_post_rkey(source_id or chunks[0], i)
            resp = requests.post(
                f"{BSKY_API}/com.atproto.repo.createRecord",
                json={
                    'repo': did,
                    'collection': 'app.bsky.feed.post',
                    'rkey': rkey,
                    'record': record
                },
                headers={'Authorization': f'Bearer {token}'},
                proxies=NO_PROXY, timeout=30
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as error:
            # createRecord成功後のtimeout/409をgetRecordで照合し、同じrkeyを再利用する。
            conflict_error = None
            try:
                existing = get_record(did, token, rkey)
                if not isinstance(existing.get('uri'), str) or not isinstance(existing.get('cid'), str):
                    raise ValueError('既存record照合結果が不正')
                if not record_matches(existing, record):
                    conflict_error = RuntimeError('決定的rkeyに異なる既存recordがあり競合')
                    raise conflict_error
                result = {'uri': existing['uri'], 'cid': existing['cid']}
            except Exception:
                result = None
            if conflict_error:
                raise conflict_error from error
            if result:
                pass
            else:
                if isinstance(error, requests.HTTPError):
                    message = (
                        f"Bluesky投稿失敗 (HTTP {resp.status_code}): "
                        f"{resp.text[:500]}"
                    )
                else:
                    message = f"Bluesky投稿失敗: {error}"
                if root_ref:
                    failure_checkpoint = dict(checkpoint)
                    failure_checkpoint.update({
                        'root_ref': root_ref, 'parent_ref': parent_ref,
                        'next_index': i,
                    })
                    raise PartialThreadError(message, failure_checkpoint) from error
                raise RuntimeError(message) from error

        ref = {'uri': result['uri'], 'cid': result['cid']}
        if i == 0:
            root_ref = ref
        parent_ref = ref
        prior_checkpoint = checkpoint
        created_records = list((prior_checkpoint or {}).get('created_records', []))
        created_records.append({'uri': ref['uri'], 'rkey': rkey})
        checkpoint = dict(prior_checkpoint or {})
        checkpoint.update({
            'root_ref': root_ref, 'parent_ref': parent_ref, 'next_index': i + 1,
            'last_rkey': rkey, 'last_record': record,
            'root_rkey': (prior_checkpoint or {}).get('root_rkey') or rkey,
            'root_record': (prior_checkpoint or {}).get('root_record') or record,
            'created_records': created_records,
        })
        if checkpoint_callback:
            checkpoint_callback(checkpoint)

        if i < len(chunks) - 1:
            time.sleep(1)

    return result['uri']


def verify_published_embed(post_uri, expected_kind, expected_images=0):
    """公開viewのembedを確認し、動画はplaylist manifestまで取得する。"""
    resp = requests.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts",
        params={'uris': post_uri}, proxies=NO_PROXY, timeout=30,
    )
    resp.raise_for_status()
    posts = resp.json().get('posts', [])
    if not posts:
        return False, '公開APIに投稿が未反映'
    embed = posts[0].get('embed')
    embed_type = (embed or {}).get('$type', '')
    if expected_kind == 'none':
        # 外部リンクカードは許容し、元メディアなしではembed確認を必須にしない。
        return True, None
    if expected_kind == 'images':
        images = (embed or {}).get('images', [])
        actual = len(images)
        if 'images#view' not in embed_type or actual != expected_images:
            return False, f'画像embedが期待{expected_images}枚に対し{actual}枚'
        for image_view in images:
            fullsize = image_view.get('fullsize')
            if fullsize:
                image_resp = requests.get(fullsize, proxies=NO_PROXY, timeout=30)
                image_resp.raise_for_status()
                if not image_resp.content:
                    return False, '画像fullsize URLが空データ'
                content_type = image_resp.headers.get('content-type', '')
                if content_type and not content_type.startswith('image/'):
                    return False, '画像fullsize URLが画像を返していない'
        return True, None
    if expected_kind == 'video':
        playlist = (embed or {}).get('playlist')
        if 'video#view' not in embed_type or not playlist:
            return False, '動画embedまたはplaylistが未反映'
        manifest = requests.get(playlist, proxies=NO_PROXY, timeout=30)
        manifest.raise_for_status()
        if '#EXTM3U' not in manifest.text:
            return False, '動画playlist manifestが不正'
        lines = [line.strip() for line in manifest.text.splitlines()
                 if line.strip() and not line.startswith('#')]
        if not lines:
            return False, '動画master playlistにvariantがない'
        variant_url = urljoin(playlist, lines[0])
        variant = requests.get(variant_url, proxies=NO_PROXY, timeout=30)
        variant.raise_for_status()
        if '#EXTM3U' not in variant.text:
            return False, '動画media playlistが不正'
        segments = [line.strip() for line in variant.text.splitlines()
                    if line.strip() and not line.startswith('#')]
        if not segments:
            return False, '動画variant playlistにsegmentがない'
        segment = requests.get(urljoin(variant_url, segments[0]),
                               proxies=NO_PROXY, timeout=30)
        segment.raise_for_status()
        segment_type = segment.headers.get('content-type', '').lower()
        if not segment.content or 'text/html' in segment_type:
            return False, '動画segmentが空またはHTML'
        return True, None
    return False, f'不明なembed種別: {expected_kind}'


def verify_failure_is_terminal(reason):
    return bool(reason and any(term in reason for term in (
        'manifestが不正', 'playlistにvariantがない', 'playlistにsegmentがない',
        'segmentが空', '画像fullsize URLが画像を返していない',
    )))


def main():
    log("=== Trump翻訳ボット 実行開始 ===")

    manual_post_url = os.environ.get('MANUAL_POST_URL', '').strip()
    if manual_post_url:
        entries = [{'id': manual_post_url, 'link': manual_post_url, 'description': ''}]
        log(f"手動指定投稿を処理: {manual_post_url}")
    else:
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

        entries = feed.entries
        log(f"RSSフィード取得成功: {len(entries)}件")

    processed = load_processed()
    completed_entries = processed_entries(processed)
    verification_jobs = pending_verifications(processed)
    if not manual_post_url:
        rss_ids = {entry.get('id') or entry.get('link', '') for entry in entries}
        restored = pending_entries_from_history(processed, rss_ids)
        if restored:
            # reversed処理で新規RSSを先にする。backlogは一実行10件まで。
            entries = restored[:10] + list(entries)
            log(f"履歴からRSS圏外の未解決投稿を復元: {len(restored)}件")
    new_posts = []
    seen_source_keys = set()

    # RSSに見えた全候補を先に履歴化し、1件処理上限でも次回以降に残す。
    if isinstance(processed, dict):
        for raw_entry in entries:
            raw_id = raw_entry.get('id') or raw_entry.get('link', '')
            if raw_id in completed_entries or has_feed_alias(processed, raw_id):
                continue
            processed['posts'].setdefault(raw_id, {
                'post_status': 'DISCOVERED', 'media_state': MediaState.PENDING.value,
                'status_url': raw_entry.get('link', ''),
                'source_text': raw_entry.get('description') or raw_entry.get('summary', ''),
                'published': raw_entry.get('published', ''), 'retry_count': 0,
                'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            })
        save_processed(processed)

    for entry in reversed(entries):  # 古い順に処理
        feed_post_id = entry.get('id') or entry.get('link', '')
        # 旧mirror URLやfingerprintが処理済みならネットワーク解決前に終了。
        raw_content = entry.get('description') or entry.get('summary', '')
        raw_text = BeautifulSoup(raw_content, 'html.parser').get_text(separator='\n').strip()
        raw_fp = text_fingerprint(raw_text) if raw_text else None
        force_known_reevaluation = should_force_known_reevaluation(
            processed, feed_post_id
        )
        if feed_post_id in completed_entries or (
            raw_fp and raw_fp in completed_entries
            and not force_known_reevaluation
            and not entry.get('_blocked_probe')
        ):
            continue
        status_link = entry.get('link', '')
        state_key, saved_state = find_source_state(
            processed, feed_post_id, status_link
        )
        ts_post_id = saved_state.get('truth_social_id') or get_ts_post_id(status_link)
        post_id = canonical_source_key(status_link or feed_post_id, ts_post_id)
        if isinstance(processed, dict):
            matched_key, matched_state = find_source_state(
                processed, feed_post_id, status_link, ts_post_id
            )
            current = processed['posts'].setdefault(post_id, {})
            source_states = []
            for key in (feed_post_id, state_key, matched_key):
                if key and key != post_id and key in processed['posts']:
                    source_states.append(processed['posts'].pop(key))
            for source_state in source_states:
                current.update(source_state)
            if feed_post_id != post_id:
                current['feed_post_id'] = feed_post_id
        if post_id in seen_source_keys:
            continue
        seen_source_keys.add(post_id)
        if new_posts:
            continue  # 1実行につき新規投稿候補は1件に限定
        if post_id in completed_entries or feed_post_id in completed_entries:
            continue
        preliminary_content = entry.get('description') or entry.get('summary', '')
        preliminary_media = extract_video(preliminary_content)
        if not preliminary_media:
            preliminary_images = extract_images(preliminary_content)
            preliminary_media = str(preliminary_images) if preliminary_images else None
        if not retry_due(
            processed, post_id, media_identity=preliminary_media,
            manual=bool(manual_post_url or entry.get('_blocked_probe')),
        ):
            log(f"再試行バックオフ中: {post_id}")
            continue

        content = preliminary_content
        if not content and isinstance(processed, dict):
            content = processed['posts'].get(post_id, {}).get('source_text', '')
        if not content:
            media = resolve_post_media(status_link, ts_post_id)
            if media['state'] != MediaState.READY:
                reason = media['reason'] or '本文も添付メディアも取得できない'
                record_post_state(
                    processed, post_id, media['state'], reason, ts_post_id,
                    status_url=status_link, source_text='',
                    published=entry.get('published', ''), fingerprint=None,
                )
                log(f"本文なし投稿を保留: {reason}")
                continue
            record_post_state(
                processed, post_id, media['state'], truth_social_id=ts_post_id,
                status_url=status_link, source_text='',
                published=entry.get('published', ''), fingerprint=None,
                media_identity=canonical_media_identity(
                    ts_post_id, media['video_url'] or media['image_urls']
                ),
            )
            new_posts.append({
                'id': post_id,
                'fp': None,
                'text': '',
                'link': entry.get('link', ''),
                'published': entry.get('published', ''),
                'video_url': media['video_url'],
                'image_urls': media['image_urls'],
                'media_state': media['state'],
                'truth_social_id': ts_post_id,
                'rt_display_name': media['rt_display_name'],
                'rt_acct': media['rt_acct'],
            })
            continue

        soup = BeautifulSoup(content, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if href.startswith('http'):
                a.replace_with(href)
        text = normalize_urls(soup.get_text(separator='\n').strip())

        fp = text_fingerprint(text) if text else None
        status_host = (urlparse(status_link).hostname or '').lower()
        if (
            not ts_post_id
            and status_host in ('trumpstruth.org', 'www.trumpstruth.org')
        ):
            # ミラーの取得失敗を「元メディアなし」と解釈すると、動画・画像付き投稿を
            # 本文だけで確定してしまう。メタデータを確認できる次回まで保留する。
            reason = 'Truth Social投稿IDを取得できず、メディア有無が不明'
            record_post_state(
                processed, post_id, MediaState.PENDING, reason, ts_post_id,
                status_url=status_link, source_text=content,
                published=entry.get('published', ''), fingerprint=fp,
            )
            log(f"{reason}、次回再試行: {post_id}")
            continue
        media = resolve_post_media(status_link, ts_post_id, content)
        if media['state'] in (MediaState.PENDING, MediaState.INVALID):
            record_post_state(
                processed, post_id, media['state'], media['reason'], ts_post_id,
                status_url=status_link, source_text=content,
                published=entry.get('published', ''), fingerprint=fp,
            )
            log(f"メディア確認を保留または停止: {media['reason']}")
            continue
        if media['state'] == MediaState.NO_MEDIA:
            if not confirm_no_media(processed, post_id):
                record_post_state(
                    processed, post_id, MediaState.PENDING,
                    'メディアなしの確認猶予中', ts_post_id,
                    status_url=status_link, source_text=content,
                    published=entry.get('published', ''), fingerprint=fp,
                )
                continue
        video_url = media['video_url']
        image_urls = media['image_urls']
        media_identity = canonical_media_identity(
            ts_post_id, video_url or image_urls
        )
        update_source_identity(processed, post_id, fp, media_identity)

        # 同文でもTruth IDまたはメディアが異なる投稿は独立投稿として扱う。
        force_reevaluate = force_known_reevaluation
        if not ts_post_id and not media_identity and not force_reevaluate and fp and (
            fp in completed_entries or is_similar_to_processed(fp, completed_entries)
        ):
            log(f"重複スキップ（IDなし内容重複）: {text[:60]}...")
            completed_entries.append(post_id)
            continue
        record_post_state(
            processed, post_id, media['state'], truth_social_id=ts_post_id,
            status_url=status_link, source_text=content,
            published=entry.get('published', ''), fingerprint=fp,
            media_identity=media_identity,
        )

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
            'media_state': media['state'],
            'truth_social_id': ts_post_id,
            'rt_display_name': media['rt_display_name'],
            'rt_acct': media['rt_acct'],
        })

    if not new_posts and not verification_jobs:
        log("新規投稿なし")
        save_processed(processed)
        return

    log(f"新規投稿: {len(new_posts)}件, 投稿後確認待ち: {len(verification_jobs)}件")

    # Blueskyログイン
    try:
        did, token, pds_audience = bsky_login()
        log(f"Blueskyログイン成功 (DID: {did})")
    except Exception as e:
        log(f"Blueskyログインエラー: {e}")
        for post in new_posts:
            record_failure(
                processed, post['id'], 'LOGIN',
                f'Blueskyログイン失敗: {e}',
                truth_social_id=post.get('truth_social_id')
            )
        save_processed(processed)
        return

    for job in verification_jobs:
        source_key = job['source_key']
        if not retry_due(processed, source_key):
            continue
        verification_state = processed['posts'][source_key]
        try:
            verified, reason = verify_published_embed(
                job['root_uri'], job.get('expected_embed', 'none'),
                int(job.get('expected_images', 0)),
            )
        except Exception as error:
            verified, reason = False, str(error)
        safe_repair = (
            not verified and job.get('root_rkey') and job.get('root_record')
            and not verification_state.get('repair_attempted')
            and reason and ('画像embed' in reason or '動画embed' in reason)
        )
        if safe_repair and begin_repair_attempt(processed, source_key, save_processed):
            try:
                current = get_record(did, token, job['root_rkey'])
                repaired = put_record(
                    did, token, job['root_rkey'], job['root_record'],
                    swap_record=current['cid'],
                )
                if repaired.get('uri'):
                    job['root_uri'] = repaired['uri']
                verified, reason = verify_published_embed(
                    job['root_uri'], job.get('expected_embed', 'none'),
                    int(job.get('expected_images', 0)),
                )
            except Exception as error:
                reason = str(error)
        if verified:
            complete_post(processed, source_key, job.get('fingerprint'))
        else:
            record_failure(processed, source_key, 'VERIFY', reason)
            state = processed['posts'][source_key]
            if state.get('retry_count', 0) >= 6 or verify_failure_is_terminal(reason):
                delete_or_mark_alert(
                    state, did, token, state.get('created_records', []), reason
                )
    save_processed(processed)

    if not new_posts:
        log("投稿後確認待ちを処理して完了")
        return

    for post in new_posts:
        checkpoint = processed.get('posts', {}).get(post['id'], {}).get(
            'thread_checkpoint'
        ) if isinstance(processed, dict) else None
        video_blob = None
        image_blobs = []
        media_upload_error = None
        prepared_video = None
        video_fallback_reason = None
        if checkpoint:
            log("投稿済みrootを再利用し、未投稿の返信から再開")
        elif post.get('video_url'):
            try:
                prepared_video = prepare_video_for_bsky(post['video_url'])
                log(f"動画基本構造確認成功: {str(post['video_url'])[:60]}")
            except InvalidMediaError as e:
                if can_fallback_video_to_text(post.get('text', '')):
                    video_fallback_reason = str(e)
                    log(f"動画が破損しているため本文のみ投稿へ切替: {e}")
                else:
                    record_post_state(
                        processed, post['id'], MediaState.INVALID, str(e),
                        post.get('truth_social_id'),
                    )
                    save_processed(processed)
                    log(f"動画が破損し本文もないため投稿を保留: {e}")
                    continue
            except Exception as e:
                if can_fallback_video_to_text(post.get('text', '')):
                    video_fallback_reason = f'動画取得・検証失敗: {e}'
                    log(f"動画を確認できないため本文のみ投稿へ切替: {e}")
                else:
                    record_post_state(
                        processed, post['id'], MediaState.PENDING,
                        f'動画取得・検証失敗: {e}', post.get('truth_social_id'),
                    )
                    save_processed(processed)
                    log(f"動画を確認できず本文もないため投稿を保留: {e}")
                    continue

            if not video_fallback_reason:
                try:
                    video_blob = upload_video_to_bsky(
                        post['video_url'], did, token, pds_audience,
                        prepared_video=prepared_video,
                    )
                    log(f"動画アップロード成功: {str(post['video_url'])[:60]}")
                except Exception as e:
                    media_upload_error = e
                    if can_fallback_video_to_text(post.get('text', '')):
                        video_fallback_reason = f'動画アップロード失敗: {e}'
                        log(f"動画をアップロードできないため本文のみ投稿へ切替: {e}")
        elif not checkpoint:
            for url_pair in post.get('image_urls', []):
                primary, fallback = url_pair if isinstance(url_pair, tuple) else (url_pair, None)
                try:
                    blob, aspect_ratio = upload_image_to_bsky(
                        primary, did, token, fallback_url=fallback
                    )
                    image_blobs.append((blob, aspect_ratio))
                    log(f"画像アップロード成功: {primary[:60]}")
                except Exception as e:
                    media_upload_error = e

        has_source_media = bool(post.get('video_url') or post.get('image_urls'))
        effective_has_source_media = has_source_media and not video_fallback_reason
        if not checkpoint and should_retry_media_post(
            effective_has_source_media, video_blob, image_blobs,
            expected_images=len(post.get('image_urls', [])),
        ):
            record_failure(
                processed, post['id'], 'MEDIA_UPLOAD',
                f'Blueskyメディアアップロード失敗: {media_upload_error}',
                truth_social_id=post.get('truth_social_id'),
            )
            save_processed(processed)
            log("元メディアを全件アップロードできず、翻訳・投稿を保留")
            continue

        if video_fallback_reason:
            record_post_state(
                processed, post['id'], MediaState.INVALID,
                truth_social_id=post.get('truth_social_id'),
                allow_text_fallback=True,
                fallback_policy_version=TEXT_FALLBACK_POLICY_VERSION,
                media_fallback_reason=video_fallback_reason,
            )
            save_processed(processed)

        log(f"翻訳中: {post['text'][:80]}...")

        # URLとホワイトスペース（\xa0等Unicode空白含む）を除いた実質的なテキストがあるか確認
        post_urls = re.findall(r'https?://\S+', post['text'])
        meaningful_text = re.sub(r'https?://\S+', '', post['text'])
        # "RT:" などの短いプレフィックスも除外
        meaningful_text = re.sub(r'\bRT:\s*', '', meaningful_text)
        meaningful_text = re.sub(r'[\s\xa0]+', '', meaningful_text)
        cached_translation = (
            processed.get('posts', {}).get(post['id'], {}).get('translation')
            if isinstance(processed, dict) else None
        )
        if cached_translation:
            translation = cached_translation
            log("保存済み翻訳を再利用")
        elif not meaningful_text:
            # テキストなし（画像のみ or URLのみ or RTのみ）→ 翻訳不要
            translation = ('\n'.join(post_urls) if post_urls else media_only_text(
                'video' if post.get('video_url') else 'images'
            ))
            log("テキストなし（画像のみ/URLのみ/RTのみ）のため翻訳スキップ")
        else:
            has_media = bool(video_blob or image_blobs)
            translation = translate_with_claude(post['text'], has_media=has_media)
            if translation and translation not in ("RATE_LIMITED",):
                translation = restore_urls(translation, post_urls)
                if not has_japanese(translation):
                    log(f"翻訳結果に日本語なし（LLMエラー応答）、スキップ: {translation[:80]}")
                    record_failure(
                        processed, post['id'], 'TRANSLATION',
                        '翻訳結果に日本語がない', truth_social_id=post.get('truth_social_id')
                    )
                    save_processed(processed)
                    continue
        if translation == "RATE_LIMITED":
            log("Claude APIレート制限、残りの投稿は次回処理")
            # 未処理投稿のfpをprocessedから除去（次回リトライさせるため）
            pending_fps = {
                p['fp'] for p in new_posts if p['id'] not in completed_entries
            }
            completed_entries[:] = [
                entry for entry in completed_entries if entry not in pending_fps
            ]
            record_failure(
                processed, post['id'], 'TRANSLATION',
                'Claude APIレート制限', truth_social_id=post.get('truth_social_id')
            )
            save_processed(processed)
            return
        if translation is None:
            log("翻訳失敗、次回再試行")
            record_failure(
                processed, post['id'], 'TRANSLATION',
                'Claude翻訳失敗', truth_social_id=post.get('truth_social_id')
            )
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
            record_failure(
                processed, post['id'], 'TRANSLATION',
                f'Claude拒否: {translation[:120]}', truth_social_id=post.get('truth_social_id')
            )
            save_processed(processed)
            continue

        record_post_state(
            processed, post['id'],
            MediaState.INVALID if video_fallback_reason else post.get(
                'media_state', MediaState.NO_MEDIA
            ),
            truth_social_id=post.get('truth_social_id'), translation=translation
        )
        save_processed(processed)

        # メディアはClaude呼び出し前にアップロード済み。
        external_embed = None
        external_url = select_external_card_url(post['text'])
        if not video_blob and not image_blobs and external_url and not has_source_media:
            try:
                external_embed = make_external_embed(external_url, did, token)
                log(f"外部リンクカード作成: {external_url}")
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
            latest_checkpoint = checkpoint
            def persist_checkpoint(value):
                nonlocal latest_checkpoint
                latest_checkpoint = value
                if isinstance(processed, dict):
                    processed['posts'][post['id']]['thread_checkpoint'] = value
                save_processed(processed)

            post_uri = post_to_bluesky(
                chunks, did, token, image_blobs, video_blob, external_embed,
                checkpoint=checkpoint, source_id=post['id'],
                checkpoint_callback=persist_checkpoint,
            )
            log(f"投稿成功 (URI: {post_uri})")
        except PartialThreadError as e:
            record_failure(
                processed, post['id'], 'POST',
                f'Blueskyスレッド途中失敗: {e}',
                truth_social_id=post.get('truth_social_id'),
                thread_checkpoint=e.checkpoint,
            )
            save_processed(processed)
            log(f"Blueskyスレッド途中失敗、次回途中から再開: {e}")
            continue
        except Exception as e:
            record_failure(
                processed, post['id'], 'POST', f'Bluesky投稿失敗: {e}',
                truth_social_id=post.get('truth_social_id')
            )
            save_processed(processed)
            log(f"Bluesky投稿エラー: {e}")
            continue

        root_uri = (
            (latest_checkpoint or {}).get('root_ref', {}).get('uri') or post_uri
        )
        expected_kind = ('video' if video_blob else 'images' if image_blobs else 'none')
        expected_image_count = len(image_blobs)
        verified = False
        verify_reason = None
        for attempt in range(4):
            try:
                verified, verify_reason = verify_published_embed(
                    root_uri, expected_kind, expected_image_count
                )
            except Exception as error:
                verify_reason = str(error)
            if verified:
                break
            if attempt < 3:
                time.sleep(2)
        if not verified:
            root_rkey = (latest_checkpoint or {}).get('root_rkey')
            root_record = (latest_checkpoint or {}).get('root_record')
            if (root_rkey and root_record and verify_reason
                    and ('画像embed' in verify_reason or '動画embed' in verify_reason)
                    and begin_repair_attempt(processed, post['id'], save_processed)):
                try:
                    current = get_record(did, token, root_rkey)
                    repaired = put_record(
                        did, token, root_rkey, root_record,
                        swap_record=current['cid'],
                    )
                    if repaired.get('uri'):
                        root_uri = repaired['uri']
                    verified, verify_reason = verify_published_embed(
                        root_uri, expected_kind,
                        expected_image_count
                    )
                except Exception as error:
                    verify_reason = str(error)
        if not verified:
            record_failure(
                processed, post['id'], 'VERIFY',
                f'投稿後embed確認待ち: {verify_reason}',
                truth_social_id=post.get('truth_social_id'),
                thread_checkpoint=latest_checkpoint,
                root_uri=root_uri, expected_embed=expected_kind,
                expected_images=expected_image_count,
                root_rkey=(latest_checkpoint or {}).get('root_rkey'),
                root_record=(latest_checkpoint or {}).get('root_record'),
                created_records=(latest_checkpoint or {}).get('created_records', []),
            )
            if verify_failure_is_terminal(verify_reason):
                state = processed['posts'][post['id']]
                delete_or_mark_alert(
                    state, did, token,
                    (latest_checkpoint or {}).get('created_records', []),
                    verify_reason,
                )
            save_processed(processed)
            log(f"投稿後embedを確認できず、完了扱いにしない: {verify_reason}")
            continue

        mark_post_processed(processed, post)
        save_processed(processed)
        time.sleep(3)

    save_processed(processed)
    log("=== 完了 ===")


if __name__ == "__main__":
    main()
