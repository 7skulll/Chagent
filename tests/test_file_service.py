import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from chagent.files import FileService, FileConflict


class FileServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fs = FileService(self.root)

    def test_save_conflict_and_crlf(self):
        self.fs.create('a.txt', 'file', '一\r\n二\r\n')
        read = self.fs.text('a.txt')
        self.assertEqual(read['newline'], 'crlf')
        self.fs.create('cr.txt', 'file', 'one\rtwo\r')
        self.assertEqual(self.fs.text('cr.txt')['newline'], 'cr')
        self.fs.create('mixed.txt', 'file', 'one\rtwo\n')
        self.assertEqual(self.fs.text('mixed.txt')['newline'], 'mixed')
        self.fs.save_text('a.txt', '三\r\n二\r\n', read['version'], read['identity'])
        self.assertEqual((self.root/'a.txt').read_bytes(), '三\r\n二\r\n'.encode())
        with self.assertRaises(FileConflict):
            self.fs.save_text('a.txt', 'stale', read['version'], read['identity'])

    def test_concurrent_saves_one_winner(self):
        self.fs.create('a', 'file', 'original')
        read = self.fs.text('a')
        def save(value):
            try:
                FileService(self.root).save_text('a', value, read['version'], read['identity'])
                return True
            except FileConflict:
                return False
        with ThreadPoolExecutor(2) as executor:
            self.assertEqual(sum(executor.map(save, ['one', 'two'])), 1)

    def test_create_rename_recycle_restore_directory(self):
        self.fs.create('folder', 'directory')
        self.fs.create('folder/a', 'file', 'hello')
        identity = self.fs.listing()['items'][0]['identity']
        self.fs.rename('folder', 'renamed', identity)
        record = self.fs.recycle('renamed', self.fs.listing()['items'][0]['identity'])
        self.assertFalse((self.root/'renamed').exists())
        fs = FileService(self.root)
        self.assertEqual(fs.trash()['items'][0]['path'], 'renamed')
        fs.create('renamed', 'file', 'keep')
        with self.assertRaises(FileExistsError):
            fs.restore(record['id'])
        fs.restore(record['id'], 'recovered')
        self.assertEqual((self.root/'recovered/a').read_text(), 'hello')
        self.assertEqual(fs.trash()['items'], [])
        self.assertNotIn('.chagent', [e['name'] for e in fs.listing(hidden=True)['items']])

    def test_boundaries_links_and_collisions(self):
        for path in ['../x', '/tmp/x', '.chagent/x', '']:
            with self.subTest(path=path), self.assertRaises((ValueError, PermissionError)):
                self.fs.create(path, 'file')
        self.fs.create('a', 'file', 'keep')
        os.symlink('a', self.root/'link')
        os.link(self.root/'a', self.root/'hard')
        for path in ['link', 'hard']:
            with self.assertRaises((PermissionError, OSError)):
                self.fs.text(path)
        os.unlink(self.root/'hard')
        with self.assertRaises(FileExistsError):
            self.fs.create('a', 'file', 'overwrite')
        self.fs.create('dir', 'directory')
        identity = next(e['identity'] for e in self.fs.listing()['items'] if e['name']=='dir')
        with self.assertRaises(FileExistsError):
            self.fs.rename('dir', 'a', identity)
        self.assertEqual((self.root/'a').read_text(), 'keep')

    def test_pagination_and_binary(self):
        for name in ['a','b','c','.hidden']:
            self.fs.create(name, 'file')
        self.assertEqual(self.fs.listing(limit=2)['next_offset'], 2)
        self.assertEqual(len(self.fs.listing(offset=2)['items']), 1)
        (self.root/'a').write_bytes(b'\xff\x00')
        with self.assertRaises(ValueError):
            self.fs.text('a')

    def test_internal_directory_symlink_is_rejected(self):
        os.symlink(self.root/'elsewhere', self.root/'.chagent')
        self.fs.create('a','file','safe')
        identity = self.fs.text('a')['identity']
        with self.assertRaises(OSError):
            self.fs.recycle('a', identity)
        self.assertEqual((self.root/'a').read_text(), 'safe')

    def test_versioned_patch_checks_all_revisions_before_writing(self):
        from chagent.tools.patch import ApplyPatchTool, PatchError
        from hashlib import sha256
        self.fs.create('a.txt','file','old\n')
        self.fs.create('b.txt','file','old\n')
        fs = FileService(self.root,require_versions=True,versions={'a.txt':sha256(b'old\n').hexdigest()})
        patch='--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-old\n+new\n'
        with self.assertRaises(PatchError):
            ApplyPatchTool().handle(fs,{'patch':patch})
        self.assertEqual((self.root/'a.txt').read_text(),'old\n')
        self.assertEqual((self.root/'b.txt').read_text(),'old\n')

    def test_large_output_overwrite_and_keep_both_stale_version(self):
        from chagent.outputs import OutputFS
        fs=OutputFS(self.root)
        large=b'x'*(2*1024*1024)
        self.assertEqual(fs.stage('large.bin',large)['status'],'committed')
        snapshot=fs.stage('large.bin',b'y'*(2*1024*1024))
        row={'path':'large.bin','payload':b'y'*(2*1024*1024),**snapshot}
        result=fs.decide(row,'overwrite')
        self.assertEqual(result['status'],'committed')
        self.assertEqual((self.root/'large.bin').read_bytes(),b'y'*(2*1024*1024))
        stale=fs.decide(row,'keep-both')
        self.assertEqual(stale['status'],'pending')
        self.assertFalse((self.root/'large-1.bin').exists())
