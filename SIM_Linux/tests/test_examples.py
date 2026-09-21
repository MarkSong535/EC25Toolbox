"""Shipped templates and documentation checks; no network or real credentials."""
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ec25toolbox_linux.config import ConfigurationError, load_config


class ExampleTests(unittest.TestCase):
    def test_all_sample_configs_parse_and_reject_placeholder_smtp(self):
        for source in ROOT.glob('config*.example.toml'):
            with self.subTest(source=source.name):
                document = tomllib.loads(source.read_text())
                self.assertEqual(document['smtp']['password'], 'SMTP_TOKEN')
                self.assertFalse(document['voice']['enabled'])
                self.assertFalse(document['archive']['enabled'])
                with self.assertRaisesRegex(ConfigurationError, 'Proton SMTP'):
                    load_config(source)
                # Disable SMTP in a temporary copy to validate the remaining schema.
                content = source.read_text().replace('[smtp]\nenabled = true', '[smtp]\nenabled = false')
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / 'config.toml'
                    target.write_text(content)
                    config = load_config(target)
                self.assertEqual(config.calls.mode, 'reject')
                self.assertEqual(config.portal.port, 9561)
                self.assertEqual(config.archive.remote_path, '/my-files/RPI-SMS')

    def test_local_markdown_links_resolve(self):
        for source in ROOT.glob('*.md'):
            for target in re.findall(r'\]\(([^)]+)\)', source.read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                target = target.split('#')[0]
                with self.subTest(source=source.name, target=target):
                    self.assertTrue((source.parent / target).exists())


if __name__ == '__main__':
    unittest.main()
