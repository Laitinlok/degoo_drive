const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('degoo', {
  getSettings:  ()   => ipcRenderer.invoke('get-settings'),
  saveSettings: (d)  => ipcRenderer.invoke('save-settings', d),
  getStatus:    ()   => ipcRenderer.invoke('get-status'),
  browseFolder: ()   => ipcRenderer.invoke('browse-folder'),
  onStatus:     (cb) => ipcRenderer.on('status', (_, s) => cb(s)),
});
