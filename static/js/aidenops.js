/* AidenOps screen: verify, check, deploy.
 *
 * Separate from app.js on purpose. That page pastes a checksum and replaces one
 * JAR; this one verifies an archive locally and then runs a pipeline that
 * reverts itself. Sharing code would mean branching on "which flow is this" in
 * both directions.
 *
 * Only one stage is on screen at a time, and nothing appears before it is
 * earned - the deploy stage does not exist until an archive has passed both
 * local checks.
 */
(function () {
    "use strict";

    var API = "/api/aidenops";

    var byId = document.getElementById.bind(document);
    var stepper = byId("stepper");
    var panels = { 1: byId("panel-1"), 2: byId("panel-2"), 3: byId("panel-3") };
    var archiveSelect = byId("archive");
    var incomingHint = byId("incoming-hint");
    var checksumInput = byId("checksum");
    var verifyButton = byId("verify");
    var releaseFacts = byId("release-facts");
    var preflightList = byId("preflight");
    var preflightButton = byId("run-preflight");
    var backButton = byId("back-to-1");
    var targetsHost = byId("targets");
    var deployFacts = byId("deploy-facts");
    var errorBox = byId("error");
    var errorMessage = byId("error-message");
    var logBox = byId("log");
    var hostLine = byId("target-host");

    var release = null;

    /* ---------- helpers -------------------------------------------------- */

    /* textContent throughout: filenames, hashes and server output all come from
       outside and must never be parsed as markup. */
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
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

    function showError(message) {
        errorMessage.textContent = message;
        errorBox.hidden = false;
    }

    function clearError() { errorBox.hidden = true; }

    async function call(path, options) {
        var response = await fetch(API + path, options);
        var body = null;
        try { body = await response.json(); } catch (ignored) { /* no body */ }
        if (!response.ok) {
            var detail = body && body.detail;
            /* A confirmation and a failed deployment both carry structure rather
               than just a sentence, so the raw detail travels with the error and
               the caller can render the parts. */
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

    function goto(stage) {
        [1, 2, 3].forEach(function (n) { panels[n].hidden = n !== stage; });
        Array.prototype.forEach.call(stepper.children, function (item) {
            var n = Number(item.dataset.stage);
            item.classList.toggle("is-done", n < stage);
            item.classList.toggle("is-current", n === stage);
        });
    }

    function fact(list, label, value, mono) {
        var row = el("div");
        row.append(el("dt", null, label), el("dd", mono ? "mono" : null, value));
        list.appendChild(row);
    }

    /* ---------- stage 1: verify ------------------------------------------ */

    async function loadArchives() {
        try {
            var data = await call("/archives");
            incomingHint.textContent = "Copy the archive into " + data.incoming_dir;
            archiveSelect.replaceChildren();

            if (!data.archives.length) {
                archiveSelect.appendChild(el("option", null, "no archives found"));
                archiveSelect.disabled = true;
                verifyButton.disabled = true;
                return;
            }
            data.archives.forEach(function (entry) {
                var option = el("option", null, entry.name + "  ·  " + bytes(entry.size));
                option.value = entry.name;
                archiveSelect.appendChild(option);
            });
            archiveSelect.disabled = false;
            verifyButton.disabled = false;
        } catch (err) {
            showError(err.message);
        }
    }

    async function verify() {
        clearError();
        verifyButton.disabled = true;
        var was = verifyButton.textContent;
        verifyButton.textContent = "Verifying…";
        try {
            var body = await post("/verify", {
                archive: archiveSelect.value,
                checksum: checksumInput.value
            });
            release = body.release;
            renderRelease();
            preflightList.replaceChildren();
            goto(2);
        } catch (err) {
            /* The hash gate has no override, so the message is the whole story. */
            showError(err.message);
        } finally {
            verifyButton.disabled = false;
            verifyButton.textContent = was;
        }
    }

    /* ---------- stage 2: what it is, and the server ---------------------- */

    function renderRelease() {
        releaseFacts.replaceChildren();
        fact(releaseFacts, "Archive", release.archive, true);
        fact(releaseFacts, "Verified hash", blocks(release.sha256), true);
        fact(releaseFacts, "Size", bytes(release.size), true);
        if (release.commits.length) {
            fact(releaseFacts, "Commits", release.commits.join(" + "), true);
        }
        if (release.built_by) { fact(releaseFacts, "Built by", release.built_by); }

        var parts = [];
        if (release.contents.has_backend) { parts.push("backend wheel"); }
        if (release.contents.has_ui) { parts.push("UI bundle"); }
        fact(releaseFacts, "Contains", parts.join(" + ") || "nothing deployable");
        fact(releaseFacts, "Files verified", release.members.length + " against SHA256SUMS.txt");
    }

    async function runPreflight() {
        clearError();
        preflightButton.disabled = true;
        var was = preflightButton.textContent;
        preflightButton.textContent = "Checking…";
        try {
            var body = await post("/preflight");
            preflightList.replaceChildren();
            body.checks.forEach(function (check) {
                var item = el("li", "check " + (check.ok ? "check-ok" : "check-bad"));
                item.append(
                    el("span", "check-name", check.name),
                    el("span", "check-detail", check.detail)
                );
                /* Warnings are shown, not suppressed: a placeholder in an
                   integration nobody uses is worth seeing without blocking. */
                (check.warn || []).forEach(function (path) {
                    item.appendChild(el("span", "check-warn", "placeholder: " + path));
                });
                preflightList.appendChild(item);
            });
            renderTargets(body.ok);
            goto(3);
        } catch (err) {
            showError(err.message);
        } finally {
            preflightButton.disabled = false;
            preflightButton.textContent = was;
        }
    }

    /* ---------- stage 3: deploy ------------------------------------------ */

    function renderTargets(preflightOk) {
        targetsHost.replaceChildren();

        if (release.contents.has_ui) {
            targetsHost.appendChild(target(
                "ui", "UI bundle", release.contents.ui, preflightOk, null));
        }
        if (release.contents.has_backend) {
            targetsHost.appendChild(target(
                "backend", "Backend wheel", release.contents.wheel, preflightOk, null));
        }
        if (!release.contents.has_ui && !release.contents.has_backend) {
            targetsHost.appendChild(el("p", "hint", "This release contains nothing deployable."));
        }
    }

    function target(key, label, filename, preflightOk, unavailable) {
        var card = el("div", "target");
        card.append(el("p", "target-label", label));
        card.append(el("p", "mono target-file", filename || "—"));

        if (unavailable) {
            card.appendChild(el("p", "notice-inline", unavailable));
            return card;
        }

        var button = el("button", "btn btn-primary", "Deploy the " + label);
        button.type = "button";
        button.disabled = !preflightOk;
        if (!preflightOk) {
            card.appendChild(el("p", "notice-inline",
                "Fix the failing server checks first."));
        }
        button.addEventListener("click", function () { deploy(key, button); });
        card.appendChild(button);
        return card;
    }

    async function deploy(targetKey, button, confirmed) {
        clearError();
        button.disabled = true;
        var was = button.textContent;
        button.textContent = "Deploying…";
        try {
            var body = await post("/deploy", { target: targetKey, confirmed: !!confirmed });
            renderOutcome(targetKey, body.result);
        } catch (err) {
            if (err.status === 409 && err.detail && err.detail.needs_confirmation) {
                askToConfirm(targetKey, err.detail, button);
            } else if (err.detail && err.detail.runbook) {
                renderFailure(err.detail);
            } else {
                showError(err.message);
            }
        } finally {
            button.disabled = false;
            button.textContent = was;
        }
    }

    function renderOutcome(targetKey, result) {
        deployFacts.replaceChildren();
        if (targetKey === "ui") {
            fact(deployFacts, "Deployed", result.tarball, true);
            fact(deployFacts, "HTTP", result.checks.http_status, true);
            fact(deployFacts, "API_URL", result.checks.api_url, true);
            fact(deployFacts, "Previous bundle", result.previous, true);
        } else {
            fact(deployFacts, "Installed", result.wheel, true);
            fact(deployFacts, "Replaced version", result.previous_version || "unknown", true);
            fact(deployFacts, "Migrations applied", String(result.migrations.count), true);
            fact(deployFacts, "Dump",
                 result.dump ? result.dump.path : "none taken \u2014 nothing was pending", true);
            fact(deployFacts, "Health",
                 result.health.status + " after " + result.health.waited + "s", true);
        }
        deployFacts.hidden = false;
    }

    /* A destructive migration or a dependency change cannot be undone by
       reinstalling the previous wheel, so the operator sees exactly what would
       change and is asked once - not per reason. */
    function askToConfirm(targetKey, detail, button) {
        var box = el("div", "confirm");
        box.append(el("p", "confirm-title", detail.reason));

        var migrations = (detail.migrations && detail.migrations.migrations) || [];
        if (migrations.length) {
            box.appendChild(el("p", "confirm-head",
                migrations.length + " migration(s) will apply when the service starts:"));
            var list = el("ul", "confirm-list");
            migrations.forEach(function (revision) {
                var item = el("li", null, revision.revision + "  " + revision.slug);
                if (revision.destructive && revision.destructive.length) {
                    item.appendChild(el("span", "confirm-bad",
                        "  DESTRUCTIVE: " + revision.destructive.join(", ")));
                }
                list.appendChild(item);
            });
            box.appendChild(list);
        }

        var changes = (detail.dependencies && detail.dependencies.changes) || [];
        if (changes.length) {
            box.appendChild(el("p", "confirm-head", "Dependency changes:"));
            var deps = el("ul", "confirm-list");
            changes.forEach(function (change) {
                deps.appendChild(el("li", null,
                    change.change + ": " + change.package + " " +
                    (change.from ? change.from + " -> " : "") + (change.to || "")));
            });
            box.appendChild(deps);
        }

        var go = el("button", "btn btn-primary", "I understand \u2014 deploy anyway");
        go.type = "button";
        go.addEventListener("click", function () {
            box.remove();
            deploy(targetKey, button, true);
        });
        box.appendChild(go);

        deployFacts.hidden = true;
        targetsHost.appendChild(box);
    }

    /* Past the start, recovery needs the database as well as the wheel - so the
       tool hands over the exact sequence rather than running any of it. */
    function renderFailure(detail) {
        showError(detail.message + (detail.stage ? "  (stage: " + detail.stage + ")" : ""));
        if (!detail.runbook || !detail.runbook.length) { return; }

        var box = el("div", "runbook");
        box.append(el("p", "runbook-title",
            detail.past_the_line
                ? "The service was started, so the schema may have changed. " +
                  "Recovery is yours to decide - these are the exact steps:"
                : "Recovery steps:"));
        box.appendChild(el("pre", "runbook-body", detail.runbook.join("\n")));
        targetsHost.appendChild(box);
    }

    /* ---------- the shared log socket ------------------------------------ */

    function connectLog() {
        var badge = byId("socket-badge");
        var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/logs";
        var socket = new WebSocket(url);

        socket.addEventListener("open", function () {
            badge.textContent = "live";
            badge.classList.add("badge-conn-on");
        });
        socket.addEventListener("close", function () {
            badge.textContent = "disconnected";
            badge.classList.remove("badge-conn-on");
            /* The log is context, not the flow - a dropped socket must not
               interrupt a deployment that is already running server-side. */
            setTimeout(connectLog, 3000);
        });
        socket.addEventListener("message", function (event) {
            var payload;
            try { payload = JSON.parse(event.data); } catch (ignored) { return; }
            if (!payload.message) { return; }
            logBox.textContent += (logBox.textContent ? "\n" : "") + payload.message;
            logBox.scrollTop = logBox.scrollHeight;
        });
    }

    /* ---------- boot ----------------------------------------------------- */

    verifyButton.addEventListener("click", verify);
    preflightButton.addEventListener("click", runPreflight);
    backButton.addEventListener("click", function () {
        clearError();
        goto(1);
    });

    (async function boot() {
        goto(1);
        try {
            var body = await call("/status");
            hostLine.textContent = body.server + "  ·  " + body.paths.ops_dir;
            byId("mode-badge").hidden = !body.dry_run;
            byId("live-badge").hidden = body.dry_run;

            /* A verified release survives a page reload, so returning to the tab
               should not mean verifying the same archive again. */
            if (body.release) {
                release = body.release;
                renderRelease();
                goto(2);
            }
        } catch (err) {
            showError(err.message);
        }
        await loadArchives();
        connectLog();
    })();
})();
