// Behaviours the operator relies on when things go wrong or are uncertain, against the
// mock (the fixtures come from the real core and the server's own advisory): a screen
// that fails to load, a dropped link, the token prompt, an inconsistent fit, an
// unresolved ambiguity, a local signal, a break in an event, site clocks, satellites the
// server cannot place, blind windows, form defaults and panel widths. Skipped against a
// real server (PETAL_URL), whose data these states cannot be forced into.
import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';

test.skip(Boolean(process.env.PETAL_URL), 'needs the mock and its fixtures');

async function open(page: Page, hash: string): Promise<void> {
  await page.goto(`/#/${hash}`);
  await page.waitForLoadState('domcontentloaded');
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('petal.theme', 'dark'));
});

test('a screen whose chunk cannot be loaded stays inside the view; the banner stays; Retry loads it (UX-2)', async ({ page }) => {
  let blocked = true;
  await page.route(/\/assets\/Network-[^/?]+\.js(\?.*)?$/, (route) => (blocked ? route.abort('connectionrefused') : route.continue()));
  await open(page, 'watch');
  await expect(page.locator('.qrow').first()).toBeVisible();
  await page.keyboard.press('3');
  const alert = page.getByRole('alert').filter({ hasText: 'The Network screen could not be loaded' });
  await expect(alert).toBeVisible();
  await expect(page.locator('.banner').first()).toHaveText('UNCLASSIFIED');
  await expect(page.locator('.banner').last()).toBeVisible();
  await expect(page.locator('.tabs')).toBeVisible();
  // the other screens still work, and coming back fails again in place (no blank page)
  await page.keyboard.press('1');
  await expect(page.locator('.qrow').first()).toBeVisible();
  await page.keyboard.press('3');
  await expect(alert).toBeVisible();
  // React.lazy keeps a failed import for good; Retry imports the screen again
  blocked = false;
  await alert.getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByRole('table', { name: 'Sites and their nodes' })).toBeVisible();
});

test('number keys move focus into the view and name the page (UX-11)', async ({ page }) => {
  await open(page, 'watch');
  await expect(page.locator('.qrow').first()).toBeVisible();
  await page.keyboard.press('4');
  await expect(page.locator('main')).toBeFocused();
  await expect(page.locator('main')).toHaveAttribute('aria-label', 'Plan');
  await expect(page).toHaveTitle('Plan – PETAL Locate');
  // moving between events keeps focus where it is
  await page.keyboard.press('1');
  await page.locator('.qrow').first().click();
  await page.keyboard.press('j');
  await expect(page.locator('main')).not.toBeFocused();
});

test('before any status the roll-up says No data, and the token dialog leaves the banners undimmed (UX-7, UX-14)', async ({ page }) => {
  await page.route('**/api/v1/meta', async (route) => {
    const r = await route.fetch();
    await route.fulfill({ response: r, json: { ...(await r.json()), api_token_required: true } });
  });
  await open(page, 'watch');
  await expect(page.locator('dialog[open]')).toBeVisible();
  const rollup = page.locator('.rollupbadge');
  await expect(rollup).toContainText('No data');
  await expect(rollup).not.toContainText('Normal');
  const backdrop = await page.evaluate(() => {
    const d = document.querySelector('dialog');
    if (!d) return null;
    const cs = getComputedStyle(d, '::backdrop');
    return { top: cs.top, bottom: cs.bottom };
  });
  expect(backdrop).toEqual({ top: '20px', bottom: '20px' });
});

/** Rewrites every status message the server sends over the console socket. */
async function rewriteStatus(page: Page, edit: (msg: Record<string, unknown>) => void): Promise<{ sockets: WebSocketRoute[] }> {
  const sockets: WebSocketRoute[] = [];
  await page.routeWebSocket(/\/ws\/ui/, (ws) => {
    sockets.push(ws);
    const server = ws.connectToServer();
    server.onMessage((m) => {
      if (typeof m !== 'string') return ws.send(m);
      try {
        const msg = JSON.parse(m) as Record<string, unknown>;
        if (msg.type === 'status') edit(msg);
        ws.send(JSON.stringify(msg));
      } catch {
        ws.send(m);
      }
    });
  });
  return { sockets };
}

test('a dropped link turns live wording into "as of" wording (UX-8)', async ({ page }) => {
  let down = false;
  const sockets: WebSocketRoute[] = [];
  await page.routeWebSocket(/\/ws\/ui/, (ws) => {
    if (down) {
      void ws.close({ code: 1011, reason: 'test: server gone' });
      return;
    }
    sockets.push(ws);
    ws.connectToServer();
  });
  await page.route('**/api/v1/**', (route) => (down ? route.abort('connectionrefused') : route.continue()));
  await open(page, 'event/EV-0042');
  await expect(page.locator('.qrow .age').first()).toHaveText('now');
  await expect(page.locator('.dhead')).toContainText('Last seen now');
  down = true;
  for (const ws of sockets) await ws.close({ code: 1011, reason: 'test: server gone' });
  await expect(page.locator('.linkstate').filter({ hasText: 'Disconnected' })).toBeVisible();
  await expect(page.locator('.queue h2').first()).toContainText('as of');
  await expect(page.locator('.qrow .age').first()).not.toHaveText('now');
  await expect(page.locator('.dhead')).not.toContainText('Last seen now');
  await expect(page.locator('.dhead .tasking')).toContainText('unknown (console disconnected)');
  await expect(page.locator('.rollupbadge')).toContainText('not current');
});

test('an inconsistent fit is a caution, never a fix (UX-1)', async ({ page }) => {
  await open(page, 'watch');
  const row = page.locator('.qrow[data-event="EV-0040"]');
  await expect(row).toContainText('Measurements inconsistent: no fix');
  await expect(row).not.toContainText('semi-axes');
  await row.click();
  const loc = page.getByRole('region', { name: 'Location' });
  await expect(loc).toContainText('Not a fix');
  await expect(loc).toContainText('do not report it');
  await expect(loc).toContainText('Computed, not a fix');
  await expect(page.getByRole('region', { name: 'Advisory' })).toContainText('do not report the computed position');
});

test('an unresolved ambiguity lists every position that fits (UX-6) with semi-axes (UX-9)', async ({ page }) => {
  await open(page, 'watch');
  const row = page.locator('.qrow[data-event="EV-0043"]');
  await expect(row).toContainText(/Ambiguous, one of 2 positions: .+ or .+/);
  await row.click();
  const loc = page.getByRole('region', { name: 'Location' });
  await expect(loc).toContainText('One of 2 positions');
  await expect(loc.locator('table.candidates tbody tr')).toHaveCount(2);
  await expect(loc).toContainText('cannot choose between them');
  await expect(loc).not.toContainText('MGRS');
});

test('a local signal shows what is received at the site, not an uplink (UX-3)', async ({ page }) => {
  await open(page, 'watch');
  const row = page.locator('.qrow[data-event="EV-0041"]');
  await expect(row).toContainText('260.5009–260.5041 MHz received at Busan');
  await expect(row).not.toContainText('MHz up');
  await row.click();
  const head = page.locator('.dhead');
  await expect(head).toContainText('Received 260.5009–260.5041 MHz at Busan');
  await expect(head).toContainText('busan-pac (muos-pac)');
  await expect(head).not.toContainText('Uplink');
});

test('a break shows when the event came back, the start to report and the set-aside ellipses (UX-5, K18, UX-14)', async ({ page }) => {
  await open(page, 'event/EV-0042');
  const head = page.locator('.dhead');
  await expect(head).toContainText(/Detected again \d{6}Z [A-Z]{3} \d{2} \d{2}:\d{2}:\d{2}Z after a break of 41 s/);
  await expect(head).toContainText(/Start \d{6}Z [A-Z]{3} \d{2} \d{2}:\d{2}:\d{2}Z/);
  await expect(head).toContainText('First seen');
  const hist = page.getByRole('region', { name: 'Ellipse history' });
  await expect(hist).toContainText('2 earlier solves set aside');
  await expect(hist.locator('th', { hasText: 'Semi-major' })).toBeVisible();
  await expect(hist.locator('tbody tr.muted')).toHaveCount(2);
  await expect(page.getByRole('region', { name: 'Transitions' }).locator('td.mono').first()).toHaveText(/^\d{6}Z [A-Z]{3} \d{2} \d{2}:\d{2}:\d{2}Z$/);
});

test('a site clock the calibrations put off is a caution in Network and the roll-up (UX-10)', async ({ page }) => {
  await open(page, 'network');
  const table = page.getByRole('table', { name: 'Sites and their nodes' });
  await expect(table).toBeVisible();
  const manila = table.locator('tr', { hasText: 'Manila' }).first();
  await expect(manila).toContainText('−1.80 µs');
  await expect(manila.getByRole('img', { name: 'Caution' }).first()).toBeVisible();
  await open(page, 'watch');
  await expect(page.locator('.rollup')).toContainText('manila');
});

test('satellites the server cannot place and blind windows are shown, not only logged (ADAPT-7, SRV-1, K1)', async ({ page }) => {
  await rewriteStatus(page, (msg) => {
    const sats = msg.satellites as Array<Record<string, unknown>>;
    const first = sats[0];
    if (first) Object.assign(first, { ephemeris_ok: false, unavailable_since_ns: msg.server_t_ns, message: `satellite ${String(first.sat_id)}: ephemeris not available since 2026-11-13 18:00:00Z (t_s outside the ephemeris table span); its pairs are not tasked` });
    const nodes = msg.nodes as Array<{ site_id: string; channels: Array<{ channel_id: string; ok: boolean; windows?: Array<Record<string, unknown>>; detection_problem?: string | null }> }>;
    const tokyo = nodes.find((n) => n.site_id === 'tokyo');
    const ch = tokyo?.channels[0];
    if (ch?.windows?.[0] && ch.windows[1]) {
      ch.ok = false;
      Object.assign(ch.windows[0], { blind: true, blind_reason: 'out_of_order' });
      Object.assign(ch.windows[1], { floor_raised_db: 9.5 });
      ch.detection_problem = 'detection is blind on one window; noise floor raised on another';
    }
  });
  await open(page, 'watch');
  await expect(page.locator('.rollup .satdown')).toContainText('ephemeris not available since');
  await expect(page.locator('.rollup')).toContainText('tokyo');
  await expect(page.locator('.rollupbadge')).toContainText(/Critical|Serious/);
  await open(page, 'network');
  const table = page.getByRole('table', { name: 'Sites and their nodes' });
  await expect(table).toContainText('detection blind on');
  await expect(table).toContainText('noise floor +9.5 dB on');
  await expect(page.getByRole('table', { name: 'Satellite ephemerides' })).toContainText('ephemeris not available since');
});

test('loading a preset over unapplied edits asks first, as Reset does (UX-12)', async ({ page }) => {
  await open(page, 'simulation');
  await expect(page.locator('.simhead')).toContainText('version');
  await page.locator('.side table.grid tbody input[type="checkbox"]').first().click();
  await expect(page.locator('.simhead')).toContainText('unapplied changes');
  await page.getByLabel('Preset').selectOption('quiet');
  await page.getByRole('button', { name: 'Load preset' }).click();
  await expect(page.locator('.simhead')).toContainText('Load quiet? Unapplied changes are lost.');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.locator('.simhead')).toContainText('unapplied changes');
});

test('the accuracy form starts from the configuration, and the side panel does not clip (ADAPT-10, UX-13)', async ({ page }) => {
  for (const [w, h] of [
    [1280, 800],
    [1920, 1080],
  ] as const) {
    await page.setViewportSize({ width: w, height: h });
    await open(page, 'plan');
    await expect(page.getByRole('table', { name: 'Elevation of each satellite from each site' })).toBeVisible();
    const look = await page.locator('.split > .side').evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(look[0], `look-angle panel at ${w} px`).toBeLessThanOrEqual(look[1] ?? 0);
    await open(page, 'plan?mode=accuracy');
    await expect(page.getByLabel('Pair 1 channel A')).toBeVisible();
    const acc = await page.locator('.split > .side').evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(acc[0], `accuracy panel at ${w} px`).toBeLessThanOrEqual(acc[1] ?? 0);
  }
  // pairs at the site watching all three satellites, in the band they all relay
  await expect(page.getByLabel('Pair 1 channel A')).toHaveValue('tokyo-pac');
  await expect(page.getByLabel('Pair 1 channel B')).toHaveValue('tokyo-io');
  await expect(page.locator('#acc-fe')).toHaveValue('301.000');
  await expect(page.locator('form.side')).toContainText('relayed by muos-pac/shared-1');
});
