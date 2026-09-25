const path = require('path');

module.exports = {
  apps: [{
    name: 'create',
    cwd: __dirname,
    script: path.join(__dirname, 'main.py'),
    // Always use this project's dependencies, even when PM2 runs outside a venv.
    interpreter: path.join(__dirname, '.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
    interpreter_args: '-u',
    exec_mode: 'fork',
    instances: 1,
    watch: false,
    restart_delay: 5000,
    time: true,
  }],
};
