/* AidenOps deployment console.
 *
 * Separate from app.js. That page pastes a checksum and replaces one JAR; this
 * one verifies an archive locally and then runs two pipelines with opposite
 * failure behaviour. Sharing code would mean branching on "which flow is this"
 * in both directions.
 *
 * The console is the part worth explaining. Everything used to land in one
 * stream: the tool's own progress, the backend journal, and both nginx logs,
 * interleaved. During a deployment that is the screen you are staring at, and
 * mixing four sources makes the one line that matters hardest to find.
 *
 * Two tabs now, one per half. The server tags each line - journal goes to the
 * backend, nginx to the frontend, since that is what each one is about - and the
 * tool's own messages go to whichever half is being deployed.
 *
 * Shared work, verification and staging, is echoed into both. That is
 * deliberate duplication: it means either tab read on its own tells the whole
 * story, including the steps that came before the deployment.
 */
(function () {
    "use strict";

    var API = "/api/aidenops";

    var byId = document.getElementById.bind(document);
    var rail = byId("rail");
    var panels = { 1: byId("panel-1"), 2: byId("panel-2"), 3: byId("panel-3") };
    var fileLine = byId("file");
    var codeInput = byId("code");
    var verifyButton = byId("verify");
    var checkButton = byId("check");
    var releaseFacts = byId("release-facts");
    var checksList = byId("checks");
    var targetsHost = byId("targets");
    var outcome = byId("outcome");
    var aftermath = byId("aftermath");
    var whereLine = byId("where");
    var whereHint = byId("where-hint");
    var errorCard = byId("error-card");
    var errorText = byId("error-text");
    var tabBar = byId("console-tabs");
    var consoleBody = byId("console-body");

    var release = null;
    var failedChecks = [];
    /* Which half is being deployed, so the tool's own lines land in the right
       tab rather than all in one place. */
    var deploying = null;

    /* ---------- the console --------------------------------------------- */

    var TABS = [
        { key: "frontend", label: "Frontend",
          empty: "The UI deployment and the nginx logs appear here." },
        { key: "backend", label: "Backend",
          empty: "The backend deployment and its journal appear here." }
    ];

    /* Where each server-tagged source belongs. nginx goes with the frontend
       because that is what it serves; the journal with the backend because that
       is what writes it. */
    var ROUTES = {
        "journal": { tab: "backend", tag: "journal" },
        "nginx-error": { tab: "frontend", tag: "nginx", bad: true },
        "nginx-access": { tab: "frontend", tag: "access" }
    };

    var lines = { frontend: [], backend: [] };
    var unread = { frontend: 0, backend: 0 };
    var hasBad = { frontend: false, backend: false };
    var active = "frontend";

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
    }

    function stamp() {
        var d = new Date();
        return String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0") + ":" +
               String(d.getSeconds()).padStart(2, "0");
    }

    /* journalctl and nginx do not tag severity, so it is read from the text. An
       operator scanning a wall of lines needs the failures to surface. */
    function severity(text, forcedBad) {
        var lower = String(text).toLowerCase();
        if (forcedBad ||
            /\b(error|failed|failure|denied|refus|cannot|traceback|critical)\b/.test(lower)) {
            return "is-bad";
        }
        if (/\b(warn|note|skipping|deprecat)\b/.test(lower)) { return "is-warn"; }
        if (/^===|^---/.test(text) ||
            /^\s*(deploying|uploading|extracting|swapping|installing|verifying|starting|stopping|dumping|pruning|copying|unpacking|archiving|setting|writing|restoring|waiting|downloading|running)/i.test(text)) {
            return "is-step";
        }
        return "";
    }

    function push(tabKey, text, tag, forcedBad) {
        var level = severity(text, forcedBad);
        lines[tabKey].push({ at: stamp(), text: String(text), tag: tag || "", level: level });
        /* Bounded: a journal tail can run a long time and the browser should not
           be the thing that gives out. */
        if (lines[tabKey].length > 2000) { lines[tabKey].shift(); }

        if (level === "is-bad") { hasBad[tabKey] = true; }
        if (tabKey === active) { renderConsole(); } else { unread[tabKey] += 1; }
        renderTabs();
    }

    /* Shared steps belong to both stories. */
    function pushBoth(text, tag) {
        TABS.forEach(function (tab) { push(tab.key, text, tag); });
    }

    function renderTabs() {
        tabBar.replaceChildren();
        TABS.forEach(function (tab) {
            var button = el("button", "console__tab" + (tab.key === active ? " is-on" : ""));
            button.type = "button";
            button.setAttribute("role", "tab");
            button.append(el("span", null, tab.label));

            var count = unread[tab.key] || lines[tab.key].length;
            if (count) {
                button.appendChild(el("span", "console__count", count));
                if (tab.key !== active && unread[tab.key]) {
                    button.classList.add(hasBad[tab.key] ? "has-bad" : "has-new");
                }
            }
            button.addEventListener("click", function () {
                active = tab.key;
                unread[tab.key] = 0;
                renderTabs();
                renderConsole();
            });
            tabBar.appendChild(button);
        });

        tabBar.appendChild(el("span", "console__spacer"));
        var clear = el("button", "console__clear", "Clear");
        clear.type = "button";
        clear.addEventListener("click", function () {
            lines[active] = [];
            hasBad[active] = false;
            renderTabs();
            renderConsole();
        });
        tabBar.appendChild(clear);
    }

    function renderConsole() {
        var stuck = consoleBody.scrollTop + consoleBody.clientHeight >=
                    consoleBody.scrollHeight - 24;
        consoleBody.replaceChildren();

        if (!lines[active].length) {
            var tab = TABS.filter(function (t) { return t.key === active; })[0];
            consoleBody.appendChild(el("div", "console__empty", tab.empty));
            return;
        }

        lines[active].forEach(function (entry) {
            var row = el("div", "line " + entry.level);
            row.appendChild(el("span", "line__at", entry.at));
            var text = el("span", "line__text");
            if (entry.tag) { text.appendChild(el("span", "line__tag", entry.tag)); }
            text.appendChild(document.createTextNode(entry.text));
            row.appendChild(text);
            consoleBody.appendChild(row);
        });

        /* Follow only if you were already at the bottom - yanking the view away
           while someone reads further up is worse than not following. */
        if (stuck) { consoleBody.scrollTop = consoleBody.scrollHeight; }
    }

    /* ---------- errors --------------------------------------------------- */

    function showError(message) {
        errorText.textContent = message;
        errorCard.hidden = false;
        push(deploying || active, message, "", true);
    }

    function clearError() { errorCard.hidden = true; }

    async function call(path, options) {
        var response = await fetch(API + path, options);
        var body = null;
        try { body = await response.json(); } catch (ignored) { /* no body */ }
        if (!response.ok) {
            var detail = body && body.detail;
            var failure = new Error(
                (typeof detail === "string" && detail) ||
                (detail && detail.message) ||
                ("Request failed (" + response.status + ")")
            );
            failure.status = response.status;
            failure.detail = detail;
            throw failure;
        }
        return body;
    }

    function post(path, payload) {
        return call(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {})
        });
    }

    /* ---------- the stage rail ------------------------------------------ */

    function goto(stage) {
        [1, 2, 3].forEach(function (n) { panels[n].hidden = n !== stage; });
        Array.prototype.forEach.call(rail.children, function (step) {
            var n = Number(step.dataset.stage);
            step.classList.toggle("is-done", n < stage);
            step.classList.toggle("is-now", n === stage);
        });
    }

    function bytes(size) {
        if (size === null || size === undefined) { return "—"; }
        if (size < 1024) { return size + " B"; }
        var units = ["KB", "MB", "GB"], value = size / 1024, i = 0;
        while (value >= 1024 && i < units.length - 1) { value = value / 1024; i += 1; }
        return value.toFixed(value < 10 ? 1 : 0) + " " + units[i];
    }

    function blocks(hash) {
        return hash ? String(hash).replace(/(.{8})/g, "$1 ").trim() : "—";
    }

    function when(epochSeconds) {
        if (!epochSeconds) { return "unknown"; }
        return new Date(epochSeconds * 1000).toLocaleString([], {
            day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"
        });
    }

    function fact(list, label, value, mono) {
        var row = el("div");
        row.append(el("dt", null, label), el("dd", mono ? "mono" : null, value));
        list.appendChild(row);
    }

    /* ---------- stage 1 -------------------------------------------------- */

    async function loadFile() {
        try {
            var data = await call("/bundle");
            whereHint.textContent = "Downloaded into " + data.incoming_dir;

            if (!data.present) {
                fileLine.className = "file is-missing";
                fileLine.textContent = data.name + " is not here yet";
                return;
            }
            fileLine.className = "file";
            fileLine.replaceChildren(
                el("span", null, data.name),
                el("span", "file__meta", bytes(data.size) + "  ·  " + when(data.modified))
            );
        } catch (err) {
            showError(err.message);
        }
    }

    /* One action, two steps. The download is best effort: if the hub is
       unreachable but the bundle was copied in by hand, that is a working path.
       Either way the pasted code decides, so a stale local file cannot slip
       through - it simply fails to match. */
    async function verify() {
        clearError();
        verifyButton.disabled = true;
        var was = verifyButton.textContent;
        var note = "";

        try {
            verifyButton.textContent = "Downloading…";
            pushBoth("Downloading the bundle from the hub");
            var fetched = await post("/fetch");
            if (fetched.simulated) {
                note = "Dry run: the download was simulated, so whatever is already " +
                       "in the incoming folder is what was verified.";
                pushBoth(note);
            } else {
                pushBoth("Downloaded " + bytes(fetched.size));
            }
        } catch (err) {
            note = "Could not download from the hub (" + err.message +
                   "). Verified the file already in the incoming folder.";
            pushBoth(note);
        }

        try {
            verifyButton.textContent = "Verifying…";
            await loadFile();
            var body = await post("/verify", { checksum: codeInput.value });
            release = body.release;
            pushBoth("Verified " + release.archive + " — " + release.members.length +
                     " files match SHA256SUMS.txt");
            renderRelease();
            checksList.replaceChildren(el("li", "hint", "Not run yet."));
            if (note) { showError(note); }
            goto(2);
        } catch (err) {
            showError(note ? note + "  " + err.message : err.message);
        } finally {
            verifyButton.disabled = false;
            verifyButton.textContent = was;
        }
    }

    /* ---------- stage 2 -------------------------------------------------- */

    function renderRelease() {
        releaseFacts.replaceChildren();
        fact(releaseFacts, "Bundle", release.archive, true);
        fact(releaseFacts, "Verified hash", blocks(release.sha256), true);
        fact(releaseFacts, "Size", bytes(release.size), true);
        if (release.commits.length) {
            fact(releaseFacts, "Commits", release.commits.join("  +  "), true);
        }
        if (release.built_by) { fact(releaseFacts, "Built by", release.built_by); }

        var parts = [];
        if (release.contents.has_backend) { parts.push("backend wheel"); }
        if (release.contents.has_ui) { parts.push("UI bundle"); }
        fact(releaseFacts, "Contains", parts.join("  +  ") || "nothing deployable");
    }

    async function runChecks() {
        clearError();
        checkButton.disabled = true;
        var was = checkButton.textContent;
        checkButton.textContent = "Checking…";
        try {
            pushBoth("Running the server checks");
            var body = await post("/preflight");
            checksList.replaceChildren();

            body.checks.forEach(function (check) {
                var cls = check.info ? "check is-info"
                                     : (check.ok ? "check" : "check is-bad");
                var row = el("li", cls);
                row.append(
                    el("span", "check__dot"),
                    el("span", "check__name", check.name),
                    el("span", "check__detail", check.detail)
                );
                /* Warnings are shown, never suppressed: a placeholder in an
                   integration nobody uses is worth seeing without blocking. */
                (check.warn || []).forEach(function (path) {
                    row.appendChild(el("span", "check__warn", "placeholder: " + path));
                });
                checksList.appendChild(row);
                pushBoth(check.name + ": " + check.detail);
            });

            failedChecks = body.checks.filter(function (c) { return !c.ok; })
                                      .map(function (c) { return c.name; });
            renderTargets(body.ok);
            goto(3);
        } catch (err) {
            showError(err.message);
        } finally {
            checkButton.disabled = false;
            checkButton.textContent = was;
        }
    }

    /* ---------- stage 3 -------------------------------------------------- */

    function renderTargets(checksPassed) {
        targetsHost.replaceChildren();
        outcome.hidden = true;
        aftermath.replaceChildren();

        if (release.contents.has_ui) {
            targetsHost.appendChild(
                target("ui", "UI bundle", "reverts itself", release.contents.ui, checksPassed));
        }
        if (release.contents.has_backend) {
            targetsHost.appendChild(
                target("backend", "Backend wheel", "migrations run on start",
                       release.contents.wheel, checksPassed));
        }
        if (!targetsHost.children.length) {
            targetsHost.appendChild(
                el("p", "hint", "This release contains nothing deployable."));
        }
    }

    function target(key, label, chip, filename, checksPassed) {
        var card = el("div", "target");
        var head = el("div", "target__head");
        head.append(el("p", "target__name", label), el("span", "chip", chip));
        card.appendChild(head);
        card.appendChild(el("p", "target__file", filename || "—"));

        if (!checksPassed) {
            /* Naming them matters: the checks are on the previous stage, and
               "fix the failing checks" without saying which sends you looking
               with nothing to go on. */
            card.appendChild(el("p", "note note--warn",
                failedChecks.length
                    ? "Fix these server checks first: " + failedChecks.join(", ")
                    : "Fix the failing server checks first."));
            return card;
        }
        card.appendChild(deployButton(key, label));
        return card;
    }

    function deployButton(key, label) {
        var button = el("button", "btn btn--go", "Deploy the " + label.toLowerCase());
        button.type = "button";
        button.addEventListener("click", function () { deploy(key, button, label); });
        return button;
    }

    async function deploy(key, button, label, confirmed) {
        clearError();
        deploying = key === "ui" ? "frontend" : "backend";
        active = deploying;
        unread[active] = 0;

        button.disabled = true;
        var was = button.textContent;
        button.textContent = "Deploying…";
        try {
            push(deploying, "=== deploying the " + label.toLowerCase() + " ===");
            var body = await post("/deploy", { target: key, confirmed: !!confirmed });
            renderOutcome(key, body.result);
            markDone(button, key, label);
            await startServerLogs(key);
        } catch (err) {
            if (err.status === 409 && err.detail && err.detail.needs_confirmation) {
                askToConfirm(key, label, err.detail, button);
            } else if (err.detail && err.detail.runbook) {
                renderFailure(err.detail);
            } else {
                showError(err.message);
            }
            /* Restored only on failure. Putting "Deploy" back after a success
               invites a second one and says nothing about what happened. */
            button.disabled = false;
            button.textContent = was;
        } finally {
            deploying = null;
        }
    }

    function renderOutcome(key, result) {
        outcome.replaceChildren();
        if (key === "ui") {
            fact(outcome, "Deployed", result.tarball, true);
            fact(outcome, "HTTP", result.checks.http_status, true);
            fact(outcome, "API_URL", result.checks.api_url, true);
            fact(outcome, "Previous bundle", result.previous, true);
        } else {
            fact(outcome, "Installed", result.wheel, true);
            fact(outcome, "Replaced version", result.previous_version || "unknown", true);
            fact(outcome, "Migrations applied", String(result.migrations.count), true);
            fact(outcome, "Dump", result.dump ? result.dump.path
                                              : "none — nothing was pending", true);
            fact(outcome, "Health",
                 result.health.status + " after " + result.health.waited + "s", true);
        }
        outcome.hidden = false;
    }

    function markDone(button, key, label) {
        var done = el("p", "done", "Deployed ✓");
        button.replaceWith(done);
        /* A redeploy is a legitimate thing to want - the same bundle over a
           broken one - so it stays reachable, just not as the default. */
        var again = el("button", "btn btn--quiet", "Deploy again");
        again.type = "button";
        again.addEventListener("click", function () {
            again.replaceWith(deployButton(key, label));
        });
        done.after(again);
    }

    /* A destructive migration or a dependency change cannot be undone by
       reinstalling the previous wheel, so the operator sees exactly what would
       change and is asked once - not once per reason. */
    function askToConfirm(key, label, detail, button) {
        var box = el("div", "note note--warn");
        box.appendChild(el("p", "note__title", detail.reason));

        var migrations = (detail.migrations && detail.migrations.migrations) || [];
        if (migrations.length) {
            box.appendChild(el("p", null,
                migrations.length + " migration(s) will apply when the service starts:"));
            var list = el("ul");
            migrations.forEach(function (revision) {
                var item = el("li", null, revision.revision + "  " + revision.slug);
                if (revision.destructive && revision.destructive.length) {
                    item.appendChild(el("strong", null,
                        "  DESTRUCTIVE: " + revision.destructive.join(", ")));
                }
                list.appendChild(item);
            });
            box.appendChild(list);
        }

        var changes = (detail.dependencies && detail.dependencies.changes) || [];
        if (changes.length) {
            box.appendChild(el("p", null, "Dependency changes:"));
            var deps = el("ul");
            changes.forEach(function (change) {
                deps.appendChild(el("li", null,
                    change.change + ": " + change.package + " " +
                    (change.from ? change.from + " → " : "") + (change.to || "")));
            });
            box.appendChild(deps);
        }

        var go = el("button", "btn btn--go", "I understand — deploy anyway");
        go.type = "button";
        go.addEventListener("click", function () {
            box.remove();
            deploy(key, button, label, true);
        });
        box.appendChild(go);
        aftermath.replaceChildren(box);
    }

    /* Past the start, recovery needs the database as well as the wheel - so the
       tool hands over the exact sequence rather than running any of it. */
    function renderFailure(detail) {
        showError(detail.message + (detail.stage ? "  (stage: " + detail.stage + ")" : ""));
        if (!detail.runbook || !detail.runbook.length) { return; }

        var box = el("div", "note note--bad");
        box.appendChild(el("p", "note__title",
            detail.past_the_line
                ? "The service was started, so the schema may have changed. " +
                  "Recovery is yours to decide — these are the exact steps:"
                : "Recovery steps:"));
        aftermath.replaceChildren(box);
        aftermath.appendChild(el("pre", "runbook", detail.runbook.join("\n")));
    }

    /* Only the logs belonging to what was just deployed. A UI deployment never
       restarts the service or touches the wheel, so following its journal here
       would put lines in the Backend tab for work that did not happen - which
       reads as though the backend had been deployed too. */
    async function startServerLogs(key) {
        var tab = key === "backend" ? "backend" : "frontend";
        try {
            var body = await post("/logs/start", { target: key });
            push(tab, body.journal
                ? "=== following " + body.unit + " ==="
                : "=== following the nginx logs ===", body.journal ? "journal" : "nginx");
        } catch (err) {
            /* Not a deployment failure: the deployment succeeded, only the
               stream did not start. */
            push(tab, "could not start the server logs: " + err.message);
        }
    }

    /* ---------- the socket ---------------------------------------------- */

    function connect() {
        var pill = byId("pill-socket");
        var url = (location.protocol === "https:" ? "wss://" : "ws://") +
                  location.host + "/ws/logs";
        var socket = new WebSocket(url);

        socket.addEventListener("open", function () {
            pill.textContent = "live";
            pill.classList.add("is-on");
        });
        socket.addEventListener("close", function () {
            pill.textContent = "reconnecting";
            pill.classList.remove("is-on");
            /* The console is context, not the flow: a dropped socket must not
               interrupt a deployment already running server-side. */
            setTimeout(connect, 3000);
        });
        socket.addEventListener("message", function (event) {
            var payload;
            try { payload = JSON.parse(event.data); } catch (ignored) { return; }
            if (!payload || !payload.message) { return; }

            var route = ROUTES[payload.type];
            if (route) {
                push(route.tab, payload.message, route.tag, route.bad);
            } else if (deploying) {
                push(deploying, payload.message);
            } else {
                /* Shared work before a deployment belongs to both stories. */
                pushBoth(payload.message);
            }
        });
    }

    /* ---------- boot ----------------------------------------------------- */

    verifyButton.addEventListener("click", verify);
    checkButton.addEventListener("click", runChecks);
    byId("back-1").addEventListener("click", function () { clearError(); goto(1); });
    byId("back-2").addEventListener("click", function () { clearError(); goto(2); });
    byId("error-dismiss").addEventListener("click", clearError);

    (async function boot() {
        goto(1);
        renderTabs();
        renderConsole();
        try {
            var body = await call("/status");
            whereLine.textContent = body.server + "  ·  " + body.paths.ops_dir;
            byId("pill-dry").hidden = !body.dry_run;
            byId("pill-live").hidden = body.dry_run;

            /* A verified release survives a reload, so returning to the tab
               should not mean verifying the same bundle again. */
            if (body.release) {
                release = body.release;
                renderRelease();
                pushBoth("Already verified: " + release.archive);
                goto(2);
            }
        } catch (err) {
            showError(err.message);
        }
        await loadFile();
        connect();
    })();
})();
