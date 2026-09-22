import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from streamly.refresh import CatalogRefresh, refresh_seconds
from streamly import app
from streamly.catalog import Catalog


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 100000
        self.cfg = {'providers': [{'id': 'old'}, {'id': 'fresh'}, {'id': 'off', 'enabled': False}]}
        self.stats = [{'provider_id': 'old', 'last_sync': self.now - 21601},
                      {'provider_id': 'fresh', 'last_sync': self.now - 100}]
        self.sync = Mock()
        self.busy = Mock(return_value=False)
        self.refresh = CatalogRefresh(self.cfg, lambda: self.stats, self.sync, self.busy, log=Mock())

    def test_only_stale_enabled_providers_are_refreshed(self):
        self.refresh.tick(self.now)
        self.sync.assert_called_once_with('old')

    def test_success_and_manual_sync_postpone_next_run_across_restart(self):
        self.refresh.tick(self.now)
        self.stats[0]['last_sync'] = self.now
        restarted = CatalogRefresh(self.cfg, lambda: self.stats, self.sync, self.busy)
        restarted.tick(self.now + 60)
        self.sync.assert_called_once_with('old')
        restarted.tick(self.now + 21600)
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ['old', 'old', 'fresh'])

    def test_never_imported_provider_is_recovered(self):
        self.cfg['providers'] = [{'id': 'new'}]
        self.refresh.tick(self.now)
        self.sync.assert_called_once_with('new')

    def test_busy_import_is_not_enqueued_repeatedly(self):
        self.busy.return_value = True
        self.refresh.tick(self.now)
        self.refresh.tick(self.now + 60)
        self.sync.assert_not_called()
        self.busy.return_value = False
        self.refresh.tick(self.now + 120)
        self.sync.assert_called_once_with('old')

    def test_failures_back_off_without_blocking_other_providers(self):
        self.cfg['providers'] = [{'id': 'old'}, {'id': 'new'}]
        self.sync.side_effect = [OSError('indisponible'), None, None, None]
        self.refresh.tick(self.now)
        self.refresh.tick(self.now + 899)
        self.assertEqual(self.sync.call_count, 2)
        self.refresh.tick(self.now + 900)
        self.assertEqual(self.sync.call_count, 4)

    def test_periodicity_can_be_disabled_or_changed(self):
        self.cfg['catalog_refresh_hours'] = 0
        self.refresh.tick(self.now)
        self.sync.assert_not_called()
        self.cfg['catalog_refresh_hours'] = 24
        self.refresh.tick(self.now)
        self.sync.assert_not_called()
        self.cfg['catalog_refresh_hours'] = 6
        self.refresh.tick(self.now)
        self.sync.assert_called_once_with('old')
        for bad in (None, 'invalid', -1, float('nan'), float('inf')):
            self.assertEqual(refresh_seconds({'catalog_refresh_hours': bad}), 21600)

    def test_close_stops_further_work(self):
        self.refresh.close()
        self.refresh.tick(self.now)
        self.sync.assert_not_called()


class RefreshImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(self.tmp.name + '/catalog.db')
        self.state = Mock()
        self.state.cfg = {'providers': [{'id': 'p'}]}
        self.state.catalog = self.catalog
        self.state.sync_lock = threading.Lock()
        self.state.sync_log = []
        self.client = self.state.client.return_value
        self.client.live_categories.return_value = [{'category_id': 1, 'category_name': 'France'}]
        self.client.live_streams.return_value = [{'stream_id': 1, 'name': 'FR| Ancienne', 'category_id': 1}]
        self.catalog.sync_provider({'id': 'p'}, self.client, lambda _: None)

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def test_upstream_failure_preserves_previous_catalog(self):
        previous = self.catalog.stats()
        self.client.live_streams.side_effect = OSError('fournisseur indisponible')
        app._run_sync('p', self.state)
        self.assertEqual(self.catalog.stats(), previous)
        self.assertIsNotNone(self.catalog.channel('p', 1))

    def test_invalid_account_cannot_erase_catalog_with_empty_lists(self):
        self.client.account_info.side_effect = ValueError('authentification refusee')
        self.client.live_streams.reset_mock()
        self.client.live_streams.return_value = []
        app._run_sync('p', self.state)
        self.client.live_streams.assert_not_called()
        self.assertIsNotNone(self.catalog.channel('p', 1))

    def test_success_replaces_removed_channels_and_imports_new_ones(self):
        self.client.live_streams.return_value = [{'stream_id': 2, 'name': 'FR| Nouvelle', 'category_id': 1}]
        self.client.vod_streams.return_value = []
        self.client.series.return_value = []
        self.client.series_categories.return_value = []
        self.client._api.return_value = []
        self.client.category_names_from_m3u.return_value = {}
        app._run_sync('p', self.state)
        self.assertIsNone(self.catalog.channel('p', 1))
        self.assertIsNotNone(self.catalog.channel('p', 2))
        self.state.player.invalidate.assert_called_once()
        self.state.guide.ensure_fresh.assert_called_once_with(force=True)

    def test_queued_automatic_run_rechecks_freshness_under_lock(self):
        # Un import manuel vient de terminer : le job periodique en attente
        # ne doit pas recommencer le meme telechargement.
        app._run_sync('p', self.state, due_only=True)
        self.state.client.assert_not_called()
        self.state.guide.ensure_fresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
