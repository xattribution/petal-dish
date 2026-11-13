// Vite dev-server plugin: serves the mock console API (REST under /api/v1, /ws/ui).
import { createMock } from './server.mjs';

export function mockApiPlugin() {
  return {
    name: 'petal-locate-mock-api',
    configureServer(server) {
      const mock = createMock({ log: () => {} });
      server.middlewares.use((req, res, next) => {
        mock.handle(req, res).then((handled) => (handled ? undefined : next()), next);
      });
      server.httpServer?.on('upgrade', (req, socket, head) => {
        if ((req.url ?? '').startsWith('/ws/ui')) mock.handleUpgrade(req, socket, head);
      });
      server.httpServer?.on('close', () => mock.close());
    },
  };
}
