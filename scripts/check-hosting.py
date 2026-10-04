"""Exercise the installer-generated Caddy config without installing a system service.
Uses the official release downloaded to tmp/host-test. No root/package changes.
"""
import hashlib,json,pathlib,socket,subprocess,tarfile,time,urllib.request,urllib.error
root=pathlib.Path(__file__).resolve().parents[1];work=root/'tmp/host-test';version='2.11.4';asset=f'caddy_{version}_linux_amd64.tar.gz'
# Fetch the official release once (the same files the installer verifies); later runs reuse it.
work.mkdir(parents=True,exist_ok=True)
for name,target in [(asset,'caddy.tar.gz'),(f'caddy_{version}_checksums.txt','checksums.txt')]:
    if not (work/target).exists():
        with urllib.request.urlopen(f'https://github.com/caddyserver/caddy/releases/download/v{version}/{name}',timeout=120) as r:(work/target).write_bytes(r.read())
expected=[l.split()[0] for l in (work/'checksums.txt').read_text().splitlines() if len(l.split())==2 and l.split()[1].lstrip('*')==asset]
assert len(expected)==1 and hashlib.sha512((work/'caddy.tar.gz').read_bytes()).hexdigest()==expected[0]
with tarfile.open(work/'caddy.tar.gz') as t:(work/'caddy').write_bytes(t.extractfile('caddy').read())
(work/'caddy').chmod(0o755)
for port in [56302,56303]:
    config=subprocess.check_output(['bash','-c','source "$1"; render_config "$2" "$3" 127.0.0.1','bash',str(root/'scripts/install-linux.sh'),str(root/'dist'),str(port)],text=True)
    path=work/f'Caddyfile-{port}';path.write_text(config)
    subprocess.run([str(work/'caddy'),'validate','--config',str(path),'--adapter','caddyfile'],check=True,capture_output=True)
    log=(work/f'caddy-{port}.log').open('w');proc=subprocess.Popen([str(work/'caddy'),'run','--config',str(path),'--adapter','caddyfile'],stdout=log,stderr=log)
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/BUILD.json',timeout=1) as r:
                    assert r.headers['Cache-Control']=='no-cache';meta=json.load(r)
                break
            except urllib.error.URLError:time.sleep(.1)
        else:raise AssertionError('Caddy did not start')
        assert meta==json.loads((root/'dist/BUILD.json').read_text())
        for name,digest in meta['files'].items():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/{name}') as r:assert hashlib.sha256(r.read()).hexdigest()==digest
        try:urllib.request.urlopen(f'http://127.0.0.1:{port}/does-not-exist')
        except urllib.error.HTTPError as e:assert e.code==404
        else:raise AssertionError('Missing route should be 404')
        print(f'PASS real Caddy HTTP on {port}: manifest, every asset SHA, cache revalidation and 404')
    finally:proc.terminate();proc.wait(timeout=10);log.close()
for value in ['0','65536','-1','abc','80;id']:
    r=subprocess.run(['bash',str(root/'scripts/install-linux.sh'),'--port',value],capture_output=True,text=True)
    assert r.returncode==2 and 'Port must' in r.stderr
assert subprocess.run(['bash',str(root/'scripts/install-linux.sh'),'--help'],capture_output=True).returncode==0
print('PASS installer argument validation before any system changes')
