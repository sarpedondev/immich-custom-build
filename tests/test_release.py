import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release as publisher


class PublicationTests(unittest.TestCase):
    def publish(self, upstream='v3.2.0', existing=None):
        calls = []
        env = dict(GITHUB_REPOSITORY='owner/repo', UPSTREAM_VERSION=upstream,
                   GITHUB_RUN_NUMBER='123', GITHUB_RUN_ID='456', BUILD_KEY='key',
                   RECIPE_COMMIT='a' * 40, UPSTREAM_SHA='b' * 40,
                   IMAGE='ghcr.io/owner/image', IMAGE_DIGEST='sha256:digest')

        def gh(args, **kwargs):
            calls.append(args)
            if args[1] == 'api':
                return json.dumps([[existing] if existing else []])
            if args[1:3] == ['run', 'download']:
                Path(args[args.index('--dir') + 1], 'app-release.apk').write_bytes(b'APK')
            if args[1:3] == ['release', 'upload']:
                apk = Path(args[4])
                self.assertEqual(apk.read_bytes(), b'APK')
                self.assertIn('immich-cloudflare.apk', Path(args[5]).read_text())
            return ''

        with patch.dict(os.environ, env), patch.object(publisher.subprocess, 'check_output', side_effect=gh):
            publisher.main()
        return calls

    def test_stable_is_published_only_after_assets_are_uploaded(self):
        calls = self.publish()
        mutations = [c for c in calls if c[1] == 'release']
        self.assertEqual([c[2] for c in mutations], ['create', 'upload', 'edit'])
        self.assertIn('--draft', mutations[0])
        self.assertIn('--prerelease=false', mutations[0])
        self.assertIn('--latest=true', mutations[-1])
        self.assertIn('--draft=false', mutations[-1])

    def test_rc_is_prerelease_and_not_latest(self):
        calls = self.publish('v3.2.0-rc.3')
        self.assertTrue(any('--prerelease=true' in c for c in calls))
        self.assertTrue(any('--latest=false' in c for c in calls))

    def test_published_release_is_not_mutated_on_rerun(self):
        calls = self.publish(existing=dict(tag_name='v3.2.0-custom.123-prcloudflare',
                            draft=False, assets=[dict(name='immich-cloudflare.apk')], html_url='url'))
        self.assertEqual(len(calls), 1)

    def test_draft_is_resumed(self):
        calls = self.publish(existing=dict(tag_name='v3.2.0-custom.123-prcloudflare', draft=True))
        self.assertEqual([c[2] for c in calls if c[1] == 'release'], ['upload', 'edit'])

    def test_missing_published_apk_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'missing its APK'):
            self.publish(existing=dict(tag_name='v3.2.0-custom.123-prcloudflare', draft=False, assets=[]))

    def test_version_matches_android_suffix_and_rebuilds_are_distinct(self):
        self.assertEqual(publisher.release_identity('v3.2.0', 123), ('v3.2.0-custom.123-prcloudflare', False))
        self.assertNotEqual(publisher.release_identity('v3.2.0', 123), publisher.release_identity('v3.2.0', 124))
        for number in [0, -1, 2_000_000_000]:
            with self.assertRaises(ValueError):
                publisher.release_identity('v3.2.0', number)
