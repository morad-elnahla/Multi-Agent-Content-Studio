"""Standalone, copy-friendly content handoff from a completed SanadSquad run.

This file deliberately shows only the finished posts and image prompts.
All research, reviewer feedback and operational logs remain in the run-detail ZIP.
No extra dependencies; generated HTML works offline in a normal browser.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path


def _escape(value: object) -> str:
    return html.escape(str(value or ''), quote=True)


def _piece_group(piece: dict) -> str:
    return 'thread' if piece.get('type') == 'tech-thread' else 'marketing'


def _card(piece: dict, index: int, group: str) -> str:
    slug = ('t' if group == 'thread' else 'm') + str(index)
    posts = piece.get('posts') or []
    topic = _escape(piece.get('topic') or f'{group.title()} {index}')
    is_thread = group == 'thread'
    post_count = len(posts)
    subtitle = f'{post_count} tweets in this thread' if is_thread else 'Single post'
    copy_button = (f'<button class="copy-btn prominent" type="button" data-copy="{slug}-all">'
                   f'<span aria-hidden="true">⧉</span> Copy full thread</button>') if is_thread else ''
    tweets = []
    for n, post in enumerate(posts, 1):
        tweet_id = f'{slug}-tweet-{n}'
        label = f'TWEET {n:02d} / {post_count:02d}' if is_thread else 'POST TEXT'
        tweets.append(f'''<section class="tweet" aria-label="{label}">
            <div class="tweet-meta"><span>{label}</span>
               <button class="copy-btn quiet" type="button" data-copy="{tweet_id}">Copy text ↗</button></div>
            <p class="tweet-text" id="{tweet_id}">{_escape(post.get('text'))}</p>
        </section>''')
    image_id = f'{slug}-image'
    image_text = _escape(piece.get('image_description') or 'No image brief available')
    label = f'THREAD {index:02d}' if is_thread else f'MARKETING POST {index:02d}'
    image = f'''<div class="image-brief">
       <div class="image-heading"><div><div class="tiny-label">VISUAL BRIEF</div><h4>Image description</h4></div>
          <button class="copy-btn quiet" type="button" data-copy="{image_id}">Copy image prompt ↗</button></div>
       <p class="image-text" id="{image_id}">{image_text}</p>
       <div class="image-hint">Text description only · No image generated</div>
     </div>'''
    return f'''<article class="content-card" id="{slug}">
      <div class="card-header">
        <div class="card-heading">
            <div class="eyebrow">{label}<span class="dot"></span>{subtitle}</div>
            <h3>{topic}</h3>
        </div>
        {copy_button}
      </div>
      <div id="{slug}-all" class="tweets">{''.join(tweets)}</div>
      {image}
    </article>'''


def render_delivery_html(final_content: dict, date_label: str | None = None) -> str:
    """Build a self-contained delivery page from final_content.json, without editing texts."""
    approved = final_content.get('approved') or []
    threads = [p for p in approved if _piece_group(p) == 'thread']
    marketing = [p for p in approved if _piece_group(p) == 'marketing']
    total_tweets = sum(len(p.get('posts') or []) for p in approved)
    date_label = _escape(date_label or datetime.now().strftime('%d %b %Y'))
    thread_cards = ''.join(_card(p, i, 'thread') for i, p in enumerate(threads, 1))
    marketing_cards = ''.join(_card(p, i, 'marketing') for i, p in enumerate(marketing, 1))
    if not thread_cards:
        thread_cards = '<p class="empty">No technical threads approved in this run.</p>'
    if not marketing_cards:
        marketing_cards = '<p class="empty">No marketing posts approved in this run.</p>'
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SanadSquad | Content Delivery</title>
<style>
:root {{ color-scheme:dark; --bg:#09131d; --panel:#111f2a; --panel2:#172a34; --line:#29404b; --text:#ecf5f0; --muted:#9fb9b7; --green:#b6efd0; --green2:#74d9af; }}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:radial-gradient(ellipse 600px 500px at 83% -7%,#1b4b48 0%,transparent 75%),var(--bg);color:var(--text);font:16px/1.65 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
.wrap{{max-width:1080px;padding:0 28px;margin:auto}}.topline{{border-bottom:1px solid var(--line);padding:26px 0;display:flex;align-items:center;justify-content:space-between;gap:20px}}.logo{{display:flex;gap:12px;align-items:center;font-size:20px;font-weight:820;letter-spacing:-.9px}}.mark{{background:#b6efd0;color:#0e3228;font-weight:950;width:37px;height:37px;border-radius:12px;display:grid;place-items:center;font-size:21px}}.header-note{{color:var(--muted);font-size:13px;letter-spacing:.08em;text-transform:uppercase}}
.hero{{padding:72px 0 48px}}.overline{{color:var(--green2);font-size:12px;letter-spacing:.17em;text-transform:uppercase;font-weight:800}}h1{{font-size:clamp(38px,6.2vw,72px);line-height:1.06;max-width:850px;margin:14px 0 20px;letter-spacing:-.058em;font-weight:830}}h1 em{{color:var(--green);font-style:normal}}.intro{{max-width:690px;color:#b1c6c5;font-size:17px;line-height:1.85;margin:0 0 32px}}.metrics{{display:flex;flex-wrap:wrap;gap:12px}}.metric{{padding:14px 20px;background:#142630;border:1px solid #28434b;border-radius:14px;display:flex;align-items:center;gap:12px;min-width:166px}}.metric b{{font-size:25px;color:var(--green);line-height:1}}.metric span{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;line-height:1.4}}
.nav{{display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin:8px 0 58px}}.nav a{{color:#d8eeeb;text-decoration:none;border:1px solid #345049;padding:10px 18px;border-radius:100px;font-size:13px;font-weight:720;transition:.2s}}.nav a:hover{{background:#214d41;border-color:#75cba9}}.nav .hint{{margin-left:auto;font-size:12px;color:var(--muted)}}
.section{{margin:0 0 72px;scroll-margin-top:30px}}.section-head{{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:30px;gap:15px}}.section-head h2{{font-size:31px;letter-spacing:-.04em;margin:2px 0 0;line-height:1.25}}.section-note{{max-width:460px;font-size:13px;color:var(--muted);margin:0}}.count{{display:inline-block;border:1px solid #365c55;background:#19352f;border-radius:9px;padding:4px 10px;color:#b6efd0;font-weight:760;font-size:12px}}
.content-card{{border:1px solid var(--line);background:linear-gradient(180deg,#13242f,#101f2a);border-radius:23px;padding:30px 34px;margin:0 0 28px;box-shadow:0 14px 42px #0002}}.card-header{{display:flex;align-items:start;justify-content:space-between;gap:22px;padding-bottom:21px}}.eyebrow{{font-size:12px;letter-spacing:.105em;font-weight:760;color:var(--green2);display:flex;align-items:center;gap:11px;text-transform:uppercase}}.dot{{width:4px;height:4px;border-radius:50%;background:#6c8d88}}h3{{font-size:clamp(22px,3vw,29px);letter-spacing:-.04em;margin:7px 0 0;line-height:1.25}}.copy-btn{{font:inherit;cursor:pointer;border:1px solid #43665a;background:#203a34;color:#d6ffea;border-radius:10px;padding:9px 14px;font-size:12px;line-height:1.4;font-weight:720;white-space:nowrap;transition:background .15s,color .15s}}.copy-btn:hover{{background:#335949}}.copy-btn.prominent{{background:var(--green);color:#10281f;border-color:var(--green)}}.copy-btn.prominent:hover{{background:#dbffe8}}.copy-btn.quiet{{background:#20343e;color:#bcdcd8;border-color:#36505b}}.copy-btn.quiet:hover{{background:#345363;color:white}}
.tweet{{padding:18px 0 19px;border-top:1px solid #2b424a}}.tweet-meta{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;color:#7fd9b8;font-size:10px;font-weight:830;letter-spacing:.14em}}.tweet .copy-btn{{font-size:11px;padding:6px 10px;letter-spacing:0}}.tweet-text{{margin:0;font-size:17px;line-height:1.76;white-space:pre-wrap;overflow-wrap:anywhere;max-width:880px;color:#f3f7f3;user-select:text}}.image-brief{{margin-top:18px;background:#1a3038;border:1px solid #3a5457;border-radius:16px;padding:20px 22px}}.image-heading{{display:flex;justify-content:space-between;align-items:center;gap:12px}}.tiny-label{{font-size:10px;letter-spacing:.15em;font-weight:780;color:#91d8bc}}h4{{margin:4px 0 0;font-size:17px;letter-spacing:-.02em}}.image-text{{color:#d1e4df;margin:12px 0 10px;font-size:14px;line-height:1.78;white-space:pre-wrap;overflow-wrap:anywhere}}.image-hint{{font-size:11px;color:#8aa7a4}}.empty{{border:1px dashed var(--line);border-radius:15px;padding:26px;color:var(--muted)}}.footer{{border-top:1px solid var(--line);padding:24px 0 45px;display:flex;gap:12px;justify-content:space-between;color:var(--muted);font-size:12px}}
button:focus-visible,a:focus-visible{{outline:3px solid #f2d783;outline-offset:3px}}
@media(max-width:700px){{.wrap{{padding:0 17px}}.topline{{padding:18px 0}}.header-note{{font-size:10px}}.hero{{padding:48px 0 32px}}.nav{{margin:8px 0 40px}}.nav .hint{{display:none}}.section-head{{display:block}}.section-note{{margin-top:10px}}.content-card{{padding:19px 17px;border-radius:17px}}.card-header{{display:block}}.card-header .copy-btn{{margin-top:17px}}.tweet-text{{font-size:15px}}.image-heading{{align-items:start}}.image-heading .copy-btn{{white-space:normal}}.footer{{display:block}}}}
@media print{{:root{{color-scheme:light}}body{{color:#15232c;background:white!important;font-size:12pt}}.wrap{{max-width:none;padding:0 15px}}.nav,.copy-btn,.metric,.header-note{{display:none!important}}.hero{{padding:15px 0 5px}}h1{{font-size:30pt;color:black}}h1 em{{color:#226650}}.intro{{font-size:11pt;color:#333}}.section-head{{margin:16px 0}}.section-head h2{{font-size:20pt;color:black}}.content-card{{border-color:#ccc;background:white!important;box-shadow:none;padding:15px;margin:0 0 20px;break-inside:avoid}}.eyebrow,.tweet-meta,.overline,.tiny-label{{color:#267a5b}}h3,h4,.tweet-text,.image-text{{color:#222}}.tweet{{border-color:#ddd}}.image-brief{{background:#f4f7f7;border-color:#ddd}}.image-text{{color:#333}}.image-hint,.section-note{{color:#666}}.footer{{color:#555}}.section{{margin-bottom:10px}}.metric{{display:none}}}}
</style></head><body>
<div class="wrap"><header class="topline"><div class="logo"><div class="mark">S</div><span>SanadSquad<span style="color:#79d9b5">.</span></span></div><span class="header-note">Content Delivery · {date_label}</span></header>
<main><div class="hero"><div class="overline">Weekly Social Content · X / Twitter</div><h1>Content, <em>ready to review.</em></h1><p class="intro">Your generated technical threads, marketing posts, and matching image briefs — organized in one place, with easy copy controls.</p>
<div class="metrics"><div class="metric"><b>{len(threads):02d}</b><span>Technical<br>threads</span></div><div class="metric"><b>{len(marketing):02d}</b><span>Marketing<br>posts</span></div><div class="metric"><b>{total_tweets:02d}</b><span>Total<br>tweets</span></div></div></div>
<nav class="nav" aria-label="Content categories"><a href="#technical">Technical Threads →</a><a href="#marketing">Marketing Posts →</a><span class="hint">Copy individual posts or full threads</span></nav>
<section class="section" id="technical"><div class="section-head"><div><div class="overline">01 / Technical</div><h2>Technical Threads <span class="count">{len(threads)}</span></h2></div><p class="section-note">Each thread is laid out tweet by tweet, followed by its visual direction.</p></div>{thread_cards}</section>
<section class="section" id="marketing"><div class="section-head"><div><div class="overline">02 / Marketing</div><h2>Marketing Posts <span class="count">{len(marketing)}</span></h2></div><p class="section-note">Standalone promotional copy with a dedicated image description for every post.</p></div>{marketing_cards}</section>
</main><footer class="footer"><span>SanadSquad · Content Pack</span><span>Original generated drafts · Editorial review recommended before publishing</span></footer></div>
<script>
(()=>{{
  function getText(target){{
    if(target.classList.contains('tweets')){{return [...target.querySelectorAll('.tweet-text')].map(el=>el.textContent.trim()).join('\\n\\n');}}
    return target.textContent.trim();
  }}
  async function copyText(value){{
    if(navigator.clipboard && window.isSecureContext){{await navigator.clipboard.writeText(value);return;}}
    const a=document.createElement('textarea');a.value=value;a.style.position='fixed';a.style.left='-9999px';document.body.appendChild(a);a.focus();a.select();
    const ok=document.execCommand('copy');a.remove();if(!ok)throw Error('Browser blocked clipboard');
  }}
  document.addEventListener('click',async e=>{{const btn=e.target.closest('button[data-copy]');if(!btn)return;
    const target=document.getElementById(btn.dataset.copy);if(!target)return;
    const old=btn.innerHTML;
    try{{await copyText(getText(target));btn.textContent='✓ Copied!';}}
    catch(_){{btn.textContent='Select text to copy';}}
    setTimeout(()=>{{btn.innerHTML=old;}},1700);
  }});
}})();
</script></body></html>'''


def write_delivery_html(final_content: dict, destination: Path, date_label: str | None = None) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_delivery_html(final_content, date_label), encoding='utf-8')
    return destination
