const path = require('path');

module.exports = function (api) {
  api.cache(true);
  const expoDir = path.dirname(require.resolve('expo/package.json'));
  const presetPath = require.resolve('babel-preset-expo', { paths: [expoDir] });
  return {
    presets: [presetPath],
  };
};
