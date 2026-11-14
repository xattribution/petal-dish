// Smoke test: walk every screen of the console in the dark and the light theme, exercise
// the paths an operator depends on (event detail, map layers, spectrum, network, plan with
// an accuracy map, configuration with a validation error and a version conflict,
// simulation emitter placement), fail on any unexpected console error, and save a
// screenshot of each screen at 1280 x 800.
//
// Against the mock (default) or a real server (PETAL_URL). Steps that need data a real
// server may not have (an ambiguous fix, a simulation) are skipped when it is absent.
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const real = Boolean(process.env.PETAL_URL);
const shots = process.env.PETAL_SHOTS ?? join('test-results', 'screens');
mkdirSync(shots, { recursive: true });

// Responses the walk provokes on purpose; the browser logs them as console errors.
const EXPECTED = [/status of 409/, /status of 422/];

function watchConsole(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error' && !EXPECTED.some((r) => r.test(m.text()))) errors.push(m.text());
  });
  page.on('pageerror', (e) => errors.push(`page error: ${e.message}`));
  return errors;
}

async function shot(page: Page, name: string, theme: string): Promise<void> {
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(shots, `${name}-${theme}.png`) });
}

async function open(page: Page, hash: string): Promise<void> {
  await page.goto(`/#/${hash}`);
  await page.waitForLoadState('domcontentloaded');
}

async function apiJson<T>(page: Page, path: string): Promise<T> {
  const token = process.env.PETAL_TOKEN;
  const r = await page.request.get(`/api/v1${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  expect(r.ok(), `${path} answered ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

/** Make the configuration the page holds stale: someone else saves first. */
async function saveFromElsewhere(page: Page): Promise<void> {
  if (!real) {
    const r = await page.request.post('/__mock/bump-config');
    expect(r.ok()).toBeTruthy();
    return;
  }
  const token = process.env.PETAL_TOKEN;
  const auth: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const g = await page.request.get('/api/v1/config', { headers: auth });
  type Channel = { id: string; monitors?: unknown[] };
  const env = (await g.json()) as { version: number; config: { sites: Array<{ channels: Channel[] }> }; derived_monitors?: string[] };
  // save it unchanged, as a client should: derived windows go back as monitors: [] so they stay derived
  const derived = new Set(env.derived_monitors ?? []);
  for (const site of env.config.sites) for (const ch of site.channels) if (derived.has(ch.id)) ch.monitors = [];
  const etag = g.headers()['etag'] ?? `"${env.version}"`;
  const p = await page.request.put('/api/v1/config', { data: env.config, headers: { ...auth, 'If-Match': etag } });
  expect(p.ok(), `saving the unchanged configuration answered ${p.status()}`).toBeTruthy();
}

for (const theme of ['dark', 'light'] as const) {
  test.describe(`console, ${theme} theme`, () => {
    test.use({ colorScheme: theme });

    test.beforeEach(async ({ page }) => {
      const token = process.env.PETAL_TOKEN;
      await page.addInitScript(([t, th]) => {
        localStorage.setItem('petal.theme', th);
        if (t) sessionStorage.setItem('petal.apiToken', t);
      }, [token ?? '', theme]);
    });

    test('walk every screen', async ({ page }, info: TestInfo) => {
      const errors = watchConsole(page);
      const meta = await apiJson<{ marking: { line: string } | null; simulation: boolean }>(page, '/meta');

      // ---- watch: roll-up, queue, map
      await open(page, 'watch');
      await expect(page.locator('.rollup')).toContainText(/nodes online/);
      if (meta.marking) await expect(page.locator('.banner').first()).toHaveText(meta.marking.line);
      else await expect(page.locator('.banner')).toHaveCount(0);
      await expect(page.locator('.clockstrip .zulu')).toContainText(/\d{6}Z [A-Z]{3} \d{2}/);
      const rows = page.locator('.qrow');
      await expect(rows.first()).toBeVisible();
      await shot(page, 'watch', theme);

      // ---- map layers: toggle footprints and check the legend reflects it
      const legend = page.getByRole('group', { name: 'Map layers' });
      await expect(legend).toBeVisible();
      const fp = legend.getByLabel(/Footprints/);
      const before = await fp.isChecked();
      await fp.click();
      await expect(fp).toBeChecked({ checked: !before });
      await page.locator('.map').focus();
      await page.keyboard.press('+');
      await page.keyboard.press('ArrowLeft');
      await shot(page, 'map-layers', theme);
      await fp.click();

      // ---- event dossier (most urgent event first)
      await rows.first().click();
      await expect(page.locator('.dossier h1')).toBeVisible();
      await expect(page.getByRole('region', { name: 'Advisory' })).toBeVisible();
      await expect(page.locator('table.matrix')).toBeVisible();
      await shot(page, 'event', theme);
      const loc = page.getByRole('region', { name: 'Location' });
      await loc.scrollIntoViewIfNeeded();
      await shot(page, 'event-location', theme);
      if (await page.locator('.fixline').count()) {
        await expect(loc).toContainText('MGRS');
        await expect(loc).toContainText('95 % ellipse');
        await expect(loc).toContainText('CEP50');
        await expect(page.getByRole('region', { name: 'Measurements' })).toContainText('TDOA');
        await page.getByRole('region', { name: 'Correlation' }).scrollIntoViewIfNeeded();
        await expect(page.getByRole('img', { name: /Correlation surface/ })).toBeVisible();
        // scrolling deep into the dossier must never scroll the shell: banners and app bar stay put
        expect(await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0)).toBe(0);
        if (meta.marking) expect((await page.locator('.banner').first().boundingBox())?.y).toBe(0);
        await expect(page.locator('.appbar')).toBeInViewport();
        await shot(page, 'event-correlation', theme);
      }
      if (await page.locator('.dossier .simtag').count()) await expect(page.locator('.dossier')).toContainText('SIMULATION');
      // keyboard: j moves to the next event
      await page.keyboard.press('j');
      await expect(page).toHaveURL(/#\/event\//);

      // ---- an ambiguous fix: every position that fits is listed (none as "the" fix) and drawn
      const ambiguous = page.locator('.qrow', { hasText: 'ambiguous' }).first();
      if (await ambiguous.count()) {
        await ambiguous.click();
        await expect(page.getByRole('region', { name: 'Location' })).toContainText(/One of \d positions|Mirror 1/);
        await page.getByRole('region', { name: 'Location' }).scrollIntoViewIfNeeded();
        await shot(page, 'event-ambiguous', theme);
      }

      // ---- spectrum: 20 windows at 2 Hz, hover readout
      await open(page, 'spectrum?cols=4&dense=1');
      await expect(page.locator('.specpanel').first()).toBeVisible();
      await expect(page.locator('.specpanel[data-state="live"]').first()).toBeVisible({ timeout: 10_000 });
      // hover one live panel and read that panel's readout (against a real server the
      // windows go live in arrival order, so "the first live panel" can change meanwhile).
      // The panel is held by its label (data-key holds a NUL, which no CSS selector matches)
      // and hovered again until the readout follows: hover() waits until the trace is
      // visible, stable and receives the pointer, and the point moves by a pixel or so each
      // time so every attempt is a fresh pointer move.
      const label = await page.locator('.specpanel[data-state="live"]').first().getAttribute('aria-label');
      if (!label) throw new Error('no live spectrum panel');
      const live = page.locator(`.specpanel[aria-label="${label}"]`);
      const trace = live.locator('canvas.trace');
      let attempt = 0;
      await expect
        .poll(
          async () => {
            const b = await trace.boundingBox();
            if (b) {
              const x = b.width * (0.55 + 0.005 * (attempt++ % 2));
              await trace.hover({ position: { x, y: b.height * 0.5 }, timeout: 2000 }).catch(() => undefined);
            }
            return (await live.locator('.readout').textContent()) ?? '';
          },
          { timeout: 5000 },
        )
        .toContain('MHz');
      const slow = await page.evaluate(
        () =>
          new Promise<number>((resolve) => {
            let last = performance.now();
            let worst = 0;
            const end = last + 3000;
            const tick = (t: number) => {
              worst = Math.max(worst, t - last);
              last = t;
              if (t < end) requestAnimationFrame(tick);
              else resolve(worst);
            };
            requestAnimationFrame(tick);
          }),
      );
      info.annotations.push({ type: 'spectrum worst frame interval', description: `${slow.toFixed(0)} ms` });
      expect(slow).toBeLessThan(250);
      await shot(page, 'spectrum', theme);

      // ---- network
      await open(page, 'network');
      await expect(page.getByRole('table', { name: 'Sites and their nodes' })).toBeVisible();
      await shot(page, 'network', theme);

      // ---- plan: look angles, then an accuracy map
      await open(page, 'plan');
      await expect(page.getByRole('table', { name: 'Elevation of each satellite from each site' })).toBeVisible();
      await shot(page, 'plan-look-angles', theme);
      await open(page, 'plan?mode=accuracy');
      await page.getByRole('button', { name: 'Compute accuracy map' }).click();
      await expect(page.locator('form.side')).toContainText(/Locatable|No cell can be located/, { timeout: 120_000 });
      await shot(page, 'plan-accuracy', theme);

      // ---- configure: a validation error lands on the field, then a version conflict
      await open(page, 'configure/site-0');
      const lat = page.locator('[id="cfg-sites.0.lat_deg"]');
      await expect(lat).toBeVisible();
      const original = await lat.inputValue();
      await lat.fill('95');
      await expect(page.locator('.field', { has: lat }).locator('.err')).toContainText(/90/, { timeout: 10_000 });
      await expect(lat).toHaveAttribute('aria-invalid', 'true');
      await shot(page, 'config-validation', theme);
      await lat.fill(original);
      const name = page.locator('[id="cfg-sites.0.name"]');
      await name.fill(`${await name.inputValue()} `);
      await expect(page.locator('.problems')).toHaveCount(0, { timeout: 10_000 });
      await saveFromElsewhere(page);
      await page.getByRole('button', { name: /^Save as version/ }).click();
      await expect(page.getByRole('alert').filter({ hasText: 'was saved while you edited' })).toBeVisible();
      await shot(page, 'config-conflict', theme);
      await page.getByRole('button', { name: /discard mine/ }).click();
      await expect(page.locator('.cfgmain .toolbar')).toContainText('no changes');
      await page.getByRole('button', { name: /YAML import and export/ }).click();
      await expect(page.getByRole('button', { name: 'Download YAML' })).toBeVisible();

      // ---- simulation: place a jammer by clicking the map
      if (meta.simulation) {
        await open(page, 'simulation');
        await expect(page.locator('.simhead .simtag')).toHaveText('SIMULATION');
        const before = await page.locator('.side table.grid tbody tr').count();
        await page.getByRole('button', { name: 'Place jammer' }).click();
        const map = page.locator('.main .map canvas');
        const mb = await map.boundingBox();
        if (!mb) throw new Error('simulation map has no size');
        await page.mouse.click(mb.x + mb.width * 0.45, mb.y + mb.height * 0.45);
        await expect(page.locator('.side table.grid tbody tr')).toHaveCount(before + 1);
        await expect(page.locator('.side')).toContainText('jammer-1');
        await shot(page, 'simulation', theme);
        if (real) await page.getByRole('button', { name: 'Discard changes' }).click();
        else {
          await page.getByRole('button', { name: 'Apply to the simulation' }).click();
          await expect(page.getByRole('status').filter({ hasText: 'Applied as scenario version' })).toBeVisible();
          await page.getByRole('button', { name: 'Reset', exact: true }).click();
          await page.getByRole('button', { name: 'Reset scenario' }).click();
          await expect(page.getByRole('status').filter({ hasText: 'Reset to the start scenario' })).toBeVisible();
        }
      }

      expect(errors, errors.join('\n')).toEqual([]);
    });
  });
}

/** The server's API token for a walk outside the themed describe blocks (PETAL_TOKEN). */
async function useToken(page: Page): Promise<void> {
  const token = process.env.PETAL_TOKEN ?? '';
  await page.addInitScript((t) => {
    if (t) sessionStorage.setItem('petal.apiToken', t);
  }, token);
}

test('narrow window (1024 px) keeps the queue and the map', async ({ page }) => {
  const errors = watchConsole(page);
  await useToken(page);
  await page.addInitScript(() => localStorage.setItem('petal.theme', 'dark'));
  await page.setViewportSize({ width: 1024, height: 768 });
  await open(page, 'watch');
  await expect(page.locator('.qrow').first()).toBeVisible();
  await expect(page.locator('.map canvas')).toBeVisible();
  await page.waitForTimeout(1500); // Natural Earth data
  await page.screenshot({ path: join(shots, 'watch-1024-dark.png') });
  expect(errors).toEqual([]);
});

test('keyboard reaches the views and shows focus', async ({ page }) => {
  await useToken(page);
  await open(page, 'watch');
  await expect(page.locator('.qrow').first()).toBeVisible();
  await page.keyboard.press('3');
  await expect(page).toHaveURL(/#\/network/);
  // focus lands in the new view, not on <body> (UX-11)
  await expect(page.locator('main')).toBeFocused();
  await expect(page).toHaveTitle(/^Network/);
  await page.keyboard.press('1');
  await expect(page).toHaveURL(/#\/watch/);
  // navigate() dispatches the hashchange synchronously, so the view and focus change
  // before the next key (ui/e2e/fixes.spec.ts, R15); waiting for the Watch title as well
  // keeps this walk independent of that timing against any server.
  await expect(page).toHaveTitle(/^Watch/);
  await expect(page.locator('main')).toBeFocused();
  await page.keyboard.press('Tab');
  const outline = await page.evaluate(() => getComputedStyle(document.activeElement ?? document.body).outlineStyle);
  expect(outline).not.toBe('none');
});
