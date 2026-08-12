import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
import requests

import trump_truth_translator as translator


class NormalizeImageForBlueskyTests(unittest.TestCase):
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

        mock_get.assert_not_called()
        mock_parse.assert_not_called()
        mock_ts_data.assert_called_once_with("117071685600361181")
        mock_upload.assert_called_once_with(
            "https://cdn.example.com/image.jpg", "did:example", "token", fallback_url=""
        )
        self.assertEqual(mock_post.call_args.args[0], ["【画像投稿】"])
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
        mock_head.return_value.headers = {"content-length": "4"}
        mock_head.return_value.raise_for_status.return_value = None
        mock_get.return_value = MagicMock(
            content=b"data", headers={"content-type": "video/mp4"}
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
