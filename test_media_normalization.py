import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
import requests

import trump_truth_translator as translator


class NormalizeImageForBlueskyTests(unittest.TestCase):
    def test_saved_truth_id_and_text_survive_mirror_to_canonical_merge(self):
        mirror = "url:https://www.trumpstruth.org/statuses/40757"
        feed_id = "truth:117082899005949110"
        history = translator.new_processing_history([])
        history["posts"][mirror] = {
            "post_status": "RETRY",
            "feed_post_id": feed_id,
            "truth_social_id": "117082899005949110",
            "source_text": "<p>Saved caption</p>",
            "translation": "保存済み訳",
        }

        key, state = translator.find_source_state(
            history, feed_id, "https://www.trumpstruth.org/statuses/40757"
        )

        self.assertEqual(key, mirror)
        self.assertEqual(state["truth_social_id"], "117082899005949110")
        self.assertEqual(state["source_text"], "<p>Saved caption</p>")

    def test_manual_retry_bypasses_backoff_but_not_completed_post(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {
            "post_status": "RETRY",
            "next_retry_at": "2099-01-01T00:00:00Z",
        }
        self.assertTrue(translator.retry_due(history, "truth:1", manual=True))
        history["posts"]["truth:1"]["post_status"] = "POSTED"
        self.assertFalse(translator.retry_due(history, "truth:1", manual=True))

    @patch("trump_truth_translator.verify_published_embed", return_value=(True, None))
    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch("trump_truth_translator.translate_with_claude")
    @patch(
        "trump_truth_translator.bsky_login",
        return_value=("did:example", "token", "did:web:pds.example"),
    )
    @patch("trump_truth_translator.resolve_post_media")
    @patch("trump_truth_translator.load_processed")
    def test_manual_truth_url_reuses_saved_mirror_text(
        self, mock_load, mock_media, mock_login, mock_translate, mock_post,
        mock_verify,
    ):
        truth_id = "117082899005949110"
        mirror = "url:https://www.trumpstruth.org/statuses/40757"
        direct = f"https://truthsocial.com/@realDonaldTrump/posts/{truth_id}"
        history = translator.new_processing_history([])
        history["posts"][mirror] = {
            "post_status": "RETRY",
            "media_state": "NO_MEDIA",
            "truth_social_id": truth_id,
            "feed_post_id": f"truth:{truth_id}",
            "status_url": "https://www.trumpstruth.org/statuses/40757",
            "source_text": "",
            "translation": "保存済み訳",
            "first_seen": "2026-08-13T00:00:00Z",
            "no_media_confirmations": 0,
        }
        mock_load.return_value = history
        mock_media.return_value = {
            "state": translator.MediaState.NO_MEDIA,
            "reason": None,
            "video_url": None,
            "image_urls": [],
            "rt_display_name": None,
            "rt_acct": None,
        }

        with (
            patch.dict(os.environ, {"MANUAL_POST_URL": direct}),
            patch("trump_truth_translator.save_processed"),
        ):
            translator.main()

        mock_translate.assert_not_called()
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.args[0], ["保存済み訳"])
        self.assertEqual(history["posts"][f"truth:{truth_id}"]["post_status"], "POSTED")

    def test_unsupported_nonempty_attachment_is_not_no_media(self):
        result = translator.classify_ts_media({"media_attachments": [{"type": "audio", "url": "https://x/a.mp3"}]})
        self.assertEqual(result["state"], translator.MediaState.INVALID)

    def test_missing_attachment_url_is_pending(self):
        result = translator.classify_ts_media({"media_attachments": [{"type": "image"}]})
        self.assertEqual(result["state"], translator.MediaState.PENDING)

    def test_media_identity_is_canonical_string(self):
        identity = translator.canonical_media_identity(
            "1171", [("https://cdn/x/photo.jpg?token=a", "https://archive/photo.jpg")]
        )
        self.assertIsInstance(identity, str)
        self.assertIn("photo.jpg", identity)
        self.assertNotIn("token", identity)

    def test_fallback_url_change_does_not_clear_post_artifacts(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {"source_fingerprint": "same", "media_identity": "attachment:photo.jpg", "root_uri": "at://root"}
        translator.update_source_identity(history, "truth:1", "same", "attachment:photo.jpg")
        self.assertEqual(history["posts"]["truth:1"]["root_uri"], "at://root")

    @patch("trump_truth_translator.get_record")
    @patch("trump_truth_translator.requests.post")
    def test_delete_created_records_in_reverse_order(self, mock_post, mock_get):
        mock_post.return_value.raise_for_status.return_value = None
        response = requests.Response()
        response.status_code = 404
        mock_get.side_effect = requests.HTTPError("not found", response=response)
        records = [{"rkey": "root"}, {"rkey": "reply"}]
        self.assertTrue(translator.delete_created_records("did:x", "token", records))
        self.assertEqual(mock_post.call_args_list[0].kwargs["json"]["rkey"], "reply")

    @patch("trump_truth_translator.get_record", side_effect=requests.Timeout("確認timeout"))
    @patch("trump_truth_translator.requests.post")
    def test_delete_timeout_is_not_treated_as_deleted(self, mock_post, _mock_get):
        mock_post.return_value.raise_for_status.return_value = None
        self.assertFalse(translator.delete_created_records(
            "did:x", "token", [{"rkey": "root"}]
        ))

    @patch("trump_truth_translator.get_record")
    @patch("trump_truth_translator.requests.post")
    def test_delete_confirmation_requires_http_404_even_if_body_says_record_not_found(
        self, mock_post, mock_get
    ):
        mock_post.return_value.raise_for_status.return_value = None
        response = MagicMock(status_code=400)
        response.json.return_value = {"error": "RecordNotFound"}
        mock_get.side_effect = requests.HTTPError("bad request", response=response)
        self.assertFalse(translator.delete_created_records(
            "did:x", "token", [{"rkey": "root"}]
        ))

    @patch("trump_truth_translator.delete_created_records", side_effect=requests.Timeout("削除timeout"))
    def test_delete_failure_is_saved_as_alert_without_escaping(self, _mock_delete):
        state = {"post_status": "POST_VERIFY_PENDING"}
        self.assertFalse(translator.delete_or_mark_alert(
            state, "did:x", "token", [{"rkey": "root"}], "動画manifest不良"
        ))
        self.assertEqual(state["post_status"], "BLOCKED")
        self.assertEqual(state["failure_stage"], "ALERT_DELETE_FAILED")
        self.assertIn("削除timeout", state["failure_reason"])

    def test_v2_history_post_verify_complete_save_round_trip(self):
        history = translator.new_processing_history([])
        translator.record_post_state(
            history, "truth:1171", translator.MediaState.READY,
            post_status="POST_VERIFY_PENDING", root_uri="at://root",
            expected_embed="images", expected_images=1,
        )
        with patch("trump_truth_translator.verify_published_embed", return_value=(True, None)):
            verified, reason = translator.verify_published_embed("at://root", "images", 1)
        self.assertTrue(verified, reason)
        translator.complete_post(history, "truth:1171", "fp:1171")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "processed.json")
            with patch.object(translator, "PROCESSED_FILE", path):
                translator.save_processed(history)
                loaded = translator.load_processed()
        self.assertEqual(loaded["posts"]["truth:1171"]["post_status"], "POSTED")
        self.assertIn("truth:1171", loaded["processed"])
        self.assertIn("fp:1171", loaded["processed"])

    @patch("trump_truth_translator.get_record", side_effect=requests.Timeout("照合timeout"))
    @patch("trump_truth_translator.requests.post", side_effect=requests.Timeout("create timeout"))
    def test_partial_thread_error_preserves_complete_checkpoint(self, _mock_post, _mock_get):
        checkpoint = {
            "root_ref": {"uri": "at://root", "cid": "root-cid"},
            "parent_ref": {"uri": "at://root", "cid": "root-cid"},
            "next_index": 1,
            "root_rkey": "root-rkey",
            "root_record": {"text": "root"},
            "created_records": [{"uri": "at://root", "rkey": "root-rkey"}],
            "expected_embed": "video",
        }
        with self.assertRaises(translator.PartialThreadError) as raised:
            translator.post_to_bluesky(
                ["root", "reply"], "did:x", "token", checkpoint=checkpoint,
                source_id="truth:1171",
            )
        saved = raised.exception.checkpoint
        self.assertEqual(saved["next_index"], 1)
        self.assertEqual(saved["root_rkey"], "root-rkey")
        self.assertEqual(saved["root_record"], {"text": "root"})
        self.assertEqual(saved["created_records"], checkpoint["created_records"])
        self.assertEqual(saved["expected_embed"], "video")

    def test_two_ticks_attempt_repair_network_at_most_once_and_preserve_failure(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {"post_status": "POST_VERIFY_PENDING"}
        saves = []
        repair_network = MagicMock(side_effect=requests.Timeout("put timeout"))
        for _tick in range(2):
            if translator.begin_repair_attempt(
                history, "truth:1",
                lambda value: saves.append(json.loads(json.dumps(value))),
            ):
                try:
                    repair_network()
                except requests.Timeout as error:
                    translator.record_failure(history, "truth:1", "VERIFY", str(error))
        self.assertTrue(saves[0]["posts"]["truth:1"]["repair_attempted"])
        self.assertEqual(saves[0]["posts"]["truth:1"]["repair_attempts"], 1)
        repair_network.assert_called_once_with()
        self.assertTrue(history["posts"]["truth:1"]["repair_attempted"])
        self.assertEqual(history["posts"]["truth:1"]["repair_attempts"], 1)
        self.assertEqual(history["posts"]["truth:1"]["failure_reason"], "put timeout")

    def test_empty_media_post_has_empty_text(self):
        self.assertEqual(translator.media_only_text("video"), "")

    def test_workflow_has_no_github_schedule(self):
        with open(".github/workflows/translate.yml") as file:
            workflow = file.read()
        self.assertNotIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)

    @patch("trump_truth_translator.anthropic.DefaultHttpxClient")
    @patch("trump_truth_translator.anthropic.Anthropic")
    def test_translation_uses_sdk_compatible_http_client(
        self, mock_anthropic, mock_default_http_client
    ):
        sdk_http_client = mock_default_http_client.return_value
        mock_anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="テスト訳")]
        )

        translator.translate_with_claude("Test")

        mock_default_http_client.assert_called_once_with(proxy=None)
        mock_anthropic.assert_called_once_with(
            api_key=translator.ANTHROPIC_API_KEY,
            http_client=sdk_http_client,
        )

    def test_production_legacy_fixture_keeps_existing_mirror_ids(self):
        with open(translator.PROCESSED_FILE) as file:
            legacy = json.load(file)
        history = translator.normalize_processing_history(legacy)
        legacy_items = legacy.get("processed", []) if isinstance(legacy, dict) else legacy
        sample = next(item for item in legacy_items if isinstance(item, str) and "trumpstruth.org/statuses/" in item and "40727" not in item)
        self.assertIn(sample, history["processed"])

    def test_old_no_media_still_requires_three_observations_and_15_minutes(self):
        history = translator.new_processing_history([])
        now = translator.datetime(2026, 1, 1, tzinfo=translator.timezone.utc)
        for minute in (0, 10, 14):
            self.assertFalse(translator.confirm_no_media(history, "truth:old", now=now + translator.timedelta(minutes=minute)))
        self.assertTrue(translator.confirm_no_media(history, "truth:old", now=now + translator.timedelta(minutes=15)))

    def test_source_change_clears_all_post_artifacts(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {
            "source_fingerprint": "old", "translation": "訳", "thread_checkpoint": {},
            "root_uri": "at://root", "root_record": {}, "expected_embed": "video",
            "post_status": "POST_VERIFY_PENDING", "failure_stage": "VERIFY",
        }
        translator.update_source_identity(history, "truth:1", "new", "media:new")
        state = history["posts"]["truth:1"]
        for key in ("translation", "thread_checkpoint", "root_uri", "root_record", "expected_embed", "failure_stage"):
            self.assertNotIn(key, state)
        self.assertEqual(state["post_status"], "READY")

    def test_complete_transition_adds_canonical_and_fp_and_clears_retry(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {"next_retry_at": "future", "failure_reason": "x"}
        translator.complete_post(history, "truth:1", "fp:text")
        self.assertIn("truth:1", history["processed"])
        self.assertIn("fp:text", history["processed"])
        self.assertNotIn("next_retry_at", history["posts"]["truth:1"])

    def test_stage_change_resets_retry_count(self):
        history = translator.new_processing_history([])
        translator.record_failure(history, "p", "MEDIA", "x")
        translator.record_failure(history, "p", "MEDIA", "x")
        translator.record_failure(history, "p", "VERIFY", "x")
        self.assertEqual(history["posts"]["p"]["retry_count"], 1)

    def test_empty_video_post_uses_neutral_media_text(self):
        self.assertEqual(translator.media_only_text("video"), "")
        self.assertEqual(translator.media_only_text("images"), "")

    @patch("trump_truth_translator.put_record")
    @patch("trump_truth_translator.get_record")
    @patch("trump_truth_translator.requests.post")
    def test_create_conflict_does_not_auto_put(self, mock_post, mock_get, mock_put):
        mock_post.side_effect = requests.Timeout("timeout")
        mock_get.return_value = {"uri": "at://x", "cid": "cid", "value": {"text": "wrong"}}
        with self.assertRaisesRegex(RuntimeError, "競合"):
            translator.post_to_bluesky(["expected"], "did:x", "token", source_id="truth:1")
        mock_put.assert_not_called()

    def test_canonical_source_key_uses_truth_id_across_urls(self):
        self.assertEqual(
            translator.canonical_source_key("https://truthsocial.com/@realDonaldTrump/posts/1171", "1171"),
            translator.canonical_source_key("https://www.trumpstruth.org/statuses/9", "1171"),
        )

    def test_fresh_zero_media_needs_three_confirmations_and_fifteen_minutes(self):
        history = translator.new_processing_history([])
        now = translator.datetime(2026, 1, 1, tzinfo=translator.timezone.utc)
        self.assertFalse(translator.confirm_no_media(history, "truth:1", now=now))
        self.assertFalse(translator.confirm_no_media(history, "truth:1", now=now + translator.timedelta(minutes=10)))
        self.assertFalse(translator.confirm_no_media(history, "truth:1", now=now + translator.timedelta(minutes=14)))
        self.assertTrue(translator.confirm_no_media(history, "truth:1", now=now + translator.timedelta(minutes=15)))

    def test_source_change_invalidates_cached_translation(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {"source_fingerprint": "old", "translation": "古い訳"}
        translator.update_source_identity(history, "truth:1", "new", "media:new")
        self.assertNotIn("translation", history["posts"]["truth:1"])

    def test_stable_record_key_is_valid_tid(self):
        rkey = translator.deterministic_post_rkey("truth:1171", 0)
        self.assertRegex(rkey, r"^[234567abcdefghij][234567abcdefghijklmnopqrstuvwxyz]{12}$")

    def test_truth_rkey_encodes_truth_post_timestamp(self):
        truth_id = 117080183664102958
        rkey = translator.deterministic_post_rkey(f"truth:{truth_id}", 0)

        self.assertEqual(
            translator.decode_tid(rkey) >> 10,
            (truth_id >> 16) * 1000,
        )

    def test_migrates_old_invalid_rkey_failures_to_immediate_retry_once(self):
        history = {
            "version": 2,
            "processed": [],
            "posts": {"truth:1": {
                "post_status": "RETRY",
                "retry_count": 5,
                "next_retry_at": "2099-01-01T00:00:00Z",
                "failure_reason": (
                    'Bluesky投稿失敗: Invalid TID string '
                    '(got \\"ttt-deadbeef\\")'
                ),
            }},
        }

        migrated = translator.normalize_processing_history(history)
        state = migrated["posts"]["truth:1"]

        self.assertEqual(state["retry_count"], 0)
        self.assertNotIn("next_retry_at", state)
        self.assertEqual(state["rkey_policy_version"], 1)

    def test_existing_record_must_match_expected_record(self):
        expected = {"$type": "app.bsky.feed.post", "text": "expected", "langs": ["ja"]}
        existing = {"value": {"$type": "app.bsky.feed.post", "text": "other", "langs": ["ja"]}}
        self.assertFalse(translator.record_matches(existing, expected))

    def test_post_verify_pending_rehydrates_verification_only(self):
        history = translator.new_processing_history([])
        history["posts"]["truth:1"] = {
            "post_status": "POST_VERIFY_PENDING", "root_uri": "at://root",
            "expected_embed": "images", "expected_images": 2,
        }
        self.assertEqual(translator.pending_verifications(history)[0]["root_uri"], "at://root")

    def test_processed_dedupe_history_exceeds_legacy_500(self):
        history = translator.new_processing_history([f"post-{i}" for i in range(900)])
        self.assertEqual(len(translator.prepare_history_for_save(history)["processed"]), 900)

    @patch("trump_truth_translator.requests.get")
    def test_video_verification_fetches_variant_and_segment(self, mock_get):
        post = MagicMock(); post.raise_for_status.return_value = None
        post.json.return_value = {"posts": [{"embed": {"$type": "app.bsky.embed.video#view", "playlist": "https://v/master.m3u8"}}]}
        master = MagicMock(text="#EXTM3U\nvariant.m3u8"); master.raise_for_status.return_value = None
        variant = MagicMock(text="#EXTM3U\nsegment.ts"); variant.raise_for_status.return_value = None
        segment = MagicMock(content=b"video"); segment.raise_for_status.return_value = None
        mock_get.side_effect = [post, master, variant, segment]
        self.assertEqual(translator.verify_published_embed("at://p", "video", 0), (True, None))

    def test_archive_partial_images_do_not_replace_api_candidates(self):
        api = [("https://api/1.jpg", None), ("https://api/2.jpg", None)]
        video, images = translator.merge_archived_media(None, api, None, ["https://archive/1.jpg"])
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0][1], "https://archive/1.jpg")

    @patch("trump_truth_translator.verify_published_embed", return_value=(True, None))
    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch("trump_truth_translator.upload_image_to_bsky", return_value=({"$type": "blob"}, {"width": 1, "height": 1}))
    @patch("trump_truth_translator.bsky_login", return_value=("did:x", "token", "did:web:pds"))
    @patch("trump_truth_translator.prefer_archived_media", side_effect=lambda u, v, i: (v, i))
    @patch("trump_truth_translator.get_ts_post_data", return_value={"media_attachments": [{"type": "image", "url": "https://img/1.jpg"}]})
    @patch("trump_truth_translator.get_ts_post_id", return_value="1171")
    @patch("trump_truth_translator.load_processed", return_value={"version": 2, "processed": [], "posts": {}})
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_empty_visible_text_with_html_image_is_not_marked_processed_before_media_resolution(
        self, mock_get, mock_parse, *mocks
    ):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"rss"
        mock_parse.return_value = SimpleNamespace(entries=[{
            "id": "mirror", "link": "https://www.trumpstruth.org/statuses/1",
            "description": '<img src="https://img/1.jpg">',
        }])
        with patch("trump_truth_translator.save_processed"):
            translator.main()
        post_mock = mocks[-7]
        self.assertTrue(post_mock.called)

    @patch("trump_truth_translator.inspect_archived_media_from_page", return_value=(None, []))
    @patch("trump_truth_translator.get_ts_post_data", return_value={})
    def test_http_200_with_missing_media_schema_is_pending(self, mock_api, mock_mirror):
        result = translator.resolve_post_media(
            "https://www.trumpstruth.org/statuses/50003", "117000000000000003"
        )
        self.assertEqual(result["state"], translator.MediaState.PENDING)
        self.assertIn("schema", result["reason"])

    def test_partial_image_upload_requires_retry(self):
        self.assertTrue(translator.should_retry_media_post(True, None, [("blob", {})], expected_images=2))

    def test_legacy_known_bad_video_is_reopened(self):
        history = translator.normalize_processing_history([
            "https://www.trumpstruth.org/statuses/40727",
            "https://truthsocial.com/@realDonaldTrump/posts/117074526504264990",
            "https://www.trumpstruth.org/statuses/40726",
        ])
        self.assertNotIn("https://www.trumpstruth.org/statuses/40727", history["processed"])
        self.assertFalse(any("117074526504264990" in item for item in history["processed"]))
        self.assertIn("https://www.trumpstruth.org/statuses/40726", history["processed"])

        history["processed"].append("https://www.trumpstruth.org/statuses/40727")
        reloaded = translator.normalize_processing_history(history)
        self.assertIn("https://www.trumpstruth.org/statuses/40727", reloaded["processed"])

    def test_known_40727_v2_discovered_falls_back_to_text_then_stays_posted(self):
        status_url = "https://www.trumpstruth.org/statuses/40727"
        truth_id = "117074526504264990"
        raw_text = (
            "Chandler Hall, representing, on Television, the foolish Center "
            "for American Lack of Progress, stated that adding the National Guard"
        )
        raw_fp = translator.text_fingerprint(raw_text)
        history = {
            "version": 2,
            "processed": [raw_fp],
            "posts": {
                status_url: {
                    "post_status": "DISCOVERED", "media_state": "PENDING",
                    "status_url": status_url, "source_text": raw_text,
                }
            },
        }
        rss_response = MagicMock(content=b"<rss />")
        rss_response.raise_for_status.return_value = None
        feed = SimpleNamespace(entries=[{
            "id": status_url, "link": status_url,
            "description": f"<p>{raw_text}</p>",
        }])
        media = {
            "state": translator.MediaState.READY, "reason": None,
            "video_url": "https://cdn.example/broken.mp4", "image_urls": [],
            "rt_display_name": None, "rt_acct": None,
        }
        with (
            patch("trump_truth_translator.requests.get", return_value=rss_response),
            patch("trump_truth_translator.feedparser.parse", return_value=feed),
            patch("trump_truth_translator.load_processed", return_value=history),
            patch("trump_truth_translator.save_processed"),
            patch("trump_truth_translator.get_ts_post_id", return_value=truth_id) as ts_id,
            patch("trump_truth_translator.resolve_post_media", return_value=media) as resolve,
            patch("trump_truth_translator.bsky_login", return_value=("did:x", "token", "did:web:pds")),
            patch("trump_truth_translator.prepare_video_for_bsky", side_effect=translator.InvalidMediaError("moov atomなし")),
            patch("trump_truth_translator.translate_with_claude", return_value="日本語訳") as claude,
            patch("trump_truth_translator.post_to_bluesky", return_value="at://posted") as post,
            patch("trump_truth_translator.verify_published_embed", return_value=(True, None)),
        ):
            translator.main()
            self.assertEqual(history["posts"][f"truth:{truth_id}"]["post_status"], "POSTED")
            translator.main()

        ts_id.assert_called_once_with(status_url)
        resolve.assert_called_once()
        claude.assert_called_once()
        post.assert_called_once()

    def test_completed_known_40727_skips_before_network_on_next_tick(self):
        status_url = "https://www.trumpstruth.org/statuses/40727"
        truth_key = "truth:117074526504264990"
        history = translator.new_processing_history([])
        history["posts"][truth_key] = {
            "post_status": "POST_VERIFY_PENDING", "feed_post_id": status_url,
        }
        translator.complete_post(history, truth_key, "fp:known")
        self.assertIn(status_url, history["processed"])

        rss_response = MagicMock(content=b"<rss />")
        rss_response.raise_for_status.return_value = None
        feed = SimpleNamespace(entries=[{
            "id": status_url, "link": status_url,
            "description": "<p>same post</p>",
        }])
        with (
            patch("trump_truth_translator.requests.get", return_value=rss_response),
            patch("trump_truth_translator.feedparser.parse", return_value=feed),
            patch("trump_truth_translator.load_processed", return_value=history),
            patch("trump_truth_translator.save_processed"),
            patch("trump_truth_translator.get_ts_post_id") as ts_id,
        ):
            translator.main()
        ts_id.assert_not_called()

    def test_known_40727_canonical_retry_ignores_legacy_fp_on_next_tick(self):
        status_url = "https://www.trumpstruth.org/statuses/40727"
        truth_id = "117074526504264990"
        raw_text = "known Chandler Hall post"
        history = {
            "version": 2,
            "processed": [translator.text_fingerprint(raw_text)],
            "posts": {
                status_url: {
                    "post_status": "DISCOVERED", "media_state": "PENDING",
                    "status_url": status_url, "source_text": raw_text,
                }
            },
        }
        rss_response = MagicMock(content=b"<rss />")
        rss_response.raise_for_status.return_value = None
        feed = SimpleNamespace(entries=[{
            "id": status_url, "link": status_url,
            "description": f"<p>{raw_text}</p>",
        }])
        pending_media = {
            "state": translator.MediaState.PENDING, "reason": "API timeout",
            "video_url": None, "image_urls": [], "rt_display_name": None,
            "rt_acct": None,
        }
        invalid_media = {
            "state": translator.MediaState.INVALID, "reason": "moov atomなし",
            "video_url": None, "image_urls": [], "rt_display_name": None,
            "rt_acct": None,
        }
        with (
            patch("trump_truth_translator.requests.get", return_value=rss_response),
            patch("trump_truth_translator.feedparser.parse", return_value=feed),
            patch("trump_truth_translator.load_processed", return_value=history),
            patch("trump_truth_translator.save_processed"),
            patch("trump_truth_translator.get_ts_post_id", return_value=truth_id) as ts_id,
            patch("trump_truth_translator.resolve_post_media", side_effect=[pending_media, invalid_media]) as resolve,
            patch("trump_truth_translator.bsky_login") as login,
        ):
            translator.main()
            canonical = history["posts"][f"truth:{truth_id}"]
            self.assertEqual(canonical["post_status"], "RETRY")
            canonical.pop("next_retry_at", None)
            translator.main()

        # 2回目は履歴に保存したTruth IDを再利用し、ミラーを再取得しない。
        self.assertEqual(ts_id.call_count, 1)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(history["posts"][f"truth:{truth_id}"]["post_status"], "BLOCKED")
        login.assert_not_called()

    def test_known_reevaluation_finds_retry_state_by_feed_alias(self):
        status_url = "https://www.trumpstruth.org/statuses/40727"
        history = translator.new_processing_history([])
        history["posts"]["truth:117074526504264990"] = {
            "post_status": "RETRY", "feed_post_id": status_url,
        }
        self.assertTrue(translator.should_force_known_reevaluation(history, status_url))
        history["posts"]["truth:117074526504264990"]["post_status"] = "BLOCKED"
        self.assertFalse(translator.should_force_known_reevaluation(history, status_url))

    def test_pending_history_rehydrates_entry_outside_rss_window(self):
        history = translator.new_processing_history([])
        history["posts"]["old-post"] = {
            "post_status": "RETRY", "media_state": "READY",
            "status_url": "https://www.trumpstruth.org/statuses/1",
            "source_text": "old text", "published": "yesterday",
            "fingerprint": "fp:old", "truth_social_id": "1171",
        }
        entries = translator.pending_entries_from_history(history, {"rss-post"})
        self.assertEqual(entries[0]["id"], "old-post")
        self.assertEqual(entries[0]["description"], "old text")

    def test_media_failure_does_not_overwrite_media_state(self):
        history = translator.new_processing_history([])
        translator.record_post_state(history, "p", translator.MediaState.READY)
        translator.record_failure(history, "p", "POST", "timeout")
        self.assertEqual(history["posts"]["p"]["media_state"], "READY")
        self.assertEqual(history["posts"]["p"]["failure_stage"], "POST")

    def test_invalid_is_blocked_until_source_changes_or_manual_retry(self):
        history = translator.new_processing_history([])
        translator.record_post_state(history, "p", translator.MediaState.INVALID, "bad", media_identity="url:a")
        self.assertFalse(translator.retry_due(history, "p"))
        self.assertTrue(translator.retry_due(history, "p", media_identity="url:b"))

    def test_deterministic_rkeys_are_stable_and_per_chunk(self):
        self.assertEqual(translator.deterministic_post_rkey("source", 0), translator.deterministic_post_rkey("source", 0))
        self.assertNotEqual(translator.deterministic_post_rkey("source", 0), translator.deterministic_post_rkey("source", 1))

    @patch("trump_truth_translator.requests.get")
    def test_verify_image_embed_requires_expected_count(self, mock_get):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"posts": [{"embed": {"$type": "app.bsky.embed.images#view", "images": [{}]}}]}
        ok, reason = translator.verify_published_embed("at://p", "images", 2)
        self.assertFalse(ok)
        self.assertIn("2", reason)

    @patch("trump_truth_translator.requests.get")
    def test_verify_video_embed_fetches_playlist_manifest(self, mock_get):
        post_response = MagicMock()
        post_response.raise_for_status.return_value = None
        post_response.json.return_value = {"posts": [{"embed": {"$type": "app.bsky.embed.video#view", "playlist": "https://video.example/list.m3u8"}}]}
        manifest_response = MagicMock(text="#EXTM3U\nvariant.m3u8")
        manifest_response.raise_for_status.return_value = None
        variant = MagicMock(text="#EXTM3U\nsegment.ts")
        variant.raise_for_status.return_value = None
        segment = MagicMock(content=b"video")
        segment.raise_for_status.return_value = None
        mock_get.side_effect = [post_response, manifest_response, variant, segment]
        self.assertEqual(translator.verify_published_embed("at://p", "video", 0), (True, None))

    @patch("trump_truth_translator.requests.post")
    @patch("trump_truth_translator.requests.get")
    def test_video_upload_http_error_with_existing_blob_is_reused(self, mock_get, mock_post):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"token": "service-token"}
        mock_post.return_value.status_code = 409
        mock_post.return_value.raise_for_status.side_effect = requests.HTTPError("already_exists")
        mock_post.return_value.json.return_value = {"blob": {"$type": "blob", "ref": {"$link": "existing"}}}
        result = translator.upload_video_via_bsky_service(
            b"video", "video/mp4", "did:x", "token", "did:web:pds"
        )
        self.assertEqual(result["ref"]["$link"], "existing")

    def test_unresolved_states_are_not_pruned(self):
        history = translator.new_processing_history([])
        for index in range(501):
            history["posts"][f"pending-{index}"] = {
                "media_state": "PENDING", "post_status": "RETRY", "updated_at": str(index)
            }
        normalized = translator.prepare_history_for_save(history)
        self.assertEqual(len(normalized["posts"]), 501)

    def test_backoff_blocks_failed_post_but_not_new_post(self):
        history = translator.new_processing_history([])
        translator.record_post_state(history, "old", translator.MediaState.PENDING, "timeout")
        self.assertFalse(translator.retry_due(history, "old"))
        self.assertTrue(translator.retry_due(history, "new"))

    def test_translation_cache_survives_post_failure_state_update(self):
        history = translator.new_processing_history([])
        translator.record_post_state(
            history, "post", translator.MediaState.READY, translation="日本語訳"
        )
        translator.record_post_state(
            history, "post", translator.MediaState.PENDING, "createRecord timeout"
        )
        self.assertEqual(history["posts"]["post"]["translation"], "日本語訳")

    @patch("trump_truth_translator.requests.post")
    def test_partial_thread_error_returns_checkpoint_and_resume_skips_root(self, mock_post):
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {"uri": "at://root", "cid": "cid-root"}
        second = MagicMock(status_code=500, text="failed")
        second.raise_for_status.side_effect = requests.HTTPError("500")
        mock_post.side_effect = [first, second]
        with self.assertRaises(translator.PartialThreadError) as caught:
            translator.post_to_bluesky(
                ["root", "reply"], "did:x", "token",
                checkpoint={"expected_embed": "video"},
            )
        checkpoint = caught.exception.checkpoint
        self.assertEqual(checkpoint["next_index"], 1)
        self.assertEqual(checkpoint["root_ref"]["uri"], "at://root")
        self.assertEqual(checkpoint["root_rkey"], translator.deterministic_post_rkey("root", 0))
        self.assertEqual(checkpoint["root_record"]["text"], "root")
        self.assertEqual(checkpoint["created_records"][0]["uri"], "at://root")
        self.assertEqual(checkpoint["expected_embed"], "video")

        resumed = MagicMock()
        resumed.raise_for_status.return_value = None
        resumed.json.return_value = {"uri": "at://reply", "cid": "cid-reply"}
        mock_post.side_effect = [resumed]
        translator.post_to_bluesky(
            ["root", "reply"], "did:x", "token", checkpoint=checkpoint
        )
        self.assertEqual(mock_post.call_count, 3)

    @patch("trump_truth_translator.get_record")
    @patch("trump_truth_translator.requests.post")
    def test_create_timeout_reuses_deterministic_existing_record(
        self, mock_post, mock_get_record
    ):
        mock_post.side_effect = requests.Timeout("after server commit")
        mock_get_record.return_value = {
            "uri": "at://existing", "cid": "cid-existing",
            "value": {"$type": "app.bsky.feed.post", "text": "root", "langs": ["ja"]},
        }
        checkpoints = []
        uri = translator.post_to_bluesky(
            ["root"], "did:x", "token", source_id="truth-1",
            checkpoint_callback=checkpoints.append,
        )
        self.assertEqual(uri, "at://existing")
        self.assertEqual(checkpoints[0]["next_index"], 1)
        sent_rkey = mock_post.call_args.kwargs["json"]["rkey"]
        self.assertEqual(sent_rkey, translator.deterministic_post_rkey("truth-1", 0))

    def test_basic_mp4_validation_requires_trak_inside_moov(self):
        mp4 = (
            (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
            + (8).to_bytes(4, "big") + b"moov"
        )
        with self.assertRaisesRegex(translator.InvalidMediaError, "trak"):
            translator.validate_basic_mp4_structure(mp4, "video/mp4", "x.mp4")

    def test_legacy_processed_list_is_migrated_to_structured_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            processed_file = os.path.join(temp_dir, "processed.json")
            with open(processed_file, "w") as file:
                json.dump(["post-1", "fp:example"], file)

            with patch.object(translator, "PROCESSED_FILE", processed_file):
                history = translator.load_processed()

            self.assertEqual(history["version"], 2)
            self.assertEqual(history["processed"], ["post-1", "fp:example"])
            self.assertEqual(history["posts"], {})

    def test_processing_history_tracks_media_state_reason_and_retries(self):
        history = translator.new_processing_history([])

        translator.record_post_state(
            history,
            "post-1",
            translator.MediaState.PENDING,
            "Truth Social API timeout",
            truth_social_id="117000000000000000",
        )
        translator.record_post_state(
            history,
            "post-1",
            translator.MediaState.PENDING,
            "Truth Social API HTTP 403",
            truth_social_id="117000000000000000",
        )

        state = history["posts"]["post-1"]
        self.assertEqual(state["media_state"], "PENDING")
        self.assertEqual(state["failure_reason"], "Truth Social API HTTP 403")
        self.assertEqual(state["retry_count"], 2)
        self.assertEqual(state["truth_social_id"], "117000000000000000")

    def test_truncated_mp4_without_complete_moov_is_invalid(self):
        # ftypは完全だが、mdatが宣言サイズより短く、moovも存在しない。
        truncated = (
            (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
            + (100).to_bytes(4, "big") + b"mdat" + b"partial"
        )

        with self.assertRaisesRegex(translator.InvalidMediaError, "切断|moov"):
            translator.validate_video_data(
                truncated, "video/mp4", "https://example.com/video.mp4"
            )

    @patch(
        "trump_truth_translator.inspect_archived_media_from_page",
        return_value=(None, []),
    )
    @patch(
        "trump_truth_translator.get_ts_post_data",
        side_effect=requests.Timeout("API timeout"),
    )
    def test_truth_api_timeout_is_pending_even_when_rss_has_no_media(
        self, mock_ts_data, mock_archive
    ):
        result = translator.resolve_post_media(
            "https://www.trumpstruth.org/statuses/50001",
            "117000000000000001",
            "<p>Text only according to RSS</p>",
        )

        self.assertEqual(result["state"], translator.MediaState.PENDING)
        self.assertIn("API timeout", result["reason"])

    @patch(
        "trump_truth_translator.inspect_archived_media_from_page",
        side_effect=requests.Timeout("mirror timeout"),
    )
    @patch(
        "trump_truth_translator.get_ts_post_data",
        return_value={"media_attachments": []},
    )
    def test_mirror_timeout_does_not_turn_empty_api_media_into_no_media(
        self, mock_ts_data, mock_archive
    ):
        result = translator.resolve_post_media(
            "https://www.trumpstruth.org/statuses/50002",
            "117000000000000002",
            "<p>Text</p>",
        )

        self.assertEqual(result["state"], translator.MediaState.PENDING)
        self.assertIn("mirror timeout", result["reason"])

    @patch("trump_truth_translator.verify_published_embed", return_value=(True, None))
    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch("trump_truth_translator.translate_with_claude", return_value="日本語訳")
    @patch(
        "trump_truth_translator.bsky_login",
        return_value=("did:example", "token", "did:web:pds.example"),
    )
    @patch("trump_truth_translator.prepare_video_for_bsky")
    @patch("trump_truth_translator.prefer_archived_media")
    @patch("trump_truth_translator.get_ts_post_data")
    @patch("trump_truth_translator.get_ts_post_id", return_value="117000000000000000")
    @patch("trump_truth_translator.load_processed")
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_invalid_video_falls_back_to_translated_text_post(
        self, mock_get, mock_parse, mock_load, mock_ts_id, mock_ts_data,
        mock_prefer, mock_prepare, mock_login, mock_translate, mock_post,
        mock_verify
    ):
        history = translator.new_processing_history([])
        mock_load.return_value = history
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"<rss />"
        mock_parse.return_value = SimpleNamespace(entries=[{
            "id": "post-video",
            "link": "https://www.trumpstruth.org/statuses/50000",
            "description": "<p>Video caption</p>",
        }])
        mock_ts_data.return_value = {
            "media_attachments": [{
                "type": "video", "url": "https://example.com/video.mp4"
            }]
        }
        mock_prefer.return_value = ("https://example.com/video.mp4", [])
        mock_prepare.side_effect = translator.InvalidMediaError(
            "MP4にmoov atomがない"
        )

        with patch("trump_truth_translator.save_processed"):
            translator.main()

        mock_translate.assert_called_once()
        mock_post.assert_called_once()
        self.assertIsNone(mock_post.call_args.args[4])
        self.assertEqual(
            mock_post.call_args.args[0],
            [
                "日本語訳\n\n動画はこちら："
                "https://truthsocial.com/@realDonaldTrump/posts/117000000000000000"
            ],
        )
        mock_verify.assert_called_with("at://posted", "none", 0)
        video_state = next(iter(history["posts"].values()))
        self.assertEqual(video_state["media_state"], "INVALID")
        self.assertEqual(video_state["post_status"], "POSTED")
        self.assertIn("moov", video_state["media_fallback_reason"])

    def test_invalid_video_without_meaningful_text_remains_blocked(self):
        self.assertFalse(translator.can_fallback_video_to_text("https://example.com"))
        self.assertFalse(translator.can_fallback_video_to_text("  RT:  \n"))
        self.assertTrue(translator.can_fallback_video_to_text("Video caption"))

    @patch("trump_truth_translator.verify_published_embed", return_value=(True, None))
    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch("trump_truth_translator.translate_with_claude", return_value="日本語訳")
    @patch("trump_truth_translator.upload_video_to_bsky", side_effect=RuntimeError("upload failed"))
    @patch("trump_truth_translator.prepare_video_for_bsky", return_value=(b"video", "video/mp4"))
    @patch("trump_truth_translator.bsky_login", return_value=("did:x", "token", "did:web:pds"))
    @patch("trump_truth_translator.resolve_post_media")
    @patch("trump_truth_translator.get_ts_post_id", return_value="117000000000000009")
    @patch("trump_truth_translator.load_processed")
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_video_upload_failure_falls_back_to_text_post(
        self, mock_get, mock_parse, mock_load, mock_ts_id, mock_media,
        mock_login, mock_prepare, mock_upload, mock_translate, mock_post,
        mock_verify,
    ):
        history = translator.new_processing_history([])
        mock_load.return_value = history
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"<rss />"
        mock_parse.return_value = SimpleNamespace(entries=[{
            "id": "https://www.trumpstruth.org/statuses/50009",
            "link": "https://www.trumpstruth.org/statuses/50009",
            "description": "<p>Video caption</p>",
        }])
        mock_media.return_value = {
            "state": translator.MediaState.READY,
            "reason": None,
            "video_url": "https://example.com/video.mp4",
            "image_urls": [],
            "rt_display_name": None,
            "rt_acct": None,
        }

        with patch("trump_truth_translator.save_processed"):
            translator.main()

        mock_upload.assert_called_once()
        mock_translate.assert_called_once()
        mock_post.assert_called_once()
        self.assertIsNone(mock_post.call_args.args[4])
        self.assertEqual(
            mock_post.call_args.args[0],
            [
                "日本語訳\n\n動画はこちら："
                "https://truthsocial.com/@realDonaldTrump/posts/117000000000000009"
            ],
        )
        mock_verify.assert_called_with("at://posted", "none", 0)

    @patch("trump_truth_translator.requests.get")
    def test_extracts_only_archived_attachments_from_mirror_page(self, mock_get):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"""
            <meta property="og:image" content="https://truth-archive.us-iad-1.linodeobjects.com/social_previews/40733/40733.jpg">
            <div class="status-card__media"><img src="https://cdn.example/card.jpg"></div>
            <div class="status-attachment status-attachment--image">
              <a class="status-attachment__link" href="https://truth-archive.us-iad-1.linodeobjects.com/attachments/17609/photo.jpg">
                <img src="https://truth-archive.us-iad-1.linodeobjects.com/attachments/17609/photo.jpg">
              </a>
            </div>
            <div class="status-details-attachment__media">
              <a href="https://truth-archive.us-iad-1.linodeobjects.com/attachments/17610/video.mp4"></a>
            </div>
        """

        video_url, image_urls = translator.scrape_archived_media_from_page(
            "https://www.trumpstruth.org/statuses/40733"
        )

        self.assertEqual(
            video_url,
            "https://truth-archive.us-iad-1.linodeobjects.com/attachments/17610/video.mp4",
        )
        self.assertEqual(
            image_urls,
            ["https://truth-archive.us-iad-1.linodeobjects.com/attachments/17609/photo.jpg"],
        )

    @patch("trump_truth_translator.requests.get")
    def test_link_preview_is_not_misclassified_as_attached_media(self, mock_get):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"""
            <meta property="og:image" content="https://truth-archive.us-iad-1.linodeobjects.com/social_previews/40729/40729.jpg">
            <div class="status-card__media">
              <img src="https://static-assets-1.truthsocial.com/cache/preview.jpg">
            </div>
        """

        self.assertEqual(
            translator.scrape_archived_media_from_page(
                "https://www.trumpstruth.org/statuses/40729"
            ),
            (None, []),
        )

    @patch("trump_truth_translator.time.sleep")
    @patch("trump_truth_translator.save_processed")
    @patch("trump_truth_translator.load_processed", return_value=[])
    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch("trump_truth_translator.upload_image_to_bsky")
    @patch(
        "trump_truth_translator.bsky_login",
        return_value=("did:example", "token", "did:web:pds.example"),
    )
    @patch("trump_truth_translator.get_ts_post_data")
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_contentless_direct_truth_post_uploads_its_image(
        self, mock_get, mock_parse, mock_ts_data, mock_login, mock_upload,
        mock_post, mock_load, mock_save, mock_sleep
    ):
        post_url = "https://truthsocial.com/@realDonaldTrump/posts/117071685600361181"
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"<rss />"
        mock_parse.return_value = SimpleNamespace(
            entries=[{"id": "mirror-1", "link": post_url}]
        )
        mock_ts_data.return_value = {
            "media_attachments": [
                {"type": "image", "url": "https://cdn.example.com/image.jpg"}
            ]
        }
        blob = {"$type": "blob", "ref": {"$link": "bafkexample"}}
        mock_upload.return_value = (blob, {"width": 1600, "height": 900})

        with patch.dict(translator.os.environ, {"MANUAL_POST_URL": post_url}):
            translator.main()

        self.assertTrue(all(
            call.args[0].startswith("https://public.api.bsky.app/")
            for call in mock_get.call_args_list
        ))
        mock_parse.assert_not_called()
        mock_ts_data.assert_called_once_with("117071685600361181")
        mock_upload.assert_called_once_with(
            "https://cdn.example.com/image.jpg", "did:example", "token", fallback_url=""
        )
        self.assertEqual(mock_post.call_args.args[0], [""])
        self.assertEqual(mock_post.call_args.args[3], [(blob, {"width": 1600, "height": 900})])

    @patch("trump_truth_translator.save_processed")
    @patch("trump_truth_translator.get_ts_post_id", return_value=None)
    @patch("trump_truth_translator.load_processed", return_value=[])
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_contentless_post_without_ts_id_remains_pending(
        self, mock_get, mock_parse, mock_load, mock_ts_id, mock_save
    ):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"<rss />"
        mock_parse.return_value = SimpleNamespace(
            entries=[{"id": "post-1", "link": "https://example.com/post-1"}]
        )

        translator.main()

        mock_save.assert_called_once_with([])

    @patch("trump_truth_translator.post_to_bluesky", return_value="at://posted")
    @patch(
        "trump_truth_translator.bsky_login",
        return_value=("did:example", "token", "did:web:pds.example"),
    )
    @patch("trump_truth_translator.translate_with_claude", return_value="日本語訳")
    @patch("trump_truth_translator.save_processed")
    @patch("trump_truth_translator.get_ts_post_id", return_value=None)
    @patch("trump_truth_translator.load_processed", return_value=[])
    @patch("trump_truth_translator.feedparser.parse")
    @patch("trump_truth_translator.requests.get")
    def test_text_post_with_unresolved_mirror_metadata_remains_pending(
        self, mock_get, mock_parse, mock_load, mock_ts_id, mock_save,
        mock_translate, mock_login, mock_post
    ):
        status_url = "https://www.trumpstruth.org/statuses/40727"
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.content = b"<rss />"
        mock_parse.return_value = SimpleNamespace(
            entries=[{
                "id": status_url,
                "link": status_url,
                "description": "<p>Chandler Hall appeared on television.</p>",
            }]
        )

        translator.main()

        mock_translate.assert_not_called()
        mock_login.assert_not_called()
        mock_post.assert_not_called()
        mock_save.assert_called_once_with([])

    def test_extracts_truth_status_id_from_any_truth_social_status_link(self):
        html = (
            '<a href="https://truthsocial.com/@realDonaldTrump/'
            'statuses/117037771483072269">Original post</a>'
        )

        self.assertEqual(
            translator.extract_ts_post_id_from_html(html), "117037771483072269"
        )

    @patch("trump_truth_translator.requests.get")
    def test_uses_direct_truth_posts_url_without_scraping_page(self, mock_get):
        self.assertEqual(
            translator.get_ts_post_id(
                "https://truthsocial.com/@realDonaldTrump/posts/117071685600361181"
            ),
            "117071685600361181",
        )
        mock_get.assert_not_called()

    def test_preserves_legacy_external_link_status_id_extraction(self):
        html = (
            '<a class="status__external-link" '
            'href="https://truthsocial.com/@realDonaldTrump/117037771483072269">'
            'Original post</a>'
        )

        self.assertEqual(
            translator.extract_ts_post_id_from_html(html), "117037771483072269"
        )

    def test_does_not_mark_media_post_ready_without_an_uploaded_blob(self):
        self.assertTrue(translator.should_retry_media_post(True, None, []))
        self.assertFalse(translator.should_retry_media_post(True, {"$type": "blob"}, []))
        self.assertFalse(
            translator.should_retry_media_post(
                True, None, [({"$type": "blob"}, {"width": 1, "height": 1})]
            )
        )
        self.assertFalse(translator.should_retry_media_post(False, None, []))

    @patch("trump_truth_translator.requests.get")
    def test_image_download_does_not_repeat_direct_request_without_proxy(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("network unavailable")

        with patch.object(translator, "BSKY_PROXIES", None):
            with self.assertRaisesRegex(Exception, "network unavailable"):
                translator.upload_image_to_bsky(
                    "https://example.com/image.jpg", "did:example", "token"
                )

        self.assertEqual(mock_get.call_count, 1)

    def test_selects_article_url_for_external_card(self):
        text = (
            "Scott Bessent interviewed by Joe Kernen!\n"
            "https://www.cnbc.com/video/2026/08/04/interview.html"
        )

        self.assertEqual(
            translator.select_external_card_url(text),
            "https://www.cnbc.com/video/2026/08/04/interview.html",
        )

    def test_does_not_use_truth_mirror_as_external_card(self):
        text = "https://www.trumpstruth.org/statuses/40470"

        self.assertIsNone(translator.select_external_card_url(text))

    @patch("trump_truth_translator.upload_image_to_bsky")
    @patch("trump_truth_translator.fetch_ogp")
    def test_external_embed_uses_only_thumbnail_blob(self, mock_ogp, mock_upload):
        mock_ogp.return_value = ("記事", "説明", "https://example.com/thumb.jpg")
        blob = {"$type": "blob", "ref": {"$link": "bafkexample"}}
        mock_upload.return_value = (blob, {"width": 1600, "height": 900})

        embed = translator.make_external_embed(
            "https://example.com/article", "did:example", "token"
        )

        self.assertEqual(embed["external"]["thumb"], blob)

    def test_marks_processed_only_after_a_successful_post(self):
        processed = []
        post = {"id": "https://example.com/post/1", "fp": "fp:example"}

        translator.mark_post_processed(processed, post)

        self.assertEqual(processed, ["fp:example", "https://example.com/post/1"])

    def test_marks_media_only_post_by_id_without_empty_fingerprint(self):
        processed = []

        translator.mark_post_processed(
            processed, {"id": "https://example.com/post/2", "fp": None}
        )

        self.assertEqual(processed, ["https://example.com/post/2"])

    @patch("trump_truth_translator.requests.post")
    def test_bluesky_error_includes_response_detail(self, mock_post):
        response = mock_post.return_value
        response.status_code = 400
        response.text = '{"error":"InvalidRequest","message":"bad embed"}'
        response.raise_for_status.side_effect = requests.HTTPError("400 Client Error")

        with self.assertRaisesRegex(RuntimeError, "InvalidRequest"):
            translator.post_to_bluesky(["テスト投稿"], "did:example", "token")

    @patch("trump_truth_translator.anthropic.Anthropic")
    def test_translation_prompt_preserves_meaning_and_marks_attached_media(
        self, mock_anthropic
    ):
        client = mock_anthropic.return_value
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="テスト訳")]
        )

        result = translator.translate_with_claude(
            "The Triumphal Arch, prior to affixing the magnificent Statues and Artwork!",
            has_media=True,
        )

        self.assertEqual(result, "テスト訳")
        prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("因果、功績、責任、主体、確信度、評価、強調", prompt)
        self.assertIn("画像または動画が添付", prompt)
        self.assertIn("断片・キャプション調", prompt)

    def test_reencodes_large_image_below_bluesky_safe_limit(self):
        image = Image.effect_noise((1800, 1200), 100).convert("RGB")
        source = io.BytesIO()
        image.save(source, format="PNG")

        data, mime_type, aspect_ratio = translator.normalize_image_for_bsky(
            source.getvalue()
        )

        self.assertLessEqual(len(data), 950_000)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertEqual(aspect_ratio, {"width": 1800, "height": 1200})

    @patch("trump_truth_translator.requests.post")
    def test_posts_native_image_embed_with_aspect_ratio(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {
            "uri": "at://did:example/app.bsky.feed.post/1",
            "cid": "bafyexample",
        }
        blob = {"$type": "blob", "ref": {"$link": "bafkexample"}}

        translator.post_to_bluesky(
            ["テスト投稿"],
            "did:example",
            "token",
            image_blobs=[(blob, {"width": 1600, "height": 900})],
        )

        record = mock_post.call_args.kwargs["json"]["record"]
        image = record["embed"]["images"][0]
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.images")
        self.assertEqual(image["image"], blob)
        self.assertEqual(image["aspectRatio"], {"width": 1600, "height": 900})

    @patch("trump_truth_translator.upload_video_via_bsky_service")
    @patch("trump_truth_translator.requests.get")
    @patch("trump_truth_translator.requests.head")
    def test_video_service_failure_does_not_create_unprocessed_video_embed(
        self, mock_head, mock_get, mock_service
    ):
        valid_mp4 = (
            (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
            + (16).to_bytes(4, "big") + b"moov"
            + (8).to_bytes(4, "big") + b"trak"
        )
        mock_head.return_value.headers = {"content-length": str(len(valid_mp4))}
        mock_head.return_value.raise_for_status.return_value = None
        mock_get.return_value = MagicMock(
            content=valid_mp4, headers={"content-type": "video/mp4"}
        )
        mock_get.return_value.raise_for_status.return_value = None
        mock_service.side_effect = RuntimeError("service unavailable")

        with self.assertRaisesRegex(RuntimeError, "service unavailable"):
            translator.upload_video_to_bsky(
                "https://example.com/video.mp4",
                "did:example",
                "token",
                "did:web:pds.example",
            )

    @patch("trump_truth_translator.requests.post")
    @patch("trump_truth_translator.requests.get")
    def test_video_service_auth_uses_get_with_query_parameters(
        self, mock_get, mock_post
    ):
        auth_response = mock_get.return_value
        auth_response.raise_for_status.return_value = None
        auth_response.json.return_value = {"token": "service-token"}
        upload_response = mock_post.return_value
        upload_response.raise_for_status.return_value = None
        upload_response.json.return_value = {"blob": {"$type": "blob"}}

        result = translator.upload_video_via_bsky_service(
            b"video",
            "video/mp4",
            "did:example",
            "access-token",
            "did:web:pds.example",
        )

        self.assertEqual(result, {"$type": "blob"})
        auth_call = mock_get.call_args
        self.assertEqual(
            auth_call.args[0], f"{translator.BSKY_API}/com.atproto.server.getServiceAuth"
        )
        self.assertEqual(auth_call.kwargs["params"]["aud"], "did:web:pds.example")
        self.assertEqual(
            auth_call.kwargs["params"]["lxm"], "com.atproto.repo.uploadBlob"
        )

    def test_extracts_pds_audience_from_login_did_document(self):
        session = {
            "did": "did:plc:user",
            "didDoc": {
                "service": [
                    {
                        "id": "#atproto_pds",
                        "type": "AtprotoPersonalDataServer",
                        "serviceEndpoint": "https://jellybaby.us-east.host.bsky.network",
                    }
                ]
            },
        }

        self.assertEqual(
            translator.get_pds_audience(session),
            "did:web:jellybaby.us-east.host.bsky.network",
        )


if __name__ == "__main__":
    unittest.main()
