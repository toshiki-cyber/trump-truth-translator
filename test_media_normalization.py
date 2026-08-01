import io
import unittest
from unittest.mock import patch

from PIL import Image

import trump_truth_translator as translator


class NormalizeImageForBlueskyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
