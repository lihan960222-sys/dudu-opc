const path = require('node:path');
const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);
// The phone and web build the same pure text formatter for the owner's steward chat.
config.watchFolders.push(path.resolve(__dirname, '../frontend/lib/steward'));
config.resolver.nodeModulesPaths.push(path.resolve(__dirname, 'node_modules'));
module.exports = config;
