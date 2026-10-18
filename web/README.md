# Syzygy UI

Vanilla HTML/CSS/JS served by `python -m edl serve`.

## FalkorDB Canvas

The live graph uses the official `@falkordb/canvas` web component (bundled into `vendor/falkordb-canvas.js`).

Rebuild the vendor bundle after upgrading the package:

```bash
cd web
npm install
npm run bundle
```

## Demo flow

1. Analyze a repo (GitHub URL or `.`)
2. Click a suggested file to scope
3. Run review — flip Full / 1-hop / Graph off to show **graph lift**
4. GitHub PR posting is intentionally marked **next**
