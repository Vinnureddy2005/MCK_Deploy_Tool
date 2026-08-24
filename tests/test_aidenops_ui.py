"""The AidenOps page: does the markup, the script and the stylesheet agree?

None of this exercises Python. It is here because the three static files are a
single mechanism split across three languages, and nothing else in the suite
looks at them. The page has already been broken once by renaming an element the
script still reached for - the script threw on load, the page came up dead, and
no test noticed. These checks are cheap and they would have caught it.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
HTML = (STATIC / "aidenops.html").read_text(encoding="utf-8")
JS = (STATIC / "js" / "aidenops.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "aidenops.css").read_text(encoding="utf-8")


def test_every_element_the_script_reaches_for_exists():
    wanted = set(re.findall(r'byId\("([^"]+)"\)', JS))
    assert wanted, "no byId() calls found - has the script been rewritten?"

    # Some elements are built by the script rather than sitting in the markup -
    # the stop-following button and the finish button only exist once there is
    # something to stop or finish. Those are looked up to avoid making a second
    # one, so an id the script assigns counts as defined.
    built = set(re.findall(r'\.id = "([^"]+)"', JS))

    missing = sorted(i for i in wanted - built if f'id="{i}"' not in HTML)
    assert not missing, f"the script looks for ids nothing defines: {missing}"


def test_no_duplicate_ids():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate ids - getElementById returns only the first: {dupes}"


def test_every_class_used_is_styled():
    """A class with no rule is either a typo or dead weight; both are worth knowing."""
    used = set()
    for blob in re.findall(r'el\(\s*"[a-z]+"\s*,\s*"([^"]*)"', JS):
        used.update(blob.split())
    for blob in re.findall(r'className\s*=\s*"([^"]*)"', JS):
        used.update(blob.split())
    for blob in re.findall(r'classList\.(?:add|remove|toggle)\(\s*"([^"]+)"', JS):
        used.add(blob)
    for blob in re.findall(r'class="([^"]*)"', HTML):
        used.update(blob.split())
    used.discard("")

    styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", CSS))
    assert not sorted(c for c in used if c not in styled)


def test_the_page_is_isolated_from_the_tx_projects_stylesheet():
    """Its own stylesheet, so a change here cannot reach the Java deployment page."""
    assert "/static/css/aidenops.css" in HTML
    assert "style.css" not in HTML


def test_the_log_console_keeps_frontend_and_backend_apart():
    tabs = re.findall(r'key:\s*"(\w+)"', JS)
    assert tabs == ["frontend", "backend"]

    # Each server log source lands in exactly one of them. nginx is the UI's
    # story, the journal is the backend's; mixing them is what made the old
    # single console unreadable.
    routes = dict(re.findall(r'"([\w-]+)":\s*\{\s*tab:\s*"(\w+)"', JS))
    assert routes == {
        "journal": "backend",
        "nginx-error": "frontend",
        "nginx-access": "frontend",
    }


def test_hidden_survives_the_display_rules():
    """Stages are shown and hidden with the `hidden` attribute.

    Any `display` rule on a card outranks the browser's own `[hidden]` rule, so
    without this the hidden panels would all be visible at once.
    """
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS)


def test_the_layout_is_responsive():
    assert 'name="viewport"' in HTML
    breakpoints = sorted(int(w) for w in re.findall(r"max-width:\s*(\d+)px", CSS))
    assert len(breakpoints) >= 3, f"only {breakpoints} - not a responsive design"

    # Below the widest breakpoint the two columns become one. A sticky console
    # in a single column pins itself over the panel you are reading, so it also
    # has to stop being sticky.
    collapse = re.search(r"@media \(max-width: 1080px\)\s*\{(.+?)\n\}", CSS, re.S)
    assert collapse, "no rule collapsing the two-column layout"
    assert "grid-template-columns: 1fr" in collapse.group(1)
    assert "position: static" in collapse.group(1)


def test_the_page_asks_only_for_the_logs_of_what_it_deployed():
    """The Backend tab must stay quiet after a UI deployment.

    The frontend pipeline never restarts the service or touches the wheel, so
    journal lines arriving here would report a backend deployment that did not
    happen.
    """
    assert 'post("/logs/start", { target: key })' in JS
    assert "startServerLogs(key)" in JS
    # The destination tab follows the half, so the UI half cannot write into the
    # Backend tab at all.
    assert 'var tab = key === "backend" ? "backend" : "frontend";' in JS
    assert 'push("backend"' not in JS.split("async function startServerLogs")[1]


def test_there_is_a_way_out_of_the_last_stage():
    """Deploying is not the end of the job.

    The last stage previously had no exit but the browser's back button: the
    release stayed verified, the logs kept following, and the next release had
    nowhere to start from.
    """
    assert "function offerToFinish" in JS
    assert "offerToFinish();" in JS, "nothing calls it after a deployment"

    finish = JS.split("async function finishUp")[1]
    assert '"/logs/stop"' in finish, "finishing must stop following the logs"
    assert '"/clear"' in finish, "finishing must clear the verified release"
    assert "goto(1)" in finish, "finishing must return to the first stage"


def test_the_page_says_when_logs_are_already_running():
    """The streams belong to the server, not the page.

    They outlive a reload, so lines can arrive with nothing having been clicked.
    Unlabelled, that reads as a deployment starting on its own.
    """
    assert "already following" in JS
    assert '"/logs/stop"' in JS, "and there must be a way to stop them"
    assert 'id="stop-logs"' in JS or '.id = "stop-logs"' in JS
