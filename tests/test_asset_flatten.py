# -*- coding: utf-8 -*-
"""贴图透明压平的等价性测试：向量化实现必须与原逐像素循环逐点一致。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PIL import Image
except ImportError:
    Image = None


def _reference_flatten(path):
    """原先的逐像素实现，作为等价性基准保留在测试里。"""
    im = Image.open(path).convert("RGBA")
    px = im.load()
    for y in range(im.size[1]):
        for x in range(im.size[0]):
            r, g, b, a = px[x, y]
            if a < 255:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if a < 32 or lum < 150:
                    px[x, y] = (255, 0, 255, 255)
                else:
                    px[x, y] = (r, g, b, 255)
    return im


@unittest.skipUnless(Image is not None, "需要 Pillow")
class TestFlattenTransparency(unittest.TestCase):
    def _make_sample(self, directory):
        """造一张覆盖各种 alpha/亮度组合的样图。"""
        from pathlib import Path
        im = Image.new("RGBA", (16, 16))
        px = im.load()
        for y in range(16):
            for x in range(16):
                # alpha 从 0 扫到 255，颜色从暗扫到亮
                a = min(255, y * 17)
                v = min(255, x * 17)
                px[x, y] = (v, v // 2, 255 - v, a)
        path = Path(directory) / "sample.png"
        im.save(path)
        return path

    def test_matches_reference_pixel_for_pixel(self):
        import tempfile
        from pikapet.pet import _flatten_transparency
        with tempfile.TemporaryDirectory() as td:
            path = self._make_sample(td)
            got = _flatten_transparency(path)
            want = _reference_flatten(path)
            self.assertEqual(got.size, want.size)
            # tobytes 而不是 getdata：后者在 Pillow 13 起已废弃
            self.assertEqual(got.tobytes(), want.tobytes())

    def test_all_pixels_opaque(self):
        """压平后不允许残留半透明：Tk 没有逐像素 alpha。"""
        import tempfile
        from pikapet.pet import _flatten_transparency
        with tempfile.TemporaryDirectory() as td:
            path = self._make_sample(td)
            data = _flatten_transparency(path).tobytes()
            self.assertEqual(set(data[3::4]), {255})

    def test_real_asset_matches_reference(self):
        from pikapet.pet import _flatten_transparency
        from pikapet.pixtokens import ASSET
        if not ASSET.exists():
            self.skipTest("仓库里没有 assets/pikachu.png")
        self.assertEqual(_flatten_transparency(ASSET).tobytes(),
                         _reference_flatten(ASSET).tobytes())


if __name__ == "__main__":
    unittest.main()
