"""Bounded, versioned raster previews; the workspace is never mounted as static files."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore

from chagent.files import FileService, FileConflict
from chagent.tools.workspace import coordinated

MAX_PREVIEW_BYTES=20*1024*1024
_DECODERS=BoundedSemaphore(2)


class PreviewUnavailable(ValueError):
    pass


def decode(data, kind, action='info', page=1):
    if not _DECODERS.acquire(timeout=2):
        raise PreviewUnavailable('预览繁忙，请稍后重试')
    try:
        with tempfile.TemporaryDirectory(prefix='chagent-preview-') as directory, tempfile.TemporaryFile() as errors:
            try:
                result=subprocess.run([sys.executable,'-m','chagent.preview_worker',kind,action,str(page)],input=data,
                    stdout=subprocess.PIPE,stderr=errors,timeout=15,cwd=directory,
                    env={'PATH':os.defpath,'PYTHONPATH':str(Path(__file__).resolve().parent.parent),
                         'HOME':directory,'TMPDIR':directory,'LANG':'C.UTF-8'})
                errors.seek(0);error_message=errors.read(2048).decode('utf-8',errors='replace')
            except (OSError,subprocess.TimeoutExpired):
                raise PreviewUnavailable('预览解码失败或超时，可下载原文件查看') from None
        if result.returncode==42:
            raise PreviewUnavailable('预览依赖未安装，请运行 uv sync')
        if result.returncode:
            message=error_message
            if result.returncode!=43:
                message='预览解码失败，可下载原文件查看'
            raise ValueError(message)
        if len(result.stdout)>16*1024*1024:
            raise ValueError('预览结果过大，请下载原文件查看')
        return result.stdout
    finally:
        _DECODERS.release()


class PreviewFS(FileService):
    @coordinated
    def state(self,path):
        with self.parent(path) as (parent,name):
            info=self.object_info(parent,name)
        return {'path':path,'identity':self.token(info),'size':info.st_size}

    @coordinated
    def snapshot(self,path,version=None):
        with self.parent(path) as (parent,name):
            data,_=self._read_at(parent,name,MAX_PREVIEW_BYTES)
            info=self.object_info(parent,name)
        digest=sha256(data).hexdigest()
        if version is not None and version!=digest:
            raise FileConflict('文件已变化，请刷新预览')
        return data,{'path':path,'version':digest,'identity':self.token(info),'size':len(data),'download_available':True}

    def information(self,path):
        # No workspace lock is held while decoding an immutable snapshot.
        state=self.state(path)
        if state['size']>MAX_PREVIEW_BYTES:
            return {**state,'kind':'unsupported','reason':'文件超过 20 MiB 预览与下载上限','download_available':False}
        data,metadata=self.snapshot(path)
        suffix=Path(path).suffix.lower()
        kind='pdf' if suffix=='.pdf' else 'image' if suffix in {'.png','.jpg','.jpeg','.gif','.webp'} else None
        if kind is None:
            return {**metadata,'kind':'unsupported','reason':'该格式暂不支持预览，可下载原文件查看'}
        try:
            if kind=='pdf' and not data.startswith(b'%PDF-'):
                raise ValueError('PDF 文件格式无效')
            info=json.loads(decode(data,kind))
        except PreviewUnavailable:
            raise
        except ValueError as error:
            return {**metadata,'kind':'unsupported','reason':str(error)}
        return {**metadata,**info}

    def raster(self,path,version,page=1):
        data,_=self.snapshot(path,version)
        suffix=Path(path).suffix.lower()
        kind='pdf' if suffix=='.pdf' else 'image' if suffix in {'.png','.jpg','.jpeg','.gif','.webp'} else None
        if kind is None:
            raise ValueError('该格式不支持预览')
        output=decode(data,kind,'page',page)
        if not output.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('预览图像无效')
        return output
