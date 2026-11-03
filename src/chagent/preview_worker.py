"""Disposable decoder process. Receives bytes, never a caller-supplied disk path."""
import io
import json
import math
import sys
import warnings


def run(data, kind, action, number):
    if kind == 'pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(data) as document:
            count=len(document)
            if not 1 <= count <= 2000:
                raise ValueError('PDF 页数超出 1–2000 页预览范围')
            if action=='info':
                return json.dumps({'kind':'pdf','pages':count}).encode()
            if not 1 <= number <= count:
                raise ValueError('PDF 页码超出范围')
            page=document[number-1]
            try:
                width,height=page.get_size()
                if not all(math.isfinite(n) and 0<n<1_000_000 for n in (width,height)):
                    raise ValueError('PDF 页面尺寸异常')
                bitmap=page.render(scale=min(2,1600/max(width,height)))
                try:
                    output=io.BytesIO()
                    bitmap.to_pil().save(output,format='PNG')
                    return output.getvalue()
                finally:
                    bitmap.close()
            finally:
                page.close()
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS=32_000_000
    warnings.simplefilter('error',Image.DecompressionBombWarning)
    with Image.open(io.BytesIO(data)) as original:
        if original.format not in {'PNG','JPEG','GIF','WEBP'}:
            raise ValueError('仅支持 PNG、JPEG、GIF、WebP 图片预览')
        width,height=original.size
        if width*height>32_000_000:
            raise ValueError('图片超过 3200 万像素预览上限')
        if action=='info':
            if original.getexif().get(274) in {5,6,7,8}:
                width,height=height,width
            return json.dumps({'kind':'image','width':width,'height':height,'pages':1,'format':original.format}).encode()
        original.seek(0)
        image=ImageOps.exif_transpose(original).convert('RGBA')
        image.thumbnail((2400,2400))
        output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()


if __name__=='__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(10,10))
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))
        result=run(sys.stdin.buffer.read(20*1024*1024+1),sys.argv[1],sys.argv[2],int(sys.argv[3]))
        sys.stdout.buffer.write(result)
    except ModuleNotFoundError:
        sys.stderr.write('预览依赖未安装，请运行 uv sync');sys.exit(42)
    except ValueError as error:
        # Only our explicit errors are exposed; decoder/library messages can contain paths.
        safe=str(error) if str(error).startswith(('PDF 页','PDF 页面','图片超过','仅支持')) else '文件损坏、加密或格式不受支持'
        sys.stderr.write(safe);sys.exit(43)
    except Exception:
        sys.stderr.write('文件损坏、加密或格式不受支持');sys.exit(43)
