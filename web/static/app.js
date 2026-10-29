/**
 * AI Trade Assistant — Frontend Logic
 *
 * Handles: drag-and-drop upload, screenshot analysis via API,
 * trade confirmation/rejection, settings management, and MT5 status polling.
 */

// ── DOM Elements ──

const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const previewContainer = document.getElementById("previewContainer");
const previewImage = document.getElementById("previewImage");
const loader = document.getElementById("loader");
const tradeCard = document.getElementById("tradeCard");
const resultBox = document.getElementById("resultBox");
resultBox.style.whiteSpace = "pre-line"; // show multi-line messages
const settingsToggle = document.getElementById("settingsToggle");
const settingsPanel = document.getElementById("settingsPanel");
const btnSaveSettings = document.getElementById("btnSaveSettings");
const btnExecute = document.getElementById("btnExecute");
const btnReject = document.getElementById("btnReject");

// Current analysis data (held between analyze and execute)
let currentAnalysis = null;

// ── Settings Toggle ──

settingsToggle.addEventListener("click", () => {
    const arrow = settingsToggle.querySelector(".toggle-arrow");
    const isOpen = settingsPanel.style.display !== "none";
    settingsPanel.style.display = isOpen ? "none" : "grid";
    arrow.classList.toggle("open", !isOpen);
});

// ── Save Settings ──

btnSaveSettings.addEventListener("click", async () => {
    const settings = {
        risk: {
            risk_percent: parseFloat(document.getElementById("riskPct").value),
            target_rr: parseFloat(document.getElementById("targetRR").value),
            min_rr: parseFloat(document.getElementById("minRR").value),
            max_lot_size: parseFloat(document.getElementById("maxLots").value),
        },
        spread: {
            max_spread_pips: parseFloat(
                document.getElementById("maxSpread").value
            ),
        },
    };

    try {
        const res = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(settings),
        });
        const data = await res.json();

        if (data.status) {
            btnSaveSettings.textContent = "✅ Saved!";
            setTimeout(() => {
                btnSaveSettings.textContent = "💾 Save Settings";
            }, 2000);
        }
    } catch (err) {
        console.error("Settings save failed:", err);
    }
});

// ── Load Settings on Page Load ──

async function loadSettings() {
    try {
        const res = await fetch("/api/settings");
        const config = await res.json();

        if (config.risk) {
            document.getElementById("riskPct").value =
                config.risk.risk_percent || 1.0;
            document.getElementById("targetRR").value =
                config.risk.target_rr || 3.0;
            document.getElementById("minRR").value =
                config.risk.min_rr || 1.5;
            document.getElementById("maxLots").value =
                config.risk.max_lot_size || 10.0;
        }
        if (config.spread) {
            document.getElementById("maxSpread").value =
                config.spread.max_spread_pips || 5.0;
        }
    } catch (err) {
        console.error("Failed to load settings:", err);
    }
}

// ── Journal Status (Notion + local CSV) ──

const journalNotion = document.getElementById("journalNotion");
const journalCsv = document.getElementById("journalCsv");

function setJournalItem(el, ok, nodes) {
    el.querySelector(".status-dot").className =
        "status-dot " + (ok ? "online" : "offline");
    const text = el.querySelector(".journal-text");
    text.replaceChildren(...nodes);
}

function link(href, label, newTab = true) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = label;
    if (newTab) {
        a.target = "_blank";
        a.rel = "noopener";
    }
    return a;
}

async function loadJournalStatus(refresh = false) {
    try {
        const res = await fetch(
            "/api/journal/status" + (refresh ? "?refresh=1" : "")
        );
        const data = await res.json();

        // Notion
        const n = data.notion;
        if (n.connected) {
            setJournalItem(journalNotion, true, [
                link(n.url, `${n.title} ↗`),
            ]);
        } else {
            const span = document.createElement("span");
            span.className = "journal-error";
            span.textContent = n.enabled ? "not connected" : "off";
            span.title = n.error || "";
            setJournalItem(journalNotion, false, [span]);
        }

        // Local CSV
        const c = data.csv;
        const summary = c.exists
            ? `${c.entries} entries · ${c.executed} trades · ${c.open} open · `
            : "no entries yet · ";
        const nodes = [document.createTextNode(summary),
                       link("/journal", "View")];
        if (c.exists) {
            nodes.push(document.createTextNode(" · "));
            nodes.push(link("/api/journal/csv", "Download", false));
        }
        setJournalItem(journalCsv, true, nodes);
        journalCsv.title = c.path;
    } catch (err) {
        console.error("Journal status failed:", err);
    }
}

// ── MT5 Status Check ──

async function checkMT5Status() {
    const statusEl = document.getElementById("mt5Status");
    const dot = statusEl.querySelector(".status-dot");
    const text = statusEl.querySelector(".status-text");
    const accountInfo = document.getElementById("accountInfo");
    const balanceEl = document.getElementById("balance");

    try {
        const res = await fetch("/api/mt5/status");
        const data = await res.json();

        if (data.connected) {
            dot.className = "status-dot online";
            text.textContent = "MT5 Connected";
            accountInfo.style.display = "block";
            balanceEl.textContent = `${data.currency} ${data.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

            // Trades that closed since the last check (P&L synced to logs)
            if (data.closed_trades && data.closed_trades.length) {
                resultBox.className = "result success";
                resultBox.textContent = "📕 " + data.closed_trades.join("\n📕 ");
                resultBox.style.display = "block";
                loadJournalStatus();
            }
        } else {
            dot.className = "status-dot offline";
            text.textContent = "MT5 Offline";
            accountInfo.style.display = "none";
        }
    } catch {
        dot.className = "status-dot offline";
        text.textContent = "MT5 Offline";
        accountInfo.style.display = "none";
    }
}

// ── Drag & Drop ──

dropZone.addEventListener("click", () => fileInput.click());

dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
});

dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("dragover");
});

dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
        analyzeFile(e.dataTransfer.files[0]);
    }
});

fileInput.addEventListener("change", (e) => {
    if (e.target.files.length) {
        analyzeFile(e.target.files[0]);
    }
});

// ── Analyze Screenshot ──

async function analyzeFile(file) {
    // Show preview
    const reader = new FileReader();
    reader.onload = (e) => {
        previewImage.src = e.target.result;
        previewContainer.style.display = "block";
    };
    reader.readAsDataURL(file);

    // Show loader, hide previous results
    loader.style.display = "block";
    tradeCard.style.display = "none";
    resultBox.style.display = "none";

    const formData = new FormData();
    formData.append("screenshot", file);

    try {
        const res = await fetch("/api/analyze", {
            method: "POST",
            body: formData,
        });
        const data = await res.json();

        if (data.error) {
            showError(data.error);
            loadJournalStatus();
            return;
        }

        currentAnalysis = data;
        displayTrade(data);
        loadJournalStatus();
    } catch (err) {
        showError("Analysis failed: " + err.message);
    } finally {
        loader.style.display = "none";
    }
}

// ── Display Trade Card ──

function displayTrade(data) {
    const t = data.adjusted;
    const e = data.extracted;

    // Pair & direction
    document.getElementById("pairBadge").textContent = t.symbol;
    document.getElementById("orderTypeBadge").textContent = t.order_type;

    const dirBadge = document.getElementById("directionBadge");
    if (t.direction === "BUY") {
        dirBadge.textContent = "▲ BUY";
        dirBadge.className = "direction-buy";
    } else {
        dirBadge.textContent = "▼ SELL";
        dirBadge.className = "direction-sell";
    }

    // Confidence bar
    const confPct = Math.round((e.confidence || 0) * 100);
    document.getElementById("confidenceFill").style.width = confPct + "%";
    document.getElementById("confidenceVal").textContent = confPct + "%";

    // Trade stats
    document.getElementById("entryVal").textContent = t.real_entry;
    document.getElementById("entrySub").textContent =
        `Chart: ${t.chart_entry}`;

    document.getElementById("slVal").textContent = t.adjusted_sl;
    document.getElementById("slSub").textContent =
        `Chart: ${t.chart_sl} (${t.actual_sl_pips} pips)`;

    document.getElementById("tpVal").textContent = t.adjusted_tp;
    document.getElementById("tpSub").textContent =
        `Chart: ${t.chart_tp} (${t.actual_tp_pips} pips)`;

    document.getElementById("rrVal").textContent = `1:${t.actual_rr}`;
    document.getElementById("rrSub").textContent =
        `Chart: 1:${t.chart_rr}`;

    document.getElementById("lotVal").textContent = t.lot_size;
    document.getElementById("riskVal").textContent =
        `$${t.risk_amount} (${t.risk_percent}%)`;
    document.getElementById("spreadVal").textContent =
        `${t.spread_pips} pips`;
    document.getElementById("profitVal").textContent =
        `$${t.potential_profit}`;

    // Spread warning
    const warnEl = document.getElementById("spreadWarning");
    if (t.spread_warning) {
        warnEl.textContent = t.spread_warning;
        warnEl.style.display = "block";
    } else {
        warnEl.style.display = "none";
    }

    // AI notes
    const notesEl = document.getElementById("aiNotes");
    if (e.notes) {
        document.getElementById("aiNotesText").textContent = e.notes;
        notesEl.style.display = "block";
    } else {
        notesEl.style.display = "none";
    }

    // Execute button state
    if (!t.is_valid) {
        btnExecute.disabled = true;
        btnExecute.textContent = "🚫 " + t.rejection_reason;
    } else {
        btnExecute.disabled = false;
        btnExecute.textContent = "🚀 Place Trade";
    }

    tradeCard.style.display = "block";
}

// ── Execute Trade ──

btnExecute.addEventListener("click", async () => {
    if (!currentAnalysis || btnExecute.disabled) return;

    btnExecute.textContent = "⏳ Placing...";
    btnExecute.disabled = true;

    try {
        const res = await fetch("/api/execute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(currentAnalysis),
        });
        const data = await res.json();

        if (data.success) {
            resultBox.className = "result success";
            const parts = [document.createTextNode(data.message)];
            if (data.notion_url) {
                parts.push(document.createTextNode("\n📓 "));
                parts.push(link(data.notion_url, "Open in Notion ↗"));
            }
            if (data.notion_error) {
                parts.push(document.createTextNode(`\n⚠️ ${data.notion_error}`));
            }
            if (data.local_logged === false) {
                parts.push(document.createTextNode(
                    "\n⚠️ Not saved to logs/trades.csv — close it if it's open in Excel"
                ));
            } else {
                parts.push(document.createTextNode("\n📄 "));
                parts.push(link("/journal", "Saved to local log"));
            }
            resultBox.replaceChildren(...parts);
            loadJournalStatus();
            resultBox.style.display = "block";
            tradeCard.style.display = "none";

            // Refresh account balance
            checkMT5Status();
        } else {
            showError(data.message || data.error);
            btnExecute.disabled = false;
            btnExecute.textContent = "🚀 Place Trade";
        }
    } catch (err) {
        showError("Execution failed: " + err.message);
        btnExecute.disabled = false;
        btnExecute.textContent = "🚀 Place Trade";
    }
});

// ── Cancel / Reject ──

btnReject.addEventListener("click", () => {
    // Record the skipped setup in the local log (fire and forget)
    if (currentAnalysis) {
        fetch("/api/skip", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(currentAnalysis),
        }).then(() => loadJournalStatus()).catch(() => {});
    }
    tradeCard.style.display = "none";
    previewContainer.style.display = "none";
    currentAnalysis = null;
    resultBox.style.display = "none";
});

// ── Error Display ──

function showError(msg) {
    resultBox.className = "result error";
    resultBox.textContent = msg;
    resultBox.style.display = "block";
}

// ── Init ──

document.addEventListener("DOMContentLoaded", () => {
    loadSettings();
    loadJournalStatus(true);
    checkMT5Status();
    // Poll MT5 status every 30 seconds
    setInterval(checkMT5Status, 30000);
});
