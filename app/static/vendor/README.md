# Bundled third-party code

| File | What | Source | Licence |
| --- | --- | --- | --- |
| `viz-global-3.30.0.js` | Viz.js 3.30.0, browser build (draws Graphviz DOT as SVG) | `https://cdn.jsdelivr.net/npm/@viz-js/viz@3.30.0/dist/viz-global.js` | MIT, see `viz-js-LICENSE.txt` |

The Viz.js file contains, in compiled (object code) form:
- **Graphviz** (https://www.graphviz.org), Eclipse Public License 1.0 / 2.0;
- **Expat** (https://libexpat.github.io), MIT licence.

It is embedded into `report.html` when `report.viz_js_mode` is `inline` (the default),
so reports open without any network request. Its SHA-256 is recorded in
`config/settings.yaml` (`report.viz_js_sha256`) and checked every time a report is
written; if the file is missing or changed, reports show only the tables.

To update: download the new version once, replace the file and the licence, and update
`viz_js_url`, `viz_js_file` and `viz_js_sha256` in `config/settings.yaml`.
