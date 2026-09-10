import puppeteer from "puppeteer-core";
import http from "http";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

import { createServer as createViteServer } from "vite";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DIST_DIR = path.resolve(__dirname, "../dist");
const EVIDENCE_DIR = path.resolve(__dirname, "../test-evidence");

const EDGE_PATH =
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CHROME_PATH =
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const executablePath =
  process.env.BS_BROWSER_EXECUTABLE ||
  (fs.existsSync(EDGE_PATH) ? EDGE_PATH : CHROME_PATH);

if (!fs.existsSync(EVIDENCE_DIR)) {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
}

// Static server serving production dist build with mock health & snapshot endpoints
function createStaticAppServer(port = 4173) {
  const mimeTypes = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
  };

  const mockSnapshot = {
    equipment: [
      {
        id: "eq-wheelchair-01",
        label: "Standard Folding Wheelchair #1",
        kind: "WHEELCHAIR",
        state: "AVAILABLE",
        version: 1,
      },
      {
        id: "eq-walker-01",
        label: "Adjustable Aluminum Walker #1",
        kind: "WALKER",
        state: "AVAILABLE",
        version: 1,
      },
      {
        id: "eq-crutches-01",
        label: "Adult Axillary Crutches #1",
        kind: "CRUTCHES",
        state: "QUARANTINED",
        version: 1,
      },
      {
        id: "eq-walker-02",
        label: "Standard Aluminum Walker #2",
        kind: "WALKER",
        state: "RESERVED",
        version: 1,
      },
    ],
    requests: [
      {
        id: "req-velachery-01",
        borrower_label: "S. Sundaram (Velachery East)",
        equipment_kind: "WHEELCHAIR",
        pickup_location:
          "Velachery Community Room, 12 Cross Road, Velachery, Chennai",
        due_at: new Date(Date.now() + 7 * 86400000).toISOString(),
        status: "REQUESTED",
        created_at: new Date().toISOString(),
      },
      {
        id: "req-velachery-02",
        borrower_label: "K. Raman (Velachery Main)",
        equipment_kind: "WALKER",
        pickup_location:
          "Velachery Community Room, 12 Cross Road, Velachery, Chennai",
        due_at: "2026-09-25T12:00:00Z",
        status: "RESERVED",
        created_at: new Date().toISOString(),
      },
    ],
    loans: [
      {
        id: "loan-velachery-02",
        request_id: "req-velachery-02",
        equipment_id: "eq-walker-02",
        status: "RESERVED",
        due_at: "2026-09-25T12:00:00Z",
        created_at: new Date().toISOString(),
      },
    ],
    tasks: [
      {
        id: "task-pickup-01",
        loan_id: "loan-velachery-02",
        kind: "PICKUP_DUE",
        status: "PENDING",
        due_at: new Date().toISOString(),
        created_at: new Date().toISOString(),
      },
      {
        id: "task-return-02",
        loan_id: "loan-velachery-02",
        kind: "RETURN_DUE",
        status: "DUE",
        due_at: "2026-09-25T12:00:00Z",
        created_at: new Date().toISOString(),
      },
      {
        id: "task-pickup-resolved-03",
        loan_id: "loan-velachery-02",
        kind: "PICKUP_DUE",
        status: "RESOLVED",
        due_at: new Date(Date.now() - 86400000).toISOString(),
        created_at: new Date(Date.now() - 86400000).toISOString(),
      },
    ],
    events: [
      {
        id: "evt-01",
        entity_type: "REQUEST",
        entity_id: "req-velachery-01",
        action: "REQUEST_CREATED",
        at: new Date().toISOString(),
      },
    ],
    agent_mode: "not_implemented",
  };

  const server = http.createServer((req, res) => {
    const parsedUrl = new URL(req.url, `http://127.0.0.1:${port}`);

    // Mock API endpoints for local fixture-backed tests
    if (parsedUrl.pathname === "/api/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: "ok",
          milestone: "M2B",
          agent_mode: "strands_ollama",
        }),
      );
      return;
    }

    if (parsedUrl.pathname === "/api/snapshot") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          ...mockSnapshot,
          agent_mode: "strands_ollama",
        }),
      );
      return;
    }

    if (
      parsedUrl.pathname === "/api/intake/interpret" &&
      req.method === "POST"
    ) {
      let bodyStr = "";
      req.on("data", (chunk) => {
        bodyStr += chunk;
      });
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            draft: {
              borrower_label: "Ananya R.",
              equipment_kind: "WHEELCHAIR",
              pickup_location: "Velachery Community Room",
              due_at: "2026-09-20T10:00:00Z",
            },
            missing_fields: [],
            provenance: {
              framework: "strands",
              provider: "ollama",
              model: "llama3.2:3b",
              inventory_tool_calls: 1,
              completed_at: new Date().toISOString(),
            },
          }),
        );
      });
      return;
    }

    // Static asset serving from dist
    let filePath = path.join(
      DIST_DIR,
      parsedUrl.pathname === "/" ? "index.html" : parsedUrl.pathname,
    );

    if (!fs.existsSync(filePath)) {
      filePath = path.join(DIST_DIR, "index.html");
    }

    const ext = path.extname(filePath);
    const contentType = mimeTypes[ext] || "application/octet-stream";

    fs.readFile(filePath, (err, content) => {
      if (err) {
        res.writeHead(500);
        res.end("Server error");
      } else {
        res.writeHead(200, { "Content-Type": contentType });
        res.end(content);
      }
    });
  });

  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

async function runBrowserVerification() {
  console.log("--- STARTING REAL BROWSER VERIFICATION (BS-002-R3) ---");
  console.log("Using browser executable:", executablePath);

  let normalServer = null;
  let offlineViteServer = null;
  let browser = null;
  const results = [];

  try {
    // 1. Start normal fixture-backed app server on 127.0.0.1:4173 (serving dist)
    normalServer = await createStaticAppServer(4173);
    console.log(
      "Serving production dist build (fixture-backed) at http://127.0.0.1:4173",
    );

    // 2. Start real installed Vite dev server on 127.0.0.1:4183 (unmocked, proxying to refusing port 58999)
    offlineViteServer = await createViteServer({
      configFile: path.resolve(__dirname, "../vite.config.ts"),
      root: path.resolve(__dirname, ".."),
      server: {
        port: 4183,
        host: "127.0.0.1",
        strictPort: true,
        proxy: {
          "/api": {
            target: "http://127.0.0.1:58999",
            changeOrigin: true,
          },
        },
      },
    });
    await offlineViteServer.listen();
    console.log(
      "Serving unmocked real Vite server with proxy targeting unavailable upstream (127.0.0.1:58999) at http://127.0.0.1:4183",
    );

    browser = await puppeteer.launch({
      executablePath,
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    });

    const browserVersion = await browser.version();
    console.log("Real Browser Engine Version:", browserVersion);

    // ----------------------------------------------------
    // TEST 1: Desktop Viewport (1280x800) & Layout
    // ----------------------------------------------------
    console.log("\n[TEST 1] Testing Desktop 1280x800 layout and overflow...");
    const desktopPage = await browser.newPage();
    await desktopPage.setViewport({ width: 1280, height: 800 });

    await desktopPage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });
    await desktopPage.waitForSelector("#equipment-heading");

    const desktopMetrics = await desktopPage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hasHorizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth,
    }));

    console.log(
      `Desktop metrics: scrollWidth=${desktopMetrics.scrollWidth}px, innerWidth=${desktopMetrics.innerWidth}px, overflow=${desktopMetrics.hasHorizontalOverflow}`,
    );
    if (desktopMetrics.hasHorizontalOverflow) {
      throw new Error(
        `Desktop has horizontal overflow! scrollWidth (${desktopMetrics.scrollWidth}) > innerWidth (${desktopMetrics.innerWidth})`,
      );
    }

    const desktopScreenshotPath = path.join(
      EVIDENCE_DIR,
      "desktop-1280x800.png",
    );
    await desktopPage.screenshot({
      path: desktopScreenshotPath,
      fullPage: true,
    });
    console.log(`Saved screenshot: ${desktopScreenshotPath}`);
    results.push({
      name: "Desktop Layout (1280x800)",
      status: "PASS",
      details: `Zero overflow (scrollWidth=${desktopMetrics.scrollWidth}px)`,
    });
    await desktopPage.close();

    // ----------------------------------------------------
    // TEST 2: Keyboard Flow & Modal Accessibility (Inspection & Allocation)
    // ----------------------------------------------------
    console.log(
      "\n[TEST 2] Testing Keyboard accessibility and Modal focus traps...",
    );
    const modalPage = await browser.newPage();
    await modalPage.setViewport({ width: 1280, height: 800 });
    await modalPage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });

    // A. Inspection Modal Keyboard Navigation
    const inspectBtn = await modalPage.waitForSelector(
      'button[aria-label="Inspect Adult Axillary Crutches #1"]',
    );
    await inspectBtn.focus();
    await modalPage.keyboard.press("Enter");

    await modalPage.waitForSelector('div[role="dialog"]');
    console.log("Inspection Modal opened via keyboard Enter.");

    // Verify initial focus is on outcome select
    const initialFocusedTag = await modalPage.evaluate(
      () => document.activeElement?.id,
    );
    console.log("Initially focused element:", initialFocusedTag);
    if (initialFocusedTag !== "outcome-select") {
      throw new Error(
        `Initial focus expected #outcome-select, but got #${initialFocusedTag}`,
      );
    }

    // Tab through modal controls
    await modalPage.keyboard.press("Tab"); // checkbox
    const secondFocusedTag = await modalPage.evaluate(
      () => document.activeElement?.id,
    );
    console.log("After Tab 1, focused element:", secondFocusedTag);
    if (secondFocusedTag !== "inspect-human-approval") {
      throw new Error(
        `Expected #inspect-human-approval, got #${secondFocusedTag}`,
      );
    }

    await modalPage.keyboard.press("Tab"); // Cancel button
    const thirdFocused = await modalPage.evaluate(() =>
      document.activeElement?.textContent?.trim(),
    );
    console.log("After Tab 2, focused element:", thirdFocused);

    await modalPage.keyboard.press("Tab"); // Focus wrap-around to outcome-select
    const wrappedFocusTag = await modalPage.evaluate(
      () => document.activeElement?.id,
    );
    console.log("After wrap-around Tab, focused element:", wrappedFocusTag);
    if (wrappedFocusTag !== "outcome-select") {
      throw new Error(
        `Focus trap failed to wrap to #outcome-select! Got #${wrappedFocusTag}`,
      );
    }

    // Shift+Tab backwards to Cancel button
    await modalPage.keyboard.down("Shift");
    await modalPage.keyboard.press("Tab");
    await modalPage.keyboard.up("Shift");
    const shiftTabFocused = await modalPage.evaluate(() =>
      document.activeElement?.textContent?.trim(),
    );
    console.log("After Shift+Tab, focused element:", shiftTabFocused);
    if (shiftTabFocused !== "Cancel") {
      throw new Error(
        `Shift+Tab wrap expected Cancel button, got: ${shiftTabFocused}`,
      );
    }

    const modalScreenshotPath = path.join(
      EVIDENCE_DIR,
      "modal-keyboard-verified.png",
    );
    await modalPage.screenshot({ path: modalScreenshotPath });

    // Press Escape to dismiss modal
    await modalPage.keyboard.press("Escape");
    await modalPage.waitForFunction(
      () => document.querySelector('div[role="dialog"]') === null,
    );
    console.log("Inspection modal closed via Escape key.");

    // Verify focus restoration
    const restoredFocusedAria = await modalPage.evaluate(() =>
      document.activeElement?.getAttribute("aria-label"),
    );
    console.log("Restored focused element aria-label:", restoredFocusedAria);
    if (restoredFocusedAria !== "Inspect Adult Axillary Crutches #1") {
      throw new Error(
        `Focus restoration failed! Expected Inspect button, got: ${restoredFocusedAria}`,
      );
    }

    // B. Allocation Modal Keyboard Navigation
    const allocBtn = await modalPage.waitForSelector(
      'button[aria-label^="Allocate equipment for"]',
    );
    await allocBtn.focus();
    await modalPage.keyboard.press("Enter");
    await modalPage.waitForSelector('div[role="dialog"]');
    console.log("Allocation Modal opened via keyboard Enter.");

    const allocInitialFocus = await modalPage.evaluate(
      () => document.activeElement?.id,
    );
    console.log("Allocation initial focus:", allocInitialFocus);
    if (allocInitialFocus !== "equipment-select") {
      throw new Error(
        `Allocation initial focus expected #equipment-select, got #${allocInitialFocus}`,
      );
    }

    // Escape closes allocation modal
    await modalPage.keyboard.press("Escape");
    await modalPage.waitForFunction(
      () => document.querySelector('div[role="dialog"]') === null,
    );
    console.log("Allocation modal closed via Escape key.");

    results.push({
      name: "Keyboard Navigation & Modal Trapping",
      status: "PASS",
      details:
        "Focus trap, Tab wrap, Escape closing, and focus restoration verified across Inspection and Allocation modals",
    });
    await modalPage.close();

    // ----------------------------------------------------
    // TEST 3: Mobile Viewport (390x844) & Measured Overflow
    // ----------------------------------------------------
    console.log("\n[TEST 3] Testing Mobile 390x844 viewport and overflow...");
    const mobilePage = await browser.newPage();
    await mobilePage.setViewport({
      width: 390,
      height: 844,
      isMobile: true,
      hasTouch: true,
    });

    await mobilePage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });
    await mobilePage.waitForSelector("#equipment-heading");

    const mobileMetrics = await mobilePage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hasHorizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth,
    }));

    console.log(
      `Mobile metrics: scrollWidth=${mobileMetrics.scrollWidth}px, innerWidth=${mobileMetrics.innerWidth}px, overflow=${mobileMetrics.hasHorizontalOverflow}`,
    );
    if (mobileMetrics.hasHorizontalOverflow) {
      throw new Error(
        `Mobile has horizontal overflow! scrollWidth (${mobileMetrics.scrollWidth}) > innerWidth (${mobileMetrics.innerWidth})`,
      );
    }

    const mobileScreenshotPath = path.join(EVIDENCE_DIR, "mobile-390x844.png");
    await mobilePage.screenshot({ path: mobileScreenshotPath, fullPage: true });
    console.log(`Saved screenshot: ${mobileScreenshotPath}`);
    results.push({
      name: "Mobile Layout (390x844)",
      status: "PASS",
      details: `Zero overflow (scrollWidth=${mobileMetrics.scrollWidth}px, innerWidth=390px)`,
    });
    await mobilePage.close();

    // ----------------------------------------------------
    // TEST 4: Real Un-intercepted Installed Vite Proxy Failure (HTTP 500 ECONNREFUSED)
    // ----------------------------------------------------
    console.log(
      "\n[TEST 4] Testing Real Installed Vite Proxy Failure with refusing upstream on http://127.0.0.1:4183...",
    );
    const proxyTestPage = await browser.newPage();
    await proxyTestPage.setViewport({ width: 1280, height: 800 });

    // Track snapshot response status to verify HTTP 500 from real Vite proxy
    let snapshotResponseStatus = null;
    proxyTestPage.on("response", (res) => {
      if (res.url().includes("/api/snapshot")) {
        snapshotResponseStatus = res.status();
      }
    });

    // Navigate to 4183 (real Vite server); proxy naturally encounters ECONNREFUSED to 58999
    await proxyTestPage.goto("http://127.0.0.1:4183", {
      waitUntil: "networkidle0",
    });
    await proxyTestPage.waitForSelector(".banner-offline");

    console.log(
      "Real Vite Proxy /api/snapshot HTTP Status:",
      snapshotResponseStatus,
    );
    if (snapshotResponseStatus !== 500) {
      throw new Error(
        `Expected real Vite proxy to return HTTP 500, but got: ${snapshotResponseStatus}`,
      );
    }

    const bannerText = await proxyTestPage.evaluate(() =>
      document.querySelector(".banner-offline")?.textContent?.trim(),
    );
    console.log("Real Vite Proxy Failure Banner:", bannerText);

    if (!bannerText || !bannerText.includes("Connection Error")) {
      throw new Error(`Expected Connection Error banner, got: ${bannerText}`);
    }

    const retryBtn = await proxyTestPage.$(
      ".banner-offline button, button.btn-secondary",
    );
    if (!retryBtn) {
      throw new Error("Retry button not found in proxy failure banner!");
    }

    const retryBtnText = await proxyTestPage.evaluate(
      (el) => el.textContent?.trim(),
      retryBtn,
    );
    console.log("Actionable Retry Button found:", retryBtnText);
    if (!retryBtnText || !retryBtnText.includes("Retry Connection")) {
      throw new Error(
        `Expected 'Retry Connection' button text, got: ${retryBtnText}`,
      );
    }

    // Verify clicking "Retry Connection" initiates a new request to /api/snapshot
    console.log(
      "Clicking 'Retry Connection' and verifying new request dispatch...",
    );
    const retryRequestPromise = proxyTestPage.waitForRequest(
      (req) => req.url().includes("/api/snapshot"),
      { timeout: 5000 },
    );
    await retryBtn.click();
    const retriedRequest = await retryRequestPromise;
    console.log(
      "New request successfully dispatched by Retry Connection:",
      retriedRequest.url(),
    );

    const offlineScreenshotPath = path.join(EVIDENCE_DIR, "offline-banner.png");
    await proxyTestPage.screenshot({ path: offlineScreenshotPath });
    console.log(`Saved screenshot: ${offlineScreenshotPath}`);

    results.push({
      name: "Real Installed Vite Proxy Failure (HTTP 500)",
      status: "PASS",
      details:
        "Real Vite createServer HTTP 500 ECONNREFUSED rendered actionable Connection Error banner; Retry Connection dispatched fresh request",
    });
    await proxyTestPage.close();

    // ----------------------------------------------------
    // TEST 5: Modal 422 Refusal In-Dialog Rendering & Preservation
    // ----------------------------------------------------
    console.log(
      "\n[TEST 5] Testing in-dialog 422 mutation refusal rendering and input preservation...",
    );
    const refusalPage = await browser.newPage();
    await refusalPage.setViewport({ width: 1280, height: 800 });

    await refusalPage.setRequestInterception(true);
    refusalPage.on("request", (interceptedReq) => {
      if (
        interceptedReq.url().includes("/api/equipment/") &&
        interceptedReq.url().includes("/inspection") &&
        interceptedReq.method() === "POST"
      ) {
        interceptedReq.respond({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({
            error: {
              code: "UNPROCESSABLE_ENTITY",
              message:
                "Equipment cannot be inspected: current state is not AWAITING_INSPECTION.",
            },
          }),
        });
      } else {
        interceptedReq.continue();
      }
    });

    await refusalPage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });

    // Open Inspection Modal for Adult Axillary Crutches #1
    const inspectBtnForRefusal = await refusalPage.waitForSelector(
      'button[aria-label="Inspect Adult Axillary Crutches #1"]',
    );
    await inspectBtnForRefusal.click();
    await refusalPage.waitForSelector('div[role="dialog"]');

    // Select outcome "REPAIR"
    await refusalPage.select("#outcome-select", "REPAIR");

    // Check affirmation checkbox
    const approvalCheckbox = await refusalPage.waitForSelector(
      "#inspect-human-approval",
    );
    await approvalCheckbox.click();

    // Submit the form
    const submitBtn = await refusalPage.waitForSelector(
      'div[role="dialog"] button[type="submit"]',
    );
    await submitBtn.click();

    // Verify dialog remains open and alert appears inside modal
    await refusalPage.waitForSelector(
      'div[role="dialog"] .modal-content div[role="alert"]',
    );
    console.log("Modal alert element appeared after 422 refusal.");

    const modalAlertText = await refusalPage.evaluate(() =>
      document
        .querySelector('div[role="dialog"] .modal-content div[role="alert"]')
        ?.textContent?.trim(),
    );
    console.log("Modal Alert Content:", modalAlertText);
    if (
      !modalAlertText ||
      !modalAlertText.includes("UNPROCESSABLE_ENTITY") ||
      !modalAlertText.includes("Equipment cannot be inspected")
    ) {
      throw new Error(
        `Expected 422 refusal alert inside modal, got: ${modalAlertText}`,
      );
    }

    // Verify page-level error is NOT rendered behind modal
    const pageLevelErrorCount = await refusalPage.evaluate(
      () => document.querySelectorAll("main > .banner-error").length,
    );
    if (pageLevelErrorCount > 0) {
      throw new Error(
        "Page-level error banner was unexpectedly rendered behind the modal backdrop!",
      );
    }

    // Verify inputs remain preserved
    const selectedOutcomeVal = await refusalPage.evaluate(
      () => document.querySelector("#outcome-select")?.value,
    );
    const isApprovalChecked = await refusalPage.evaluate(
      () => document.querySelector("#inspect-human-approval")?.checked,
    );
    console.log(
      `Preserved inputs check: outcome=${selectedOutcomeVal}, checked=${isApprovalChecked}`,
    );
    if (selectedOutcomeVal !== "REPAIR" || !isApprovalChecked) {
      throw new Error(
        `User inputs were not preserved on 422 refusal! outcome=${selectedOutcomeVal}, checked=${isApprovalChecked}`,
      );
    }

    const refusalScreenshotPath = path.join(
      EVIDENCE_DIR,
      "modal-422-refusal-verified.png",
    );
    await refusalPage.screenshot({ path: refusalScreenshotPath });
    console.log(`Saved screenshot: ${refusalScreenshotPath}`);

    // Verify Cancel button cleanly dismisses modal after refusal
    const cancelBtn = await refusalPage.waitForSelector(
      'div[role="dialog"] button.btn-secondary',
    );
    await cancelBtn.click();
    await refusalPage.waitForFunction(
      () => document.querySelector('div[role="dialog"]') === null,
    );
    console.log("Modal dismissed via Cancel button after refusal.");

    results.push({
      name: "Modal 422 Refusal In-Dialog Rendering",
      status: "PASS",
      details:
        "422 UNPROCESSABLE_ENTITY rendered inside modal role=alert, inputs preserved, page-level banner suppressed, Cancel dismisses cleanly",
    });
    await refusalPage.close();

    // ----------------------------------------------------
    // TEST 6: M2A Synthetic Intake Suggestion & Human-in-the-Loop Review
    // ----------------------------------------------------
    console.log(
      "\n[TEST 6] Testing M2A Synthetic Intake Assistant, deliberate 'Use draft', and form population...",
    );
    const intakePage = await browser.newPage();
    await intakePage.setViewport({ width: 1280, height: 800 });

    await intakePage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });

    // Check heading and agent mode
    await intakePage.waitForSelector("#intake-assistant-heading");
    const headerMode = await intakePage.evaluate(() =>
      document.querySelector(".header-status")?.textContent?.trim(),
    );
    console.log("Header status display:", headerMode);

    // Verify textarea is available and type intake text
    const intakeTextarea = await intakePage.waitForSelector(
      "#synthetic-intake-text",
    );
    await intakeTextarea.click();
    await intakeTextarea.type(
      "Velachery resident Ananya R. requires a wheelchair pickup at Velachery Community Room by 2026-09-20T10:00:00Z",
    );

    // Click 'Interpret Intake'
    await intakePage.evaluate(() => {
      const btns = Array.from(document.querySelectorAll("button"));
      const btn = btns.find((b) => b.textContent.includes("Interpret Intake"));
      if (btn) btn.click();
    });

    // Verify Unsaved Intake Suggestion appears with provenance
    await intakePage.waitForSelector(".intake-suggestion-box");
    console.log("Unsaved Intake Suggestion box rendered.");

    const provenanceText = await intakePage.evaluate(() =>
      document.querySelector(".provenance-block")?.textContent?.trim(),
    );
    console.log("Validated Provenance Content:", provenanceText);
    if (
      !provenanceText ||
      !provenanceText.includes("strands") ||
      !provenanceText.includes("ollama")
    ) {
      throw new Error(
        `Expected provenance block with strands and ollama, got: ${provenanceText}`,
      );
    }

    // Verify borrower field in form is NOT yet populated with draft value before deliberate action
    const borrowerBefore = await intakePage.evaluate(
      () => document.querySelector("#borrower-label")?.value,
    );
    console.log("Borrower value before Use Draft:", borrowerBefore);
    if (borrowerBefore === "Ananya R.") {
      throw new Error(
        `Form was unexpectedly populated with draft before clicking 'Use Draft'! value=${borrowerBefore}`,
      );
    }

    // Deliberate click on 'Use Draft (Populate Form)'
    await intakePage.evaluate(() => {
      const btns = Array.from(document.querySelectorAll("button"));
      const btn = btns.find((b) =>
        b.textContent.includes("Use Draft (Populate Form)"),
      );
      if (btn) btn.click();
    });

    // Verify form is now populated
    await intakePage.waitForFunction(
      () => document.querySelector("#borrower-label")?.value === "Ananya R.",
    );
    const borrowerAfter = await intakePage.evaluate(
      () => document.querySelector("#borrower-label")?.value,
    );
    const equipmentAfter = await intakePage.evaluate(
      () => document.querySelector("#equipment-kind")?.value,
    );
    console.log(
      `Populated form fields: borrower='${borrowerAfter}', equipment='${equipmentAfter}'`,
    );
    if (borrowerAfter !== "Ananya R." || equipmentAfter !== "WHEELCHAIR") {
      throw new Error(
        `Form fields were not correctly populated! borrower='${borrowerAfter}', equipment='${equipmentAfter}'`,
      );
    }

    // Take screenshot of M2A intake flow
    const intakeScreenshotPath = path.join(
      EVIDENCE_DIR,
      "m2a-intake-flow-verified.png",
    );
    await intakePage.screenshot({ path: intakeScreenshotPath });
    console.log(`Saved screenshot: ${intakeScreenshotPath}`);

    // Check 390px mobile responsiveness for intake box
    await intakePage.setViewport({ width: 390, height: 844 });
    const intakeMobileMetrics = await intakePage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hasHorizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth,
    }));
    console.log("M2A Mobile 390px layout check:", intakeMobileMetrics);
    if (intakeMobileMetrics.hasHorizontalOverflow) {
      throw new Error(
        `M2A Intake caused horizontal overflow on 390px mobile! scrollWidth=${intakeMobileMetrics.scrollWidth}`,
      );
    }

    const intakeMobileScreenshotPath = path.join(
      EVIDENCE_DIR,
      "m2a-intake-mobile-390px.png",
    );
    await intakePage.screenshot({ path: intakeMobileScreenshotPath });
    console.log(`Saved screenshot: ${intakeMobileScreenshotPath}`);

    results.push({
      name: "M2A Synthetic Intake & Human Review",
      status: "PASS",
      details:
        "Synthetic intake interpretation, validated provenance display, deliberate Use Draft form population, and 390px mobile usability verified",
    });
    await intakePage.close();

    // ----------------------------------------------------
    // TEST 7: M2B Coordination Section (Desktop & 390px Mobile Viewports)
    // ----------------------------------------------------
    console.log(
      "\n[TEST 7] Testing M2B Coordination Section (active/resolved tasks, badges, in-app scope, desktop & 390px mobile)...",
    );
    const coordPage = await browser.newPage();
    await coordPage.setViewport({ width: 1280, height: 800 });

    await coordPage.goto("http://127.0.0.1:4173", {
      waitUntil: "networkidle0",
    });

    // 1. Verify heading and in-app scope banner
    await coordPage.waitForSelector("#coordination-heading");
    const headingText = await coordPage.evaluate(() =>
      document.querySelector("#coordination-heading")?.textContent?.trim(),
    );
    console.log("Coordination heading text:", headingText);
    if (!headingText || !headingText.includes("Coordination")) {
      throw new Error(`Expected Coordination heading, got: ${headingText}`);
    }

    const scopeNotice = await coordPage.evaluate(() =>
      document.querySelector(".coordination-scope-notice")?.textContent?.trim(),
    );
    console.log("Scope notice text:", scopeNotice);
    if (!scopeNotice || !scopeNotice.includes("no external email/SMS")) {
      throw new Error(
        `Expected in-app scope notice explicitly stating no external email/SMS, got: ${scopeNotice}`,
      );
    }

    // 2. Verify active tasks (PICKUP_DUE and RETURN_DUE)
    const activeTaskItems = await coordPage.evaluate(() => {
      const items = Array.from(
        document.querySelectorAll(".coordination-task-active"),
      );
      return items.map((el) => ({
        id: el.getAttribute("data-testid"),
        text: el.textContent?.trim() || "",
        badge: el.querySelector(".tag")?.textContent?.trim() || "",
      }));
    });
    console.log("Active tasks rendered:", activeTaskItems);
    if (activeTaskItems.length < 2) {
      throw new Error(
        `Expected at least 2 active tasks, got: ${activeTaskItems.length}`,
      );
    }

    const pickupActive = activeTaskItems.find(
      (t) => t.text.includes("Arrange pickup") && t.badge === "PENDING",
    );
    if (!pickupActive) {
      throw new Error("Active PICKUP_DUE task with PENDING badge not found!");
    }
    if (
      !pickupActive.text.includes("Velachery Community Room") ||
      !pickupActive.text.includes("K. Raman")
    ) {
      throw new Error(
        `Active pickup task missing location or borrower: ${pickupActive.text}`,
      );
    }

    const returnActive = activeTaskItems.find(
      (t) =>
        (t.text.includes("Return due") || t.text.includes("Return reminder")) &&
        t.badge === "DUE",
    );
    if (!returnActive) {
      throw new Error("Active RETURN_DUE task with DUE badge not found!");
    }
    if (!returnActive.text.includes("K. Raman")) {
      throw new Error(
        `Active return task missing borrower: ${returnActive.text}`,
      );
    }

    // 3. Verify resolved tasks inside <details>
    const resolvedSummary = await coordPage.evaluate(() =>
      document
        .querySelector(".coordination-resolved-details summary")
        ?.textContent?.trim(),
    );
    console.log("Resolved summary text:", resolvedSummary);
    if (!resolvedSummary || !resolvedSummary.includes("Resolved")) {
      throw new Error(
        `Expected resolved tasks details element, got: ${resolvedSummary}`,
      );
    }

    // Expand details element to verify resolved task content
    await coordPage.evaluate(() => {
      const details = document.querySelector(".coordination-resolved-details");
      if (details) details.open = true;
    });

    const resolvedBadge = await coordPage.evaluate(() =>
      document
        .querySelector(".coordination-task-resolved .tag")
        ?.textContent?.trim(),
    );
    console.log("Resolved task badge:", resolvedBadge);
    if (resolvedBadge !== "RESOLVED") {
      throw new Error(`Expected RESOLVED badge, got: ${resolvedBadge}`);
    }

    // 4. Verify zero dummy buttons inside coordination section
    const dummyButtons = await coordPage.evaluate(() => {
      const section = document.querySelector(".coordination-section");
      if (!section) return [];
      const buttons = Array.from(section.querySelectorAll("button"));
      return buttons.map((b) => b.textContent?.trim());
    });
    console.log("Buttons inside CoordinationSection:", dummyButtons);
    if (dummyButtons.length > 0) {
      throw new Error(
        `Coordination section must have zero dummy buttons! Found: ${JSON.stringify(dummyButtons)}`,
      );
    }

    // 5. Check desktop screenshot
    const coordDesktopScreenshotPath = path.join(
      EVIDENCE_DIR,
      "m2b-coordination-desktop.png",
    );
    await coordPage.screenshot({
      path: coordDesktopScreenshotPath,
      fullPage: true,
    });
    console.log(`Saved screenshot: ${coordDesktopScreenshotPath}`);

    // 6. Check 390px mobile responsiveness
    await coordPage.setViewport({ width: 390, height: 844, isMobile: true });
    const coordMobileMetrics = await coordPage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hasHorizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth,
    }));
    console.log(
      "M2B Coordination Mobile 390px layout check:",
      coordMobileMetrics,
    );
    if (coordMobileMetrics.hasHorizontalOverflow) {
      throw new Error(
        `Coordination section caused horizontal overflow on 390px mobile! scrollWidth=${coordMobileMetrics.scrollWidth}`,
      );
    }

    const coordMobileScreenshotPath = path.join(
      EVIDENCE_DIR,
      "m2b-coordination-mobile-390px.png",
    );
    const coordinationSection = await coordPage.$(".coordination-section");
    if (!coordinationSection)
      throw new Error("Missing mobile coordination section");
    await coordinationSection.screenshot({ path: coordMobileScreenshotPath });
    console.log(`Saved screenshot: ${coordMobileScreenshotPath}`);

    results.push({
      name: "M2B Coordination Desktop & Mobile 390px",
      status: "PASS",
      details:
        "Active PICKUP_DUE (PENDING) and RETURN_DUE (DUE) tasks, resolved details, in-app scope notice, zero dummy buttons, and 390px mobile verified",
    });
    await coordPage.close();

    console.log("\n--- ALL REAL BROWSER CHECKS PASSED ---");
    console.table(results);
  } finally {
    if (browser) {
      await browser.close();
    }
    if (normalServer) {
      normalServer.close();
    }
    if (offlineViteServer) {
      await offlineViteServer.close();
    }
  }
}

runBrowserVerification().catch((err) => {
  console.error("\nBROWSER VERIFICATION FAILED:", err);
  process.exit(1);
});
