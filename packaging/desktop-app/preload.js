/**
 * OpenBiliClaw Desktop — preload script.
 *
 * Exposes safe Electron APIs to the renderer process via contextBridge.
 * This replaces Chrome extension APIs that the web UI would normally use.
 */

const { contextBridge, ipcRenderer, clipboard } = require("electron");

contextBridge.exposeInMainWorld("obcDesktop", {
  // Clipboard access (replaces navigator.clipboard for Electron)
  copyText: (text) => {
    try {
      clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  },

  // Open URL in default browser (replaces chrome.tabs.create)
  openExternal: (url) => ipcRenderer.invoke("open-external", url),

  // Platform info
  platform: process.platform,

  // App version
  appVersion: process.env.npm_package_version || "0.3.147",
});
