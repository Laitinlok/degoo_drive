const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('degoo', {
  getSettings:  ()     => ipcRenderer.invoke('get-settings'),
  saveSettings: (d)    => ipcRenderer.invoke('save-settings', d),
  getStatus:    ()     => ipcRenderer.invoke('get-status'),
  startMount:   ()     => ipcRenderer.invoke('start-mount'),
  stopMount:    ()     => ipcRenderer.invoke('stop-mount'),
  startSync:    ()     => ipcRenderer.invoke('start-sync'),
  stopSync:     ()     => ipcRenderer.invoke('stop-sync'),
  browseFolder: ()     => ipcRenderer.invoke('browse-folder'),
  onStatus:     (cb)   => ipcRenderer.on('status',       (_, v) => cb(v)),
  onProgress:   (cb)   => ipcRenderer.on('sync_progress',(_, v) => cb(v)),
});
