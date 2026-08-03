import io
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
import requests

import trump_truth_translator as translator


class NormalizeImageForBlueskyTests(unittest.TestCase):
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

    @patch("trump_truth_translator.upload_video_blob_direct")
    @patch("trump_truth_translator.upload_video_via_bsky_service")
    @patch("trump_truth_translator.requests.get")
    @patch("trump_truth_translator.requests.head")
    def test_video_service_failure_falls_back_to_native_blob_upload(
        self, mock_head, mock_get, mock_service, mock_direct
    ):
        mock_head.return_value.headers = {"content-length": "4"}
        mock_get.return_value = MagicMock(
            content=b"data", headers={"content-type": "video/mp4"}
        )
        mock_get.return_value.raise_for_status.return_value = None
        mock_service.side_effect = RuntimeError("service unavailable")
        mock_direct.return_value = {"$type": "blob"}

        result = translator.upload_video_to_bsky(
            "https://example.com/video.mp4", "did:example", "token"
        )

        self.assertEqual(result, {"$type": "blob"})
        mock_direct.assert_called_once_with(b"data", "video/mp4", "token")


if __name__ == "__main__":
    unittest.main()
