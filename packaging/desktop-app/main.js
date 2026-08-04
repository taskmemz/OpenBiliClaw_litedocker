/**
 * OpenBiliClaw Desktop — Electron main process.
 *
 * Wraps the OpenBiliClaw web UI (/web) as a standalone desktop app.
 * Optionally manages the backend Python process lifecycle.
 */

const { app, BrowserWindow, Tray, Menu, nativeImage, shell, dialog, Notification } = require("electron");
const path = require("path");
const { spawn } = require("child_process");

// ── Configuration ────────────────────────────────────────────
const DEFAULT_BACKEND_HOST = "127.0.0.1";
const DEFAULT_BACKEND_PORT = "8420";
const BACKEND_PROTO = "http";
const WINDOW_TITLE = "OpenBiliClaw";
const WINDOW_WIDTH = 1200;
const WINDOW_HEIGHT = 800;
const WINDOW_MIN_WIDTH = 480;
const WINDOW_MIN_HEIGHT = 600;

// ── State ────────────────────────────────────────────────────
let mainWindow = null;
let tray = null;
let backendProcess = null;
let backendUrl = `${BACKEND_PROTO}://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}/web`;

// ── Launch Backend Process ───────────────────────────────────
function startBackend() {
  const backendScript = process.env.OPENBILICLAW_BACKEND;
  if (!backendScript) {
    console.log("OPENBILICLAW_BACKEND not set; assuming backend is already running.");
    return;
  }
  console.log("Starting backend:", backendScript);
  backendProcess = spawn(backendScript, ["start"], {
    stdio: ["ignore", "pipe", "pipe"],
    detached: false,
    shell: true,
  });
  backendProcess.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backendProcess.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  backendProcess.on("exit", (code) => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

function stopBackend() {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
}

// ── Create Window ────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    minWidth: WINDOW_MIN_WIDTH,
    minHeight: WINDOW_MIN_HEIGHT,
    title: WINDOW_TITLE,
    icon: path.join(__dirname, "..", "icons", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.loadURL(backendUrl);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    // Open external links (bilibili, youtube, etc.) in the default browser
    if (url.startsWith(BACKEND_PROTO)) {
      return { action: "allow" };
    }
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // Build app menu
  const menu = Menu.buildFromTemplate([
    {
      label: "OpenBiliClaw",
      submenu: [
        { role: "reload", label: "重新加载" },
        { role: "forceReload", label: "强制重新加载" },
        { type: "separator" },
        { role: "toggleDevTools", label: "开发者工具" },
        { type: "separator" },
        { label: "退出", accelerator: "CmdOrControl+Q", click: () => app.quit() },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { role: "resetZoom", label: "重置缩放" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
      ],
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "GitHub 仓库",
          click: () => shell.openExternal("https://github.com/whiteguo233/OpenBiliClaw"),
        },
        {
          label: "反馈问题",
          click: () => shell.openExternal("https://github.com/whiteguo233/OpenBiliClaw/issues"),
        },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);
}

// ── System Tray ──────────────────────────────────────────────
function createTray() {
  const iconPath = path.join(__dirname, "..", "icons", "icon.png");
  let trayIcon;
  try {
    trayIcon = nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 });
  } catch {
    return; // Tray icon not available
  }
  tray = new Tray(trayIcon);
  tray.setToolTip("OpenBiliClaw");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "显示窗口", click: () => mainWindow?.show() },
      { label: "隐藏窗口", click: () => mainWindow?.hide() },
      { type: "separator" },
      { label: "退出", click: () => app.quit() },
    ])
  );
  tray.on("click", () => mainWindow?.show());
}

// ── App Lifecycle ────────────────────────────────────────────
app.whenReady().then(() => {
  startBackend();
  createWindow();
  createTray();

  app.on("activate", () => {
    if (mainWindow === null) createWindow();
    else mainWindow.show();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});
