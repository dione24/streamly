import pathlib
import socket
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from streamly.egress import RelayGateway, open_public


class EgressTests(unittest.TestCase):
    def addresses(self, host, port, **kwargs):
        ip = '127.0.0.1' if host == 'internal.test' else '93.184.216.34'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    def test_connection_uses_only_the_validated_address(self):
        sock, conn = Mock(), Mock()
        conn.getresponse.return_value.status = 200
        with patch('socket.getaddrinfo', side_effect=self.addresses) as dns, \
                patch('socket.socket', return_value=sock), \
                patch('http.client.HTTPConnection', return_value=conn):
            with open_public('http://public.test:8080/live', 'Test'):
                pass
        dns.assert_called_once_with('public.test', 8080, type=socket.SOCK_STREAM)
        sock.connect.assert_called_once_with(('93.184.216.34', 8080))
        self.assertIs(conn.sock, sock)
        self.assertEqual(conn.request.call_args.args[:2], ('GET', '/live'))

    def test_redirect_to_private_address_never_connects(self):
        sock, conn = Mock(), Mock()
        conn.getresponse.return_value.status = 302
        conn.getresponse.return_value.getheader.return_value = 'http://internal.test/admin'
        with patch('socket.getaddrinfo', side_effect=self.addresses), \
                patch('socket.socket', return_value=sock) as sockets, \
                patch('http.client.HTTPConnection', return_value=conn):
            with self.assertRaises(ValueError):
                with open_public('http://public.test/start', 'Test'):
                    self.fail('redirection privee acceptee')
        self.assertEqual(sockets.call_count, 1)

    def test_https_keeps_original_hostname_for_certificate_check(self):
        sock, conn, context = Mock(), Mock(), Mock()
        conn.getresponse.return_value.status = 200
        with patch('socket.getaddrinfo', side_effect=self.addresses), \
                patch('socket.socket', return_value=sock), \
                patch('ssl.create_default_context', return_value=context), \
                patch('http.client.HTTPConnection', return_value=conn):
            with open_public('https://public.test/live', 'Test'):
                pass
        context.wrap_socket.assert_called_once_with(sock, server_hostname='public.test')


class GatewayTests(unittest.TestCase):
    def setUp(self):
        with patch('socket.getfqdn', return_value='localhost'):
            self.gateway = RelayGateway('Test')
        self.addCleanup(self.gateway.close)
        self.gateway.grant('worker', 'https://public.test/master.m3u8')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def test_all_hls_resources_are_rewritten_and_resolved_after_redirect(self):
        playlist = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,URI="audio/index.m3u8"
#EXT-X-KEY:METHOD=AES-128,URI="/key"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2,
seg.ts
https://cdn.test/next.m3u8
'''
        rewritten = self.gateway.playlist('worker', playlist, 'https://redirect.test/live/index.m3u8').decode()
        import re
        uris = re.findall(r'URI="([^"]+)"', rewritten)
        uris += [line for line in rewritten.splitlines() if line and not line.startswith('#')]
        sources = [self.gateway.source(urllib.parse.urlsplit(uri).path)[1] for uri in uris]
        self.assertEqual(sources, ['https://redirect.test/live/audio/index.m3u8',
                                  'https://redirect.test/key', 'https://redirect.test/live/init.mp4',
                                  'https://redirect.test/live/seg.ts', 'https://cdn.test/next.m3u8'])

    def test_private_segment_and_key_are_rejected_at_fetch(self):
        for source in ('http://127.0.0.1/secret.ts', 'http://169.254.169.254/key'):
            url = self.gateway.url('worker', source)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.opener.open(url, timeout=3)
            self.assertEqual(error.exception.code, 502)
            error.exception.close()

    def test_unquoted_hls_uri_cannot_bypass_gateway(self):
        body = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=http://127.0.0.1/key,IV=0x1234\n'
        result = self.gateway.playlist('worker', body, 'https://public.test/live').decode()
        self.assertNotIn('URI=http://127.0.0.1', result)
        self.assertIn('URI="' + self.gateway.base + '/', result)

    def test_non_http_playlist_resources_are_rejected(self):
        for uri in ('file:///etc/passwd', 'tcp://127.0.0.1:22', 'data:text/plain,secret', 'crypto+http://x/y'):
            with self.assertRaises(ValueError):
                self.gateway.playlist('worker', ('#EXTM3U\n' + uri).encode(), 'https://public.test/live')
            with self.assertRaises(ValueError):
                self.gateway.playlist('worker', ('#EXTM3U\n#EXT-X-KEY:URI="' + uri + '"').encode(), 'https://public.test/live')

    def test_revoked_or_tampered_grant_cannot_fetch(self):
        url = self.gateway.url('worker', 'https://public.test/live')
        path = urllib.parse.urlsplit(url).path
        with self.assertRaises(ValueError):
            self.gateway.source(path.replace('/worker/', '/other/'))
        self.gateway.revoke('worker')
        with self.assertRaises(ValueError):
            self.gateway.source(path)


if __name__ == '__main__':
    unittest.main()
