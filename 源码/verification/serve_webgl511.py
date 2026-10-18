"""Loopback-only real WebGL regression page; never starts capture or a proxy."""
import gzip,json,mimetypes
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT.parent/'验证'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/':target=ROOT/'verification/webgl511.html'
        elif path=='/reported-state.json':target=OUT/'reported-state.json'
        else:
            target=(ROOT/'Parser/ErCore/web'/path.lstrip('/')).resolve()
            if not target.is_relative_to((ROOT/'Parser/ErCore/web').resolve()):self.send_error(403);return
            if path.startswith('/m3d/') and not target.exists():target=ROOT.parent/'程序/models'/Path(path).name
        zipped=False
        if not target.exists() and target.with_suffix(target.suffix+'.gz').exists():target=target.with_suffix(target.suffix+'.gz');zipped=True
        if not target.is_file():self.send_error(404);return
        data=gzip.decompress(target.read_bytes()) if zipped else target.read_bytes()
        content_type='application/javascript' if path.endswith(('.js','.mjs')) else mimetypes.guess_type(path if path!='/' else 'test.html')[0] or 'application/octet-stream'
        self.send_response(200);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        if self.path!='/results':self.send_error(404);return
        data=self.rfile.read(int(self.headers['Content-Length']));json.loads(data)
        (OUT/'webgl-results.json').write_bytes(data);self.send_response(204);self.end_headers()
    def log_message(self,*args):pass
print('WebGL regression http://127.0.0.1:17992/',flush=True)
ThreadingHTTPServer(('127.0.0.1',17992),Handler).serve_forever()
