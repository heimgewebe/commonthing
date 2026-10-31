"""Asset contract and deterministic output checks; Python stdlib only."""
import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('generate', HERE / 'generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class YarnAssets(unittest.TestCase):
    def test_checked_in_assets_are_reproducible_and_self_contained(self):
        for word in ('Login', 'Einstellungen'):
            for theme in ('light', 'dark'):
                with self.subTest(word=word, theme=theme):
                    source = generator.render(word, theme)
                    target = generator.OUTPUT / f'{word.lower()}-{theme}.svg'
                    self.assertEqual(source, target.read_text())
                    self.assertLess(target.stat().st_size, 200_000)
                    root = ET.fromstring(source)
                    self.assertEqual(set(e.tag.split('}')[-1] for e in root.iter()),
                                     {'svg', 'g', 'path'})
                    self.assertNotIn('url(', source)
                    self.assertNotIn('href', source)
                    self.assertNotIn('style=', source)
                    for e in root.iter():
                        self.assertNotIn('filter', e.attrib)

    def test_approved_glyphs_and_curve_commands(self):
        self.assertTrue(set('äöüßÄÖÜS').issubset(generator.GLYPHS))
        self.assertEqual(generator.GLYPHS['S'][1][0],
                         'M 68 37 C 70 24 56 18 41 21 C 20 22 10 36 13 51 C 17 70 71 64 71 91 C 71 116 34 122 16 104 C 10 98 11 91 19 91')
        for path in ('M 0 0 L 10 10', 'M 0 0 Q 5 10 10 0',
                     'M 0 0 C 2 10 8 10 10 0 Z'):
            points = generator.sample(path)
            self.assertGreater(len(points), 2)
            self.assertEqual(points[0][:2], (0, 0))


if __name__ == '__main__':
    unittest.main()
