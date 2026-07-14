'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getSettings:  ()    => ipcRenderer.invoke('get-settings'),
  saveSettings: (d)   => ipcRenderer.invoke('save-settings', d),
  getStatus:    ()    => ipcRenderer.invoke('get-status'),
  browseFolder: ()    => ipcRenderer.invoke('browse-folder'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  openFolder:   (p)   => ipcRenderer.invoke('open-folder', p),
  onStatus: (cb)      => {
    const listener = (_event, status) => cb(status);
    ipcRenderer.on('status', listener);
    return () => ipcRenderer.removeListener('status', listener);
  },
});
