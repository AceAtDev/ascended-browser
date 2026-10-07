# Build and deploy the documentation site

The site uses the QA quickstart and interruption guide in `docs/`, plus `site/index.md`. It generates plain HTML with no client-side JavaScript or analytics.

## Preview locally

From the repository root, install the build dependency and run:

```bash
PIP_USER=0 python -m pip install Markdown==3.10.2
python scripts/build_site.py
python -m http.server 8765 --directory _site
```

Open `http://localhost:8765/`. Relative navigation works locally; canonical URLs and the sitemap deliberately use the production GitHub Pages URL.

`PIP_USER=0` avoids a forced user-site installation when your Python is already inside a virtual environment.

## Deploy on GitHub Pages

Set repository Settings → Pages → Source to GitHub Actions. `.github/workflows/pages.yml` builds and deploys `_site` on pushes to `main` affecting site files. It can also be run manually. No package release is created.

## Edit and validate

Edit `docs/qa-quickstart.md`, `docs/interrupted-actions.md`, `site/index.md` or `site/style.css`. Update titles/descriptions in `scripts/build_site.py` when the page purpose changes. Build and check links, narrow-screen layouts, commands and source claims before pushing. No Search Console verification token is included; verification requires the owner's Google account.
