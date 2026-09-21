from dataclasses import replace
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ec25toolbox_linux.archive import ArchiveError, run_archive, recover_archive, digest_file
from ec25toolbox_linux.config import load_config
from ec25toolbox_linux.portal_store import PortalStore
from ec25toolbox_linux.storage import EventStore


class FakeDrive:
    def __init__(self, path, corrupt=False):
        self.path=path
        self.corrupt=corrupt
        self.uploads=0
    def upload(self,path):
        self.uploads+=1
        shutil.copyfile(path,self.path/path.name)
    def download(self,name,directory):
        target=Path(directory)/name
        shutil.copyfile(self.path/name,target)
        if self.corrupt:
            with target.open("ab") as handle: handle.write(b"corrupt")
        return target


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        config=self.root/"config.toml"
        config.write_text('[service]\ndatabase_path="events.sqlite3"\n[smtp]\nenabled=false\n'
                          'from_address="noreply@anonymunication.ch"\nfrom_name="RPI SMS"\n'
                          'to_addresses=["test@example.com"]\npassword="DO_NOT_ARCHIVE_SECRET"\n'
                          '[archive]\nenabled=true\n')
        self.config=load_config(config)
        self.events=EventStore(self.config.service.database_path)
        self.store=PortalStore(self.events.path)
        self.events.enqueue("sms:test","sms",{"sender":"+12345678901","body":"Original message"})
        self.store.record_login("hash","user@example.com","Verified Access session","Desktop · macOS","Safari")
        self.remote=self.root/"remote"
        self.remote.mkdir()
        self.client=FakeDrive(self.remote)

    def test_upload_verify_cleanup_and_recover(self):
        self.assertIn("download-verified",run_archive(self.config,self.client))
        receipt=self.store.archives()[0]
        self.assertEqual(self.events.count(),1)
        self.assertEqual(list(self.root.glob("archive-*")),[])
        with tarfile.open(self.remote/receipt["name"]) as tar:
            self.assertIn("gateway.sqlite3",tar.getnames())
            manifest=json.load(tar.extractfile("manifest.json"))
            for name,digest in manifest["files"].items():
                data=tar.extractfile(name).read()
                self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
                self.assertNotIn(b"DO_NOT_ARCHIVE_SECRET",data)
            mail=tar.extractfile(next(n for n in tar.getnames() if n.endswith(".eml"))).read()
            self.assertIn(b"RPI SMS",mail)
            self.assertIn(b"Original message",mail)
        restored=recover_archive(self.config,receipt["name"],receipt["sha256"],self.root/"recovery",self.client)
        self.assertEqual(digest_file(restored),receipt["sha256"])
        with self.assertRaises(ArchiveError):
            recover_archive(self.config,receipt["name"],receipt["sha256"],self.root/"recovery",self.client)
        self.assertEqual(run_archive(self.config,self.client),"Archive not due")
        self.assertEqual(self.client.uploads,1)

    def test_corrupt_remote_never_gets_receipt_or_deletes_live_data(self):
        with self.assertRaises(ArchiveError):
            run_archive(self.config,FakeDrive(self.remote,corrupt=True))
        self.assertEqual(self.store.archives(),[])
        self.assertEqual(self.events.count(),1)
        self.assertEqual(len(self.store.logins(2**63-1)),1)

    def test_disabled_archive_does_not_contact_drive(self):
        c=replace(self.config,archive=replace(self.config.archive,enabled=False))
        self.assertIn("disabled",run_archive(c,self.client))
        self.assertEqual(self.client.uploads,0)
