#!/usr/bin/env node
// Standalone mock server: the console's production bundle plus the mock API, no Vite.
//   node mock/serve.mjs [--port 4173] [--static ../src/petal_locate/server/static]
// MOCK_TOKEN=secret node mock/serve.mjs   requires an API token (to exercise the token prompt).
import { createServer } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { dirname, extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createMock } from './server.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const port = Number(opt('port', process.env.PORT ?? 4173));
const root = resolve(opt('static', join(here, '../../src/petal_locate/server/static')));
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };

if (!existsSync(join(root, 'index.html'))) {
  console.error(`no index.html in ${root}: run "npm run build" first`);
  process.exit(1);
}
const mock = createMock();
const server = createServer(async (req, res) => {
  if (await mock.handle(req, res)) return;
  const url = new URL(req.url ?? '/', 'http://x');
  let file = normalize(join(root, decodeURIComponent(url.pathname)));
  if (!file.startsWith(root)) {
    res.writeHead(403).end();
    return;
  }
  if (!existsSync(file) || statSync(file).isDirectory()) file = join(root, 'index.html');
  res.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream' });
  createReadStream(file).pipe(res);
});
server.on('upgrade', (req, socket, head) => {
  if (!mock.handleUpgrade(req, socket, head)) socket.destroy();
});
server.listen(port, '127.0.0.1', () => console.log(`mock console on http://127.0.0.1:${port}/ (static ${root})`));
const stop = () => {
  mock.close();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
};
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
