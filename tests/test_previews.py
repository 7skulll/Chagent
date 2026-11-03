import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from chagent.files import FileConflict
from chagent.previews import PreviewFS, PreviewUnavailable
try:
    from preview_fixtures import pdf_bytes
except ImportError:
    from tests.preview_fixtures import pdf_bytes


class PreviewTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);self.fs=PreviewFS(self.root)

    def test_pdf_page_navigation_and_version_binding(self):
        (self.root/'sample.pdf').write_bytes(pdf_bytes())
        info=self.fs.information('sample.pdf')
        self.assertEqual((info['kind'],info['pages']),('pdf',2))
        first=self.fs.raster('sample.pdf',info['version'],1)
        second=self.fs.raster('sample.pdf',info['version'],2)
        self.assertNotEqual(first,second)
        with Image.open(io.BytesIO(first)) as image:
            self.assertEqual(image.size,(600,800))
        with self.assertRaises(ValueError):self.fs.raster('sample.pdf',info['version'],3)
        (self.root/'sample.pdf').write_bytes(pdf_bytes()+b'\n')
        with self.assertRaises(FileConflict):self.fs.raster('sample.pdf',info['version'],1)

    def test_image_raster_is_decoded_png_and_not_executable_content(self):
        for suffix,format in [('png','PNG'),('jpg','JPEG'),('gif','GIF'),('webp','WEBP')]:
            stream=io.BytesIO();Image.new('RGB',(80,40),'teal').save(stream,format=format)
            path='image.'+suffix;(self.root/path).write_bytes(stream.getvalue())
            info=self.fs.information(path)
            self.assertEqual(info['kind'],'image',info)
            self.assertEqual((info['width'],info['height']),(80,40))
            with Image.open(io.BytesIO(self.fs.raster(path,info['version']))) as image:
                self.assertEqual(image.format,'PNG')
        (self.root/'fake.png').write_text('<script>alert(1)</script>')
        self.assertEqual(self.fs.information('fake.png')['kind'],'unsupported')
        (self.root/'unsafe.svg').write_text('<svg onload="alert(1)"/>')
        self.assertEqual(self.fs.information('unsafe.svg')['kind'],'unsupported')

    def test_bad_pdf_large_file_and_links_have_clear_boundaries(self):
        (self.root/'broken.pdf').write_bytes(b'%PDF-1.4\ninvalid')
        self.assertEqual(self.fs.information('broken.pdf')['kind'],'unsupported')
        with (self.root/'large.png').open('wb') as stream:stream.truncate(21*1024*1024)
        self.assertFalse(self.fs.information('large.png')['download_available'])
        (self.root/'small.pdf').write_bytes(pdf_bytes());os.symlink('small.pdf',self.root/'link.pdf')
        with self.assertRaises(OSError):self.fs.information('link.pdf')
        for path in ['../outside.pdf','/etc/passwd','.chagent/trash/a']:
            with self.assertRaises((ValueError,PermissionError)):self.fs.state(path)

    def test_missing_dependencies_and_decoder_timeout_are_retryable(self):
        (self.root/'sample.pdf').write_bytes(pdf_bytes())
        import subprocess
        with patch('chagent.previews.subprocess.run',side_effect=subprocess.TimeoutExpired('decoder',15)):
            with self.assertRaises(PreviewUnavailable):self.fs.information('sample.pdf')
        with patch('chagent.previews.subprocess.run',return_value=subprocess.CompletedProcess([],42,b'',b'')):
            with self.assertRaisesRegex(PreviewUnavailable,'uv sync'):self.fs.information('sample.pdf')
