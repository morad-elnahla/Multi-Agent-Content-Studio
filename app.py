"""SanadSquad Studio: one focused interface for producing reviewed weekly content.

The UI never collects secrets or exposes internal agent/research configuration.
All provider settings are loaded from environment/.env at startup. The CLI retains
advanced controls for engineers; the Streamlit app deliberately does not.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import os
import threading
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from sanad.agents import HAS_LANGGRAPH, run_week
from sanad.config import Config, Plan, ROOT, Slot
from sanad.providers import LiveProvider, runtime_options
from sanad.research import WebResearch

st.set_page_config(
    page_title="SanadSquad | Content Studio",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
/* Quiet app chrome, consistent spacing, and accessible contrast. */
#MainMenu, footer, [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stHeader"], [data-testid="stToolbar"] {display:none !important;}
[data-testid="stAppViewContainer"] {
    background: radial-gradient(ellipse 80% 30% at 80% -10%, #173b38 0%, transparent 70%),
                linear-gradient(180deg,#0c121a 0%,#0f1620 70%,#101823 100%);
    color: #e9f1ef;
}
.block-container {max-width:1100px;padding:3rem 2rem 5rem;}
[data-testid="stVerticalBlockBorderWrapper"] > div {border-color:#2a3b42!important;border-radius:18px!important;}
[data-testid="stVerticalBlockBorderWrapper"] {background:rgba(22,33,43,0.48);border-radius:18px;}
[data-testid="stTextInput"] input {
    background:#101c26!important;color:#eef8f3!important;border:1px solid #344b54!important;
    border-radius:11px!important;padding:13px 14px!important;
}
[data-testid="stTextInput"] input:focus {border-color:#6bddad!important;box-shadow:0 0 0 1px #6bddad44!important;}
[data-testid="stTextInput"] label p {font-weight:650!important;color:#d9e7e5!important;}
[data-testid="stButton"] button[kind="primary"] {
    background:#a6edc8!important;color:#10281f!important;border:0!important;
    border-radius:13px!important;min-height:53px!important;font-weight:750!important;
    font-size:1rem!important;box-shadow:0 8px 30px #2bb77f20!important;
}
[data-testid="stButton"] button[kind="primary"]:hover {background:#c4f8da!important;transform:translateY(-1px);}
[data-testid="stButton"] button[kind="primary"]:disabled {opacity:.5;box-shadow:none!important;transform:none;}
[data-testid="stDownloadButton"] button {border-radius:11px;min-height:46px;}
[data-testid="stProgressBar"] div[role="progressbar"] > div {background:#a6edc8!important;}
[data-testid="stSelectbox"] > div > div {background:#15232b;color:#e9f1ef;border-radius:11px;}
[data-testid="stAlert"] {border-radius:13px;}
.hero-tag {color:#99d8bd;font-size:.8rem;font-weight:750;letter-spacing:.16em;text-transform:uppercase;margin:0 0 18px;}
.hero-title {font-size:clamp(2.3rem,5.5vw,4.25rem);font-weight:800;letter-spacing:-.055em;
    line-height:1.07;color:#f5fbf8;margin:0 0 16px;max-width:820px;}
.hero-title span {color:#a6edc8;}
.hero-copy {color:#aabfbd;font-size:1.03rem;line-height:1.7;margin:0 0 30px;max-width:670px;}
.brand {display:inline-flex;align-items:center;gap:10px;color:#e8f3f0;font-weight:750;
    letter-spacing:-.025em;margin-bottom:34px;font-size:1.15rem;}
.brand-icon {background:#1e5344;color:#b8f2d3;border:1px solid #477d67;border-radius:11px;padding:7px 10px;}
.flow-hint {display:flex;gap:12px;align-items:center;color:#8da7a7;font-size:.82rem;margin:8px 0 23px;flex-wrap:wrap;}
.flow-hint b {color:#a6edc8;font-weight:700;}
.flow-hint .sep {color:#486662;}
.section-tag {font-size:.78rem;text-transform:uppercase;letter-spacing:.14em;font-weight:750;color:#8edfc0;margin:0 0 8px;}
.section-heading {font-size:1.5rem;color:#f1f8f5;letter-spacing:-.035em;font-weight:750;margin:4px 0 5px;}
.section-description {color:#9fb4b5;font-size:.9rem;line-height:1.55;margin-bottom:22px;}
.group-head {color:#ebf6f2;font-size:1.17rem;font-weight:760;margin:5px 0 3px;}
.group-sub {color:#91aaa9;font-size:.82rem;margin-bottom:15px;line-height:1.5;}
.service-label {color:#91c7b2;font-size:.75rem;margin:-6px 0 14px;}
.tip {color:#8ca4a3;font-size:.8rem;margin:10px 0 22px;}
.runline {display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 15px;
    margin-bottom:7px;border:1px solid #2a3b42;background:#131f29;border-radius:11px;}
.runlabel {color:#e7f0ed;font-weight:650;font-size:.91rem;}
.runstatus {color:#a5c9bb;font-size:.81rem;}
.post-card {background:#15232d;border:1px solid #2b4248;border-radius:15px;padding:20px 22px;margin:9px 0;}
.post-index {color:#7fbeab;letter-spacing:.12em;text-transform:uppercase;font-size:.72rem;font-weight:750;margin-bottom:11px;}
.post-text {font-size:1.03rem;line-height:1.74;color:#f1f6f3;white-space:pre-wrap;overflow-wrap:anywhere;}
.outcome {margin:10px 0 16px;color:#a7c2ba;font-size:.9rem;}
.softline {border:0;border-top:1px solid #2a3b42;margin:26px 0 18px;}
@media(max-width:650px){.block-container{padding:1.7rem 1rem 3rem;}.brand{margin-bottom:24px;}}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def server_lock() -> threading.Lock:
    return threading.Lock()


@dataclass
class Job:
    folder: Path
    events: deque = field(default_factory=lambda: deque(maxlen=400))
    done: bool = False
    summary: dict | None = None
    error: str = ""

    def event(self, event: dict):
        self.events.append(dict(event))


def start(plan, config: Config, reuse: Path | None = None) -> Job:
    """Always run the real agents. No demo switch and no silent fixture fallback."""
    lock = server_lock()
    if not lock.acquire(blocking=False):
        raise ValueError("Another content run is already in progress. Please wait for it to finish.")
    folder = ROOT / "output" / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    job = Job(folder)
    try:
        options = runtime_options(ROOT / '.env')
        provider = LiveProvider(options=options, progress=job.event)
        search = WebResearch(config, "live", options.get("TAVILY_API_KEY"))
        concurrency = int(options.get("SANAD_CONCURRENCY", "2"))
        if not 1 <= concurrency <= 8:
            raise ValueError("SANAD_CONCURRENCY must be between 1 and 8")
    except Exception:
        lock.release()
        raise

    def worker():
        try:
            job.summary = asyncio.run(run_week(plan, config, provider, search, folder, concurrency, job.event, reuse=reuse))
        except Exception as error:
            # Never display raw network/API exception messages (which may contain secrets).
            print(f"[SanadSquad] Run stopped: {type(error).__name__}", flush=True)
            job.error = "The run stopped unexpectedly. Check your terminal for the error type."
        finally:
            job.done = True
            lock.release()

    threading.Thread(target=worker, name="sanad-run", daemon=True).start()
    return job


def read_json(folder: Path, name: str, fallback=None):
    path = folder / name
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def logs_zip(folder: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".csv", ".json"):
                archive.write(path, arcname=path.relative_to(folder).as_posix())
    return buffer.getvalue()


def recent_runs() -> list[Path]:
    root = ROOT / "output"
    if not root.is_dir():
        return []
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "run_summary.json").is_file()]
    # The shipped offline fixture remains available for CLI tests but is not shown as a real run.
    return sorted((p for p in candidates if (read_json(p, "run_summary.json", {}) or {}).get("mode") == "live"), reverse=True)


def friendly_slot(slot) -> str:
    ident = slot if isinstance(slot, str) else slot["id"] if isinstance(slot, dict) else slot.id
    if ident.startswith("thread_"):
        return "Thread " + ident.split("_")[-1]
    if ident.startswith("marketing_"):
        return "Post " + ident.split("_")[-1]
    return ident.replace("_", " ").title()


def escaped(value) -> str:
    return html.escape(str(value), quote=True)


def display_run(folder: Path):
    summary = read_json(folder, "run_summary.json", {}) or {}
    pieces = read_json(folder, "pieces.json", []) or []
    if not pieces:
        st.info("No finished posts are available for this run.")
        return
    heading = 'Drafts ready for your review.' if not summary.get('pieces_not_shipped', 0) else 'Some pieces still need attention.'
    st.markdown('<div class="section-tag">THE RESULT</div><div class="section-heading">'+heading+'</div>', unsafe_allow_html=True)
    st.caption("Every approved post has passed the writing rules and AI review. Review the wording before publishing.")

    one, two, three = st.columns(3)
    one.metric("Requested", summary.get("pieces_requested", len(pieces)))
    two.metric("Approved", summary.get("pieces_shipped", 0))
    three.metric("Not approved", summary.get("pieces_not_shipped", 0))
    for warning in summary.get('warnings', []):
        st.warning(warning)

    approved = [piece for piece in pieces if piece.get("status") == "shipped"]
    if approved:
        st.markdown('<hr class="softline">', unsafe_allow_html=True)
        ids = [piece["slot"]["id"] for piece in approved]
        selected_id = st.selectbox("Preview a finished piece", ids, format_func=lambda ident: next(
            (friendly_slot(item["slot"]["id"]) + "  ·  " + (item.get("topic") or item["slot"].get("focus") or "Untitled"))
            for item in approved if item["slot"]["id"] == ident),
            key="piece_" + folder.name,
        )
        piece = next(item for item in approved if item["slot"]["id"] == selected_id)
        for n, post in enumerate(piece.get("draft", {}).get("posts", []), start=1):
            st.markdown(
                f'<div class="post-card"><div class="post-index">{n:02d} / {len(piece["draft"]["posts"]):02d}</div>'
                f'<div class="post-text">{escaped(post.get("text", ""))}</div></div>',
                unsafe_allow_html=True,
            )
        with st.expander("Image description"):
            st.write((piece.get("visual") or {}).get("prompt", "No description available."))
        with st.expander("Sources used"):
            sources = (piece.get("research") or {}).get("sources", [])
            if not sources:
                st.caption("No source details were recorded.")
            for source in sources:
                st.markdown("**" + escaped(source.get("title") or source.get("id") or "Source") + "**")
                st.caption(source.get("url") or source.get("report_url") or source.get("id", ""))
    else:
        st.warning("No posts passed review. Check the feedback and logs below.")

    # The public-facing download is a readable offline HTML handoff, not a flattened CSV.
    # Older runs still work: generate the page in memory from final_content.json.
    if approved:
        from sanad.delivery import render_delivery_html
        delivery_path = folder / "content_delivery.html"
        if delivery_path.is_file():
            delivery_bytes = delivery_path.read_bytes()
        else:
            content = read_json(folder, "final_content.json", {}) or {}
            delivery_bytes = render_delivery_html(content).encode("utf-8")
        st.download_button(
            "↓ Download final content pack",
            delivery_bytes,
            "SanadSquad_Content_Pack.html",
            "text/html",
            use_container_width=True,
            type="primary",
            key="approved_html_" + folder.name,
            help="Readable offline page with every tweet, marketing post, and image description."
        )

    rejected = [piece for piece in pieces if piece.get("status") != "shipped"]
    if rejected:
        with st.expander(f"See reasons for {len(rejected)} unapproved piece(s)"):
            for piece in rejected:
                st.markdown("**" + friendly_slot(piece["slot"]["id"]) + "**")
                reasons = piece.get("reasons", [])
                st.write("; ".join(map(str, reasons)) or "No detailed reason was saved.")
        # Re-run only what failed. Approved pieces are copied as they are, so no tokens are spent on them again. This is
        # the right button after a provider limit ("token limit reached, resets in about N min").
        if st.button("↻  Retry only the unapproved pieces", key="retry_" + folder.name, disabled=running or bool(needed),
                     help="Approved pieces are kept untouched. Only the pieces above run again."):
            try:
                plan_slots = [Slot.model_validate({k: v for k, v in piece["slot"].items() if k in Slot.model_fields}) for piece in pieces]
                st.session_state["job"] = start(Plan(slots=plan_slots), config, reuse=folder)
                st.session_state["expected_slots"] = [slot.model_dump() for slot in plan_slots]
                st.rerun()
            except (ValueError, RuntimeError) as error:
                st.error(str(error))
    with st.expander("Download complete run details"):
        st.caption("Research sources, all drafts, reviewer feedback, usage and run summary.")
        st.download_button("Download full report (ZIP)", logs_zip(folder), "sanad_run_details.zip", "application/zip", use_container_width=True, key="run_zip_" + folder.name)


try:
    config = Config(ROOT)
except Exception as error:
    st.error(f"Project configuration could not be loaded: {type(error).__name__}. See terminal details.")
    st.stop()

job = st.session_state.get("job")
running = bool(job and not job.done)

# A single, calm landing page. No provider dropdown, no secrets, no research policy table.
st.markdown(
    '<div class="brand"><span class="brand-icon">✦</span> SanadSquad <span style="color:#698e84;font-weight:500">/ Studio</span></div>'
    '<div class="hero-tag">AI CONTENT WORKSPACE</div>'
    '<div class="hero-title">Your next week of content, <span>handled.</span></div>'
    '<div class="hero-copy">Choose your topics. Your AI team researches, writes and reviews each piece, then delivers everything in one place.</div>'
    '<div class="flow-hint"><b>01</b> Choose topics <span class="sep">→</span> <b>02</b> Generate <span class="sep">→</span> <b>03</b> Review & download</div>',
    unsafe_allow_html=True,
)

st.markdown('<div class="section-tag">YOUR CONTENT PLAN</div><div class="section-heading">What should we write about?</div>', unsafe_allow_html=True)
st.markdown('<div class="section-description">Six pieces are prepared for the week. Change any topic or keep the suggested ones.</div>', unsafe_allow_html=True)

plan = config.plan.model_copy(deep=True)
left, right = st.columns(2, gap="large")
with left:
    with st.container(border=True):
        st.markdown('<div class="group-head">⌘ Technical threads</div><div class="group-sub">3 researched threads · 5–10 posts each</div>', unsafe_allow_html=True)
        threads = [slot for slot in plan.slots if config.post_type(slot)["source_policy"] == "technical"]
        for slot in threads:
            slot.focus = st.text_input(friendly_slot(slot), value=slot.focus, key="topic_" + slot.id, disabled=running,
                                       help="Leave a technical topic empty for automatic topic selection.")
with right:
    with st.container(border=True):
        st.markdown('<div class="group-head">✦ Marketing posts</div><div class="group-sub">3 sourced posts · company voice</div>', unsafe_allow_html=True)
        marketing = [slot for slot in plan.slots if config.post_type(slot)["source_policy"] == "marketing"]
        for slot in marketing:
            slot.focus = st.text_input(friendly_slot(slot), value=slot.focus, key="topic_" + slot.id, disabled=running)
            st.markdown(f'<div class="service-label">{escaped(slot.service.replace("-", " ").title())}</div>', unsafe_allow_html=True)

st.markdown('<div class="tip">Nothing is published automatically. You choose what to use.</div>', unsafe_allow_html=True)

# Privacy: credentials are configured once in .env, not collected or displayed by the UI.
needed = []
current_options = runtime_options(ROOT / '.env')
if not current_options.get("OPENAI_API_KEY"):
    needed.append("OPENAI_API_KEY")
if marketing and not current_options.get("TAVILY_API_KEY"):
    needed.append("TAVILY_API_KEY")
if needed:
    st.warning("The AI connection needs one-time setup in the local `.env` file. See `README.md` → Quick start. No keys are entered into this page.")
if not HAS_LANGGRAPH:
    st.warning("The AI engine isn't installed yet. Run `pip install -r requirements.txt` and refresh.")
try:
    config.validate_plan(plan)
    validation_error = ""
except ValueError as error:
    validation_error = str(error)
    st.error(validation_error)

if st.button("✦  Generate my weekly content", type="primary", use_container_width=True,
             disabled=running or bool(needed) or bool(validation_error) or not HAS_LANGGRAPH):
    try:
        st.session_state["job"] = start(plan, config)
        st.session_state["expected_slots"] = [slot.model_dump() for slot in plan.slots]
        st.rerun()
    except (ValueError, RuntimeError) as error:
        # Provider setup exceptions here are safe, application-authored strings.
        st.error(str(error))
    except Exception:
        st.error("Could not begin the run. See the terminal for details.")

st.markdown('<hr class="softline">', unsafe_allow_html=True)


@st.fragment(run_every="1s")
def work_and_results():
    current = st.session_state.get("job")
    if current and not current.done:
        st.markdown('<div class="section-tag">IN PROGRESS</div><div class="section-heading">Your AI team is working.</div>', unsafe_allow_html=True)
        latest = {}
        for event in list(current.events):
            if event.get("slot") and event.get("stage"):
                latest[event["slot"]] = event
        stages = {"research": "Finding sources", "write": "Writing", "review": "Checking quality", "visual": "Preparing image idea"}
        complete = 0
        for slot in st.session_state.get("expected_slots", []):
            name = slot["id"]
            event = latest.get(name, {})
            done = event.get("status") in ("shipped", "dropped", "failed") or event.get("event") == "failed"
            complete += bool(done)
            label = ("Approved" if event.get("status") == "shipped" else
                     "Rejected" if event.get("status") == "dropped" else
                     "Failed" if event.get("status") == "failed" or event.get("event") == "failed" else
                     (stages.get(event.get("stage"), "Waiting") if event else "Queued"))
            st.markdown(f'<div class="runline"><span class="runlabel">{escaped(name.replace("_", " ").title())}</span>'
                        f'<span class="runstatus">{escaped(label)}</span></div>', unsafe_allow_html=True)
            if label in ("Rejected", "Failed") and event.get("reasons"):
                st.caption("Why: " + " | ".join(str(r) for r in event["reasons"]))
        st.progress(min(complete / max(1, len(st.session_state.get("expected_slots", []))), 1),
                    text=f"{complete} of {len(st.session_state.get('expected_slots', []))} finished")
        return
    if current and current.done:
        if st.session_state.get("completed_refresh") != current.folder.name:
            st.session_state["completed_refresh"] = current.folder.name
            st.rerun(scope="app")
        if current.error:
            st.error(current.error)
            return
        display_run(current.folder)
        return

    folders = recent_runs()
    if folders:
        display_run(folders[0])
    else:
        st.markdown('<div class="section-tag">READY WHEN YOU ARE</div><div class="section-heading">Your results will appear here.</div>', unsafe_allow_html=True)
        st.caption("Once the team finishes, you'll be able to preview the approved content and download it.")


work_and_results()

# Archived runs are discoverable but don't crowd the everyday workflow.
previous = recent_runs()
if len(previous) > 1:
    with st.expander("Previous content runs"):
        older = st.selectbox("Choose a previous run", previous[1:], format_func=lambda p: p.name)
        display_run(older)
