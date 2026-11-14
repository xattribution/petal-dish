// Behaviors the operator relies on when things go wrong or are uncertain, against the
// mock (the fixtures come from the real core and the server's own advisory): a screen
// that fails to load, a dropped link, the token prompt, an inconsistent fit, a position
// that is not a fix or was withdrawn, a width that is a lower bound, an unresolved
// ambiguity, a local signal, a break in an event, site clocks, satellites the server
// cannot place, blind windows, form defaults and panel widths. Skipped against a real
// server (PETAL_URL), whose data these states cannot be forced into.
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

test('a number key changes the view and focus before the next key; Back returns (R15)', async ({ page }) => {
  await open(page, 'watch');
  await expect(page.locator('.qrow').first()).toBeVisible();
  // navigate() set location.hash, whose hashchange fires only in a later task: a key
  // handled in between (Tab) went to the old view and focus was then pulled back.
  const seen = await page.evaluate(async () => {
    let changes = 0;
    window.addEventListener('hashchange', () => (changes += 1));
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: '3', bubbles: true }));
    const sync = { changes, hash: location.hash };
    // microtasks only (React flushes a store update there): no other task, so no key, runs
    for (let i = 0; i < 200 && !document.title.startsWith('Network'); i += 1) await Promise.resolve();
    return { ...sync, title: document.title, focused: document.activeElement?.tagName ?? null };
  });
  expect(seen).toEqual({ changes: 1, hash: '#/network', title: 'Network – PETAL Locate', focused: 'MAIN' });
  // each view change is a history entry: Back and Forward move between them
  await page.keyboard.press('4');
  await expect(page).toHaveTitle('Plan – PETAL Locate');
  await page.goBack();
  await expect(page).toHaveURL(/#\/network$/);
  await expect(page).toHaveTitle('Network – PETAL Locate');
  await page.goBack();
  await expect(page).toHaveTitle('Watch – PETAL Locate');
  await page.goForward();
  await expect(page).toHaveTitle('Network – PETAL Locate');
  // the view already shown adds no entry
  await page.keyboard.press('3');
  await page.goBack();
  await expect(page).toHaveTitle('Watch – PETAL Locate');
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

type Doc = Record<string, unknown>;

/**
 * Rewrites one event wherever the console reads it: GET /events/<id>, its row in GET
 * /events and every "event" message on the console socket. `edit` changes the Event
 * document; `summary` the EventSummary row.
 */
async function rewriteEvent(page: Page, id: string, edit: (e: Doc) => void, summary: (s: Doc) => void): Promise<void> {
  await page.route(new RegExp(`/api/v1/events/${id}$`), async (route) => {
    const r = await route.fetch();
    const doc = (await r.json()) as Doc;
    edit(doc);
    await route.fulfill({ response: r, json: doc });
  });
  await page.route(/\/api\/v1\/events(\?.*)?$/, async (route) => {
    const r = await route.fetch();
    const rows = (await r.json()) as Doc[];
    for (const row of rows) if (row.id === id) summary(row);
    await route.fulfill({ response: r, json: rows });
  });
  await page.routeWebSocket(/\/ws\/ui/, (ws) => {
    const server = ws.connectToServer();
    server.onMessage((m) => {
      if (typeof m !== 'string') return ws.send(m);
      const msg = JSON.parse(m) as Doc;
      const ev = msg.event as Doc | undefined;
      if (msg.type === 'event' && ev?.id === id) edit(ev);
      ws.send(JSON.stringify(msg));
    });
  });
}

test('a consistent solution with no redundancy is not a fix, and a width that fills its window is a lower bound (R4, ADAPT-5)', async ({ page }) => {
  const reason = 'no redundancy: 2 measurements for the two unknowns of a position, so the fit cannot be checked against itself';
  await rewriteEvent(
    page,
    'EV-0042',
    (e) => {
      const sol = e.solution as Doc;
      Object.assign(sol, { is_fix: false, not_fix_reason: reason, dof: 0, n_measurements: 2 });
      const hist = e.solution_history as Doc[];
      const last = hist[hist.length - 1];
      if (last) last.is_fix = false;
      Object.assign(e, { state: 'locating', width_lower_bound: true });
    },
    (s) => {
      Object.assign(s.solution as Doc, { is_fix: false });
      Object.assign(s, { state: 'locating', width_lower_bound: true });
    },
  );
  await open(page, 'watch');
  const row = page.locator('.qrow[data-event="EV-0042"]');
  await expect(row).toContainText('Not a fix yet: more measurements needed');
  await expect(row).toContainText('at least 8.00 kHz wide');
  await expect(row).not.toContainText('semi-axes');
  await row.click();
  const loc = page.getByRole('region', { name: 'Location' });
  await expect(loc).toContainText('Not a fix');
  await expect(loc).toContainText('No redundancy: 2 measurements for the two unknowns of a position');
  await expect(loc).toContainText('do not report it until more measurements make it a fix');
  await expect(loc).toContainText('Computed, not a fix');
  await expect(loc).toContainText('no redundancy');
  await expect(page.locator('.dhead')).toContainText('(at least 8.00 kHz)');
  await expect(page.getByRole('region', { name: 'Ellipse history' }).locator('tbody tr').last()).toContainText('not a fix');
});

test('a position the newest measurements no longer admit is withdrawn, with the reason (R6)', async ({ page }) => {
  const why = 'the newest measurements, added to the 21 it rested on, admit no position (no admissible solution)';
  await rewriteEvent(
    page,
    'EV-0042',
    (e) => {
      const transitions = e.transitions as Doc[];
      const t = transitions[transitions.length - 1]?.t_ns ?? e.last_seen_ns;
      Object.assign(e, { state: 'locating', solution: null, solution_t_ns: null });
      transitions.push({ t_ns: t, state: 'locating', verdict: 'uplink', reason: `position withdrawn: ${why}` });
    },
    (s) => Object.assign(s, { state: 'locating', solution: null }),
  );
  await open(page, 'event/EV-0042');
  const loc = page.getByRole('region', { name: 'Location' });
  await expect(loc).toContainText('Position withdrawn');
  await expect(loc).toContainText('The newest measurements, added to the 21 it rested on, admit no position (no admissible solution). Do not report the earlier position.');
});

test('an ellipse widened for ephemeris doubt says for which satellites and by how much; a doubt it does not cover is a caution (closing D1)', async ({ page }) => {
  let doubted = ['muos-pac'];
  let allowance: Doc[] = [{ sat_id: 'muos-pac', scale: 8, sigma_ric_m: [800, 8000, 8000], capped: false }];
  await rewriteEvent(
    page,
    'EV-0042',
    (e) => {
      Object.assign(e.solution as Doc, { ephemeris_allowance: allowance, ellipse_widening: allowance.length ? 1.214 : null });
      e.doubted_satellite_ids = doubted;
    },
    () => undefined,
  );
  await open(page, 'event/EV-0042');
  const loc = page.getByRole('region', { name: 'Location' });
  await expect(loc).toContainText('widened for ephemeris doubt');
  await expect(loc).toContainText('Ephemeris allowance');
  await expect(loc).toContainText('Position sigma of muos-pac ×8, about 8.0 km one sigma.');
  await expect(loc).toContainText("The 95 % ellipse's major axis is 21 % longer than with the configured uncertainty.");
  // covered: no caution
  await expect(loc).not.toContainText('Ephemeris in doubt');
  await expect(loc.getByRole('button', { name: 'About ephemeris allowance' })).toBeVisible();

  // the largest scale, and a second satellite in doubt that the solve did not widen for yet
  doubted = ['muos-pac', 'uhf-172e'];
  allowance = [{ sat_id: 'muos-pac', scale: 20, sigma_ric_m: [2000, 20000, 20000], capped: true }];
  await page.reload();
  await expect(loc).toContainText('Ephemeris in doubt');
  await expect(loc).toContainText('the position of muos-pac wrong by more than the largest allowance the system makes, or by an amount they cannot size');
  await expect(loc).toContainText('the position of uhf-172e wrong beyond its configured uncertainty, and the ellipse does not include that error yet');
  await expect(loc).toContainText('Position sigma of muos-pac ×20, about 20 km one sigma, the largest allowed.');
  await expect(loc.locator('.notice.caution .word', { hasText: 'Ephemeris in doubt' })).toBeVisible();

  // nothing in doubt: nothing said
  doubted = [];
  allowance = [];
  await page.reload();
  await expect(loc).toContainText('95 % ellipse');
  await expect(loc).not.toContainText('Ephemeris allowance');
  await expect(loc).not.toContainText('widened for ephemeris doubt');
  await expect(loc).not.toContainText('Ephemeris in doubt');
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

test('the dossier header wraps instead of widening the dossier at 1024 px (final recheck)', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  for (const id of ['EV-0040', 'EV-0042']) {
    await open(page, `event/${id}`);
    await expect(page.locator('.dhead')).toBeVisible();
    const [scroll, client] = await page.locator('article.dossier').evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(scroll, `dossier ${id} at 1024 px`).toBeLessThanOrEqual((client ?? 0) + 1);
  }
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

test('a site whose offset differs between satellites blames the satellite, not its clock (R3)', async ({ page }) => {
  const note =
    'Reference calibrations put the timing of Seoul (city center) differently through different satellites (through muos-pac about 0.0 µs; through uhf-172e about 1.3 µs ahead). ' +
    'A clock offset would show alike through every satellite, so the clock of Seoul (city center) is probably fine and the position of uhf-172e is probably wrong beyond its configured uncertainty (check its ephemeris).';
  await rewriteStatus(page, (msg) => {
    const clocks = msg.site_clocks as Array<Record<string, unknown>>;
    const seoul = clocks.find((c) => c.site_id === 'seoul');
    if (seoul) Object.assign(seoul, { offset_us: 0.65, caution: false, message: note });
  });
  await open(page, 'network');
  const table = page.getByRole('table', { name: 'Sites and their nodes' });
  const seoul = table.locator('tr', { hasText: 'Seoul' }).first();
  await expect(seoul).toContainText('differs by satellite');
  await expect(seoul.getByRole('img', { name: 'Caution' })).toHaveCount(0);
  const notes = page.getByRole('region', { name: 'Site clocks by calibration' });
  await expect(notes).toContainText('the position of uhf-172e is probably wrong beyond its configured uncertainty');
  // the clock fault the calibrations through two satellites agree on stays a caution
  await expect(notes.locator('li', { hasText: 'Manila' })).toContainText('calibrations through muos-io and uhf-172e agree');
  await expect(notes.locator('li', { hasText: 'Manila' }).getByRole('img', { name: 'Caution' })).toBeVisible();
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

test('the accuracy map can calibrate each cell against the reference nearest it (R7)', async ({ page }) => {
  const sent: unknown[] = [];
  page.on('request', (r) => {
    if (r.url().endsWith('/api/v1/analysis/accuracy') && r.method() === 'POST') sent.push(r.postDataJSON());
  });
  await open(page, 'plan?mode=accuracy');
  await page.getByLabel('Reference emitter').selectOption('nearest');
  await page.getByRole('button', { name: 'Compute accuracy map' }).click();
  await expect(page.locator('form.side')).toContainText(/Locatable|No cell can be located/);
  expect(sent.at(-1)).toMatchObject({ reference_carrier_id: 'nearest' });
});

test('the default accuracy map calibrates as tasking does and says plainly why it cannot (closing D2)', async ({ page }) => {
  const sent: Array<Record<string, unknown>> = [];
  page.on('request', (r) => {
    if (r.url().endsWith('/api/v1/analysis/accuracy') && r.method() === 'POST') sent.push(r.postDataJSON() as Record<string, unknown>);
  });
  await open(page, 'plan?mode=accuracy');
  // live tasking always calibrates against the nearest eligible reference: so does the form, untouched
  await expect(page.getByLabel('Reference emitter')).toHaveValue('nearest');
  await page.getByRole('button', { name: 'Compute accuracy map' }).click();
  const pairs = page.getByRole('list', { name: 'Pairs of the map' });
  await expect(pairs).toBeVisible();
  expect(sent.at(-1)).toMatchObject({ reference_carrier_id: 'nearest', f_e_hz: 301_000_000 });
  const items = pairs.getByRole('listitem');
  await expect(items).toHaveCount(2);
  await expect(items.first()).toContainText('tokyo-pac − tokyo-io');
  await expect(items.first()).toContainText('muos-pac − muos-io');
  await expect(items.first()).toContainText('none (no eligible reference carrier)');
  for (const item of await items.all()) {
    const warning = item.getByRole('note');
    await expect(warning).toContainText('Uncalibrated');
    await expect(warning).toContainText('All 2501 cells are uncalibrated: no reference carrier clear of the target band is relayed by both transponders (ref-guam overlaps the target band');
  }
  // the panel still fits at every width
  for (const [w, h] of [
    [1024, 768],
    [1280, 800],
    [1920, 1080],
  ] as const) {
    await page.setViewportSize({ width: w, height: h });
    const acc = await page.locator('.split > .side').evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(acc[0], `accuracy panel with the result at ${w} px`).toBeLessThanOrEqual(acc[1] ?? 0);
  }
  // the operator's choice stays: none, then computed again
  await page.getByLabel('Reference emitter').selectOption('');
  await page.getByRole('button', { name: 'Compute accuracy map' }).click();
  await expect.poll(() => sent.at(-1)?.reference_carrier_id).toBeNull();
  await expect(page.getByLabel('Reference emitter')).toHaveValue('');
  await expect(pairs.getByRole('listitem').first()).toContainText('none (no reference emitter chosen)');
  await expect(pairs.getByRole('note')).toHaveCount(0);
});

test('the accuracy form starts from the configuration, and the side panel does not clip (ADAPT-10, UX-13)', async ({ page }) => {
  // 1024 px too (final recheck: the elevation table and a hidden tip widened the panel there)
  for (const [w, h] of [
    [1280, 800],
    [1920, 1080],
    [1024, 768],
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
