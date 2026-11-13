# Third-party material in PETAL Locate

The Python package ships the built operator console (`src/petal_locate/server/static/`,
built from `ui/`). That build contains the third-party material below, at the versions
pinned in `ui/package-lock.json`. Nothing else third-party is bundled: Python
dependencies are installed from the package index, and the build and test tools
(TypeScript, Vite, Vitest, Playwright and their dependencies) are not shipped.

| Material in the console build | Version | Licence | Copyright and source |
|---|---|---|---|
| Natural Earth vector data (land 1:110m and 1:50m, admin-0 countries 1:50m) as TopoJSON, from the world-atlas package | Natural Earth 4.1.0; world-atlas 2.0.2 | Natural Earth: public domain. world-atlas packaging: ISC | naturalearthdata.com; Michael Bostock, github.com/topojson/world-atlas |
| B612 and B612 Mono fonts (Latin subset, weights 400 and 700), from @fontsource/b612 and @fontsource/b612-mono | 5.3.0 | SIL Open Font License 1.1 (OFL-1.1) | Copyright 2012 The B612 Project Authors, github.com/polarsys/b612 |
| React, React DOM, scheduler | 19.1.10, 19.1.10, 0.26.0 | MIT | Copyright (c) Meta Platforms, Inc. and affiliates |
| d3-geo, d3-array | 3.1.1, 3.2.5 | ISC | Copyright 2010-2024 and 2010-2026 Mike Bostock |
| topojson-client | 3.1.0 | ISC | Copyright 2012-2019 Michael Bostock |
| mgrs | 2.2.0 | MIT | Copyright (c) 2012 Mike Adair, Richard Greenwood, Didier Richard, Stephen Irons, Olivier Terral, Calvin Metcalf |
| yaml | 2.9.1 | ISC | Copyright Eemeli Aro |

The full licence texts are in each package's `LICENSE` file (in `ui/node_modules/` after
`npm ci`). The OFL-1.1 text is also at openfontlicense.org. No other fonts are bundled;
the console falls back to the browser's sans-serif and monospace fonts.
