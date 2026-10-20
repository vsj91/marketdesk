import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from marketdesk.server import Handler,remote_clients

def handler(client,host,remote=('192.168.1.20',)):
    h=Handler.__new__(Handler)
    h.client_address=(client,50000);h.headers={'Host':host}
    h.server=SimpleNamespace(server_port=8765,remote=frozenset(remote))
    return h

class RemoteAccessTest(unittest.TestCase):
    def test_file_lists_addresses_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'remote-access.txt'
            self.assertEqual(remote_clients(file),frozenset())
            file.write_text('# Windows laptop\n192.168.1.20  # desk\n\n')
            self.assertEqual(remote_clients(file),frozenset({'192.168.1.20'}))
            file.write_text('windows-pc\n')
            with self.assertRaises(ValueError):remote_clients(file)

    def test_mac_keeps_existing_rules(self):
        self.assertTrue(handler('127.0.0.1','localhost:8765').allowed())
        self.assertTrue(handler('127.0.0.1','127.0.0.1:8765').allowed())
        self.assertFalse(handler('127.0.0.1','evil.example:8765').allowed())

    def test_only_listed_machine_by_ip_address(self):
        self.assertTrue(handler('192.168.1.20','192.168.1.2:8765').allowed())
        self.assertFalse(handler('192.168.1.21','192.168.1.2:8765').allowed())
        self.assertFalse(handler('192.168.1.20','192.168.1.2:8765',remote=()).allowed())
        # A hostname could be rebound to this Mac by a hostile page.
        self.assertFalse(handler('192.168.1.20','rebind.example:8765').allowed())
        self.assertFalse(handler('192.168.1.20','localhost:8765').allowed())

    def test_remote_origin_must_match_its_host(self):
        self.assertEqual(handler('192.168.1.20','192.168.1.2:8765').origins(),('http://192.168.1.2:8765',))
        self.assertNotIn('http://192.168.1.2:8765',handler('127.0.0.1','localhost:8765').origins())

if __name__=='__main__':unittest.main()
