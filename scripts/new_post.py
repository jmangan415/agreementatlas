"""Stamp a new engineering-log post from the site template.

    python scripts/new_post.py "Title of the post"

Creates web/blog/YYYY-MM-DD-slug.html with the site chrome and an empty
article body, and prints the <li> block to paste into web/blog/index.html.
Posts are plain HTML on purpose: no generator, no dependency, nothing to
break — the same trade the rest of the site makes.
"""

import datetime
import html
import re
import sys
from pathlib import Path

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{title} — an AgreementAtlas improvement pass.">
  <title>{title} — AgreementAtlas</title>
  <link rel="icon" href="/assets/agreementatlas-favicon.svg">
  <link rel="stylesheet" href="/styles.css">
</head>
<body class="story-page">
<header class="topbar">
  <a class="brand" href="/" aria-label="AgreementAtlas home">
    <img class="brand-mark" src="/assets/agreementatlas-mark-dark.svg" width="34" height="34" alt="" aria-hidden="true">
    <span class="wordmark">
      <span class="wordmark-name"><span class="wm-a">Agreement</span><span class="wm-b">Atlas</span></span>
      <small>License Intelligence</small>
    </span>
  </a>
  <div class="product-promise">
    <span class="eyebrow">LOCAL-FIRST LEGAL RAG</span>
    <span>Map obligations, permissions, scope and precedence to exact clauses.</span>
  </div>
  <a class="topbar-cta" href="/app.html">Open the live tool →</a>
</header>

<main class="story-main">
  <article class="story-section blog-post">
    <p class="eyebrow">Improvement pass · {pretty_date}</p>
    <h1>{title}</h1>

    <p>Write here.</p>
  </article>
</main>

<footer class="story-footer">
  <a href="/blog/">Engineering log</a>
  <a href="/app.html">Live tool</a>
  <a href="/privacy.html">Privacy</a>
  <a href="/terms.html">Terms</a>
</footer>
</body>
</html>
"""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit('usage: python scripts/new_post.py "Title of the post"')
    title = sys.argv[1].strip()
    today = datetime.date.today()
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:60]
    name = f"{today.isoformat()}-{slug}.html"
    target = Path(__file__).resolve().parent.parent / "web" / "blog" / name
    if target.exists():
        raise SystemExit(f"{target} already exists")
    pretty = today.strftime("%-d %B %Y")
    target.write_text(
        TEMPLATE.format(title=html.escape(title), pretty_date=pretty),
        encoding="utf-8",
    )
    print(f"created web/blog/{name}\n")
    print("paste into the list in web/blog/index.html:\n")
    print(
        f"      <li>\n"
        f'        <span class="blog-date">{pretty.replace(" ", "&nbsp;")}</span>\n'
        f'        <a href="/blog/{name}">{html.escape(title)}</a>\n'
        f"        <p>One-line summary.</p>\n"
        f"      </li>"
    )


if __name__ == "__main__":
    main()
